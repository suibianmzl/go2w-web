#!/usr/bin/env bash
# reverse-register.sh — dog-side reverse SSH with optional auto-reconnect
#
# Design:
# - Upstream / gateway SDK passes host + allocated port (and optional user).
# - This script establishes reverse SSH, logs, overwrites a state file with ONLY
#   the current registration public host + remote port.
# - On ssh exit: reconnect by default (interval + backoff). Port-busy
#   ("remote port forwarding failed") waits longer for server ClientAlive ~90s.
# - Ctrl+C / SIGTERM: stop cleanly (no further reconnect).
# - SSH session keepalive via ServerAlive* (not broker allocate/release TTL).
#
# Typical tunnel:
#   ssh -N -R <REMOTE_PORT>:127.0.0.1:<LOCAL_PORT> \
#       -p <PUBLIC_SSH_PORT> <SSH_USER>@<PUBLIC_HOST>
#
# No secrets are embedded. Prefer IDENTITY_FILE or ssh-agent; never put
# private keys or passwords in this repo / state file.

set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VERSION="0.2.3"

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
# SSH session keepalive (TCP/SSH layer). Not broker release TTL.
# Defaults fixed at 30s × 3 ≈ 90s dead-peer detect; override via env if needed.
SERVER_ALIVE_INTERVAL="${SERVER_ALIVE_INTERVAL:-30}"
SERVER_ALIVE_COUNT_MAX="${SERVER_ALIVE_COUNT_MAX:-3}"

# Auto-reconnect after ssh exit (default on). RECONNECT=0 → one-shot.
RECONNECT="${RECONNECT:-1}"
RECONNECT_INTERVAL="${RECONNECT_INTERVAL:-5}"
# 0 = infinite attempts
RECONNECT_MAX="${RECONNECT_MAX:-0}"
RECONNECT_BACKOFF_MAX="${RECONNECT_BACKOFF_MAX:-60}"
# Extra wait when remote listen port still held by zombie session (~ClientAlive 90s)
PORT_BUSY_WAIT="${PORT_BUSY_WAIT:-15}"
PORT_BUSY_WAIT_MAX="${PORT_BUSY_WAIT_MAX:-90}"
# After a tunnel stayed up this long (seconds), reset backoff to RECONNECT_INTERVAL
RECONNECT_RESET_AFTER="${RECONNECT_RESET_AFTER:-30}"
# Seconds ssh must stay alive after start (ExitOnForwardFailure) before logging session_up
FORWARD_OK_WAIT="${FORWARD_OK_WAIT:-2}"

# Default paths: sibling dirs under SCRIPT_DIR (relative to the script).
# Env STATE_DIR / LOG_DIR / STATE_FILE / LOG_FILE (or flags) always win when set.
STATE_DIR="${STATE_DIR:-${SCRIPT_DIR}/state}"
LOG_DIR="${LOG_DIR:-${SCRIPT_DIR}/log}"
STATE_FILE="${STATE_FILE:-${STATE_DIR}/current-binding.env}"
LOG_FILE="${LOG_FILE:-${LOG_DIR}/reverse-register.log}"
# Optional dog name for log correlation with portbroker (JSON dog_id)
DOG_ID="${DOG_ID:-}"
# Optional request correlation; generated if empty
TRACE_ID="${TRACE_ID:-}"
SERVICE_NAME="ssh-portbroker"
COMPONENT="dog-agent"

