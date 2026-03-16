#!/bin/sh
# Runs a command against a private Xvfb display. Replaces xvfb-run, which hangs as a
# container's PID 1 because it waits for a SIGUSR1 from Xvfb that never arrives.
set -eu
display_number=99
Xvfb ":$display_number" -screen 0 1280x1024x24 -nolisten tcp &
socket="/tmp/.X11-unix/X$display_number"
for _ in $(seq 50); do
  [ -S "$socket" ] && break
  sleep 0.1
done
[ -S "$socket" ] || { echo "Xvfb did not start within 5s" >&2; exit 1; }
export DISPLAY=":$display_number"
exec "$@"
