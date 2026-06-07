#!/bin/bash
# Startet Chrome mit Remote-Debugging + noVNC Web-UI
# Aufruf: automatisch beim Container-Start

set -e

DISPLAY_NUM=99
DISPLAY=":${DISPLAY_NUM}"
SCREEN_RES="1920x1080x24"
VNC_PORT=5900
NOVNC_PORT=6080
CDP_PORT=9222
CHROME_PROFILE=/chrome-data

echo "=== paperflow chrome-desktop ==="
echo "noVNC Web-UI:    http://<server>:${NOVNC_PORT}/vnc.html"
echo "Chrome CDP:      http://<server>:${CDP_PORT}"
echo "================================="

# 1. Virtuelles Display starten
Xvfb ${DISPLAY} -screen 0 ${SCREEN_RES} -ac +extension GLX +render -noreset &
XVFB_PID=$!
echo "Xvfb gestartet (PID ${XVFB_PID})"
sleep 1

# 2. Fenstermanager (leichtgewichtig)
export DISPLAY=${DISPLAY}
openbox --config-file /dev/null &
sleep 1

# 3. x11vnc (VNC-Server)
x11vnc \
  -display ${DISPLAY} \
  -rfbport ${VNC_PORT} \
  -forever \
  -shared \
  -nopw \
  -quiet &
echo "x11vnc gestartet (Port ${VNC_PORT})"
sleep 1

# 4. noVNC WebSocket-Proxy
websockify \
  --web /usr/share/novnc \
  --wrap-mode=ignore \
  ${NOVNC_PORT} \
  localhost:${VNC_PORT} &
echo "noVNC gestartet (Port ${NOVNC_PORT})"
sleep 1

# 5. Chrome mit Remote-Debugging
mkdir -p ${CHROME_PROFILE}

chromium-browser \
  --no-sandbox \
  --disable-dev-shm-usage \
  --remote-debugging-port=${CDP_PORT} \
  --remote-debugging-address=0.0.0.0 \
  --user-data-dir=${CHROME_PROFILE} \
  --no-first-run \
  --disable-default-apps \
  --disable-extensions-except= \
  --disable-background-networking \
  --disable-client-side-phishing-detection \
  --disable-default-apps \
  --disable-hang-monitor \
  --disable-popup-blocking \
  --disable-prompt-on-repost \
  --disable-sync \
  --disable-translate \
  --metrics-recording-only \
  --safebrowsing-disable-auto-update \
  --password-store=basic \
  --use-mock-keychain \
  --window-size=1280,900 \
  --window-position=0,0 \
  https://www.amazon.de &
CHROME_PID=$!
echo "Chrome gestartet (PID ${CHROME_PID}, CDP Port ${CDP_PORT})"

# Auf alle Prozesse warten
wait ${XVFB_PID}
