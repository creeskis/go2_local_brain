#!/usr/bin/env bash
set -euo pipefail

need() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "missing dependency: $1" >&2
    exit 127
  fi
}

need expect

: "${GUN_ACTION:?set GUN_ACTION to START, STOP, TEST, or STATUS}"
: "${GUN_JETSON_PASSWORD:?set GUN_JETSON_PASSWORD in .env}"
export GUN_JETSON_SUDO_PASSWORD="${GUN_JETSON_SUDO_PASSWORD:-$GUN_JETSON_PASSWORD}"

export GUN_JETSON_USER="${GUN_JETSON_USER:-unitree}"
export GUN_LOCAL_SSH_PORT="${GUN_LOCAL_SSH_PORT:-10022}"
export GUN_FIRE_COMMAND="${GUN_FIRE_COMMAND:-cat /dev/ttyUSB0 | xxd}"
export GUN_STOP_COMMAND="${GUN_STOP_COMMAND:-printf '\\x30' > /dev/ttyUSB0}"
export GUN_LOG_FILE="${GUN_LOG_FILE:-/tmp/go2_gun_relay.log}"
export GUN_REMOTE_LOG_FILE="${GUN_REMOTE_LOG_FILE:-/tmp/go2_gun_remote.log}"
export GUN_STOP_TIMEOUT_S="${GUN_STOP_TIMEOUT_S:-3}"

log() {
  printf '%s [%s] %s\n' "$(date -Is)" "$GUN_ACTION" "$*" >> "$GUN_LOG_FILE"
}

case "$GUN_ACTION" in
  START | STOP | TEST | STATUS) ;;
  *)
    echo "unknown GUN_ACTION: $GUN_ACTION" >&2
    exit 2
    ;;
esac

printf -v ACTION_Q "%q" "$GUN_ACTION"
printf -v SUDO_PASS_Q "%q" "$GUN_JETSON_SUDO_PASSWORD"
printf -v FIRE_COMMAND_Q "%q" "$GUN_FIRE_COMMAND"
printf -v STOP_COMMAND_Q "%q" "$GUN_STOP_COMMAND"
printf -v REMOTE_LOG_Q "%q" "$GUN_REMOTE_LOG_FILE"
printf -v STOP_TIMEOUT_Q "%q" "$GUN_STOP_TIMEOUT_S"

export REMOTE_BOOTSTRAP="printf '__GO2_REMOTE_READY__\\n'; bash -s"
export REMOTE_SCRIPT
REMOTE_SCRIPT="$(cat <<REMOTE
GUN_ACTION=$ACTION_Q
GUN_JETSON_SUDO_PASSWORD=$SUDO_PASS_Q
GUN_FIRE_COMMAND=$FIRE_COMMAND_Q
GUN_STOP_COMMAND=$STOP_COMMAND_Q
GUN_REMOTE_LOG_FILE=$REMOTE_LOG_Q
GUN_STOP_TIMEOUT_S=$STOP_TIMEOUT_Q
REMOTE
cat <<'REMOTE'
set -u

log_remote() {
  printf '%s %s\n' "$(date -Is)" "$*" >> "$GUN_REMOTE_LOG_FILE"
}

chmod_usb() {
  printf '%s\n' "$GUN_JETSON_SUDO_PASSWORD" | sudo -S chmod 666 /dev/ttyUSB0
}

require_usb() {
  if [ ! -e /dev/ttyUSB0 ]; then
    log_remote "$GUN_ACTION missing /dev/ttyUSB0"
    echo "ERR $GUN_ACTION missing-ttyUSB0"
    exit 10
  fi
}

kill_fire_pid() {
  if [ ! -f /tmp/go2_gun_fire.pid ]; then
    log_remote "STOP no pid file"
    return
  fi
  pid="$(cat /tmp/go2_gun_fire.pid 2>/dev/null || true)"
  log_remote "STOP pid=$pid"
  if [ -n "$pid" ]; then
    kill -INT "-$pid" 2>/dev/null || kill -INT "$pid" 2>/dev/null || true
    sleep 0.2
    kill -TERM "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    sleep 0.1
    kill -KILL "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
  fi
  rm -f /tmp/go2_gun_fire.pid
}

log_remote "$GUN_ACTION requested user=$(whoami)"