usage() {
  cat <<EOF
Usage: ${SCRIPT_NAME} [options]

Establish a reverse SSH tunnel. By default, reconnects after ssh exits
(interval + backoff). Ctrl+C / SIGTERM stops cleanly.

Required (env or flag):
  --remote-port PORT     REMOTE_PORT   allocated remote bind port on public host

Common:
  --public-host HOST     PUBLIC_HOST   default: ssh-portbroker.evoxrobot.com
  --public-ssh-port N    PUBLIC_SSH_PORT  landing SSH port, default: 22
  --local-port PORT      LOCAL_PORT    local sshd to expose, default: 22
  --user USER            SSH_USER      default: robot
  --dog-id ID            DOG_ID        optional dog name for JSON dog_id
  --trace-id ID          TRACE_ID      optional correlation id (auto if empty)
  -i, --identity FILE    IDENTITY_FILE private key path
                         default: <script-dir>/id_portbroker_rsa
                         (override via env IDENTITY_FILE or -i; no secrets in git)
  --strict MODE          STRICT_HOST_KEY_CHECKING  default: accept-new
                         (yes|no|accept-new)

Session keepalive (env only; always passed to ssh -o):
  SERVER_ALIVE_INTERVAL  default 30   (seconds between client probes)
  SERVER_ALIVE_COUNT_MAX default 3    (~90s dead-peer with interval=30)

Reconnect (env; optional flags):
  RECONNECT              default 1    (0/no = one-shot, no loop)
  RECONNECT_INTERVAL     default 5    base wait seconds after exit
  RECONNECT_MAX          default 0    max attempts (0 = infinite)
  RECONNECT_BACKOFF_MAX  default 60   cap for exponential backoff
  PORT_BUSY_WAIT         default 15   initial wait on listen-port busy
  PORT_BUSY_WAIT_MAX     default 90   cap (~server ClientAlive window)
  --no-reconnect         same as RECONNECT=0
  --reconnect-interval N set RECONNECT_INTERVAL
  --reconnect-max N      set RECONNECT_MAX (0=infinite)

Paths (default: next to this script):
  --state-dir DIR        STATE_DIR     default: <script-dir>/state
  --log-dir DIR          LOG_DIR       default: <script-dir>/log
  --state-file FILE      STATE_FILE    default: \$STATE_DIR/current-binding.env
  --log-file FILE        LOG_FILE      default: \$LOG_DIR/reverse-register.log

Other:
  -h, --help             show this help
  -V, --version          show version

Exit codes:
  0   stopped after clean ssh exit with reconnect off, or max attempts with last rc=0
  1   usage / missing required args
  2   failed to prepare state/log dirs or write state
  130 interrupted (Ctrl+C / SIGTERM)
  >2  last ssh failure rc (when reconnect exhausted or disabled)

Example (SDK / dog):
  DOG_ID=alpha PUBLIC_HOST=ssh-portbroker.evoxrobot.com REMOTE_PORT=22000 \\
    ${SCRIPT_NAME} --user robot -i /etc/ssh-portbroker-dog/id_ed25519

Packaging: ship this script + README to dog R&D; deliver the private key
separately/securely (never in the same tarball/repo).
EOF
}

_json_escape() {
  # Minimal JSON string escape (no secrets expected in msg)
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//$'\n'/\\n}"
  s="${s//$'\r'/\\r}"
  s="${s//$'\t'/\\t}"
  printf '%s' "$s"
}

_ensure_trace_id() {
  if [[ -z "${TRACE_ID}" ]]; then
    if command -v uuidgen >/dev/null 2>&1; then
      TRACE_ID="$(uuidgen | tr -d '-' | cut -c1-8)"
    else
      TRACE_ID="$(date -u +%s%N 2>/dev/null | tail -c 9 || echo "t$$")"
    fi
  fi
}

