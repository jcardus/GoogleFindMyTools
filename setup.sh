#!/usr/bin/env bash
set -euo pipefail

zip_url="https://github.com/jcardus/GoogleFindMyTools/archive/refs/heads/main.zip"
zip_path="GoogleFindMyTools-main.zip"
repo_path="GoogleFindMyTools-main"

if [[ -d "/opt/homebrew/opt/expat/lib" ]]; then
  export DYLD_LIBRARY_PATH="/opt/homebrew/opt/expat/lib:${DYLD_LIBRARY_PATH:-}"
elif [[ -d "/usr/local/opt/expat/lib" ]]; then
  export DYLD_LIBRARY_PATH="/usr/local/opt/expat/lib:${DYLD_LIBRARY_PATH:-}"
fi

echo "[Tagora] Downloading GoogleFindMyTools..."
curl -L -o "$zip_path" "$zip_url"

echo "[Tagora] Extracting files..."
rm -rf "$repo_path"
unzip -o "$zip_path"

cd "$repo_path"

echo "[Tagora] Creating Python 3.12 virtual environment..."
python3.12 -m venv .venv

echo "[Tagora] Installing dependencies..."
. .venv/bin/activate
python -m pip install -r requirements.txt

echo "[Tagora] Starting Google account provisioning..."
if [[ -r /dev/tty ]]; then
  python provision_google_account.py < /dev/tty
else
  python provision_google_account.py
fi
