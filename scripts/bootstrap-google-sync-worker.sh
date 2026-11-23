#!/usr/bin/env bash
# Setup for a Hetzner Google sync worker from a GoogleFindMyTools checkout.
# Expects /etc/google-sync.env and a populated /opt/GoogleFindMyTools (no git clone).
set -euo pipefail

ENV_FILE=/etc/google-sync.env
TAGS_DIR=/opt/tags/GoogleFindMyTools
UNIT_SRC="${TAGS_DIR}/deploy/hetzner/google-sync.service"
UNIT_DST=/etc/systemd/system/google-sync.service

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing ${ENV_FILE}" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

: "${SUPABASE_URL:?SUPABASE_URL is required in ${ENV_FILE}}"
: "${SUPABASE_SERVICE_ROLE:?SUPABASE_SERVICE_ROLE is required in ${ENV_FILE}}"
: "${GOOGLE_SECRETS_R2_ACCOUNT_ID:?GOOGLE_SECRETS_R2_ACCOUNT_ID is required in ${ENV_FILE}}"
: "${GOOGLE_SECRETS_R2_ACCESS_KEY_ID:?GOOGLE_SECRETS_R2_ACCESS_KEY_ID is required in ${ENV_FILE}}"
: "${GOOGLE_SECRETS_R2_SECRET_ACCESS_KEY:?GOOGLE_SECRETS_R2_SECRET_ACCESS_KEY is required in ${ENV_FILE}}"

if [[ ! -f "${TAGS_DIR}/scripts/sync-google-accounts.py" ]]; then
  echo "Missing ${TAGS_DIR}/scripts/sync-google-accounts.py; clone the repository with submodules before bootstrapping" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates \
  curl \
  gnupg \
  python3 \
  python3-pip \
  python3-venv \
  rsync \
  xvfb

HUB_DIR="${TAGS_DIR}"
if [[ ! -f "${HUB_DIR}/microservice.py" ]]; then
  echo "GoogleFindMyTools/microservice.py missing at ${TAGS_DIR}" >&2
  exit 1
fi

python3 -m venv "${HUB_DIR}/.venv"
"${HUB_DIR}/.venv/bin/pip" install --upgrade pip
"${HUB_DIR}/.venv/bin/pip" install -r "${HUB_DIR}/requirements.txt"

if ! grep -q '^TAGS_DIR=' "${ENV_FILE}"; then
  printf '\nTAGS_DIR=%s\n' "${TAGS_DIR}" >> "${ENV_FILE}"
fi

install -m 0644 "${UNIT_SRC}" "${UNIT_DST}"
systemctl daemon-reload
systemctl enable --now google-sync.service

echo "google-sync worker bootstrapped; follow logs with: journalctl -u google-sync -f"