_truthy() {
  case "${1:-}" in
    1|y|Y|yes|YES|true|TRUE|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

# Structured JSON one-line log (ELK). No env field — collector labels env.
# Usage: log LEVEL EVENT "human msg" [extra_json_fragment]
log() {
  local level="$1"
  local event="$2"
  local msg="$3"
  local extra="${4:-}"
  local ts dog_json port_json line
  _ensure_trace_id
  ts="$(date -u '+%Y-%m-%dT%H:%M:%S.000Z')"
  if [[ -n "${DOG_ID}" ]]; then
    dog_json="\"$(_json_escape "${DOG_ID}")\""
  else
    dog_json="null"
  fi
  if [[ -n "${REMOTE_PORT:-}" && "${REMOTE_PORT}" =~ ^[0-9]+$ ]]; then
    port_json="${REMOTE_PORT}"
  else
    port_json="null"
  fi
  line="$(printf '{"@timestamp":"%s","service":"%s","level":"%s","event":"%s","dog_id":%s,"port":%s,"trace_id":"%s","component":"%s","msg":"%s"%s}' \
    "${ts}" "${SERVICE_NAME}" "${level}" "${event}" "${dog_json}" "${port_json}" \
    "$(_json_escape "${TRACE_ID}")" "${COMPONENT}" "$(_json_escape "${msg}")" \
    "$( [[ -n "${extra}" ]] && printf ',%s' "${extra}" || true )")"
  if [[ -n "${LOG_FILE:-}" ]] && [[ -w "$(dirname "${LOG_FILE}")" || -w "${LOG_FILE}" ]] 2>/dev/null; then
    printf '%s\n' "${line}" >>"${LOG_FILE}" 2>/dev/null || true
  fi
  printf '%s\n' "${line}" >&2
}

die() {
  local code="$1"; shift
  log ERROR "dog_agent.reverse_fail" "$*" '"outcome":"error"'
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
    --dog-id)            DOG_ID="$2"; shift 2 ;;
    --trace-id)          TRACE_ID="$2"; shift 2 ;;
    -i|--identity)       IDENTITY_FILE="$2"; shift 2 ;;
    --strict)            STRICT_HOST_KEY_CHECKING="$2"; shift 2 ;;
    --state-dir)         STATE_DIR="$2"; STATE_FILE="${STATE_DIR}/current-binding.env"; shift 2 ;;
    --log-dir)           LOG_DIR="$2"; LOG_FILE="${LOG_DIR}/reverse-register.log"; shift 2 ;;
    --state-file)        STATE_FILE="$2"; shift 2 ;;
    --log-file)          LOG_FILE="$2"; shift 2 ;;
    --no-reconnect)      RECONNECT=0; shift ;;
    --reconnect-interval) RECONNECT_INTERVAL="$2"; shift 2 ;;
    --reconnect-max)     RECONNECT_MAX="$2"; shift 2 ;;
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
[[ "${RECONNECT_INTERVAL}" =~ ^[0-9]+$ ]] || die 1 "RECONNECT_INTERVAL must be an integer"
[[ "${RECONNECT_MAX}" =~ ^[0-9]+$ ]] || die 1 "RECONNECT_MAX must be an integer"
[[ "${RECONNECT_BACKOFF_MAX}" =~ ^[0-9]+$ ]] || die 1 "RECONNECT_BACKOFF_MAX must be an integer"
[[ "${PORT_BUSY_WAIT}" =~ ^[0-9]+$ ]] || die 1 "PORT_BUSY_WAIT must be an integer"
[[ "${PORT_BUSY_WAIT_MAX}" =~ ^[0-9]+$ ]] || die 1 "PORT_BUSY_WAIT_MAX must be an integer"

case "${STRICT_HOST_KEY_CHECKING}" in
  yes|no|accept-new) ;;
  *) die 1 "STRICT_HOST_KEY_CHECKING must be yes|no|accept-new" ;;
esac

if [[ -n "${IDENTITY_FILE}" ]]; then
  [[ -f "${IDENTITY_FILE}" ]] || die 1 "IDENTITY_FILE not found: ${IDENTITY_FILE}"
  # Tighten perms if needed; never print / dump key material
  chmod 600 "${IDENTITY_FILE}" 2>/dev/null || \
    log WARN "dog_agent.reverse_start" "could not chmod 600 IDENTITY_FILE (continuing)" '"outcome":"warn"'
fi

# --- prepare dirs / logging -------------------------------------------------
mkdir -p "${STATE_DIR}" "${LOG_DIR}" 2>/dev/null || \
  die 2 "cannot create STATE_DIR=${STATE_DIR} or LOG_DIR=${LOG_DIR} (check perms or set STATE_DIR/LOG_DIR)"

# Ensure log file is writable before we rely on log()
touch "${LOG_FILE}" 2>/dev/null || die 2 "cannot write LOG_FILE=${LOG_FILE}"

_ensure_trace_id
log INFO "dog_agent.reverse_start" \
  "start ${SCRIPT_NAME} v${VERSION} host=${PUBLIC_HOST} landing_port=${PUBLIC_SSH_PORT} remote_port=${REMOTE_PORT} local_port=${LOCAL_PORT} user=${SSH_USER} reconnect=${RECONNECT}" \
  "\"public_host\":\"$(_json_escape "${PUBLIC_HOST}")\",\"remote_port\":${REMOTE_PORT},\"local_port\":${LOCAL_PORT},\"reconnect\":$(_truthy "${RECONNECT}" && echo true || echo false),\"outcome\":\"start\""

# --- write state (ONLY current registration host + port; overwrite) ---------
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
  log INFO "dog_agent.reverse_start" \
    "wrote state STATE_FILE=${STATE_FILE} PUBLIC_HOST=${PUBLIC_HOST} REMOTE_PORT=${REMOTE_PORT}" \
    "\"public_host\":\"$(_json_escape "${PUBLIC_HOST}")\",\"remote_port\":${REMOTE_PORT},\"outcome\":\"state_written\""
}

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

identity_set="no"
[[ -n "${IDENTITY_FILE}" ]] && identity_set="yes"

# --- signal / reconnect loop ------------------------------------------------
STOPPED=0
SSH_PID=""
LAST_RC=0
ATTEMPT=0
DELAY="${RECONNECT_INTERVAL}"
BUSY_DELAY="${PORT_BUSY_WAIT}"
SESSION_WAS_UP=0

