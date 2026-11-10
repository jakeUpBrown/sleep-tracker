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

echo "== LED strip: SPI on, build tools for its Python packages"
# lgpio (pulled in by the LED packages) fails to build in the venv without swig and liblgpio-dev
if ! dpkg -s swig liblgpio-dev python3-dev > /dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y --no-install-recommends swig liblgpio-dev python3-dev
fi
sudo raspi-config nonint do_spi 0
BOOT_CONFIG=/boot/firmware/config.txt
BOOT_CMDLINE=/boot/firmware/cmdline.txt
REBOOT=""
# A steady SPI clock, and a buffer big enough for the whole strip in one transfer
if ! grep -q '^core_freq=250' "$BOOT_CONFIG"; then
  [ -z "$(tail -c1 "$BOOT_CONFIG")" ] || echo | sudo tee -a "$BOOT_CONFIG" > /dev/null
  printf '[all]\ncore_freq=250\n' | sudo tee -a "$BOOT_CONFIG" > /dev/null
  REBOOT=1
fi
if ! grep -q 'spidev\.bufsiz=32768' "$BOOT_CMDLINE"; then
  sudo sed -i '1 s/$/ spidev.bufsiz=32768/' "$BOOT_CMDLINE"
  REBOOT=1
fi
# Still waiting for the reboot from an earlier run
if [ -r /proc/cmdline ] && ! grep -q 'spidev\.bufsiz=32768' /proc/cmdline; then
  REBOOT=1
fi

echo "== Watch dock power: uhubctl switches the Pi's USB ports"
if ! command -v uhubctl > /dev/null; then
  sudo apt-get update
  sudo apt-get install -y --no-install-recommends uhubctl
fi
# Lets the sleepcharge service switch the ports without sudo: the rules from uhubctl's README, for the
# Pi's own hub (vendor 0424) and, on kernels 6.0 and later, the sysfs files it switches through
UDEV_RULES=$(cat <<'RULES'
SUBSYSTEM=="usb", DRIVER=="hub|usb", MODE="0666", ATTR{idVendor}=="0424"
SUBSYSTEM=="usb", DRIVER=="hub|usb", RUN="/bin/sh -c \"chmod -f 666 $sys$devpath/*port*/disable || true\""
RULES
)
if [ "$(cat /etc/udev/rules.d/52-usb.rules 2>/dev/null)" != "$UDEV_RULES" ]; then
  echo "$UDEV_RULES" | sudo tee /etc/udev/rules.d/52-usb.rules > /dev/null
  sudo udevadm control --reload-rules
  sudo udevadm trigger --attr-match=subsystem=usb
fi

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

echo "== Smart alarm service (wake.py)"
sudo tee /etc/systemd/system/sleepwake.service > /dev/null <<SVC
[Unit]
Description=Sleep tracker smart alarm
After=network-online.target bluetooth.target
Wants=network-online.target

[Service]
User=$(id -un)
ExecStart=$VENV/bin/python -u $REPO/wake/wake.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SVC
sudo systemctl daemon-reload
sudo systemctl enable --quiet sleepwake
sudo systemctl restart sleepwake
sleep 2
systemctl is-active --quiet sleepwake && echo "   running" || echo "   NOT running: check 'journalctl -u sleepwake -n 20'"

echo "== Watch charge service (charge.py)"
sudo tee /etc/systemd/system/sleepcharge.service > /dev/null <<SVC
[Unit]
Description=Sleep tracker watch charge control
After=bluetooth.target

[Service]
User=$(id -un)
ExecStart=$VENV/bin/python -u $REPO/charge/charge.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SVC
sudo systemctl daemon-reload
# Opt-in: it switches the USB ports that power the watch dock, so it stays off until the hand checks
# in docs/watch-charging-design.md pass and the owner enables it once
if systemctl is-enabled --quiet sleepcharge 2>/dev/null; then
  sudo systemctl restart sleepcharge
  sleep 2
  systemctl is-active --quiet sleepcharge && echo "   running" || echo "   NOT running: check 'journalctl -u sleepcharge -n 20'"
else
  echo "   installed but off: turn it on with 'sudo systemctl enable --now sleepcharge' (see README)"
fi

echo "$NEW" > "$STAMP"
echo
echo "Installed $(git -C "$REPO" log -1 --format='%h %s')"
echo "Web app: http://$(hostname -I | awk '{print $1}'):8000"
echo "Alarm:   $VENV/bin/python $REPO/wake/wake.py --status"
echo "Charge:  $VENV/bin/python $REPO/charge/charge.py --status"

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

if [ -n "$REBOOT" ]; then
  echo
  echo "ACTION: reboot the Pi (sudo reboot) so the LED strip settings take effect"
fi
