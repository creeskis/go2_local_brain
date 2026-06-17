#!/usr/bin/env bash
set -euo pipefail

need() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "missing dependency: $1" >&2
    exit 127
  fi
}

need expect

: "${GUN_ACTION:?set GUN_ACTION to START, STOP, or TEST}"
: "${GUN_JETSON_PASSWORD:?set GUN_JETSON_PASSWORD in .env}"
export GUN_JETSON_SUDO_PASSWORD="${GUN_JETSON_SUDO_PASSWORD:-$GUN_JETSON_PASSWORD}"

export GUN_JETSON_USER="${GUN_JETSON_USER:-unitree}"
export GUN_LOCAL_SSH_PORT="${GUN_LOCAL_SSH_PORT:-10022}"
export GUN_FIRE_COMMAND="${GUN_FIRE_COMMAND:-cat /dev/ttyUSB0 | xxd}"
export GUN_STOP_COMMAND="${GUN_STOP_COMMAND:-printf '\\x30' > /dev/ttyUSB0}"

printf -v SUDO_PASS_Q "%q" "$GUN_JETSON_SUDO_PASSWORD"
printf -v FIRE_COMMAND_Q "%q" "$GUN_FIRE_COMMAND"
printf -v STOP_COMMAND_Q "%q" "$GUN_STOP_COMMAND"
CHMOD_USB="printf '%s\n' $SUDO_PASS_Q | sudo -S chmod 666 /dev/ttyUSB0"

case "$GUN_ACTION" in
  START)
    REMOTE_COMMAND="if [ -f /tmp/go2_gun_fire.pid ] && kill -0 \$(cat /tmp/go2_gun_fire.pid) 2>/dev/null; then echo OK START already-active; else rm -f /tmp/go2_gun_fire.pid; $CHMOD_USB && setsid bash -lc $FIRE_COMMAND_Q >/tmp/go2_gun_fire.log 2>&1 & pid=\$!; echo \$pid > /tmp/go2_gun_fire.pid; echo OK START pid=\$pid; fi"
    ;;
  STOP)
    REMOTE_COMMAND="if [ -f /tmp/go2_gun_fire.pid ]; then pid=\$(cat /tmp/go2_gun_fire.pid); kill -INT -\$pid 2>/dev/null || kill -INT \$pid 2>/dev/null || true; sleep 0.2; kill -TERM -\$pid 2>/dev/null || kill -TERM \$pid 2>/dev/null || true; rm -f /tmp/go2_gun_fire.pid; fi; $CHMOD_USB && bash -lc $STOP_COMMAND_Q && echo OK STOP"
    ;;
  TEST)
    REMOTE_COMMAND="$CHMOD_USB && echo OK TEST"
    ;;
  *)
    echo "unknown GUN_ACTION: $GUN_ACTION" >&2
    exit 2
    ;;
esac

export REMOTE_COMMAND

exec expect <<'EXPECT'
set timeout 15
log_user 0

spawn ssh -p $env(GUN_LOCAL_SSH_PORT) -o StrictHostKeyChecking=accept-new $env(GUN_JETSON_USER)@127.0.0.1 $env(REMOTE_COMMAND)
expect {
  -re "(?i)password:" { send -- "$env(GUN_JETSON_PASSWORD)\r"; exp_continue }
  -re "(?i)sorry" { puts stderr "sudo password rejected"; exit 1 }
  -re "OK (START|STOP|TEST)" {
    puts $expect_out(0,string)
    expect eof
    exit 0
  }
  timeout { puts stderr "timed out running $env(GUN_ACTION)"; exit 124 }
  eof { puts stderr "ssh command exited before OK $env(GUN_ACTION)"; exit 1 }
}
EXPECT
