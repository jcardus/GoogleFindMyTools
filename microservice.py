import argparse
import asyncio
import datetime
import logging
import secrets
import threading
import time
import os
import sys
from pathlib import Path
from typing import Optional

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('hub')

from flask import Flask, request, jsonify, abort
from supabase import Client, create_client

from NovaApi.ListDevices.nbe_list_devices import request_device_list
from ProtoDecoders.decoder import parse_device_list_protobuf, get_canonic_ids, parse_device_update_protobuf
from NovaApi.ExecuteAction.LocateTracker.location_request import create_location_request
from NovaApi.nova_request import nova_request
from NovaApi.scopes import NOVA_ACTION_API_SCOPE
from NovaApi.util import generate_random_uuid
from Auth.fcm_receiver import FcmReceiver
from NovaApi.ExecuteAction.LocateTracker.decrypt_locations import extract_locations
from FMDNCrypto.key_derivation import FMDNOwnerOperations
from FMDNCrypto.eid_generator import ROTATION_PERIOD, generate_eid
from KeyBackup.cloud_key_decryptor import encrypt_aes_gcm
from ProtoDecoders.DeviceUpdate_pb2 import (
    DeviceComponentInformation, SpotDeviceType,
    RegisterBleDeviceRequest, PublicKeyIdList, DevicesList,
)
from SpotApi.CreateBleDevice.config import mcu_fast_pair_model_id, max_truncated_eid_seconds_server
from SpotApi.CreateBleDevice.util import flip_bits
from SpotApi.GetEidInfoForE2eeDevices.get_owner_key import get_owner_key
from SpotApi.spot_request import spot_request
from SpotApi.UploadPrecomputedPublicKeyIds.upload_precomputed_public_key_ids import refresh_custom_trackers
from NovaApi.ExecuteAction.LocateTracker.decrypt_locations import is_mcu_tracker, retrieve_identity_key

app = Flask(__name__)
API_TOKEN = None
SB: Optional[Client] = None
SYNC_INTERVAL = int(os.getenv('SYNC_INTERVAL', '120'))
EID_REFRESH_INTERVAL = int(os.getenv('EID_REFRESH_INTERVAL', str(3 * 24 * 60 * 60)))
EID_REFRESH_STATE_FILE = os.getenv('EID_REFRESH_STATE_FILE', '/tmp/tagora-google-hub-eid-refresh.txt')
GOOGLE_ACCOUNT: Optional[str] = None  # set at startup; None means no filter (default account)
_fetch_location_lock = threading.Lock()
_eid_refresh_lock = threading.Lock()
_last_eid_refresh_at = 0.0


def _require_bearer_token():
    auth_header = request.headers.get('Authorization', '')
    scheme, _, token = auth_header.partition(' ')
    if scheme.lower() != 'bearer' or not token or token != API_TOKEN:
        abort(401, description='Invalid or missing bearer token')


@app.before_request
def before_request():
    _require_bearer_token()


@app.route('/devices', methods=['GET'])
def list_devices():
    result_hex = request_device_list()
    device_list = parse_device_list_protobuf(result_hex)
    canonic_ids = get_canonic_ids(device_list)
    devices = [{'name': name, 'id': cid} for name, cid in canonic_ids]
    return jsonify({'devices': devices})


