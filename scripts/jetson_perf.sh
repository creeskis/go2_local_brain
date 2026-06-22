#!/usr/bin/env bash
# Pin the Jetson to maximum sustained performance for real-time control.
#
# nvpmodel -m 0  = MAXN (max CPU/GPU/EMC clocks). jetson_clocks then disables the
# DVFS governor so clocks stay pinned, which keeps the WebRTC + LiDAR + control
# loop latency consistent instead of drooping under light load.
#
# Override the power mode with GO2_JETSON_NVP_MODE (e.g. 1 for the 25W sustained
# profile on JetPack 6.2 Super). Safe to run anywhere: if the Jetson tools are
# missing it warns and exits 0, so it works as a systemd ExecStartPre on a
# non-Jetson dev box too. Run as root (the service uses ExecStartPre=+).
set -u

MODE="${GO2_JETSON_NVP_MODE:-0}"
log() { printf '%s [jetson_perf] %s\n' "$(date -Is)" "$*"; }

if command -v nvpmodel >/dev/null 2>&1; then
  log "nvpmodel -m ${MODE}"
  nvpmodel -m "${MODE}" </dev/null || log "WARN: nvpmodel failed (continuing)"
else
  log "nvpmodel not found; skipping power-mode change (not a Jetson?)"
fi

if command -v jetson_clocks >/dev/null 2>&1; then
  log "jetson_clocks (pinning CPU/GPU/EMC to max)"
  jetson_clocks || log "WARN: jetson_clocks failed (continuing)"
else
  log "jetson_clocks not found; skipping clock pin"
fi

exit 0
