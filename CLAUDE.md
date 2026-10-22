# Notes for Claude

Read README.md first: it describes every component and the install/update flow, and it must stay accurate. Update it in the same change whenever setup steps, paths or commands change.

## Hardware and status
- Raspberry Pi 3B+ (Raspberry Pi OS, Python 3.13), Bangle.js 2 watch, SNOOZ white noise machine. DS3231 clock and an addressable LED strip are on order.
- Working: SNOOZ control (`snooz/`), watch app + Pi logger (`watch/`), phone web app with diary (`web/`), sleep analysis (`analysis/`), smart alarm service (`wake/`). The movement thresholds (`active_floor`, `active_ratio`) were set from the first real night, 2026-10-14; the others still have no real nights behind them.
- Not built yet: `sunrise.py` (LED light ramp). When it arrives it joins the fade in `wake.py`.
- Enclosure (`enclosure/sconce.scad`, October 2026): a wall sconce to the left of the headboard holding the Pi, a Perma-Proto HAT and the 1 m strip (5 rows of 12) behind a diffuser and a louver that aims the light at the owner's pillow and away from the partner's. Designed, not printed yet. The SNOOZ stays across the room.
- Alarm semantics (owner's choice, October 2026): `alarm_time` is the time to be awake by. From `alarm_time - window_minutes` the SNOOZ fade starts at the first light-sleep sign, otherwise at `alarm_time - fade_minutes` (a window no longer than the fade means no watching: the fade starts when the window opens); the fade ends by `alarm_time`; the watch buzzes at `alarm_time` as the last resort, with the watch's own built-in alarm as a backup. The SNOOZ fade is the wake stimulus, not the buzz.

## Conventions
- The owner runs every command on the Pi himself over SSH. Give exact copy-paste commands, and keep `install.sh` idempotent so updating is always `git pull && ./install.sh`.
- Code lives in `~/sleeptracker/code`; data, settings and device secrets live outside the repo (see the README table). Never commit sleep data, `snooz.json`, `bangle.json`, IP addresses, usernames or emails: the repo is public.
- Bluetooth Python runs in `~/snooz-env`. `pysnooz` is pinned to 0.10.0: there `set_volume()` takes no duration; fades use `turn_on(volume, duration)` / `turn_off(duration)`.
- The watch accepts one Bluetooth connection at a time, so `watch_logger.py` is started by hand, not as a service (an always-on logger would lock out the Web IDE and phone). It is also the only process that talks to the watch: `wake.py` asks for a buzz through `~/sleeptracker/watch-cmd.json` and reads `~/sleeptracker/watch-status.json`.
- `analysis/sleep_analysis.py` is the single source of truth for light-sleep detection: the web chart overlay and the live alarm must go through the same `Detector` so they agree. Thresholds are named tunables with defaults, overridable from `settings.json` under `tuning`. Standard library only (the web app runs under the system python3).
- Tests: `python3 -m unittest discover -s tests -v` from the repo root; no packages needed. `analysis/make_test_night.py` makes a synthetic night for the web app and tests.
- Watch app: edit `watch/src/`, then run `python3 watch/build_installer.py` and commit both.
- Show times on a 12-hour clock in anything user-facing.
- Tasks are tracked in Linear with numbered titles (1.x before parts arrived, 2.x after); keep that numbering for new tasks.
