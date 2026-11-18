#!/usr/bin/env bash
# reverse-register.sh — dog-side reverse SSH for ONE session (SDK-callable)
#
# Design (confirmed):
# - Upstream / gateway SDK decides disconnect / reconnect and passes host + port
#   (and optional user) into this script on every (re)registration.
# - This script only: establish reverse SSH for THIS session; log; overwrite a
#   state file with ONLY the current registration public host + remote port.
# - Do NOT implement heartbeat / reconnect loops here — upstream owns that.
# - Exit nonzero if ssh fails so the SDK can retry / re-invoke.
#
# Typical tunnel:
#   ssh -N -R <REMOTE_PORT>:127.0.0.1:<LOCAL_PORT> \
#       -p <PUBLIC_SSH_PORT> <SSH_USER>@<PUBLIC_HOST>
#
# No secrets are embedded. Prefer IDENTITY_FILE or ssh-agent; never put
# private keys or passwords in this repo / state file.
#
#set -euo pipefail  # set below after usage helper for clean --help exit

set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VERSION="0.1.0"

# --- defaults (overridable by env or flags) ---------------------------------
PUBLIC_HOST="${PUBLIC_HOST:-ssh-portbroker.evoxrobot.com}"
PUBLIC_SSH_PORT="${PUBLIC_SSH_PORT:-22}"
REMOTE_PORT="${REMOTE_PORT:-}"
LOCAL_PORT="${LOCAL_PORT:-22}"
SSH_USER="${SSH_USER:-robot}"
# Default identity: private key next to this script (relative → absolute via SCRIPT_DIR).
# Override with IDENTITY_FILE / -i. Do not commit the private key to git.
IDENTITY_FILE="${IDENTITY_FILE:-${SCRIPT_DIR}/id_portbroker_rsa}"
STRICT_HOST_KEY_CHECKING="${STRICT_HOST_KEY_CHECKING:-accept-new}"
# BatchMode: never prompt for password / passphrase (SDK-friendly)
SSH_BATCH_MODE="${SSH_BATCH_MODE:-yes}"
CONNECT_TIMEOUT="${CONNECT_TIMEOUT:-15}"
SERVER_ALIVE_INTERVAL="${SERVER_ALIVE_INTERVAL:-30}"
SERVER_ALIVE_COUNT_MAX="${SERVER_ALIVE_COUNT_MAX:-3}"

STATE_DIR="${STATE_DIR:-/var/lib/ssh-portbroker-dog}"
LOG_DIR="${LOG_DIR:-/var/log/ssh-portbroker-dog}"
STATE_FILE="${STATE_FILE:-${STATE_DIR}/current-binding.env}"
LOG_FILE="${LOG_FILE:-${LOG_DIR}/reverse-register.log}"

usage() {
  cat <<EOF
Usage: ${SCRIPT_NAME} [options]

Establish a reverse SSH tunnel for THIS session only. Upstream owns
disconnect / reconnect; re-invoke this script with fresh args each time.

Required (env or flag):
  --remote-port PORT     REMOTE_PORT   allocated remote bind port on public host

Common:
  --public-host HOST     PUBLIC_HOST   default: ssh-portbroker.evoxrobot.com
  --public-ssh-port N    PUBLIC_SSH_PORT  landing SSH port, default: 22
  --local-port PORT      LOCAL_PORT    local sshd to expose, default: 22
  --user USER            SSH_USER      default: robot
  -i, --identity FILE    IDENTITY_FILE private key path
                         default: <script-dir>/id_portbroker_rsa
                         (override via env IDENTITY_FILE or -i; no secrets in git)
  --strict MODE          STRICT_HOST_KEY_CHECKING  default: accept-new
                         (yes|no|accept-new)

Paths:
  --state-dir DIR        STATE_DIR     default: /var/lib/ssh-portbroker-dog
  --log-dir DIR          LOG_DIR       default: /var/log/ssh-portbroker-dog
  --state-file FILE      STATE_FILE    default: \$STATE_DIR/current-binding.env
  --log-file FILE        LOG_FILE      default: \$LOG_DIR/reverse-register.log

Other:
  -h, --help             show this help
  -V, --version          show version

Exit codes:
  0   ssh session ended cleanly (remote closed / signal)
  1   usage / missing required args
  2   failed to prepare state/log dirs or write state
  >2  ssh failed (nonzero) — SDK may retry

Example (SDK / dog):
  PUBLIC_HOST=ssh-portbroker.evoxrobot.com REMOTE_PORT=22000 \\
    ${SCRIPT_NAME} --user robot -i /etc/ssh-portbroker-dog/id_ed25519

Packaging: ship this script + README to dog R&D; deliver the private key
separately/securely (never in the same tarball/repo).
EOF
}

