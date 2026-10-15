#!/usr/bin/env bash
# Install or update everything on the Pi. Safe to run again any time.
#   First time:  git clone https://github.com/jakeUpBrown/sleep-tracker.git ~/sleeptracker/code
#                ~/sleeptracker/code/install.sh
#   Update:      cd ~/sleeptracker/code && git pull && ./install.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="$HOME/sleeptracker"
VENV="$HOME/snooz-env"
STAMP="$STATE/.installed-commit"
OLD="$(cat "$STAMP" 2>/dev/null || true)"
NEW="$(git -C "$REPO" rev-parse HEAD)"

# True if any of the given paths changed since the last install (or we can't tell)
changed() { ! git -C "$REPO" diff --quiet "$OLD" "$NEW" -- "$@" 2>/dev/null; }

echo "== Data folder: $STATE/data"
mkdir -p "$STATE/data"

echo "== Bluetooth on"
sudo rfkill unblock bluetooth || true

echo "== Python packages in $VENV"
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet -r "$REPO/requirements.txt"

echo "== Web app service (port 8000)"
sudo tee /etc/systemd/system/sleepweb.service > /dev/null <<SVC
[Unit]
Description=Sleep tracker web app
After=network-online.target
Wants=network-online.target

[Service]
User=$(id -un)
ExecStart=/usr/bin/python3 -u $REPO/web/sleepweb.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SVC
sudo systemctl daemon-reload
sudo systemctl enable --quiet sleepweb
sudo systemctl restart sleepweb
sleep 2
systemctl is-active --quiet sleepweb && echo "   running" || echo "   NOT running: check 'journalctl -u sleepweb -n 20'"

echo "$NEW" > "$STAMP"
echo
echo "Installed $(git -C "$REPO" log -1 --format='%h %s')"
echo "Web app: http://$(hostname -I | awk '{print $1}'):8000"

if [ -n "$OLD" ] && [ "$OLD" != "$NEW" ]; then
  if changed watch/pilog_install.js; then
    echo
    echo "ACTION: the watch app changed. Upload watch/pilog_install.js with the Espruino Web IDE (see README)."
  fi
  if changed watch/watch_logger.py && pgrep -f watch_logger.py > /dev/null; then
    echo
    echo "ACTION: watch_logger.py changed and the old one is still running. Restart it when you're not sleeping:"
    echo "  pkill -f watch_logger.py"
  fi
fi
