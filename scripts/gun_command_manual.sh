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

log() {
  printf '%s [%s] %s\n' "$(date -Is)" "$GUN_ACTION" "$*" >> "$GUN_LOG_FILE"
}

printf -v SUDO_PASS_Q "%q" "$GUN_JETSON_SUDO_PASSWORD"
printf -v FIRE_COMMAND_Q "%q" "$GUN_FIRE_COMMAND"
printf -v STOP_COMMAND_Q "%q" "$GUN_STOP_COMMAND"
printf -v REMOTE_LOG_Q "%q" "$GUN_REMOTE_LOG_FILE"
CHMOD_USB="printf '%s\n' $SUDO_PASS_Q | sudo -S chmod 666 /dev/ttyUSB0"

case "$GUN_ACTION" in
  START)
    REMOTE_COMMAND="echo \"\$(date -Is) START requested\" >> $REMOTE_LOG_Q; if [ -f /tmp/go2_gun_fire.pid ] && kill -0 \$(cat /tmp/go2_gun_fire.pid) 2>/dev/null; then echo \"\$(date -Is) START already active pid=\$(cat /tmp/go2_gun_fire.pid)\" >> $REMOTE_LOG_Q; echo OK START already-active pid=\$(cat /tmp/go2_gun_fire.pid); else rm -f /tmp/go2_gun_fire.pid; $CHMOD_USB && { nohup setsid bash -lc $FIRE_COMMAND_Q </dev/null >/tmp/go2_gun_fire.log 2>&1 & pid=\$!; echo \$pid > /tmp/go2_gun_fire.pid; echo \"\$(date -Is) START pid=\$pid\" >> $REMOTE_LOG_Q; echo OK START pid=\$pid; }; fi"
    ;;
  STOP)
    REMOTE_COMMAND="echo \"\$(date -Is) STOP requested\" >> $REMOTE_LOG_Q; if [ -f /tmp/go2_gun_fire.pid ]; then pid=\$(cat /tmp/go2_gun_fire.pid); echo \"\$(date -Is) STOP pid=\$pid\" >> $REMOTE_LOG_Q; kill -INT -\$pid 2>/dev/null || kill -INT \$pid 2>/dev/null || true; sleep 0.3; kill -TERM -\$pid 2>/dev/null || kill -TERM \$pid 2>/dev/null || true; sleep 0.2; kill -KILL -\$pid 2>/dev/null || kill -KILL \$pid 2>/dev/null || true; rm -f /tmp/go2_gun_fire.pid; else echo \"\$(date -Is) STOP no pid file\" >> $REMOTE_LOG_Q; fi; $CHMOD_USB && bash -lc $STOP_COMMAND_Q; status=\$?; echo \"\$(date -Is) STOP stop_command_status=\$status\" >> $REMOTE_LOG_Q; echo OK STOP status=\$status; exit 0"
    ;;
  TEST)
    REMOTE_COMMAND="echo \"\$(date -Is) TEST requested\" >> $REMOTE_LOG_Q; $CHMOD_USB && echo OK TEST"
    ;;
  STATUS)
    REMOTE_COMMAND="echo \"\$(date -Is) STATUS requested\" >> $REMOTE_LOG_Q; tty=0; [ -e /dev/ttyUSB0 ] && tty=1; active=0; pid=; if [ -f /tmp/go2_gun_fire.pid ]; then pid=\$(cat /tmp/go2_gun_fire.pid 2>/dev/null || true); if [ -n \"\$pid\" ] && kill -0 \"\$pid\" 2>/dev/null; then active=1; fi; fi; chmod_status=1; $CHMOD_USB >/dev/null 2>&1 && chmod_status=0; echo \"\$(date -Is) STATUS tty=\$tty active=\$active pid=\$pid chmod_status=\$chmod_status\" >> $REMOTE_LOG_Q; echo OK STATUS tty=\$tty active=\$active pid=\$pid chmod_status=\$chmod_status"
    ;;
  *)
    echo "unknown GUN_ACTION: $GUN_ACTION" >&2
    exit 2
    ;;
esac

export REMOTE_COMMAND

log "local command begin port=$GUN_LOCAL_SSH_PORT user=$GUN_JETSON_USER"
out_file="$(mktemp)"
err_file="$(mktemp)"
trap 'rm -f "$out_file" "$err_file"' EXIT

set +e
expect >"$out_file" 2>"$err_file" <<'EXPECT'
set timeout 15
log_user 0

spawn ssh -p $env(GUN_LOCAL_SSH_PORT) -o StrictHostKeyChecking=accept-new $env(GUN_JETSON_USER)@127.0.0.1 $env(REMOTE_COMMAND)
expect {
  -re "(?i)password:" { send -- "$env(GUN_JETSON_PASSWORD)\r"; exp_continue }
  -re "(?i)sorry" { puts stderr "sudo password rejected"; exit 1 }
  -re "OK (START|STOP|TEST|STATUS)(\[^\r\n\]*)?" {
    puts $expect_out(0,string)
    exit 0
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
