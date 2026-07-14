#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

import json
import os

SECRETS_FILE = 'secrets.json'
R2_BUCKET_ENV = 'GOOGLE_SECRETS_R2_BUCKET'
R2_ACCOUNT_ID_ENV = 'GOOGLE_SECRETS_R2_ACCOUNT_ID'
R2_ACCESS_KEY_ID_ENV = 'GOOGLE_SECRETS_R2_ACCESS_KEY_ID'
R2_SECRET_ACCESS_KEY_ENV = 'GOOGLE_SECRETS_R2_SECRET_ACCESS_KEY'
R2_ENDPOINT_ENV = 'GOOGLE_SECRETS_R2_ENDPOINT'
R2_KEY_ENV = 'GOOGLE_SECRETS_R2_KEY'

def get_cached_value_or_set(name: str, generator: callable):

    existing_value = get_cached_value(name)

    if existing_value is not None:
        return existing_value

    value = generator()
    set_cached_value(name, value)
    return value


def get_cached_value(name: str):
    data = _read_cache()
    if data:
        value = data.get(name)
        if value:
            return value
    return None


def set_cached_value(name: str, value: str):
    data = _read_cache(strict=True) or {}
    data[name] = value
    _write_cache(data)


def _get_secrets_file():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(script_dir, SECRETS_FILE)


def _read_cache(strict: bool = False):
    if _r2_is_configured():
        data = _read_r2_cache(strict=strict)
        if data is not None:
            return data

    return _read_local_cache(strict=strict)


def _write_cache(data: dict):
    if _r2_is_configured():
        _write_r2_cache(data)
        return

    _write_local_cache(data)


def _read_local_cache(strict: bool = False):
    secrets_file = _get_secrets_file()

    if not os.path.exists(secrets_file):
        return None

    with open(secrets_file, 'r') as file:
        try:
            return json.load(file)
        except json.JSONDecodeError:
            if strict:
                raise Exception("Could not read secrets file. Aborting.")
            return None


def _write_local_cache(data: dict):
    secrets_file = _get_secrets_file()
    os.makedirs(os.path.dirname(secrets_file), exist_ok=True)
    with open(secrets_file, 'w') as file:
        json.dump(data, file)


def _r2_is_configured():
    return bool(
        os.getenv(R2_BUCKET_ENV)
        and os.getenv(R2_ACCOUNT_ID_ENV)
        and os.getenv(R2_ACCESS_KEY_ID_ENV)
        and os.getenv(R2_SECRET_ACCESS_KEY_ENV)
        and os.getenv(R2_KEY_ENV)
    )


def _r2_client():
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError(
            "R2 secrets storage requires boto3. Install GoogleFindMyTools requirements."
        ) from exc

    account_id = os.environ[R2_ACCOUNT_ID_ENV]
    endpoint = os.getenv(R2_ENDPOINT_ENV) or f'https://{account_id}.r2.cloudflarestorage.com'
    return boto3.client(
        's3',
        endpoint_url=endpoint,
        region_name='auto',
        aws_access_key_id=os.environ[R2_ACCESS_KEY_ID_ENV],
        aws_secret_access_key=os.environ[R2_SECRET_ACCESS_KEY_ENV],
    )


def _read_r2_cache(strict: bool = False):
    try:
        obj = _r2_client().get_object(Bucket=os.environ[R2_BUCKET_ENV], Key=os.environ[R2_KEY_ENV])
    except Exception as exc:
        response = getattr(exc, 'response', {}) or {}
        code = response.get('Error', {}).get('Code')
        if code in ('NoSuchKey', '404', 'NotFound'):
            return None
        raise

    try:
        return json.loads(obj['Body'].read().decode('utf-8'))
    except json.JSONDecodeError:
        if strict:
            raise Exception("Could not read R2 secrets object. Aborting.")
        return None


def _write_r2_cache(data: dict):
    body = json.dumps(data).encode('utf-8')
    _r2_client().put_object(
        Bucket=os.environ[R2_BUCKET_ENV],
        Key=os.environ[R2_KEY_ENV],
        Body=body,
        ContentType='application/json',
    )
