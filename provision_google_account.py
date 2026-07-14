"""
Provision an existing Google account for the GoogleFindMyTools hub.

This script can run the local Chrome auth flow, upload the resulting auth cache
to Cloudflare R2, and upsert the matching Supabase google_accounts row.
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys
from datetime import datetime, timezone
from typing import Optional

import boto3
import requests

from provision_account_auth import infer_google_account, load_secrets
from python_version import require_python_312

require_python_312()


def optional_env(primary: str, fallback: Optional[str] = None) -> Optional[str]:
    value = os.getenv(primary)
    if value:
        return value
    return os.getenv(fallback) if fallback else None


def require_value(name: str, value: Optional[str]) -> str:
    if value and value.strip():
        return value.strip()
    raise SystemExit(f"{name} is required. Pass --{name.replace('_', '-')} or set the matching environment variable.")


def default_secrets_path(tools_root: pathlib.Path, google_account: Optional[str]) -> pathlib.Path:
    if google_account:
        return tools_root / "Auth" / f"{google_account.strip().lower()}.json"
    return tools_root / "Auth" / "secrets.json"


def get_secrets_account(secrets_path: pathlib.Path) -> Optional[str]:
    return infer_google_account(load_secrets(secrets_path))


def run_auth_helper(args: argparse.Namespace, tools_root: pathlib.Path, secrets_path: pathlib.Path, google_account: Optional[str]) -> None:
    command = [
        sys.executable,
        str(tools_root / "provision_account_auth.py"),
        "--secrets-file",
        str(secrets_path),
        "--tools-root",
        str(tools_root),
    ]
    if google_account:
        command.extend(["--google-account", google_account])
    if args.skip_owner_key:
        command.append("--skip-owner-key")
    if args.yes:
        command.append("--yes")
    if args.backup_existing:
        command.append("--backup-existing")

    subprocess.run(command, cwd=str(tools_root), check=True)


def r2_key_for_account(prefix: str, google_account: str) -> str:
    clean_prefix = prefix.strip("/")
    account_file = f"{google_account}.json"
    return f"{clean_prefix}/{account_file}" if clean_prefix else account_file


def upload_to_r2(
    *,
    bucket: str,
    key: str,
    body: bytes,
    account_id: str,
    access_key_id: str,
    secret_access_key: str,
    endpoint: Optional[str],
) -> None:
    endpoint_url = endpoint or f"https://{account_id}.r2.cloudflarestorage.com"
    client = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        region_name="auto",
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
    )
    client.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")


def upsert_supabase_account(
    *,
    supabase_url: str,
    service_key: str,
    payload: dict,
) -> None:
    endpoint = f"{supabase_url.rstrip('/')}/rest/v1/google_accounts?on_conflict=google_account"
    response = requests.post(
        endpoint,
        json=payload,
        headers={
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Prefer": "resolution=merge-duplicates,return=representation",
        },
        timeout=30,
    )
    response.raise_for_status()
    if response.text:
        print(response.text)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--google-account")
    parser.add_argument("--secrets-file")
    parser.add_argument("--tools-root")
    parser.add_argument("--run-auth", action="store_true")
    parser.add_argument("--skip-owner-key", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--backup-existing", action="store_true")
    parser.add_argument("--supabase-url", default=os.getenv("SUPABASE_URL"))
    parser.add_argument("--supabase-service-key", default=optional_env("SUPABASE_SERVICE_ROLE", "SUPABASE_SERVICE_KEY"))
    parser.add_argument("--r2-bucket", default=os.getenv("GOOGLE_SECRETS_R2_BUCKET"))
    parser.add_argument("--r2-account-id", default=os.getenv("GOOGLE_SECRETS_R2_ACCOUNT_ID"))
    parser.add_argument("--r2-access-key-id", default=os.getenv("GOOGLE_SECRETS_R2_ACCESS_KEY_ID"))
    parser.add_argument("--r2-secret-access-key", default=os.getenv("GOOGLE_SECRETS_R2_SECRET_ACCESS_KEY"))
    parser.add_argument("--r2-endpoint", default=os.getenv("GOOGLE_SECRETS_R2_ENDPOINT"))
    parser.add_argument("--r2-prefix", default=os.getenv("GOOGLE_SECRETS_R2_PREFIX", "google-secrets"))
    parser.add_argument("--status", default="ready")
    parser.add_argument("--notes", default="")
    args = parser.parse_args()

    tools_root = pathlib.Path(args.tools_root).expanduser().resolve() if args.tools_root else pathlib.Path(__file__).resolve().parent
    google_account = args.google_account.strip().lower() if args.google_account else None
    secrets_path = pathlib.Path(args.secrets_file).expanduser().resolve() if args.secrets_file else default_secrets_path(tools_root, google_account)

    if not google_account:
        google_account = get_secrets_account(secrets_path)

    if args.run_auth:
        run_auth_helper(args, tools_root, secrets_path, google_account)
        google_account = get_secrets_account(secrets_path)

    if not secrets_path.exists():
        raise SystemExit(f"Secrets file not found: {secrets_path}. Run with --run-auth or pass --secrets-file.")

    if not google_account:
        google_account = get_secrets_account(secrets_path)
    if not google_account:
        raise SystemExit(f"Could not infer Google account from secrets file: {secrets_path}")

    supabase_url = require_value("supabase_url", args.supabase_url)
    supabase_service_key = require_value("supabase_service_key", args.supabase_service_key)
    r2_bucket = require_value("r2_bucket", args.r2_bucket)
    r2_account_id = require_value("r2_account_id", args.r2_account_id)
    r2_access_key_id = require_value("r2_access_key_id", args.r2_access_key_id)
    r2_secret_access_key = require_value("r2_secret_access_key", args.r2_secret_access_key)

    secret_object = load_secrets(secrets_path)
    secret_object["username"] = google_account
    upload_json = json.dumps(secret_object, separators=(",", ":"))
    r2_key = r2_key_for_account(args.r2_prefix, google_account)

    print(f"Uploading auth cache to R2: {r2_bucket}/{r2_key}")
    upload_to_r2(
        bucket=r2_bucket,
        key=r2_key,
        body=upload_json.encode("utf-8"),
        account_id=r2_account_id,
        access_key_id=r2_access_key_id,
        secret_access_key=r2_secret_access_key,
        endpoint=args.r2_endpoint,
    )

    payload = {
        "google_account": google_account,
        "secrets_r2_bucket": r2_bucket,
        "secrets_r2_key": r2_key,
        "status": args.status,
        "notes": args.notes,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    print(f"Upserting Supabase google_accounts row for {google_account}")
    upsert_supabase_account(
        supabase_url=supabase_url,
        service_key=supabase_service_key,
        payload=payload,
    )

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
