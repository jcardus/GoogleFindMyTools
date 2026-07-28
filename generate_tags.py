"""
Register Google Find My devices and insert them directly into Supabase.

Usage:
    python generate_tags.py
    python generate_tags.py --prefix HI --from 1000 --to 1099 --secrets-file Auth/secrets.json
    python generate_tags.py --prefix HI --range 1000-1099 --secrets-file Auth/secrets.json
    python generate_tags.py --prefix MK --range 1000-1099 --fill-missing-apple-keys
    python generate_tags.py --from MK1000 --to MK1099 --fill-missing-apple-keys
"""
import argparse
import base64
import hashlib
import json
import os
import pathlib
import re
import secrets
import subprocess
import sys
import time
import string
from typing import Optional, cast

from cryptography.hazmat.primitives.asymmetric import ec

# Import GoogleFindMyTools modules from this checkout.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

SUPABASE_PROJECT_REF = 'qxabzyhabkrdmyztmjul'
DEFAULT_PREFIX = 'MK'
DEFAULT_START = 1000
DEFAULT_SECRETS_FILE = os.path.join('Auth', 'secrets.json')


def generate_apple_keys() -> dict[str, str]:
    """Generate OpenHaystack-compatible Apple private and advertising keys."""
    while True:
        key = ec.generate_private_key(ec.SECP224R1())
        numbers = key.private_numbers()
        priv_bytes = numbers.private_value.to_bytes(28, 'big')
        adv_bytes = numbers.public_numbers.x.to_bytes(28, 'big')

        # OpenHaystack requirement: no '/' in first 7 chars of hashed adv key.
        hashed_adv = base64.b64encode(hashlib.sha256(adv_bytes).digest()).decode('ascii')
        if '/' not in hashed_adv[:7]:
            return {
                'apple_priv_key': base64.b64encode(priv_bytes).decode('ascii'),
                'apple_adv_key': base64.b64encode(adv_bytes).decode('ascii'),
            }


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


def get_supabase_client():
    sb_url = os.getenv('SUPABASE_URL') or os.getenv('NEXT_PUBLIC_SUPABASE_URL')
    sb_key = os.getenv('SUPABASE_SERVICE_ROLE') or os.getenv('SUPABASE_SERVICE_KEY')
    if not sb_url or not sb_key:
        print('Fetching Supabase credentials via CLI...')
        sb_url, sb_key = get_supabase_credentials()

    from supabase import create_client
    sb = create_client(sb_url, sb_key)
    print(f'Supabase ready ({sb_url})')
    return sb


def parse_range(value: str) -> tuple[int, int]:
    match = re.fullmatch(r'(\d+)-(\d+)', value.strip())
    if not match:
        raise argparse.ArgumentTypeError('range must look like START-END, for example 1000-1099')
    start, end = int(match.group(1)), int(match.group(2))
    if start > end:
        raise argparse.ArgumentTypeError('range start must be less than or equal to range end')
    return start, end


def parse_tag_bound(value: str, default_prefix: str) -> tuple[str, int]:
    value = value.strip()
    if value.isdigit():
        return default_prefix, int(value)

    match = re.fullmatch(r'([A-Za-z]+)(\d+)', value)
    if not match:
        raise argparse.ArgumentTypeError('tag bound must be numeric or look like PREFIX1234')
    return match.group(1), int(match.group(2))


def parse_from_to(from_value: str, to_value: str, default_prefix: str) -> tuple[str, int, int]:
    from_prefix, start = parse_tag_bound(from_value, default_prefix)
    to_prefix, end = parse_tag_bound(to_value, from_prefix)
    if from_prefix != to_prefix:
        raise argparse.ArgumentTypeError('--from and --to prefixes must match')
    if start > end:
        raise argparse.ArgumentTypeError('--from must be less than or equal to --to')
    return from_prefix, start, end


def infer_google_account(secrets_file: str) -> Optional[str]:
    try:
        with open(secrets_file, 'r') as file:
            data = json.load(file)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        sys.exit(f'Error: could not parse secrets file: {secrets_file}')

    username = data.get('username')
    return username.strip() if isinstance(username, str) and username.strip() else None


def tag_bounds(prefix: str, start: int, end: int) -> tuple[str, str]:
    return f'{prefix}{start}', f'{prefix}{end}'


def next_available_tag_ids(existing: set[str], prefix: str, count: int) -> list[str]:
    suffixes = [
        int(match.group(1))
        for tag_id in existing
        if (match := re.fullmatch(rf'{re.escape(prefix)}(\d+)', tag_id))
    ]
    suffix = max(suffixes, default=DEFAULT_START - 1) + 1
    if suffix + count - 1 > 9999:
        raise RuntimeError(f'No room for {count} more six-character tag IDs with prefix {prefix}')
    result = []
    while len(result) < count:
        tag_id = f'{prefix}{suffix}'
        if tag_id not in existing:
            result.append(tag_id)
        suffix += 1
    return result


