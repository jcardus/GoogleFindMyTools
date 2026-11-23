#!/usr/bin/env python3
"""Sync the Google account that has gone longest without a pass.

The runner claims that row in google_accounts (last_sync_status='running')
before starting, so another server takes the next-coldest account instead of
the same one. A local lock keeps one runner per machine. Each runner has
ACCOUNT_CONCURRENCY workers, and a free worker claims again as soon as it
finishes.
"""

import argparse
import datetime
import fcntl
import hashlib
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

from supabase import create_client


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("google-accounts-sync")
DEFAULT_LOCK_FILE = "/tmp/tagora-google-accounts-sync.lock"
AUTH_FAILURE_EXIT_CODE = 2
GENERATE_TIMEOUT_SECONDS = int(os.getenv("GOOGLE_GENERATE_TIMEOUT", "300"))
SYNC_TIMEOUT_SECONDS = int(os.getenv("GOOGLE_SYNC_TIMEOUT", "1200"))
GENERATE_CHECK_INTERVAL_SECONDS = int(os.getenv("GOOGLE_GENERATE_CHECK_INTERVAL", str(24 * 60 * 60)))
# Each account is its own process, FCM connection, and Google quota. Two
# 100-tag accounts overlapped in the same ~150s as one of them, and the HTTP
# 429 rate stayed at about 12 timeouts per 100 tags. Tag concurrency inside an
# account stays at 1: those requests share one FCM connection and Google
# answers the extras with 429. Cap at 4 for this host's memory.
ACCOUNT_CONCURRENCY = min(4, max(1, int(os.getenv("GOOGLE_ACCOUNT_CONCURRENCY", "2"))))
# A dead runner stops refreshing updated_at. After this lease another runner
# may claim the row. The heartbeat is shorter than the lease so a live sync
# that runs for many minutes keeps the claim.
CLAIM_LEASE_SECONDS = max(60, int(os.getenv("GOOGLE_SYNC_CLAIM_LEASE", "300")))
CLAIM_HEARTBEAT_SECONDS = min(60, max(15, CLAIM_LEASE_SECONDS // 3))
_supabase_lock = threading.Lock()


def _generate_check_marker_file(google_account: str) -> Path:
    account_hash = hashlib.sha256(google_account.encode()).hexdigest()[:16]
    return Path(f"/tmp/tagora-google-generate-check-{account_hash}.txt")


def _should_check_generate(google_account: str, now: float) -> bool:
    if GENERATE_CHECK_INTERVAL_SECONDS <= 0:
        return True
    try:
        last_check = float(_generate_check_marker_file(google_account).read_text().strip())
    except (OSError, ValueError):
        return True
    return (now - last_check) >= GENERATE_CHECK_INTERVAL_SECONDS


def _write_generate_check_marker(google_account: str, now: float) -> None:
    try:
        _generate_check_marker_file(google_account).write_text(str(now))
    except OSError:
        log.warning("could not write generate-check marker for %s", google_account)


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"{name} environment variable is required")
    return value


def _acquire_lock():
    lock_path = os.getenv("GOOGLE_SYNC_LOCK_FILE", DEFAULT_LOCK_FILE)
    lock_file = open(lock_path, "w", encoding="utf-8")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return None
    return lock_file


def _parse_ts(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def _cold_sort_key(row: dict) -> tuple:
    last_sync_at = row.get("last_sync_at")
    # None sorts first: an account that has never synced is the coldest.
    return (last_sync_at is not None, last_sync_at or "", row.get("google_account") or "")


def _claim_is_fresh(row: dict, now: datetime.datetime) -> bool:
    if row.get("last_sync_status") != "running":
        return False
    updated = _parse_ts(row.get("updated_at"))
    if updated is None:
        return False
    return (now - updated).total_seconds() < CLAIM_LEASE_SECONDS


def _fetch_ready_accounts(supabase) -> list[dict]:
    rows: list[dict] = []
    page_size = 1000
    while True:
        page = (
            supabase.table("google_accounts")
            .select("google_account,secrets_r2_bucket,secrets_r2_key,last_sync_at,last_sync_status,last_sync_message,updated_at")
            .eq("status", "ready")
            .order("google_account")
            .range(len(rows), len(rows) + page_size - 1)
            .execute()
            .data
            or []
        )
        rows.extend(page)
        if len(page) < page_size:
            return rows


def _match_observed(query, column: str, value):
    if value is None:
        return query.is_(column, "null")
    return query.eq(column, value)


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{threading.current_thread().name}"


def _signal_claim(supabase, row: dict, worker: str) -> bool:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    query = (
        supabase.table("google_accounts")
        .update({
            "last_sync_status": "running",
            "last_sync_message": f"claimed by {worker}"[:1000],
            "updated_at": now,
        })
        .eq("google_account", row["google_account"])
        .eq("status", "ready")
    )
    query = _match_observed(query, "last_sync_at", row.get("last_sync_at"))
    query = _match_observed(query, "last_sync_status", row.get("last_sync_status"))
    query = _match_observed(query, "updated_at", row.get("updated_at"))
    response = query.execute()
    data = response.data or []
    return len(data) == 1


def _log_claim(row: dict, worker: str) -> None:
    last_sync_at = _parse_ts(row.get("last_sync_at"))
    if last_sync_at is None:
        cold = "never synced"
    else:
        age = (datetime.datetime.now(datetime.timezone.utc) - last_sync_at).total_seconds()
        cold = f"last sync {age:.0f}s ago"
    log.info("Claimed %s (%s) for %s", row["google_account"], cold, worker)


def _claim_coldest(supabase, google_account: str | None = None) -> dict | None:
    """Mark the coldest ready account running. None if another runner won the race."""
    with _supabase_lock:
        rows = _fetch_ready_accounts(supabase)
        now = datetime.datetime.now(datetime.timezone.utc)
        if google_account is not None:
            rows = [row for row in rows if row["google_account"] == google_account]
            if not rows:
                return None
            if _claim_is_fresh(rows[0], now):
                log.info("%s is already claimed; leaving it", google_account)
                return None
        else:
            rows = [row for row in rows if not _claim_is_fresh(row, now)]
            rows.sort(key=_cold_sort_key)
            if not rows:
                log.info("No cold ready Google account to claim")
                return None
        worker = _worker_id()
        for row in rows:
            if _signal_claim(supabase, row, worker):
                _log_claim(row, worker)
                return row
        log.info("Coldest Google account was claimed by another runner")
        return None


def _touch_claim(supabase, google_account: str) -> None:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        with _supabase_lock:
            (
                supabase.table("google_accounts")
                .update({"updated_at": now})
                .eq("google_account", google_account)
                .eq("last_sync_status", "running")
                .execute()
            )
    except Exception:
        log.exception("Failed to refresh claim for %s", google_account)


def _heartbeat_claim(supabase, google_account: str, stop: threading.Event) -> None:
    while not stop.wait(CLAIM_HEARTBEAT_SECONDS):
        _touch_claim(supabase, google_account)


def _mark_auth_failed(supabase, google_account: str) -> None:
    with _supabase_lock:
        (
            supabase.table("google_accounts")
            .update({
                "status": "auth_failed",
                "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            })
            .eq("google_account", google_account)
            .execute()
        )
    log.warning("Marked %s as auth_failed in google_accounts", google_account)


def _log_sync(supabase, google_account: str, status: str, message: str) -> None:
    try:
        with _supabase_lock:
            (
                supabase.table("google_accounts")
                .update({
                    "last_sync_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "last_sync_status": status,
                    "last_sync_message": message[:1000],
                    "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                })
                .eq("google_account", google_account)
                .execute()
            )
    except Exception:
        log.exception("Failed to write sync log for %s", google_account)


def _run_account(supabase, hub_dir: Path, account: dict) -> int:
    google_account = account.get("google_account")
    bucket = account.get("secrets_r2_bucket")
    key = account.get("secrets_r2_key")
    if not all((google_account, bucket, key)):
        log.error("Skipping invalid google_accounts row: %r", account)
        if google_account:
            _log_sync(supabase, google_account, "error", "invalid google_accounts row")
        return 1

    stop_heartbeat = threading.Event()
    heartbeat = threading.Thread(
        target=_heartbeat_claim,
        args=(supabase, google_account, stop_heartbeat),
        name=f"claim-{google_account}",
        daemon=True,
    )
    heartbeat.start()
    try:
        return _sync_claimed_account(supabase, hub_dir, account, env=None)
    finally:
        stop_heartbeat.set()
        heartbeat.join(timeout=5)


def _sync_claimed_account(supabase, hub_dir: Path, account: dict, env: dict | None) -> int:
    google_account = account["google_account"]
    bucket = account["secrets_r2_bucket"]
    key = account["secrets_r2_key"]
    if env is None:
        env = os.environ.copy()
    # The database row is authoritative; never inherit another account's object.
    env["GOOGLE_SECRETS_R2_BUCKET"] = bucket
    env["GOOGLE_SECRETS_R2_KEY"] = key

    account_started = time.monotonic()
    generate_s = 0.0
    if _should_check_generate(google_account, time.time()):
        generate_command = [
            sys.executable,
            str(hub_dir / "generate_tags.py"),
            "--ensure-count",
            "100",
            "--google-account",
            google_account,
        ]
        log.info("Ensuring %s has 100 tags", google_account)
        generate_started = time.monotonic()
        try:
            generate_result = subprocess.run(
                generate_command, cwd=hub_dir, env=env, check=False, timeout=GENERATE_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            generate_s = time.monotonic() - generate_started
            log.error(
                "Tag generation timed out for %s after %.1fs",
                google_account,
                generate_s,
            )
            _log_sync(supabase, google_account, "error", f"tag generation timed out after {GENERATE_TIMEOUT_SECONDS}s")
            return 1
        generate_s = time.monotonic() - generate_started
        log.info("Tag check for %s finished in %.1fs", google_account, generate_s)
        if generate_result.returncode != 0:
            log.error("Tag generation failed for %s", google_account)
            _log_sync(supabase, google_account, "error", f"tag generation failed (exit {generate_result.returncode})")
            return generate_result.returncode
        _write_generate_check_marker(google_account, time.time())
    else:
        log.info("Skipping tag-count check for %s (checked within the last %ds)", google_account, GENERATE_CHECK_INTERVAL_SECONDS)

    command = [
        sys.executable,
        str(hub_dir / "microservice.py"),
        "--google-account",
        google_account,
    ]
    log.info("Syncing %s with R2 object %s/%s", google_account, bucket, key)
    sync_started = time.monotonic()
    try:
        result = subprocess.run(command, cwd=hub_dir, env=env, check=False, timeout=SYNC_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        sync_s = time.monotonic() - sync_started
        log.error(
            "Sync timed out for %s after %.1fs (tag check %.1fs, total %.1fs)",
            google_account,
            sync_s,
            generate_s,
            time.monotonic() - account_started,
        )
        _log_sync(supabase, google_account, "error", f"sync timed out after {SYNC_TIMEOUT_SECONDS}s")
        return 1
    sync_s = time.monotonic() - sync_started
    log.info(
        "%s finished in %.1fs (tag check %.1fs, sync %.1fs, exit %d)",
        google_account,
        time.monotonic() - account_started,
        generate_s,
        sync_s,
        result.returncode,
    )
    if result.returncode == 0:
        _log_sync(supabase, google_account, "ok", "sync completed")
    else:
        _log_sync(supabase, google_account, "error", f"microservice sync failed (exit {result.returncode})")
    return result.returncode


def _record_failure(supabase, account: dict, return_code: int) -> None:
    if return_code == 0:
        return
    google_account = account.get("google_account", "(unknown)")
    log.error("Sync failed for %s", google_account)
    if return_code == AUTH_FAILURE_EXIT_CODE and account.get("google_account"):
        try:
            _mark_auth_failed(supabase, account["google_account"])
        except Exception:
            log.exception("Failed to mark %s as auth_failed", account["google_account"])


def _sync_forever(supabase, hub_dir: Path) -> None:
    while True:
        try:
            account = _claim_coldest(supabase)
        except Exception:
            log.exception("Failed to claim the coldest Google account")
            time.sleep(5)
            continue
        if account is None:
            time.sleep(5)
            continue
        try:
            return_code = _run_account(supabase, hub_dir, account)
        except Exception:
            log.exception("Sync crashed for %s", account.get("google_account", "(unknown)"))
            try:
                _log_sync(supabase, account["google_account"], "error", "sync crashed")
            except Exception:
                log.exception(
                    "Failed to clear the claim for %s",
                    account.get("google_account", "(unknown)"),
                )
            return_code = 1
        _record_failure(supabase, account, return_code)


def main() -> int:
    default_hub_dir = Path(os.getenv("GOOGLE_HUB_DIR", Path(__file__).resolve().parents[1]))
    parser = argparse.ArgumentParser(
        description="Claim and sync the coldest ready Google account in Supabase"
    )
    parser.add_argument(
        "--hub-dir",
        default=str(default_hub_dir),
        help="GoogleFindMyTools checkout (defaults to this repository)",
    )
    parser.add_argument(
        "--google-account",
        help="Claim and sync this ready Google account, then exit",
    )
    parser.add_argument(
        "--skip-lock",
        action="store_true",
        help="Run without acquiring the global sync lock (may overlap another sync)",
    )
    args = parser.parse_args()

    hub_dir = Path(args.hub_dir).expanduser().resolve()
    if not (hub_dir / "microservice.py").is_file():
        parser.error(f"microservice.py not found in {hub_dir}")

    # The child process uses these credentials for R2's S3-compatible API.
    _required_env("GOOGLE_SECRETS_R2_ACCOUNT_ID")
    _required_env("GOOGLE_SECRETS_R2_ACCESS_KEY_ID")
    _required_env("GOOGLE_SECRETS_R2_SECRET_ACCESS_KEY")
    supabase_url = _required_env("SUPABASE_URL")
    service_role = _required_env("SUPABASE_SERVICE_ROLE")

    lock_file = None
    if args.skip_lock:
        log.warning("Skipping the global Google account sync lock")
    else:
        lock_file = _acquire_lock()
        if lock_file is None:
            log.info("Another Google account sync is still running; skipping this invocation")
            return 0

    supabase = create_client(supabase_url, service_role)
    if args.google_account:
        with _supabase_lock:
            ready = _fetch_ready_accounts(supabase)
        if not any(row["google_account"] == args.google_account for row in ready):
            log.error("Ready Google account not found: %s", args.google_account)
            return 1
        account = _claim_coldest(supabase, args.google_account)
        if account is None:
            return 0
        try:
            return_code = _run_account(supabase, hub_dir, account)
        except Exception:
            log.exception("Sync crashed for %s", args.google_account)
            _log_sync(supabase, args.google_account, "error", "sync crashed")
            return_code = 1
        _record_failure(supabase, account, return_code)
        return 1 if return_code else 0

    log.info(
        "Claiming the coldest ready Google account; concurrency=%d",
        ACCOUNT_CONCURRENCY,
    )
    threads = []
    for index in range(ACCOUNT_CONCURRENCY):
        thread = threading.Thread(
            target=_sync_forever,
            args=(supabase, hub_dir),
            name=f"google-sync-{index + 1}",
            daemon=False,
        )
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
