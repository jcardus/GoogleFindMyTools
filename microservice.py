import argparse
import asyncio
import datetime
import hashlib
import logging
import threading
import time
import os
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from python_version import require_python_312

require_python_312()

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('hub')

from supabase import Client, create_client

from NovaApi.ListDevices.nbe_list_devices import request_device_list
from ProtoDecoders.decoder import parse_device_list_protobuf, parse_device_update_protobuf
from NovaApi.ExecuteAction.LocateTracker.location_request import create_location_request
from NovaApi.nova_request import nova_request
from NovaApi.scopes import NOVA_ACTION_API_SCOPE
from NovaApi.util import generate_random_uuid
from Auth.fcm_receiver import FcmReceiver
from FMDNCrypto.eid_generator import generate_eid
from FMDNCrypto.foreign_tracker_cryptor import decrypt
from KeyBackup.cloud_key_decryptor import decrypt_aes_gcm
from ProtoDecoders import Common_pb2
from ProtoDecoders.DeviceUpdate_pb2 import DevicesList, Location
from SpotApi.UploadPrecomputedPublicKeyIds.upload_precomputed_public_key_ids import refresh_custom_trackers
from NovaApi.ExecuteAction.LocateTracker.decrypt_locations import is_mcu_tracker, retrieve_identity_key

SB: Optional[Client] = None
EID_REFRESH_INTERVAL = int(os.getenv('EID_REFRESH_INTERVAL', str(3 * 24 * 60 * 60)))
EID_REFRESH_STATE_FILE = os.getenv('EID_REFRESH_STATE_FILE', '/tmp/tagora-google-hub-eid-refresh.txt')
GOOGLE_ACCOUNT: Optional[str] = None  # set at startup; None means no filter (default account)
_last_eid_refresh_at = 0.0


def _google_account_secrets_r2_key(google_account):
    prefix = os.getenv('GOOGLE_SECRETS_R2_PREFIX', 'google-secrets').strip('/')
    account_key = quote(google_account, safe='@._-+')
    return f'{prefix}/{account_key}.json' if prefix else f'{account_key}.json'


def _extract_locations(device_update):
    device_registration = device_update.deviceMetadata.information.deviceRegistration
    identity_key = retrieve_identity_key(device_registration)
    locations_proto = device_update.deviceMetadata.information.locationInformation.reports.recentLocationAndNetworkLocations
    is_mcu = is_mcu_tracker(device_registration)

    locations = []
    network_locations = list(locations_proto.networkLocations)
    network_timestamps = list(locations_proto.networkLocationTimestamps)

    if locations_proto.HasField('recentLocation'):
        network_locations.append(locations_proto.recentLocation)
        network_timestamps.append(locations_proto.recentLocationTimestamp)

    for loc, timestamp in zip(network_locations, network_timestamps):
        if loc.status == Common_pb2.Status.SEMANTIC:
            continue

        encrypted_location = loc.geoLocation.encryptedReport.encryptedLocation
        public_key_random = loc.geoLocation.encryptedReport.publicKeyRandom

        if public_key_random == b'':
            identity_key_hash = hashlib.sha256(identity_key).digest()
            decrypted_location = decrypt_aes_gcm(identity_key_hash, encrypted_location)
        else:
            time_offset = 0 if is_mcu else loc.geoLocation.deviceTimeOffset
            decrypted_location = decrypt(identity_key, encrypted_location, public_key_random, time_offset)

        parsed = Location()
        parsed.ParseFromString(decrypted_location)
        locations.append({
            'latitude': parsed.latitude / 1e7,
            'longitude': parsed.longitude / 1e7,
            'altitude': parsed.altitude,
            'time': int(timestamp.seconds),
            'accuracy': loc.geoLocation.accuracy,
            'is_own_report': loc.geoLocation.encryptedReport.isOwnReport,
        })

    return locations


