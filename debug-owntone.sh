#!/usr/bin/env bash
# Puts OwnTone in debug mode and restarts it, so the RTSP handshake with the
# speakers shows up in the log. Run ./debug-owntone.sh off to go back.
#
# Filter out "Running query" when reading the log, or SQL drowns everything.

set -euo pipefail
CONF=/etc/owntone.conf

LEVEL=debug
[ "${1:-}" = "off" ] && LEVEL=log

echo "== loglevel = $LEVEL"
sudo sed -i "s/^\([[:space:]]*\)loglevel[[:space:]]*=.*/\1loglevel = $LEVEL/" "$CONF"
grep -E '^[[:space:]]*loglevel' "$CONF"

echo "== restarting"
sudo systemctl restart owntone
sleep 4

echo "== ready. Follow along with:"
echo "   journalctl -u owntone -f | grep -v 'Running query'"
