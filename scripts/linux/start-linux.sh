#!/bin/bash
# Run the bridge server against the current X11 session ($DISPLAY). No elevation.
cd "$(dirname "$0")/../.."
[ -n "$DISPLAY" ] || { echo "DISPLAY is not set: start inside an X11 session or source a test desktop env" >&2; exit 1; }
[ "${XDG_SESSION_TYPE:-x11}" = wayland ] && echo "warning: Wayland session; XTest input only reaches XWayland windows" >&2
exec python3 server.py "$@"