def _fetch_location(device_id, timeout=15):
    result = None
    request_uuid = generate_random_uuid()
    done = threading.Event()

    def handler(resp_hex):
        nonlocal result
        update = parse_device_update_protobuf(resp_hex)
        if update.fcmMetadata.requestUuid == request_uuid:
            result = update
            done.set()

    receiver = FcmReceiver()
    fcm_token = receiver.register_for_location_updates(handler)

    try:
        payload = create_location_request(device_id, fcm_token, request_uuid)
        nova_request(NOVA_ACTION_API_SCOPE, payload)
        if not done.wait(timeout):
            log.info('location request timed out after %ss (request_uuid=%s)', timeout, request_uuid)
    finally:
        receiver.stop_listening()

    return _extract_locations(result) if result else []


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
        query = SB.table('hybrid_tags').select('google_id, google_eik, google_adv_key, tag_id').filter('google_id', 'not.is', 'null').filter('google_id', 'neq', '')
        if GOOGLE_ACCOUNT is not None:
            query = query.filter('google_account', 'eq', GOOGLE_ACCOUNT)
        rows = query.execute().data or []
        eik_by_google_id = {r['google_id']: r.get('google_eik') for r in rows}
        adv_key_by_google_id = {r['google_id']: r.get('google_adv_key') for r in rows}
        tag_by_google_id = {r['google_id']: r['tag_id'] for r in rows}
        if not eik_by_google_id:
            return True

        log.info('refreshing EID key list with Google (%d known devices)', len(eik_by_google_id))
        result_hex = request_device_list()
        device_list = parse_device_list_protobuf(result_hex)

        filtered = DevicesList()
        unmatched_google_devices = []
        for d in device_list.deviceMetadata:
            canonic_ids = [c.id for c in d.identifierInformation.canonicIds.canonicId]
            matched_id = next((cid for cid in canonic_ids if cid in eik_by_google_id), None)
            if matched_id is None:
                unmatched_google_devices.append((d.userDefinedDeviceName or '(unnamed)', canonic_ids))
                continue
            filtered.deviceMetadata.append(d)
            tag_id = tag_by_google_id.get(matched_id, matched_id)
            stored_eik = eik_by_google_id.get(matched_id)
            metadata = _device_metadata_summary(d, matched_id)
            log.info(
                '%s Google metadata: google_id=%s backend_name=%s canonic_ids=%s '
                'pair_date=%s creation_date=%s owner_key_version=%s '
                'encrypted_identity_key_len=%s encrypted_account_key_len=%s '
                'encrypted_public_address_len=%s manufacturer=%s model=%s fast_pair_model_id=%s',
                tag_id,
                metadata['matched_google_id'],
                metadata['backend_name'],
                metadata['canonic_ids'],
                metadata['pair_date'],
                metadata['creation_date'],
                metadata['owner_key_version'],
                metadata['encrypted_identity_key_len'],
                metadata['encrypted_account_key_len'],
                metadata['encrypted_public_address_len'],
                metadata['manufacturer'],
                metadata['model'],
                metadata['fast_pair_model_id'],
            )
            try:
                decrypted_eik = retrieve_identity_key(d.information.deviceRegistration)
                google_eid = generate_eid(decrypted_eik, 0).hex()
                stored_adv_key = adv_key_by_google_id.get(matched_id)
                if stored_eik:
                    match = decrypted_eik.hex() == stored_eik
                    log.info('%s EIK %s', tag_id, 'matches Supabase' if match else f'MISMATCH (decrypted={decrypted_eik.hex()[:16]}… stored={stored_eik[:16]}…)')
                else:
                    log.info('%s EIK decrypted=%s (no stored EIK to compare)', tag_id, decrypted_eik.hex()[:16] + '…')
                if stored_adv_key:
                    match = google_eid == stored_adv_key
                    log.info('%s EID %s (google=%s stored=%s)', tag_id, 'matches Supabase' if match else 'MISMATCH', google_eid, stored_adv_key)
                else:
                    log.info('%s EID google=%s (no stored google_adv_key to compare)', tag_id, google_eid)
            except SystemExit:
                log.warning('%s EIK decryption failed (owner key mismatch)', tag_id)

        if unmatched_google_devices:
            for device_name, canonic_ids in unmatched_google_devices:
                log.info(
                    'Google device not found in Supabase: name=%s canonic_ids=%s',
                    device_name,
                    ','.join(canonic_ids) if canonic_ids else '(none)',
                )

        log.info(
            'refreshing EIDs for %d matched Supabase device(s); Google returned %d total device(s)',
            len(filtered.deviceMetadata),
            len(device_list.deviceMetadata),
        )
        if not refresh_custom_trackers(filtered):
            log.warning('EID key list refresh upload failed')
            return False
        log.info('EID key list refreshed')
        return True
    except Exception:
        log.exception('EID refresh failed')
        return False