def _register_ble_device(eik: bytes, name: str):
    """Register a BLE device with Google. Returns (canonic_id, eid_hex)."""
    owner_key = get_owner_key()
    eid = generate_eid(eik, 0)
    pair_date = int(time.time())

    reg = RegisterBleDeviceRequest()
    reg.fastPairModelId = mcu_fast_pair_model_id

    reg.description.userDefinedName = name
    reg.description.deviceType = SpotDeviceType.DEVICE_TYPE_BEACON

    component = DeviceComponentInformation()
    component.imageUrl = ""
    reg.description.deviceComponentsInformation.append(component)

    reg.capabilities.isAdvertising = True
    reg.capabilities.trackableComponents = 1
    reg.capabilities.capableComponents = 1

    reg.e2eePublicKeyRegistration.rotationExponent = 10
    reg.e2eePublicKeyRegistration.pairingDate = pair_date
    reg.e2eePublicKeyRegistration.encryptedUserSecrets.encryptedIdentityKey = flip_bits(encrypt_aes_gcm(owner_key, eik), True)
    reg.e2eePublicKeyRegistration.encryptedUserSecrets.encryptedAccountKey = secrets.token_bytes(44)
    reg.e2eePublicKeyRegistration.encryptedUserSecrets.encryptedSha256AccountKeyPublicAddress = secrets.token_bytes(60)
    reg.e2eePublicKeyRegistration.encryptedUserSecrets.ownerKeyVersion = 1
    reg.e2eePublicKeyRegistration.encryptedUserSecrets.creationDate.seconds = pair_date

    time_counter = pair_date
    truncated_eid = eid[:10]
    for _ in range(int(max_truncated_eid_seconds_server / ROTATION_PERIOD)):
        info = PublicKeyIdList.PublicKeyIdInfo()
        info.publicKeyId.truncatedEid = truncated_eid
        info.timestamp.seconds = time_counter
        reg.e2eePublicKeyRegistration.publicKeyIdList.publicKeyIdInfo.append(info)
        time_counter += ROTATION_PERIOD

    reg.manufacturerName = "Tagora"
    reg.modelName = name

    owner_ops = FMDNOwnerOperations()
    owner_ops.generate_keys(identity_key=eik)
    reg.ringKey = owner_ops.ringing_key
    reg.recoveryKey = owner_ops.recovery_key
    reg.unwantedTrackingKey = owner_ops.tracking_key

    spot_request("CreateBleDevice", reg.SerializeToString())

    result_hex = request_device_list()
    device_list = parse_device_list_protobuf(result_hex)
    for device_name, canonic_id in get_canonic_ids(device_list):
        if device_name == name:
            return canonic_id, eid.hex()

    raise RuntimeError(f'Device "{name}" not found in device list after registration')


@app.route('/devices/register', methods=['POST'])
def register_device():
    body = request.get_json(force=True) or {}
    eik_hex = body.get('eik', '')
    name = body.get('name', '')

    if not eik_hex or len(eik_hex) != 64:
        abort(400, description='eik must be a 64-character hex string (32 bytes)')
    if not name:
        abort(400, description='name is required')

    try:
        eik = bytes.fromhex(eik_hex)
        google_id, google_adv_key = _register_ble_device(eik, name)
    except ValueError:
        abort(400, description='eik is not valid hex')
    except Exception as e:
        abort(500, description=str(e))
    else:
        return jsonify({'google_id': google_id, 'google_adv_key': google_adv_key})


def _fetch_location(device_id, timeout=15):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = None
    request_uuid = generate_random_uuid()
    done = asyncio.Event()

    def handler(resp_hex):
        nonlocal result
        update = parse_device_update_protobuf(resp_hex)
        if update.fcmMetadata.requestUuid == request_uuid:
            result = update
            done.set()

    with _fetch_location_lock:
        receiver = FcmReceiver()
        fcm_token = receiver.register_for_location_updates(handler)

        try:
            payload = create_location_request(device_id, fcm_token, request_uuid)
            nova_request(NOVA_ACTION_API_SCOPE, payload)
            asyncio.get_event_loop().run_until_complete(asyncio.wait_for(done.wait(), timeout))
        finally:
            receiver.stop_listening()
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for task in pending:
                task.cancel()
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            asyncio.set_event_loop(None)

    return extract_locations(result) if result else []


@app.route('/devices/<device_id>/location', methods=['GET'])
def get_device_location(device_id):
    locations = _fetch_location(device_id)
    return jsonify({'locations': locations})


