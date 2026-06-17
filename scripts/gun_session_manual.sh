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
export GUN_FIRE_COMMAND="${GUN_FIRE_COMMAND:-cat /dev/ttyUSB0 | xxd}"
export GUN_STOP_COMMAND="${GUN_STOP_COMMAND:-printf '\\x30' > /dev/ttyUSB0}"

exec expect <<'EXPECT'
set timeout 15
log_user 0
fconfigure stdout -buffering line

proc die {message code} {
  puts "ERR $message"
  exit $code
}

proc wait_prompt {} {
  expect {
    -re {[$#] $} { return }
    timeout { die "shell prompt not seen" 124 }
    eof { die "ssh exited" 1 }
  }
}

proc handle_sudo_or_prompt {} {
  expect {
    -re "(?i)password.*:" { send -- "$env(GUN_JETSON_SUDO_PASSWORD)\r"; exp_continue }
    -re "(?i)sorry" { die "sudo password rejected" 1 }
    -re {[$#] $} { return }
    timeout { die "command did not return" 124 }
    eof { die "ssh exited during command" 1 }
  }
}

proc chmod_usb {} {
  send -- "printf '%s\\n' '$env(GUN_JETSON_SUDO_PASSWORD)' | sudo -S chmod 666 /dev/ttyUSB0\r"
  expect {
    -re "(?i)sorry" { die "sudo password rejected" 1 }
    -re {[$#] $} { return }
    timeout { die "USB chmod did not return" 124 }
    eof { die "jetson ssh exited during USB chmod" 1 }
  }
}

spawn ssh -tt -o StrictHostKeyChecking=accept-new $env(GUN_DOG_USER)@$env(GUN_DOG_HOST)
expect {
  -re "(?i)password:" { send -- "$env(GUN_DOG_PASSWORD)\r" }
  -re {[$#] $} {}
  timeout { die "timeout connecting to dog" 124 }
  eof { die "dog ssh exited" 1 }
}
wait_prompt

send -- "ssh -tt -o StrictHostKeyChecking=accept-new $env(GUN_JETSON_USER)@$env(GUN_JETSON_HOST)\r"
expect {
  -re "(?i)password:" { send -- "$env(GUN_JETSON_PASSWORD)\r" }
  -re {[$#] $} {}
  timeout { die "timeout connecting to jetson through dog" 124 }
  eof { die "jetson ssh exited" 1 }
}
wait_prompt

puts "READY"
set firing 0

while {[gets stdin cmd] >= 0} {
  set cmd [string trim [string toupper $cmd]]
  if {$cmd eq "START"} {
    if {$firing} {
      puts "OK START already-active"
      continue
    }
    chmod_usb
    send -- "$env(GUN_FIRE_COMMAND)\r"
    set timeout 1
    expect {
      -re "(?i)password.*:" { send -- "$env(GUN_JETSON_SUDO_PASSWORD)\r"; exp_continue }
      -re "(?i)sorry" { die "sudo password rejected" 1 }
      -re {[$#] $} { die "fire command returned immediately" 1 }
      timeout {}
      eof { die "fire command exited" 1 }
    }
    set timeout 15
    set firing 1
    puts "OK START"
  } elseif {$cmd eq "STOP"} {
    if {$firing} {
      send -- "\003"
      after 300
      wait_prompt
      set firing 0
    }
    chmod_usb
    send -- "$env(GUN_STOP_COMMAND)\r"
    handle_sudo_or_prompt
    puts "OK STOP"
  } elseif {$cmd eq "TEST"} {
    chmod_usb
    send -- "printf relay-ok\r"
    expect {
      -re "relay-ok" {}
      timeout { die "test command did not return relay-ok" 124 }
      eof { die "jetson ssh exited before test" 1 }
    }
    wait_prompt
    puts "OK TEST"
  } elseif {$cmd eq "EXIT"} {
    if {$firing} {
      send -- "\003"
      after 300
      wait_prompt
    }
    send -- "exit\r"
    wait_prompt
    send -- "exit\r"
    puts "OK EXIT"
    exit 0
  } else {
    puts "ERR unknown command"
  }
}
EXPECT
