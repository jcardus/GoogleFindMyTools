"""
Register Google Find My devices and insert them directly into Supabase.

Usage:
    python generate_tags.py
    python generate_tags.py --prefix HI --from 1000 --to 1099 --secrets-file Auth/secrets.json
    python generate_tags.py --prefix HI --range 1000-1099 --secrets-file Auth/secrets.json
"""
import argparse
import json
import os
import pathlib
import re
import secrets
import subprocess
import sys
import time

# Import GoogleFindMyTools modules from this checkout.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

SUPABASE_PROJECT_REF = 'qxabzyhabkrdmyztmjul'
DEFAULT_PREFIX = 'MK'
DEFAULT_SECRETS_FILE = os.path.join('Auth', 'secrets.json')


def _register_ble_device(eik: bytes, name: str):
    """Register a BLE device with Google. Returns (canonic_id, eid_hex)."""
    from NovaApi.ListDevices.nbe_list_devices import request_device_list
    from ProtoDecoders.decoder import parse_device_list_protobuf, get_canonic_ids
    from FMDNCrypto.key_derivation import FMDNOwnerOperations
    from FMDNCrypto.eid_generator import ROTATION_PERIOD, generate_eid
    from KeyBackup.cloud_key_decryptor import encrypt_aes_gcm
    from ProtoDecoders.DeviceUpdate_pb2 import (
        DeviceComponentInformation, SpotDeviceType,
        RegisterBleDeviceRequest, PublicKeyIdList,
    )
    from SpotApi.CreateBleDevice.config import mcu_fast_pair_model_id, max_truncated_eid_seconds_server
    from SpotApi.CreateBleDevice.util import flip_bits
    from SpotApi.GetEidInfoForE2eeDevices.get_owner_key import get_owner_key
    from SpotApi.spot_request import spot_request

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


def get_supabase_credentials():
    url = f'https://{SUPABASE_PROJECT_REF}.supabase.co'
    result = subprocess.run(
        ['npx', 'supabase', 'projects', 'api-keys', '--project-ref', SUPABASE_PROJECT_REF],
        capture_output=True, text=True,
    )
    match = re.search(r'service_role\s*\|\s*(\S+)', result.stdout)
    if not match:
        sys.exit('Error: could not retrieve service_role key. Run: npx supabase login')
    return url, match.group(1)


def parse_range(value):
    match = re.fullmatch(r'(\d+)-(\d+)', value.strip())
    if not match:
        raise argparse.ArgumentTypeError('range must look like START-END, for example 1000-1099')
    start, end = int(match.group(1)), int(match.group(2))
    if start > end:
        raise argparse.ArgumentTypeError('range start must be less than or equal to range end')
    return start, end


def infer_google_account(secrets_file):
    try:
        with open(secrets_file, 'r') as file:
            data = json.load(file)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        sys.exit(f'Error: could not parse secrets file: {secrets_file}')

    username = data.get('username')
    return username.strip() if isinstance(username, str) and username.strip() else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--prefix', default=DEFAULT_PREFIX,
                        help=f'Tag ID prefix (default: {DEFAULT_PREFIX})')
    range_group = parser.add_mutually_exclusive_group(required=True)
    range_group.add_argument('--range', dest='tag_range', type=parse_range,
                             help='Numeric tag suffix range, inclusive, as START-END')
    range_group.add_argument('--from', dest='from_id', type=int,
                             help='First numeric tag ID suffix, inclusive. Requires --to.')
    parser.add_argument('--to', dest='to_id', type=int,
                        help='Last numeric tag ID suffix, inclusive. Required with --from.')
    parser.add_argument('--secrets-file', default=os.getenv('GOOGLE_SECRETS_FILE', DEFAULT_SECRETS_FILE),
                        help=f'Path to the Google secrets.json for this account (default: {DEFAULT_SECRETS_FILE})')
    parser.add_argument('--google-account',
                        help='Value to store in hybrid_tags.google_account. Defaults to the username cached in --secrets-file.')
    parser.add_argument('--delay', type=float, default=1.0,
                        help='Seconds between registrations (default: 1)')
    args = parser.parse_args()

    if args.tag_range:
        args.from_id, args.to_id = args.tag_range
    elif args.to_id is None:
        parser.error('--to is required when using --from')

    if args.from_id > args.to_id:
        parser.error('--from must be less than or equal to --to')

    tag_ids = [f'{args.prefix}{n}' for n in range(args.from_id, args.to_id + 1)]
    google_account = args.google_account or infer_google_account(args.secrets_file)
    if not google_account:
        parser.error('--google-account is required when --secrets-file does not contain a cached username')

    import Auth.token_cache as _tc
    _tc._get_secrets_file = lambda: args.secrets_file

    sb_url = os.getenv('SUPABASE_URL')
    sb_key = os.getenv('SUPABASE_SERVICE_ROLE')
    if not sb_url or not sb_key:
        print('Fetching Supabase credentials via CLI...')
        sb_url, sb_key = get_supabase_credentials()

    from supabase import create_client
    sb = create_client(sb_url, sb_key)
    print(f'Supabase ready ({sb_url})')
    print(f'Generating {len(tag_ids)} tags: {tag_ids[0]}–{tag_ids[-1]}')
    print(f'Google account: {google_account}')
    print(f'Google secrets: {args.secrets_file}')

    existing = {r['tag_id'] for r in sb.table('hybrid_tags').select('tag_id').execute().data or []}
    conflicts = [t for t in tag_ids if t in existing]
    if conflicts:
        sys.exit(f'Error: these tag_ids already exist in DB: {conflicts}')

    success = 0
    errors = 0

    for i, tag_id in enumerate(tag_ids):
        eik = secrets.token_bytes(32)
        print(f'[{i+1}/{len(tag_ids)}] {tag_id} registering...', end=' ', flush=True)
        try:
            google_id, google_adv_key = _register_ble_device(eik, tag_id)
            sb.table('hybrid_tags').insert({
                'tag_id': tag_id,
                'google_eik': eik.hex(),
                'google_id': google_id,
                'google_adv_key': google_adv_key,
                'google_account': google_account,
            }).execute()
            print(f'OK  {google_id}')
            success += 1
        except Exception as e:
            print(f'FAILED: {e}')
            errors += 1

        if i < len(tag_ids) - 1:
            time.sleep(args.delay)

    print(f'\nDone: {success} registered, {errors} failed.')
    sys.exit(1 if errors == len(tag_ids) else 0)


if __name__ == '__main__':
    main()
