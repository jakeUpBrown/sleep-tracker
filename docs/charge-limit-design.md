# Watch charge limit: design notes

A starting point for a new session. Nothing here is built yet. When it is, track it in Linear as the next 2.x task and update the README in the same change.

## Goal

Stop the Bangle.js 2 charging at about 80% to extend the life of its battery. The cell is about 175 mAh and glued in. Keeping it at 100% for hours is the main thing that wears it, more than the number of charges.

## What we found

- **The watch can't limit its own charging.** The Bangle.js 2 has a basic charger chip with no settings ([Espruino forum](https://forum.espruino.com/conversations/383470)). The only way is to cut the charger's power from outside.
- **The battery reading is a rough estimate.** `E.getBattery()` works from the battery voltage. It reads high while charging and drops once the power is cut. Some watches never read above 93-95% even when full ([forum](https://forum.espruino.com/conversations/369412/), [forum](https://forum.espruino.com/conversations/395420/)). The limits apply to that reading, not to the true charge.
- **The Pi can cut USB power with `uhubctl`, with caveats on a 3B+** ([uhubctl](https://github.com/OsaroAI/uhubctl), [OctoPrint thread](https://community.octoprint.org/t/octoprint-usbcontrol/7659)).
  - Some or all of the four USB-A ports switch together, and accounts of the exact layout differ. Anything else plugged into USB loses power as well.
  - The Ethernet chip sits on the same internal hub. Choosing the wrong port can take the network and SSH down until the Pi's power is pulled ([guide](https://wiki.stadtfabrikanten.org/books/trikarus-project-hangprinter/page/smart-stepper-usb-soft-and-hard-reset)).
  - Run `sudo uhubctl` with no options to see the layout. Test while you can reach the Pi's power plug.
  - The 3B+'s on-board Bluetooth isn't on USB, so cutting USB power shouldn't affect the SNOOZ or watch connections. Check this on the real Pi.
- **Other ways to switch the power:** a small USB power switch or MOSFET wired to a GPIO pin (only affects the watch), or a smart plug on the charger.
- **The alternative with no new code:** the Charge Gently ("chargent") watch app alerts you near full so you can unplug the watch yourself.

## The conflict with recording

`watch/src/pilog.boot.js` turns recording on when charging stops and off when it starts again. If the Pi cuts power at 80%, the watch reads that as being taken off the charger. It would record a fake night on the nightstand with the heart rate sensor on, which drains the battery the limiter is meant to protect.

**Preferred fix:** when charging stops, wait until the watch is picked up before recording.
- Go into a "waiting" state instead of writing `{on:true}` straight away.
- Start recording at the first clear movement.
- If charging resumes while waiting, cancel the wait.
- The manual tap in Pi Sleep Log still starts recording straight away.
- This also stops a charger knocked loose in the night from starting a recording.

**Other option:** the watch remembers that it reached the limit while charging, and ignores the next charging stop unless it then moves. This is more state for the same result.

## Proposed design

### Watch (`watch/src/pilog.boot.js`)

- Put the battery level and charging state in the Bluetooth advertisement, using `NRF.setAdvertising` with manufacturer data. Espruino's company ID is `0x0590`.
- Refresh it every minute and on each `charging` event.
- This has to run before the `if(!isOn()) return;` line, so it works when recording is off.
- Check that changing the advertisement doesn't stop the phone or the Web IDE finding the watch.
- Add the movement-gated recording start described above.
- After editing `watch/src/`, run `python3 watch/build_installer.py` and commit both.

### Pi (new script, e.g. `watch/charge_limit.py`)

- Listen for the watch's advertisements with `bleak` in `~/snooz-env`. It never connects, so the phone, the Web IDE and `watch_logger.py` aren't locked out.
- Because it never connects, it can run as a systemd service, installed by `install.sh`. This is unlike `watch_logger.py`, which is started by hand.
- Use two limits so it doesn't flicker: power off when the reading reaches 80% while charging, and back on when it falls to 70% off power.
- If no advertisement arrives for a while, turn the power back on. A fault should leave the watch charging, not dead before the night.
- Allow a "charge to 100% once" override for travel, set in `settings.json` or from the web app.
- Read the limits from `settings.json` with defaults, like the other tunables.
- Log each switch and the battery reading, to tune the limits from real data.
- Keep the decision logic (limits, timeout, override) in a pure standard-library module, so `python3 -m unittest discover -s tests -v` runs it without `bleak`.
- Check that listening for advertisements doesn't interfere with the SNOOZ connection or `watch_logger.py`, since they share the one Bluetooth adapter.
- When the watch is connected to the logger it may stop advertising. That's fine: it's off the charger at night.

## Open questions for the owner

1. Does the watch charger plug into the Pi's USB, or into a wall adapter?
2. Is the Pi on Wi-Fi or Ethernet? This decides how risky `uhubctl` testing is.
3. Is anything else plugged into the Pi's USB ports?
4. What does `sudo uhubctl` print on the Pi?
5. Should the limits be 80/70, or something else?
6. Is the override needed, and should it live in the web app?

## Starting prompt

> Read `CLAUDE.md`, `README.md` and `docs/charge-limit-design.md`. Help me refine the watch charge-limit design. My answers to the open questions are: ... Then propose the changes to `watch/src/pilog.boot.js` and a new Pi script before writing any code.
