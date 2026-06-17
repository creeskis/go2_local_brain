#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_SCRIPT="${ROOT_DIR}/scripts/setup_dog_jetson_1042_subnet.sh"

DOG_HOST="${DOG_HOST:-192.168.123.121}"
DOG_USER="${DOG_USER:-root}"
DOG_PASSWORD="${DOG_PASSWORD:-${GUN_DOG_PASSWORD:-}}"
DOG_REMOTE_SCRIPT="${DOG_REMOTE_SCRIPT:-/tmp/go2_setup_jetson_1042_subnet.sh}"

need() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "missing dependency: $1" >&2
    exit 127
  fi
}

need expect

if [[ ! -f "$LOCAL_SCRIPT" ]]; then
  echo "missing local script: $LOCAL_SCRIPT" >&2
  exit 1
fi

if [[ -z "$DOG_PASSWORD" ]]; then
  echo "Set DOG_PASSWORD or GUN_DOG_PASSWORD before running this wrapper." >&2
  exit 1
fi

export DOG_HOST DOG_USER DOG_PASSWORD DOG_REMOTE_SCRIPT LOCAL_SCRIPT
export DOG_ETH_IF="${DOG_ETH_IF:-eth0}"
export DOG_WIFI_IF="${DOG_WIFI_IF:-wlan0}"
export DOG_ETH_IP="${DOG_ETH_IP:-10.42.0.1/24}"
export JETSON_CIDR="${JETSON_CIDR:-10.42.0.0/24}"

echo "Copying subnet setup script to ${DOG_USER}@${DOG_HOST}:${DOG_REMOTE_SCRIPT}"
expect <<'EXPECT'
set timeout 20
log_user 1
spawn scp -O -o StrictHostKeyChecking=accept-new $env(LOCAL_SCRIPT) $env(DOG_USER)@$env(DOG_HOST):$env(DOG_REMOTE_SCRIPT)
expect {
  -re "(?i)password:" { send -- "$env(DOG_PASSWORD)\r"; exp_continue }
  eof {}
  timeout { puts stderr "timeout copying subnet script to dog"; exit 124 }
}
catch wait result
set code [lindex $result 3]
if {$code != 0} { exit $code }
EXPECT

echo "Running subnet setup on dog"
expect <<'EXPECT'
set timeout 30
log_user 1
set command "DOG_ETH_IF=$env(DOG_ETH_IF) DOG_WIFI_IF=$env(DOG_WIFI_IF) DOG_ETH_IP=$env(DOG_ETH_IP) JETSON_CIDR=$env(JETSON_CIDR) sh $env(DOG_REMOTE_SCRIPT)"
spawn ssh -o StrictHostKeyChecking=accept-new $env(DOG_USER)@$env(DOG_HOST) $command
expect {
  -re "(?i)password:" { send -- "$env(DOG_PASSWORD)\r"; exp_continue }
  eof {}
  timeout { puts stderr "timeout running subnet setup on dog"; exit 124 }
}
catch wait result
set code [lindex $result 3]
if {$code != 0} { exit $code }
EXPECT