case "$GUN_ACTION" in
  START)
    require_usb
    if [ -f /tmp/go2_gun_fire.pid ]; then
      oldpid="$(cat /tmp/go2_gun_fire.pid 2>/dev/null || true)"
      if [ -n "$oldpid" ] && kill -0 "$oldpid" 2>/dev/null; then
        log_remote "START already active pid=$oldpid"
        echo "OK START already-active pid=$oldpid"
        exit 0
      fi
      log_remote "START removing stale pid=$oldpid"
      rm -f /tmp/go2_gun_fire.pid
    fi
    chmod_usb
    status=$?
    if [ "$status" -ne 0 ]; then
      log_remote "START chmod failed status=$status dev=$(ls -l /dev/ttyUSB0 2>&1)"
      echo "ERR START chmod-failed status=$status"
      exit "$status"
    fi
    nohup setsid bash -lc "$GUN_FIRE_COMMAND" </dev/null >/tmp/go2_gun_fire.log 2>&1 &
    pid=$!
    sleep 0.2
    if ! kill -0 "$pid" 2>/dev/null; then
      log_remote "START command exited pid=$pid log=$(tail -n 20 /tmp/go2_gun_fire.log 2>&1 | tr '\n' '|')"
      echo "ERR START command-exited pid=$pid"
      exit 11
    fi
    echo "$pid" > /tmp/go2_gun_fire.pid
    log_remote "START pid=$pid dev=$(ls -l /dev/ttyUSB0 2>&1)"
    echo "OK START pid=$pid"
    ;;
  STOP)
    kill_fire_pid
    require_usb
    chmod_usb
    status=$?
    if [ "$status" -ne 0 ]; then
      log_remote "STOP chmod failed status=$status dev=$(ls -l /dev/ttyUSB0 2>&1)"
      echo "ERR STOP chmod-failed status=$status"
      exit "$status"
    fi
    if command -v timeout >/dev/null 2>&1; then
      timeout "${GUN_STOP_TIMEOUT_S}s" bash -lc "$GUN_STOP_COMMAND"
      status=$?
    else
      bash -lc "$GUN_STOP_COMMAND"
      status=$?
    fi
    log_remote "STOP stop_command_status=$status"
    if [ "$status" = 0 ]; then
      echo "OK STOP status=$status"
    elif [ "$status" = 124 ]; then
      echo "ERR STOP stop-command-timeout"
    else
      echo "ERR STOP status=$status"
    fi
    exit "$status"
    ;;
  TEST)
    require_usb
    chmod_usb
    status=$?
    if [ "$status" -eq 0 ]; then
      echo "OK TEST"
    else
      echo "ERR TEST chmod-failed status=$status"
      exit "$status"
    fi
    ;;
  STATUS)
    tty=0
    [ -e /dev/ttyUSB0 ] && tty=1
    active=0
    pid=
    if [ -f /tmp/go2_gun_fire.pid ]; then
      pid="$(cat /tmp/go2_gun_fire.pid 2>/dev/null || true)"
      if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        active=1
      fi
    fi
    chmod_status=1
    chmod_usb >/dev/null 2>&1 && chmod_status=0
    log_remote "STATUS tty=$tty active=$active pid=$pid chmod_status=$chmod_status"
    echo "OK STATUS tty=$tty active=$active pid=$pid chmod_status=$chmod_status"
    ;;
esac
REMOTE
)"

log "local command begin port=$GUN_LOCAL_SSH_PORT user=$GUN_JETSON_USER"
out_file="$(mktemp)"
err_file="$(mktemp)"
trap 'rm -f "$out_file" "$err_file"' EXIT

set +e
expect >"$out_file" 2>"$err_file" <<'EXPECT'
set timeout 15
log_user 0

spawn ssh -p $env(GUN_LOCAL_SSH_PORT) -o StrictHostKeyChecking=accept-new $env(GUN_JETSON_USER)@127.0.0.1 $env(REMOTE_BOOTSTRAP)
expect {
  -re "(?i)password:" { send -- "$env(GUN_JETSON_PASSWORD)\r"; exp_continue }
  -re "(?i)sorry" { puts stderr "sudo password rejected"; exit 1 }
  -re "__GO2_REMOTE_READY__" {
    send -- "$env(REMOTE_SCRIPT)\n"
    send -- "exit\n"
    exp_continue
  }
  -re "OK (START|STOP|TEST|STATUS)(\[^\r\n\]*)?" {
    puts $expect_out(0,string)
    exit 0
  }
  -re "ERR (START|STOP|TEST|STATUS)(\[^\r\n\]*)?" {
    puts stderr $expect_out(0,string)
    exit 1
  }
  timeout { puts stderr "timed out running $env(GUN_ACTION)"; exit 124 }
  eof { puts stderr "ssh command exited before OK $env(GUN_ACTION)"; exit 1 }
}
EXPECT
rc=$?
set -e
out="$(cat "$out_file")"
err="$(cat "$err_file")"
log "local command exit rc=$rc stdout=$(printf %q "$out") stderr=$(printf %q "$err")"
printf '%s\n' "$out"
if [[ -n "$err" ]]; then
  printf '%s\n' "$err" >&2
fi
exit "$rc"