log() {
  local level="$1"; shift
  local ts
  ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  local line="${ts} [${level}] $*"
  # Prefer append to log file; always mirror to stderr for SDK capture
  if [[ -n "${LOG_FILE:-}" ]] && [[ -w "$(dirname "${LOG_FILE}")" || -w "${LOG_FILE}" ]] 2>/dev/null; then
    printf '%s\n' "${line}" >>"${LOG_FILE}" 2>/dev/null || true
  fi
  printf '%s\n' "${line}" >&2
}

die() {
  local code="$1"; shift
  log ERROR "$*"
  exit "${code}"
}

# --- parse args -------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --public-host)       PUBLIC_HOST="$2"; shift 2 ;;
    --public-ssh-port)   PUBLIC_SSH_PORT="$2"; shift 2 ;;
    --remote-port)       REMOTE_PORT="$2"; shift 2 ;;
    --local-port)        LOCAL_PORT="$2"; shift 2 ;;
    --user|--ssh-user)   SSH_USER="$2"; shift 2 ;;
    -i|--identity)       IDENTITY_FILE="$2"; shift 2 ;;
    --strict)            STRICT_HOST_KEY_CHECKING="$2"; shift 2 ;;
    --state-dir)         STATE_DIR="$2"; STATE_FILE="${STATE_DIR}/current-binding.env"; shift 2 ;;
    --log-dir)           LOG_DIR="$2"; LOG_FILE="${LOG_DIR}/reverse-register.log"; shift 2 ;;
    --state-file)        STATE_FILE="$2"; shift 2 ;;
    --log-file)          LOG_FILE="$2"; shift 2 ;;
    -h|--help)           usage; exit 0 ;;
    -V|--version)        printf '%s %s\n' "${SCRIPT_NAME}" "${VERSION}"; exit 0 ;;
    *)
      die 1 "unknown argument: $1 (see --help)"
      ;;
  esac
done

# --- validate ---------------------------------------------------------------
[[ -n "${REMOTE_PORT}" ]] || die 1 "REMOTE_PORT / --remote-port is required"
[[ "${REMOTE_PORT}" =~ ^[0-9]+$ ]] || die 1 "REMOTE_PORT must be an integer"
[[ "${PUBLIC_SSH_PORT}" =~ ^[0-9]+$ ]] || die 1 "PUBLIC_SSH_PORT must be an integer"
[[ "${LOCAL_PORT}" =~ ^[0-9]+$ ]] || die 1 "LOCAL_PORT must be an integer"
[[ -n "${PUBLIC_HOST}" ]] || die 1 "PUBLIC_HOST is empty"
[[ -n "${SSH_USER}" ]] || die 1 "SSH_USER is empty"

case "${STRICT_HOST_KEY_CHECKING}" in
  yes|no|accept-new) ;;
  *) die 1 "STRICT_HOST_KEY_CHECKING must be yes|no|accept-new" ;;
esac

if [[ -n "${IDENTITY_FILE}" ]]; then
  [[ -f "${IDENTITY_FILE}" ]] || die 1 "IDENTITY_FILE not found: ${IDENTITY_FILE}"
  # Tighten perms if needed; never print / dump key material
  chmod 600 "${IDENTITY_FILE}" 2>/dev/null || \
    log WARN "could not chmod 600 IDENTITY_FILE (continuing; check perms manually)"
