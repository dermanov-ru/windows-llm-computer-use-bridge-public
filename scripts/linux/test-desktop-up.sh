#!/bin/bash
# Isolated X11 test desktop: Xvfb + private D-Bus session + AT-SPI bus + openbox.
# Agents can drive it without touching the real desktop. Usage:
#   scripts/linux/test-desktop-up.sh [:display] [WxH]
#   . /tmp/bridge-desktop<display>.env   # exports DISPLAY and DBUS_SESSION_BUS_ADDRESS
set -e
DISP=${1:-:99}; SIZE=${2:-1920x1080}; N=${DISP#:}
STATE=/tmp/bridge-desktop$N
Xvfb "$DISP" -screen 0 "${SIZE}x24" -nolisten tcp >"$STATE.xvfb.log" 2>&1 & echo $! >"$STATE.xvfb.pid"
sleep 1
# DISPLAY must reach the D-Bus activation environment; otherwise activated
# services (gnome-terminal-server, portals) open windows on the real display.
export DISPLAY="$DISP"
setsid dbus-run-session -- bash -c '
  dbus-update-activation-environment DISPLAY XAUTHORITY
  echo "export DISPLAY='"$DISP"' DBUS_SESSION_BUS_ADDRESS=$DBUS_SESSION_BUS_ADDRESS" > '"$STATE"'.env
  /usr/libexec/at-spi-bus-launcher --launch-immediately &
  sleep 0.5; openbox &
  sleep infinity' >"$STATE.session.log" 2>&1 </dev/null & echo $! >"$STATE.session.pid"
for _ in $(seq 50); do [ -s "$STATE.env" ] && break; sleep 0.1; done
cat "$STATE.env"