def _upload_location(device_id, tag_id, user_id, location):
    if SB is None:
        raise RuntimeError('Supabase client not configured')
    if not location:
        return False

    ts = datetime.datetime.fromtimestamp(float(location['time']), tz=datetime.timezone.utc).isoformat()
    row = {
        'tag_id': tag_id,
        'user_id': user_id,
        'lat': location['latitude'],
        'lon': location['longitude'],
        'source': 'google',
        'timestamp': ts,
    }

    resp = SB.table('positions').upsert(
        row, on_conflict='tag_id,source,timestamp', ignore_duplicates=True,
    ).execute()
    inserted = bool(resp.data)
    log.info(
        '%s upsert ts=%s lat=%s lon=%s -> %s',
        tag_id, ts, location['latitude'], location['longitude'],
        'inserted' if inserted else 'duplicate (ignored)',
    )
    return inserted


def _read_eid_refresh_marker():
    if not EID_REFRESH_STATE_FILE:
        return 0.0
    try:
        return float(Path(EID_REFRESH_STATE_FILE).read_text().strip())
    except (OSError, ValueError):
        return 0.0


def _write_eid_refresh_marker(timestamp):
    if not EID_REFRESH_STATE_FILE:
        return
    try:
        path = Path(EID_REFRESH_STATE_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(timestamp))
    except OSError:
        log.warning('could not write EID refresh marker: %s', EID_REFRESH_STATE_FILE)


def _should_refresh_eids(now):
    if EID_REFRESH_INTERVAL <= 0:
        return True
    last_refresh = max(_last_eid_refresh_at, _read_eid_refresh_marker())
    age = now - last_refresh
    if age >= EID_REFRESH_INTERVAL:
        return True
    log.info(
        'skipping EID refresh; last refresh was %.0fs ago (interval=%ds)',
        max(age, 0),
        EID_REFRESH_INTERVAL,
    )
    return False


def _refresh_eids():
    try:
        if SB is None:
            return False
        query = SB.table('hybrid_tags').select('google_id, google_eik, tag_id').filter('google_id', 'not.is', 'null').filter('google_id', 'neq', '')
        if GOOGLE_ACCOUNT is not None:
            query = query.filter('google_account', 'eq', GOOGLE_ACCOUNT)
        rows = query.execute().data or []
        eik_by_google_id = {r['google_id']: r.get('google_eik') for r in rows}
        tag_by_google_id = {r['google_id']: r['tag_id'] for r in rows}
        if not eik_by_google_id:
            return True

        log.info('refreshing EID key list with Google (%d known devices)', len(eik_by_google_id))
        result_hex = request_device_list()
        device_list = parse_device_list_protobuf(result_hex)

        filtered = DevicesList()
        for d in device_list.deviceMetadata:
            canonic_ids = [c.id for c in d.identifierInformation.canonicIds.canonicId]
            matched_id = next((cid for cid in canonic_ids if cid in eik_by_google_id), None)
            if matched_id is None:
                continue
            filtered.deviceMetadata.append(d)
            tag_id = tag_by_google_id.get(matched_id, matched_id)
            stored_eik = eik_by_google_id.get(matched_id)
            try:
                decrypted_eik = retrieve_identity_key(d.information.deviceRegistration)
                if stored_eik:
                    match = decrypted_eik.hex() == stored_eik
                    log.info('%s EIK %s', tag_id, 'matches Supabase' if match else f'MISMATCH (decrypted={decrypted_eik.hex()[:16]}… stored={stored_eik[:16]}…)')
                else:
                    log.info('%s EIK decrypted=%s (no stored EIK to compare)', tag_id, decrypted_eik.hex()[:16] + '…')
            except SystemExit:
                log.warning('%s EIK decryption failed (owner key mismatch)', tag_id)

        log.info('refreshing EIDs for %d/%d devices', len(filtered.deviceMetadata), len(device_list.deviceMetadata))
        refresh_custom_trackers(filtered)
        log.info('EID key list refreshed')
        return True
    except Exception:
        log.exception('EID refresh failed')
        return False


def _refresh_eids_if_due():
    global _last_eid_refresh_at
    now = time.time()
    with _eid_refresh_lock:
        if not _should_refresh_eids(now):
            return
        if _refresh_eids():
            _last_eid_refresh_at = now
            _write_eid_refresh_marker(now)