fi

# --- prepare dirs / logging -------------------------------------------------
mkdir -p "${STATE_DIR}" "${LOG_DIR}" 2>/dev/null || \
  die 2 "cannot create STATE_DIR=${STATE_DIR} or LOG_DIR=${LOG_DIR} (run as root or set paths)"

# Ensure log file is writable before we rely on log()
touch "${LOG_FILE}" 2>/dev/null || die 2 "cannot write LOG_FILE=${LOG_FILE}"

log INFO "start ${SCRIPT_NAME} v${VERSION} host=${PUBLIC_HOST} landing_port=${PUBLIC_SSH_PORT} remote_port=${REMOTE_PORT} local_port=${LOCAL_PORT} user=${SSH_USER}"

# --- write state (ONLY current registration host + port; overwrite) ---------
# State is intentionally minimal: what ops/SDK need to know about THIS binding.
# Do not store user, identity path, or any secrets.
write_state() {
  local tmp
  tmp="$(mktemp "${STATE_DIR}/.current-binding.XXXXXX")"
  # shellcheck disable=SC2094
  cat >"${tmp}" <<EOF
# ssh-portbroker dog current binding — overwritten each reverse-register run
# Only public registration host + allocated remote port for THIS session.
PUBLIC_HOST=${PUBLIC_HOST}
REMOTE_PORT=${REMOTE_PORT}
UPDATED_AT_UTC=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
EOF
  # Also drop a tiny JSON sibling for SDKs that prefer JSON (same fields only).
  local json_tmp json_file
  json_file="${STATE_FILE%.env}.json"
  if [[ "${STATE_FILE}" == *.env ]]; then
    json_tmp="$(mktemp "${STATE_DIR}/.current-binding-json.XXXXXX")"
    printf '{"public_host":"%s","remote_port":%s,"updated_at_utc":"%s"}\n' \
      "${PUBLIC_HOST}" "${REMOTE_PORT}" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
      >"${json_tmp}"
    mv -f "${json_tmp}" "${json_file}"
  fi
  mv -f "${tmp}" "${STATE_FILE}"
  log INFO "wrote state STATE_FILE=${STATE_FILE} PUBLIC_HOST=${PUBLIC_HOST} REMOTE_PORT=${REMOTE_PORT}"
}

write_state || die 2 "failed to write state file"

# --- build ssh argv ---------------------------------------------------------
SSH_ARGS=(
  -N
  -R "${REMOTE_PORT}:127.0.0.1:${LOCAL_PORT}"
  -p "${PUBLIC_SSH_PORT}"
  -o "BatchMode=${SSH_BATCH_MODE}"
  -o "StrictHostKeyChecking=${STRICT_HOST_KEY_CHECKING}"
  -o "ExitOnForwardFailure=yes"
  -o "ConnectTimeout=${CONNECT_TIMEOUT}"
  -o "ServerAliveInterval=${SERVER_ALIVE_INTERVAL}"
  -o "ServerAliveCountMax=${SERVER_ALIVE_COUNT_MAX}"
)

if [[ -n "${IDENTITY_FILE}" ]]; then
  SSH_ARGS+=(-i "${IDENTITY_FILE}" -o IdentitiesOnly=yes)
fi

TARGET="${SSH_USER}@${PUBLIC_HOST}"

log INFO "exec: ssh ${SSH_ARGS[*]} ${TARGET}"

# Run in foreground — session lives until ssh exits. No reconnect loop.
set +e
ssh "${SSH_ARGS[@]}" "${TARGET}"
rc=$?
set -e

if [[ "${rc}" -eq 0 ]]; then
  log INFO "ssh exited cleanly rc=0"
else
  log ERROR "ssh failed rc=${rc} (SDK may retry / re-invoke with fresh host+port)"
fi
exit "${rc}"