def _device_metadata_summary(device_metadata, matched_id):
    device_registration = device_metadata.information.deviceRegistration
    encrypted_user_secrets = device_registration.encryptedUserSecrets
    canonic_ids = [c.id for c in device_metadata.identifierInformation.canonicIds.canonicId]
    creation_date = encrypted_user_secrets.creationDate.seconds if encrypted_user_secrets.HasField('creationDate') else 0
    return {
        'backend_name': device_metadata.userDefinedDeviceName or '(unnamed)',
        'matched_google_id': matched_id,
        'canonic_ids': ','.join(canonic_ids) if canonic_ids else '(none)',
        'pair_date': device_registration.pairDate,
        'creation_date': creation_date,
        'owner_key_version': encrypted_user_secrets.ownerKeyVersion,
        'encrypted_identity_key_len': len(encrypted_user_secrets.encryptedIdentityKey),
        'encrypted_account_key_len': len(encrypted_user_secrets.encryptedAccountKey),
        'encrypted_public_address_len': len(encrypted_user_secrets.encryptedSha256AccountKeyPublicAddress),
        'manufacturer': device_registration.manufacturer or '(none)',
        'model': device_registration.model or '(none)',
        'fast_pair_model_id': device_registration.fastPairModelId or '(none)',
    }


def _refresh_eids_if_due():
    global _last_eid_refresh_at
    now = time.time()
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


def main():
    parser = argparse.ArgumentParser(description="Google Find Hub Sync")
    parser.add_argument('--secrets-file', default=os.getenv('GOOGLE_SECRETS_FILE'),
                        help='Path to the Google secrets.json for this account. Defaults to Auth/<google-account>.json.')
    parser.add_argument('--google-account',
                        help='Only sync tags whose google_account matches this value')
    args = parser.parse_args()

    secrets_file = args.secrets_file
    if not secrets_file and args.google_account:
        secrets_file = os.path.join('Auth', f'{args.google_account}.json')

    if args.google_account and os.getenv('GOOGLE_SECRETS_R2_BUCKET') and not os.getenv('GOOGLE_SECRETS_R2_KEY'):
        os.environ['GOOGLE_SECRETS_R2_KEY'] = _google_account_secrets_r2_key(args.google_account)

    if secrets_file:
        import Auth.token_cache as _tc
        _tc._get_secrets_file = lambda: secrets_file
        log.info('Using secrets file: %s', secrets_file)
    if os.getenv('GOOGLE_SECRETS_R2_BUCKET') and os.getenv('GOOGLE_SECRETS_R2_KEY'):
        log.info(
            'Using R2 secrets object: %s/%s',
            os.getenv('GOOGLE_SECRETS_R2_BUCKET'),
            os.getenv('GOOGLE_SECRETS_R2_KEY'),
        )

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

    log.info('Running single sync pass')
    error_count = _sync_pass()
    sys.exit(1 if error_count else 0)


if __name__ == '__main__':
    main()