_on_signal() {
  STOPPED=1
  local ts_now
  ts_now="$(date -u '+%Y-%m-%dT%H:%M:%S.000Z')"
  if [[ "${SESSION_WAS_UP}" -eq 1 ]]; then
    log WARN "dog_agent.session_down" \
      "session down reason=signal attempt=${ATTEMPT} port=${REMOTE_PORT}" \
      "\"remote_port\":${REMOTE_PORT},\"rc\":null,\"uptime_s\":null,\"attempt\":${ATTEMPT},\"reason\":\"signal\",\"outcome\":\"session_down\",\"at\":\"${ts_now}\""
    SESSION_WAS_UP=0
  fi
  log WARN "dog_agent.reverse_stop" \
    "signal received; stopping reconnect loop attempt=${ATTEMPT}" \
    "\"outcome\":\"stopped\",\"attempt\":${ATTEMPT},\"at\":\"${ts_now}\""
  if [[ -n "${SSH_PID}" ]] && kill -0 "${SSH_PID}" 2>/dev/null; then
    kill "${SSH_PID}" 2>/dev/null || true
  fi
}

trap '_on_signal; exit 130' INT TERM

_is_port_busy() {
  local f="$1"
  [[ -s "${f}" ]] || return 1
  grep -qiE \
    'remote port forwarding failed|address already in use|bind:.*already|cannot listen to port' \
    "${f}" 2>/dev/null
}

_is_keepalive_timeout() {
  local f="$1"
  [[ -s "${f}" ]] || return 1
  grep -qiE \
    'Timeout, server not responding|Connection timed out during banner|Broken pipe' \
    "${f}" 2>/dev/null
}

# Classify session_down reason: keepalive | port_busy | clean | signal | auth | other
_down_reason() {
  local rc="$1" uptime_s="$2" errf="$3" was_up="$4"
  if [[ "${STOPPED}" -ne 0 ]]; then
    printf '%s' "signal"
    return
  fi
  if _is_port_busy "${errf}"; then
    printf '%s' "port_busy"
    return
  fi
  if _is_keepalive_timeout "${errf}"; then
    printf '%s' "keepalive"
    return
  fi
  if [[ "${rc}" -eq 0 ]]; then
    printf '%s' "clean"
    return
  fi
  # Sustained session then non-zero exit ≈ dead peer / ServerAlive path
  local ka_min=$((SERVER_ALIVE_INTERVAL * SERVER_ALIVE_COUNT_MAX))
  if [[ "${was_up}" -eq 1 && "${uptime_s}" -ge "${ka_min}" ]]; then
    printf '%s' "keepalive"
    return
  fi
  if grep -qiE 'Permission denied|No more authentication methods' "${errf}" 2>/dev/null; then
    printf '%s' "auth"
    return
  fi
  if [[ "${was_up}" -eq 0 ]]; then
    printf '%s' "connect_fail"
    return
  fi
  printf '%s' "other"
}

_sleep_interruptible() {
  local secs="$1"
  [[ "${secs}" -gt 0 ]] || return 0
  sleep "${secs}" &
  local spid=$!
  wait "${spid}" 2>/dev/null || true
  [[ "${STOPPED}" -eq 0 ]]
}

# Wait until ssh dies or FORWARD_OK_WAIT elapses while still alive → session_up.
# Relies on ExitOnForwardFailure=yes: if -R fails, ssh exits before settling.
_wait_forward_ok_or_exit() {
  local pid="$1"
  local wait_s="${FORWARD_OK_WAIT}"
  local deadline end
  end="$(date +%s)"
  deadline=$((end + wait_s))
  while kill -0 "${pid}" 2>/dev/null; do
    end="$(date +%s)"
    if [[ "${end}" -ge "${deadline}" ]]; then
      return 0
    fi
    sleep 0.2 2>/dev/null || sleep 1
  done
  return 1
}

