#!/usr/bin/env bash
# One helper for the dog + Jetson over the SSH bridge. Run from the repo root.
#
#   scripts/go2.sh <command>
#     dog        shell into the dog
#     jetson     shell into the Jetson (hops through the dog automatically)
#     video      stream the Jetson camera to http://127.0.0.1:8788 (Ctrl-C stops)
#     logs       follow the autonomy log on the Jetson
#     status     autonomy service status
#     restart    restart the autonomy service        (asks the Jetson sudo pw)
#     stop       stop the autonomy service            (asks the Jetson sudo pw)
#     deploy     push code + restart, DRY RUN (no motion)
#     live       push code + restart, LIVE (robot moves)
#
# Passwords come from the repo's .env (gitignored, never committed):
#   GUN_DOG_PASSWORD, GUN_JETSON_PASSWORD, GUN_JETSON_SUDO_PASSWORD
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "$ROOT/.env" ]]; then set -a; . "$ROOT/.env"; set +a; fi

DOG="${GUN_DOG_HOST:-192.168.123.121}"; DOG_USER="${GUN_DOG_USER:-root}"
JET="${GUN_JETSON_HOST:-10.42.0.2}";    JET_USER="${GUN_JETSON_USER:-unitree}"
VPORT="${GO2_AUTONOMY_PORT:-8788}"
UNIT="${GO2_JETSON_UNIT:-go2-autonomy}"
SSHO=(-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1  (sudo apt install $1)" >&2; exit 127; }; }
need sshpass; need ssh

usage() { sed -n '3,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

_proxy() { printf 'sshpass -p %q ssh %s -W %%h:%%p %s@%s' "${GUN_DOG_PASSWORD:?set GUN_DOG_PASSWORD in .env}" "${SSHO[*]}" "$DOG_USER" "$DOG"; }

dog() { : "${GUN_DOG_PASSWORD:?set GUN_DOG_PASSWORD in .env}"
        sshpass -p "$GUN_DOG_PASSWORD" ssh "${SSHO[@]}" -t "$DOG_USER@$DOG" "$@"; }

jet() { : "${GUN_JETSON_PASSWORD:?set GUN_JETSON_PASSWORD in .env}"
        sshpass -p "$GUN_JETSON_PASSWORD" ssh "${SSHO[@]}" -t \
          -o ProxyCommand="$(_proxy)" "$JET_USER@$JET" "$@"; }

cmd="${1:-help}"; shift || true
case "$cmd" in
  dog)     dog ;;
  jetson)  jet ;;
  logs)    jet "journalctl -u $UNIT -n 100 -f 2>/dev/null || sudo journalctl -u $UNIT -n 100 -f" ;;
  status)  jet "systemctl status $UNIT --no-pager 2>/dev/null | head -30 || sudo systemctl status $UNIT --no-pager | head -30" ;;
  restart) jet "sudo systemctl restart $UNIT && echo restarted" ;;
  stop)    jet "sudo systemctl stop $UNIT && echo stopped" ;;
  video)   : "${GUN_JETSON_PASSWORD:?set GUN_JETSON_PASSWORD in .env}"
           echo "video -> http://127.0.0.1:$VPORT   (Ctrl-C to stop)"
           sshpass -p "$GUN_JETSON_PASSWORD" ssh "${SSHO[@]}" -N \
             -o ProxyCommand="$(_proxy)" \
             -L "127.0.0.1:$VPORT:127.0.0.1:$VPORT" "$JET_USER@$JET" ;;
  deploy)  bash "$ROOT/scripts/deploy_patrol_to_jetson.sh" ;;
  live)    bash "$ROOT/scripts/deploy_patrol_to_jetson.sh" --live ;;
  help|-h|--help) usage ;;
  *)       echo "unknown command: $cmd" >&2; usage; exit 2 ;;
esac
