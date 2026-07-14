"""
Provision an existing Google account for the GoogleFindMyTools hub.

This script can run the local Chrome auth flow, upload the resulting auth cache
through the Tagora backend, and let the backend upsert the matching Supabase
google_accounts row.
"""
import argparse
import getpass
import json
import os
import pathlib
import subprocess
import sys
from typing import Optional

import requests
from requests import HTTPError

from provision_account_auth import infer_google_account, load_secrets
from python_version import require_python_312

require_python_312()

DEFAULT_PROVISIONING_URL = "https://tagora.uk/api/google-accounts/provision"


def require_value(name: str, value: Optional[str]) -> str:
    if value and value.strip():
        return value.strip()
    raise SystemExit(f"{name} is required. Pass --{name.replace('_', '-')} or set the matching environment variable.")


def prompt_secret(name: str, value: Optional[str]) -> str:
    if value and value.strip():
        return value.strip()

    entered = getpass.getpass(f"{name}: ").strip()
    if entered:
        return entered
    raise SystemExit(f"{name} is required.")


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


def provision_backend(
    *,
    provisioning_url: str,
    provisioning_token: str,
    payload: dict,
) -> dict:
    response = requests.post(
        provisioning_url,
        json=payload,
        headers={
            "Authorization": f"Bearer {provisioning_token}",
        },
        timeout=30,
    )
    try:
        response.raise_for_status()
    except HTTPError as exc:
        body = response.text.strip()
        detail = f"\n{body}" if body else ""
        raise SystemExit(f"Provisioning request failed: HTTP {response.status_code}{detail}") from exc
    return response.json()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--google-account")
    parser.add_argument("--secrets-file")
    parser.add_argument("--tools-root")
    parser.add_argument("--run-auth", action="store_true")
    parser.add_argument("--skip-owner-key", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--backup-existing", action="store_true")
    parser.add_argument("--provisioning-url", default=os.getenv("PROVISIONING_URL", DEFAULT_PROVISIONING_URL))
    parser.add_argument("--provisioning-token", default=os.getenv("PROVISIONING_TOKEN"))
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

    provisioning_url = require_value("provisioning_url", args.provisioning_url)
    provisioning_token = prompt_secret("PROVISIONING_TOKEN", args.provisioning_token)

    secret_object = load_secrets(secrets_path)
    secret_object["username"] = google_account

    payload = {
        "google_account": google_account,
        "secrets": secret_object,
        "status": args.status,
        "notes": args.notes,
    }
    print(f"Provisioning {google_account} through {provisioning_url}")
    result = provision_backend(
        provisioning_url=provisioning_url,
        provisioning_token=provisioning_token,
        payload=payload,
    )

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