while true; do
  ATTEMPT=$((ATTEMPT + 1))
  attempt_at="$(date -u '+%Y-%m-%dT%H:%M:%S.000Z')"
  SESSION_WAS_UP=0

  if [[ "${ATTEMPT}" -eq 1 ]]; then
    write_state || die 2 "failed to write state file"
  else
    write_state || log WARN "dog_agent.reverse_reconnect" \
      "failed to refresh state file (continuing)" \
      "\"outcome\":\"warn\",\"reason\":\"state_write\",\"attempt\":${ATTEMPT},\"at\":\"${attempt_at}\""
  fi

  log INFO "dog_agent.reverse_start" \
    "attempt=${ATTEMPT} at=${attempt_at} exec reverse ssh target=${TARGET} remote_port=${REMOTE_PORT} local_port=${LOCAL_PORT} identity_set=${identity_set}" \
    "\"target\":\"$(_json_escape "${TARGET}")\",\"remote_port\":${REMOTE_PORT},\"local_port\":${LOCAL_PORT},\"identity_set\":\"${identity_set}\",\"attempt\":${ATTEMPT},\"at\":\"${attempt_at}\",\"outcome\":\"exec\""

  errfile="$(mktemp "${STATE_DIR}/.ssh-stderr.XXXXXX")"
  t0="$(date +%s)"
  set +e
  # Capture stderr for reason detection; replay after exit (ssh -N quiet while up)
  ssh "${SSH_ARGS[@]}" "${TARGET}" 2>"${errfile}" &
  SSH_PID=$!

  if _wait_forward_ok_or_exit "${SSH_PID}"; then
    # Guard: if stderr already shows port-busy / forward fail, do not claim session_up
    if _is_port_busy "${errfile}"; then
      SESSION_WAS_UP=0
    else
      SESSION_WAS_UP=1
      up_at="$(date -u '+%Y-%m-%dT%H:%M:%S.000Z')"
      log INFO "dog_agent.session_up" \
        ">>> SESSION UP attempt=${ATTEMPT} port=${REMOTE_PORT} host=${PUBLIC_HOST} forward_ok at=${up_at}" \
        "\"remote_port\":${REMOTE_PORT},\"public_host\":\"$(_json_escape "${PUBLIC_HOST}")\",\"attempt\":${ATTEMPT},\"at\":\"${up_at}\",\"outcome\":\"session_up\",\"forward_ok\":true"
    fi
  fi

  wait "${SSH_PID}"
  LAST_RC=$?
  set -e
  SSH_PID=""
  t1="$(date +%s)"
  uptime=$((t1 - t0))
  down_at="$(date -u '+%Y-%m-%dT%H:%M:%S.000Z')"

  if [[ -s "${errfile}" ]]; then
    cat "${errfile}" >&2 || true
  fi

  reason="$(_down_reason "${LAST_RC}" "${uptime}" "${errfile}" "${SESSION_WAS_UP}")"
  port_busy=0
  [[ "${reason}" == "port_busy" ]] && port_busy=1

  log WARN "dog_agent.session_down" \
    ">>> SESSION DOWN attempt=${ATTEMPT} reason=${reason} rc=${LAST_RC} uptime_s=${uptime} port=${REMOTE_PORT} at=${down_at}" \
    "\"remote_port\":${REMOTE_PORT},\"rc\":${LAST_RC},\"uptime_s\":${uptime},\"attempt\":${ATTEMPT},\"reason\":\"${reason}\",\"was_up\":$( [[ "${SESSION_WAS_UP}" -eq 1 ]] && echo true || echo false ),\"at\":\"${down_at}\",\"outcome\":\"session_down\""

  # Extra human hint for connect_fail vs port_busy
  if [[ "${reason}" == "connect_fail" ]]; then
    log WARN "dog_agent.session_down" \
      ">>> HINT connect_fail=cannot reach ${PUBLIC_HOST}:${PUBLIC_SSH_PORT} (DNS/route/firewall/sshd). NOT remote -R listen busy." \
      "\"remote_port\":${REMOTE_PORT},\"attempt\":${ATTEMPT},\"reason\":\"connect_fail\",\"public_host\":\"$(_json_escape "${PUBLIC_HOST}")\",\"public_ssh_port\":${PUBLIC_SSH_PORT},\"outcome\":\"hint\""
  elif [[ "${reason}" == "port_busy" ]]; then
    log WARN "dog_agent.session_down" \
      ">>> HINT port_busy=SSH ok but remote listen ${REMOTE_PORT} still held (zombie). Wait ClientAlive ~90s or kill old sshd." \
      "\"remote_port\":${REMOTE_PORT},\"attempt\":${ATTEMPT},\"reason\":\"port_busy\",\"outcome\":\"hint\""
  fi

  SESSION_WAS_UP=0

  if [[ "${STOPPED}" -ne 0 ]]; then
    rm -f "${errfile}"
    exit 130
  fi

  # Keep legacy events for greppability
  if [[ "${LAST_RC}" -eq 0 ]]; then
    log INFO "dog_agent.reverse_ok" \
      "ssh exited cleanly rc=0 remote_port=${REMOTE_PORT} uptime_s=${uptime} attempt=${ATTEMPT}" \
      "\"remote_port\":${REMOTE_PORT},\"rc\":0,\"uptime_s\":${uptime},\"attempt\":${ATTEMPT},\"outcome\":\"ok\""
  elif [[ "${port_busy}" -eq 1 ]]; then
    log WARN "dog_agent.reverse_port_busy" \
      "remote listen port busy remote_port=${REMOTE_PORT} rc=${LAST_RC} attempt=${ATTEMPT}" \
      "\"remote_port\":${REMOTE_PORT},\"rc\":${LAST_RC},\"uptime_s\":${uptime},\"attempt\":${ATTEMPT},\"outcome\":\"port_busy\""
  else
    log ERROR "dog_agent.reverse_fail" \
      "ssh failed rc=${LAST_RC} reason=${reason} remote_port=${REMOTE_PORT} uptime_s=${uptime} attempt=${ATTEMPT}" \
      "\"remote_port\":${REMOTE_PORT},\"rc\":${LAST_RC},\"uptime_s\":${uptime},\"attempt\":${ATTEMPT},\"reason\":\"${reason}\",\"outcome\":\"error\""
  fi

  rm -f "${errfile}"

  if ! _truthy "${RECONNECT}"; then
    log WARN "dog_agent.reconnect" \
      ">>> RECONNECT disabled (RECONNECT=0); not retrying after reason=${reason}" \
      "\"attempt\":${ATTEMPT},\"last_reason\":\"$(_json_escape "${reason}")\",\"outcome\":\"reconnect_disabled\""
    exit "${LAST_RC}"
  fi

  if [[ "${RECONNECT_MAX}" -gt 0 && "${ATTEMPT}" -ge "${RECONNECT_MAX}" ]]; then
    log ERROR "dog_agent.reconnect" \
      ">>> RECONNECT give up max=${RECONNECT_MAX} last_reason=${reason} last_rc=${LAST_RC}" \
      "\"remote_port\":${REMOTE_PORT},\"rc\":${LAST_RC},\"attempt\":${ATTEMPT},\"last_reason\":\"$(_json_escape "${reason}")\",\"at\":\"${down_at}\",\"outcome\":\"max_attempts\""
    exit "${LAST_RC}"
  fi

  if [[ "${uptime}" -ge "${RECONNECT_RESET_AFTER}" ]]; then
    DELAY="${RECONNECT_INTERVAL}"
    BUSY_DELAY="${PORT_BUSY_WAIT}"
  fi

  if [[ "${port_busy}" -eq 1 ]]; then
    wait_s="${BUSY_DELAY}"
    next_busy=$((BUSY_DELAY * 2))
    if [[ "${next_busy}" -gt "${PORT_BUSY_WAIT_MAX}" ]]; then
      next_busy="${PORT_BUSY_WAIT_MAX}"
    fi
    BUSY_DELAY="${next_busy}"
  else
    wait_s="${DELAY}"
    next_delay=$((DELAY * 2))
    if [[ "${next_delay}" -gt "${RECONNECT_BACKOFF_MAX}" ]]; then
      next_delay="${RECONNECT_BACKOFF_MAX}"
    fi
    DELAY="${next_delay}"
  fi

  next_attempt=$((ATTEMPT + 1))
  log INFO "dog_agent.reconnect" \
    ">>> RECONNECT in ${wait_s}s attempt=${next_attempt} after_s=${wait_s} last_reason=${reason} last_rc=${LAST_RC} last_attempt=${ATTEMPT}" \
    "\"remote_port\":${REMOTE_PORT},\"attempt\":${next_attempt},\"last_attempt\":${ATTEMPT},\"after_s\":${wait_s},\"last_reason\":\"$(_json_escape "${reason}")\",\"rc\":${LAST_RC},\"at\":\"${down_at}\",\"outcome\":\"reconnect\""
  # legacy alias event
  log INFO "dog_agent.reverse_reconnect" \
    "reconnect wait_s=${wait_s} next_attempt=${next_attempt} reason=${reason}" \
    "\"remote_port\":${REMOTE_PORT},\"wait_s\":${wait_s},\"attempt\":${ATTEMPT},\"next_attempt\":${next_attempt},\"reason\":\"$(_json_escape "${reason}")\",\"outcome\":\"reconnect\""

  _sleep_interruptible "${wait_s}" || exit 130
done
