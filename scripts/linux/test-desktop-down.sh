#!/bin/bash
# Stop the isolated test desktop started by test-desktop-up.sh.
DISP=${1:-:99}; N=${DISP#:}; STATE=/tmp/bridge-desktop$N
[ -f "$STATE.session.pid" ] && kill -- -"$(cat "$STATE.session.pid")" 2>/dev/null
[ -f "$STATE.xvfb.pid" ] && kill "$(cat "$STATE.xvfb.pid")" 2>/dev/null
rm -f "$STATE".{env,session.pid,xvfb.pid}
echo "stopped $DISP (programs started with launch_app run in their own sessions; close them first)"
