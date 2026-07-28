"""
Create or refresh a GoogleFindMyTools auth cache for an existing Google account.

This does not create a Google account. The account must already exist and be
ready to sign in through Chrome.
"""
import argparse
import shutil
import json
import pathlib
import sys
from datetime import datetime, timezone
from typing import Optional

from python_version import require_python_312

require_python_312()


def load_secrets(secrets_path: pathlib.Path) -> dict:
    if not secrets_path.exists():
        return {}

    try:
        with secrets_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError:
        print(f"Could not parse existing secrets file: {secrets_path}", file=sys.stderr)
        raise SystemExit(1)

    return data if isinstance(data, dict) else {}


def infer_google_account(data: dict) -> Optional[str]:
    username = data.get("username")
    if isinstance(username, str) and username.strip():
        return username.strip().lower()
    return None


def backup_secrets_file(secrets_path: pathlib.Path) -> pathlib.Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = secrets_path.with_name(f"{secrets_path.name}.{timestamp}.bak")
    suffix = 1
    while backup_path.exists():
        backup_path = secrets_path.with_name(f"{secrets_path.name}.{timestamp}.{suffix}.bak")
        suffix += 1

    shutil.move(str(secrets_path), str(backup_path))
    return backup_path


def confirm_existing_secrets(
    secrets_path: pathlib.Path,
    data: dict,
    target_account: Optional[str],
    assume_yes: bool,
    backup_existing: bool,
) -> dict:
    if not secrets_path.exists():
        return data

    existing_account = infer_google_account(data) or "unknown"
    if backup_existing:
        backup_path = backup_secrets_file(secrets_path)
        print(f"Existing secrets file moved to: {backup_path}")
        return {}

    if assume_yes:
        return data

    print("")
    print(f"Existing secrets file found: {secrets_path}")
    print(f"Google account in file: {existing_account}")
    print(f"Target Google account: {target_account or 'will infer after login'}")
    print("")
    print("Choose one:")
    print("  c  Continue and update this secrets file")
    print("  b  Move existing file to a .bak name and start fresh")
    print("  q  Abort")
    choice = input("Selection [q]: ").strip().lower()

    if choice == "c":
        return data
    if choice == "b":
        backup_path = backup_secrets_file(secrets_path)
        print(f"Existing secrets file moved to: {backup_path}")
        return {}

    print("Aborted; secrets file was not changed.", file=sys.stderr)
    raise SystemExit(3)


def infer_google_account_from_file(secrets_path: pathlib.Path) -> Optional[str]:
    return infer_google_account(load_secrets(secrets_path))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--google-account")
    parser.add_argument("--secrets-file")
    parser.add_argument("--tools-root")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Reuse an existing secrets file without prompting.",
    )
    parser.add_argument(
        "--backup-existing",
        action="store_true",
        help="Move an existing secrets file to a timestamped .bak path before authenticating.",
    )
    parser.add_argument(
        "--skip-owner-key",
        action="store_true",
        help="Only fetch account OAuth tokens; do not fetch the Find Hub owner key.",
    )
    args = parser.parse_args()

    tools_root = (
        pathlib.Path(args.tools_root).expanduser().resolve()
        if args.tools_root
        else pathlib.Path(__file__).resolve().parent
    )
    if args.secrets_file:
        secrets_path = pathlib.Path(args.secrets_file).expanduser().resolve()
    elif args.google_account:
        account_name = args.google_account.strip().lower()
        secrets_path = tools_root / "Auth" / f"{account_name}.json"
    else:
        secrets_path = tools_root / "Auth" / "secrets.json"

    secrets_path.parent.mkdir(parents=True, exist_ok=True)
    data = load_secrets(secrets_path)
    account = args.google_account.strip().lower() if args.google_account else infer_google_account(data)
    data = confirm_existing_secrets(
        secrets_path=secrets_path,
        data=data,
        target_account=account,
        assume_yes=args.yes,
        backup_existing=args.backup_existing,
    )

    sys.path.insert(0, str(tools_root))

    import Auth.token_cache as token_cache

    token_cache._get_secrets_file = lambda: str(secrets_path)
    # Interactive provisioning must build one complete local cache. The
    # PowerShell wrapper uploads that exact file to the selected account's R2
    # object only after all token checks and owner-key derivation succeed.
    token_cache._r2_is_configured = lambda: False

    with secrets_path.open("w", encoding="utf-8") as fh:
        if account:
            data["username"] = account
        json.dump(data, fh)

    from Auth.adm_token_retrieval import get_adm_token
    from Auth.aas_token_retrieval import get_aas_token
    from Auth.spot_token_retrieval import get_spot_token
    from chrome_driver import create_driver, safe_quit_driver

    driver = create_driver()
    try:
        if not args.skip_owner_key:
            from KeyBackup.shared_key_retrieval import get_shared_key

            print("Authenticating and requesting Find Hub encrypted-key approval in Chrome.")
            get_shared_key(driver)
        elif account:
            print(f"Authenticating {account} in Chrome.")
        else:
            print("Authenticating in Chrome.")

        # The encrypted-key flow above establishes the Google session. Reuse it
        # to obtain the OAuth account token without another interactive login.
        get_aas_token(driver)
        if not account:
            account = infer_google_account_from_file(secrets_path)
            if not account:
                print(
                    "Could not infer Google account from the generated secrets file.",
                    file=sys.stderr,
                )
                return 2
            print(f"Inferred Google account: {account}")

        get_adm_token(account)
        get_spot_token(account)

        if not args.skip_owner_key:
            from SpotApi.GetEidInfoForE2eeDevices.get_owner_key import get_owner_key

            print("Deriving the Find Hub owner key from the approved shared key.")
            get_owner_key(driver)
    finally:
        safe_quit_driver(driver)

    with secrets_path.open("r", encoding="utf-8") as fh:
        final_data = json.load(fh)
    final_data["username"] = account
    with secrets_path.open("w", encoding="utf-8") as fh:
        json.dump(final_data, fh)

    print(f"Wrote auth cache: {secrets_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
