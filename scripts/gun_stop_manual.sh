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
: "${GUN_JETSON_PASSWORD:?set GUN_JETSON_PASSWORD in .env}"
export GUN_JETSON_SUDO_PASSWORD="${GUN_JETSON_SUDO_PASSWORD:-$GUN_JETSON_PASSWORD}"

export GUN_DOG_HOST="${GUN_DOG_HOST:-192.168.123.121}"
export GUN_DOG_USER="${GUN_DOG_USER:-root}"
export GUN_JETSON_HOST="${GUN_JETSON_HOST:-10.42.0.2}"
export GUN_JETSON_USER="${GUN_JETSON_USER:-unitree}"
export GUN_STOP_COMMAND="${GUN_STOP_COMMAND:-sudo bash -lc 'printf \"\\x30\" > /dev/ttyUSB0'}"

exec expect <<'EXPECT'
set timeout 12
log_user 1

spawn ssh -tt -o StrictHostKeyChecking=accept-new $env(GUN_DOG_USER)@$env(GUN_DOG_HOST)
expect {
  -re "(?i)password:" { send -- "$env(GUN_DOG_PASSWORD)\r" }
  -re {[$#] $} {}
  timeout { puts stderr "timeout connecting to dog"; exit 124 }
  eof { puts stderr "dog ssh exited"; exit 1 }
}
expect {
  -re {[$#] $} {}
  timeout { puts stderr "dog shell prompt not seen"; exit 124 }
  eof { puts stderr "dog ssh exited before shell"; exit 1 }
}

send -- "ssh -tt -o StrictHostKeyChecking=accept-new $env(GUN_JETSON_USER)@$env(GUN_JETSON_HOST)\r"
expect {
  -re "(?i)password:" { send -- "$env(GUN_JETSON_PASSWORD)\r" }
  -re {[$#] $} {}
  timeout { puts stderr "timeout connecting to jetson through dog"; exit 124 }
  eof { puts stderr "jetson ssh exited"; exit 1 }
}
expect {
  -re {[$#] $} {}
  timeout { puts stderr "jetson shell prompt not seen"; exit 124 }
  eof { puts stderr "jetson ssh exited before shell"; exit 1 }
}

send -- "\003"
after 300
send -- "sudo chmod 666 /dev/ttyUSB0\r"
expect {
  -re "(?i)password.*:" { send -- "$env(GUN_JETSON_SUDO_PASSWORD)\r"; exp_continue }
  -re "(?i)sorry" { puts stderr "sudo rejected the Jetson password"; exit 1 }
  -re {[$#] $} {}
  timeout { puts stderr "USB chmod did not return"; exit 124 }
  eof { puts stderr "jetson ssh exited during USB chmod"; exit 1 }
}
send -- "$env(GUN_STOP_COMMAND)\r"
expect {
  -re "(?i)password.*:" { send -- "$env(GUN_JETSON_SUDO_PASSWORD)\r"; exp_continue }
  -re "(?i)sorry" { puts stderr "sudo rejected the Jetson password"; exit 1 }
  -re {[$#] $} {}
  timeout { puts stderr "stop command did not return"; exit 124 }
  eof {}
}
send -- "exit\r"
expect {
  -re {[$#] $} {}
  eof {}
  timeout {}
}
send -- "exit\r"
puts "USB stop byte sent"
EXPECT
