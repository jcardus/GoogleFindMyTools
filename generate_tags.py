"""
Register devices MK1000–MK1099 with Google Find My and insert directly into Supabase.

Usage:
    cd ~/IdeaProjects/tags/google-hub
    python generate_tags.py
"""
import argparse
import os
import pathlib
import re
import secrets
import subprocess
import sys
import time

# GoogleFindMyTools contains the API modules
sys.path.insert(0, str(pathlib.Path.home() / 'GoogleFindMyTools'))

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

SUPABASE_PROJECT_REF = 'qxabzyhabkrdmyztmjul'
TAG_IDS = [f'MK{n}' for n in range(1000, 1100)]
GOOGLE_ACCOUNT = 'entrack.plataforma@gmail.com'


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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--delay', type=float, default=1.0,
                        help='Seconds between registrations (default: 1)')
    args = parser.parse_args()

    sb_url = os.getenv('SUPABASE_URL')
    sb_key = os.getenv('SUPABASE_SERVICE_ROLE')
    if not sb_url or not sb_key:
        print('Fetching Supabase credentials via CLI...')
        sb_url, sb_key = get_supabase_credentials()

    from supabase import create_client
    sb = create_client(sb_url, sb_key)
    print(f'Supabase ready ({sb_url})')

    existing = {r['tag_id'] for r in sb.table('hybrid_tags').select('tag_id').execute().data or []}
    conflicts = [t for t in TAG_IDS if t in existing]
    if conflicts:
        sys.exit(f'Error: these tag_ids already exist in DB: {conflicts}')

    success = 0
    errors = 0

    for i, tag_id in enumerate(TAG_IDS):
        eik = secrets.token_bytes(32)
        print(f'[{i+1}/{len(TAG_IDS)}] {tag_id} registering...', end=' ', flush=True)
        try:
            google_id, google_adv_key = _register_ble_device(eik, tag_id)
            sb.table('hybrid_tags').insert({
                'tag_id': tag_id,
                'google_eik': eik.hex(),
                'google_id': google_id,
                'google_adv_key': google_adv_key,
                'google_account': GOOGLE_ACCOUNT,
            }).execute()
            print(f'OK  {google_id}')
            success += 1
        except Exception as e:
            print(f'FAILED: {e}')
            errors += 1

        if i < len(TAG_IDS) - 1:
            time.sleep(args.delay)

    print(f'\nDone: {success} registered, {errors} failed.')
    sys.exit(1 if errors == len(TAG_IDS) else 0)


if __name__ == '__main__':
    main()