def _sync_pass():
    if SB is None:
        log.warning('sync skipped: Supabase client not configured')
        return 0

    _refresh_eids_if_due()
    started = time.monotonic()
    query = SB.table('hybrid_tags') \
        .select('tag_id, user_id, google_id') \
        .filter('google_id', 'not.is', 'null') \
        .filter('google_id', 'neq', '') \
        .filter('user_id', 'not.is', 'null')
    if GOOGLE_ACCOUNT is not None:
        query = query.filter('google_account', 'eq', GOOGLE_ACCOUNT)
    resp = query.execute()
    tags = resp.data or []
    log.info('sync pass start: %d tag(s)', len(tags))

    inserted_count = 0
    no_location_count = 0
    error_count = 0

    for t in tags:
        gid = t['google_id']
        tag_id = t['tag_id']
        try:
            log.info('%s fetching location (google_id=%s)', tag_id, gid)
            locations = _fetch_location(gid)
            log.info('%s got %d location(s) from Google', tag_id, len(locations))
            usable = [l for l in locations if 'latitude' in l and 'longitude' in l]
            if usable:
                for location in usable:
                    if _upload_location(gid, tag_id, t['user_id'], location):
                        inserted_count += 1
            else:
                log.info('%s no usable location in response', tag_id)
                no_location_count += 1
        except Exception as e:
            error_count += 1
            log.exception('%s sync error: %s', tag_id, e)

    elapsed = time.monotonic() - started
    log.info(
        'sync pass done in %.1fs: %d inserted, %d no-location, %d error(s)',
        elapsed, inserted_count, no_location_count, error_count,
    )
    return error_count


def _sync_loop():
    while True:
        try:
            _sync_pass()
        except Exception as e:
            log.exception('sync pass crashed: %s', e)
        log.info('sleeping %ds before next sync pass', SYNC_INTERVAL)
        time.sleep(SYNC_INTERVAL)


def main():
    parser = argparse.ArgumentParser(description="Google Find Hub Sync")
    parser.add_argument('--serve', action='store_true',
                        help='Run as a long-lived HTTP service with background sync loop')
    parser.add_argument('--auth-token', default=os.getenv('AUTH_TOKEN'),
                        help='Bearer token for the HTTP API (required with --serve)')
    parser.add_argument('--host', default=os.getenv('HOST', '0.0.0.0'))
    parser.add_argument('--port', type=int, default=int(os.getenv('PORT', '8080')))
    parser.add_argument('--secrets-file', default=os.getenv('GOOGLE_SECRETS_FILE'),
                        help='Path to the Google secrets.json for this account')
    parser.add_argument('--google-account',
                        help='Only sync tags whose google_account matches this value')
    args = parser.parse_args()

    if args.secrets_file:
        import Auth.token_cache as _tc
        _tc._get_secrets_file = lambda: args.secrets_file
        log.info('Using secrets file: %s', args.secrets_file)

    global GOOGLE_ACCOUNT
    GOOGLE_ACCOUNT = args.google_account
    if GOOGLE_ACCOUNT is not None:
        log.info('Filtering sync to google_account=%s', GOOGLE_ACCOUNT)

    sb_url = os.getenv('SUPABASE_URL')
    sb_key = os.getenv('SUPABASE_SERVICE_ROLE')
    if not sb_url or not sb_key:
        parser.error('SUPABASE_URL and SUPABASE_SERVICE_ROLE environment variables are required')

    global SB
    SB = create_client(sb_url, sb_key)
    log.info('Supabase client ready (%s)', sb_url)

    if args.serve:
        if not args.auth_token:
            parser.error('--auth-token or AUTH_TOKEN is required with --serve')
        global API_TOKEN
        API_TOKEN = args.auth_token
        log.info('Starting sync loop (interval=%ds)', SYNC_INTERVAL)
        threading.Thread(target=_sync_loop, daemon=True).start()
        log.info('Listening on %s:%d', args.host, args.port)
        app.run(host=args.host, port=args.port)
    else:
        log.info('Running single sync pass (cron mode)')
        error_count = _sync_pass()
        sys.exit(1 if error_count else 0)


if __name__ == '__main__':
    main()
