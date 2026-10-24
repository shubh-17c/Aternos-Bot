#!/usr/bin/env sh
# Runs as root only long enough to fix bind-mount ownership, then drops to the
# unprivileged `app` user. Also adds a virtual display when HEADLESS=false.
set -eu

APP_UID=10001
APP_GID=10001

if [ "$(id -u)" = "0" ]; then
    mkdir -p /app/state
    chown -R "$APP_UID:$APP_GID" /app/state 2>/dev/null || true
    exec setpriv --reuid="$APP_UID" --regid="$APP_GID" --init-groups "$0" "$@"
fi

if [ "${HEADLESS:-true}" = "false" ] && command -v xvfb-run >/dev/null 2>&1; then
    exec xvfb-run -a --server-args="-screen 0 1366x900x24" "$@"
fi

exec "$@"
