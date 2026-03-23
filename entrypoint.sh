#!/bin/bash
set -e

if [ "${CAMOUFOX_DEBUG:-0}" = "1" ]; then
    echo "DEBUG mode: starting Xvfb + x11vnc + noVNC on port 6080"

    # Start virtual framebuffer
    Xvfb :99 -screen 0 1280x1100x24 -ac &
    sleep 1
    export DISPLAY=:99

    # Start VNC server (no password, localhost only is fine in Docker)
    x11vnc -display :99 -forever -nopw -quiet &

    # Start noVNC web client (proxies VNC over websocket)
    websockify --web /usr/share/novnc 6080 localhost:5900 &
fi

exec uvicorn server:app --host 0.0.0.0 --port 8000
