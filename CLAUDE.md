# Notes for Claude

Read README.md first: it describes every component and the install/update flow, and it must stay accurate. Update it in the same change whenever setup steps, paths or commands change.

## Hardware and status
- Raspberry Pi 3B+ (Raspberry Pi OS, Python 3.13), Bangle.js 2 watch, SNOOZ white noise machine. DS3231 clock and an addressable LED strip are on order.
- Working: SNOOZ control (`snooz/`), watch app + Pi logger (`watch/`), phone web app (`web/`).
- Not built yet: `sunrise.py` (LED light ramp) and `wake.py` (smart alarm). `wake.py` reads `~/sleeptracker/settings.json` written by the web app; the smart wake window opens `window_minutes` before `alarm_time`; light sleep is detected from the watch stream; the watch buzz is the last-resort alarm, with the watch's own built-in alarm as a backup.

## Conventions
- The owner runs every command on the Pi himself over SSH. Give exact copy-paste commands, and keep `install.sh` idempotent so updating is always `git pull && ./install.sh`.
- Code lives in `~/sleeptracker/code`; data, settings and device secrets live outside the repo (see the README table). Never commit sleep data, `snooz.json`, `bangle.json`, IP addresses, usernames or emails: the repo is public.
- Bluetooth Python runs in `~/snooz-env`. `pysnooz` is pinned to 0.10.0: there `set_volume()` takes no duration; fades use `turn_on(volume, duration)` / `turn_off(duration)`.
- The watch accepts one Bluetooth connection at a time, so `watch_logger.py` is started by hand, not as a service (an always-on logger would lock out the Web IDE and phone).
- Watch app: edit `watch/src/`, then run `python3 watch/build_installer.py` and commit both.
- Show times on a 12-hour clock in anything user-facing.
- Tasks are tracked in Linear with numbered titles (1.x before parts arrived, 2.x after); keep that numbering for new tasks.
