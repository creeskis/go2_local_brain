#!/usr/bin/env bash
set -euo pipefail

need() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "missing dependency: $1" >&2
    exit 127
  fi
}

need expect

: "${GUN_DOG_PASSWORD:?set GUN_DOG_PASSWORD in .env}"

export GUN_DOG_HOST="${GUN_DOG_HOST:-192.168.123.121}"
export GUN_DOG_USER="${GUN_DOG_USER:-root}"
export GUN_JETSON_HOST="${GUN_JETSON_HOST:-10.42.0.2}"
export GUN_LOCAL_SSH_PORT="${GUN_LOCAL_SSH_PORT:-10022}"

exec expect <<'EXPECT'
set timeout 15
log_user 0
fconfigure stdout -buffering line

spawn ssh -N -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=accept-new -L 127.0.0.1:$env(GUN_LOCAL_SSH_PORT):$env(GUN_JETSON_HOST):22 $env(GUN_DOG_USER)@$env(GUN_DOG_HOST)
expect {
  -re "(?i)password:" { send -- "$env(GUN_DOG_PASSWORD)\r" }
  timeout { puts "ERR timeout connecting to dog tunnel"; exit 124 }
  eof { puts "ERR dog tunnel exited"; exit 1 }
}
puts "READY tunnel"
set timeout -1
expect {
  eof { exit 0 }
}
EXPECT