def account_tag_prefix(google_account: str, existing_rows: list[dict]) -> str:
    account = google_account.strip().lower()
    local_part = account.split('@', 1)[0]
    local_chars = ''.join(char for char in local_part.upper() if char in string.ascii_uppercase + string.digits)
    preferred = (local_chars + 'XX')[:2]

    owners_by_prefix: dict[str, set[str]] = {}
    for row in existing_rows:
        tag_id = row.get('tag_id')
        owner = (row.get('google_account') or '').strip().lower()
        if not isinstance(tag_id, str) or not owner:
            continue
        match = re.fullmatch(r'([A-Z0-9]{2})\d{4}', tag_id.upper())
        if match:
            owners_by_prefix.setdefault(match.group(1), set()).add(owner)

    def available(prefix: str) -> bool:
        owners = owners_by_prefix.get(prefix, set())
        return not owners or owners == {account}

    if preferred != DEFAULT_PREFIX and available(preferred):
        return preferred

    alphabet = string.ascii_uppercase + string.digits
    total = len(alphabet) ** 2
    start = int.from_bytes(hashlib.sha256(account.encode('utf-8')).digest()[:4], 'big') % total
    for offset in range(total):
        value = (start + offset) % total
        prefix = alphabet[value // len(alphabet)] + alphabet[value % len(alphabet)]
        if prefix != DEFAULT_PREFIX and available(prefix):
            return prefix
    raise RuntimeError('No account-specific two-character tag prefix is available')


def load_existing_tags(sb, page_size: int = 1000) -> list[dict]:
    rows = []
    offset = 0
    while True:
        page = (
            sb.table('hybrid_tags')
            .select('tag_id,google_account,google_id')
            .range(offset, offset + page_size - 1)
            .execute()
            .data or []
        )
        rows.extend(page)
        if len(page) < page_size:
            return rows
        offset += page_size


def fill_missing_apple_keys(sb, prefix: str, start: int, end: int) -> int:
    from_id, to_id = tag_bounds(prefix, start, end)
    resp = (
        sb.table('hybrid_tags')
        .select('id, tag_id')
        .gte('tag_id', from_id)
        .lte('tag_id', to_id)
        .or_('apple_priv_key.is.null,apple_adv_key.is.null')
        .order('tag_id')
        .execute()
    )
    rows = resp.data or []
    if not rows:
        print(f'No tags missing Apple keys in range {from_id}–{to_id}.')
        return 0

    print(f'Found {len(rows)} tag(s) missing Apple keys ({from_id}–{to_id})')
    ok = 0
    for row in rows:
        apple = generate_apple_keys()
        try:
            sb.table('hybrid_tags').update(apple).eq('id', row['id']).execute()
            print(f"  {row['tag_id']} OK  adv={apple['apple_adv_key'][:12]}...")
            ok += 1
        except Exception as e:
            print(f"  {row['tag_id']} FAILED: {e}")

    print(f'\nDone. {ok}/{len(rows)} updated.')
    return ok


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--prefix',
                        help=f'Tag ID prefix (default: account-based with --ensure-count, otherwise {DEFAULT_PREFIX})')
    range_group = parser.add_mutually_exclusive_group(required=True)
    range_group.add_argument('--range', dest='tag_range', type=parse_range,
                             help='Numeric tag suffix range, inclusive, as START-END')
    range_group.add_argument('--from', dest='from_id',
                             help='First tag ID or numeric suffix, inclusive. Requires --to.')
    range_group.add_argument('--ensure-count', type=int,
                             help='Ensure this Google account has this many tags, generating only the deficit.')
    parser.add_argument('--to', dest='to_id',
                        help='Last tag ID or numeric suffix, inclusive. Required with --from.')
    parser.add_argument('--secrets-file', default=os.getenv('GOOGLE_SECRETS_FILE', DEFAULT_SECRETS_FILE),
                        help=f'Path to the Google secrets.json for this account (default: {DEFAULT_SECRETS_FILE})')
    parser.add_argument('--google-account',
                        help='Value to store in hybrid_tags.google_account. Defaults to the username cached in --secrets-file.')
    parser.add_argument('--delay', type=float, default=1.0,
                        help='Seconds between registrations (default: 1)')
    parser.add_argument('--fill-missing-apple-keys', action='store_true',
                        help='Only generate Apple keys for existing tags in the selected range that are missing them.')
    args = parser.parse_args()

    if args.ensure_count is not None:
        if args.ensure_count < 1:
            parser.error('--ensure-count must be at least 1')
        prefix = args.prefix or DEFAULT_PREFIX
        start = end = 0
    elif args.tag_range is not None:
        start, end = cast(tuple[int, int], args.tag_range)
        prefix = args.prefix or DEFAULT_PREFIX
    elif args.to_id is None:
        parser.error('--to is required when using --from')
    else:
        try:
            prefix, start, end = parse_from_to(str(args.from_id), str(args.to_id), args.prefix or DEFAULT_PREFIX)
        except argparse.ArgumentTypeError as e:
            parser.error(str(e))

    if start > end:
        parser.error('--from must be less than or equal to --to')

    sb = get_supabase_client()
    if args.fill_missing_apple_keys:
        fill_missing_apple_keys(sb, prefix, start, end)
        return

    cached_google_account = infer_google_account(args.secrets_file)
    if (
        args.google_account
        and cached_google_account
        and args.google_account.strip().lower() != cached_google_account.strip().lower()
    ):
        parser.error(
            f'--google-account {args.google_account.strip().lower()} does not match '
            f'the account in {args.secrets_file}: {cached_google_account}. '
            'Select the secrets file provisioned for that Google account.'
        )

    google_account = args.google_account or cached_google_account
    if not google_account:
        parser.error('--google-account is required when --secrets-file does not contain a cached username')
    google_account = google_account.strip().lower()

    existing_rows = load_existing_tags(sb)
    existing = {r['tag_id'] for r in existing_rows if isinstance(r.get('tag_id'), str)}
    if args.ensure_count is not None:
        if args.prefix is None:
            prefix = account_tag_prefix(google_account, existing_rows)
        account_count = sum(
            1 for row in existing_rows
            if (row.get('google_account') or '').strip().lower() == google_account
            and isinstance(row.get('google_id'), str)
            and bool(row['google_id'].strip())
        )
        missing = max(0, args.ensure_count - account_count)
        print(f'{google_account}: {account_count}/{args.ensure_count} tags registered')
        if not missing:
            print('No tags need to be generated.')
            return
        tag_ids = next_available_tag_ids(existing, prefix, missing)
    else:
        tag_ids = [f'{prefix}{n}' for n in range(start, end + 1)]

    import Auth.token_cache as _tc
    _tc._get_secrets_file = lambda: args.secrets_file

    print(f'Generating {len(tag_ids)} tags: {tag_ids[0]}–{tag_ids[-1]}')
    print(f'Google account: {google_account}')
    if os.getenv('GOOGLE_SECRETS_R2_BUCKET') and os.getenv('GOOGLE_SECRETS_R2_KEY'):
        print(
            'Google secrets: '
            f"{os.environ['GOOGLE_SECRETS_R2_BUCKET']}/{os.environ['GOOGLE_SECRETS_R2_KEY']}"
        )
    else:
        print(f'Google secrets: {args.secrets_file}')

    conflicts = [t for t in tag_ids if t in existing]
    if conflicts:
        sys.exit(f'Error: these tag_ids already exist in DB: {conflicts}')

    from Auth.auth_response import GoogleAuthError
    from Auth.spot_token_retrieval import get_spot_token

    try:
        get_spot_token(google_account)
    except GoogleAuthError as e:
        sys.exit(f'Google authentication failed before registration: {e}')

    success = 0
    errors = 0

    for i, tag_id in enumerate(tag_ids):
        eik = secrets.token_bytes(32)
        apple = generate_apple_keys()
        print(f'[{i+1}/{len(tag_ids)}] {tag_id} registering...', end=' ', flush=True)
        try:
            google_id, google_adv_key = _register_ble_device(eik, tag_id)
            sb.table('hybrid_tags').insert({
                'tag_id': tag_id,
                'apple_priv_key': apple['apple_priv_key'],
                'apple_adv_key': apple['apple_adv_key'],
                'google_eik': eik.hex(),
                'google_id': google_id,
                'google_adv_key': google_adv_key,
                'google_account': google_account,
            }).execute()
            print(f'OK  {google_id}')
            success += 1
        except GoogleAuthError as e:
            print(f'FAILED: {e}')
            sys.exit('Google authentication failed; stopping remaining registrations.')
        except Exception as e:
            print(f'FAILED: {e}')
            errors += 1

        if i < len(tag_ids) - 1:
            time.sleep(args.delay)

    print(f'\nDone: {success} registered, {errors} failed.')
    if errors and (args.ensure_count is not None or errors == len(tag_ids)):
        sys.exit(1)


if __name__ == '__main__':
    main()
