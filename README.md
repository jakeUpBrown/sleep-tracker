# Sleep Tracker

A DIY sleep tracker and smart alarm. A Bangle.js 2 watch records movement and heart rate all night and streams it to a Raspberry Pi 3B+. The Pi saves each night, controls a SNOOZ white noise machine over Bluetooth, and serves a small web app on the home network where you set the alarm, keep a sleep diary and look at your sleep. Inside a window before your alarm time it waits for a sign of light sleep, then fades the SNOOZ out to wake you gently. The watch buzz at the alarm time is the last resort. An LED sunrise will join later.

| Folder | Runs on | What it does |
|---|---|---|
| `snooz/` | Pi | Pair with the SNOOZ and control it: on, off, volume, fades |
| `watch/` | Watch and Pi | **Pi Sleep Log** watch app (recording and the alarm screen), plus `watch_logger.py`, which saves its data on the Pi |
| `analysis/` | Pi | `sleep_analysis.py`: finds sleep onset, light sleep and the moment to start the fade. Shared by the web app and the alarm so they always agree |
| `wake/` | Pi | `wake.py`: the smart alarm service |
| `web/` | Pi | Phone web app: alarm settings, sleep diary and sleep charts |
| `tests/` | Laptop | Unit tests, run with `python3 -m unittest discover -s tests` |
| `docs/` | | `wake-prediction-research.md`: what the evidence says about timing a wake-up, which factors to add from the data already collected, and which new non-intrusive data is worth it |
| `install.sh` | Pi | Installs or updates everything on the Pi |

Not built yet: `sunrise.py` (LED light ramp).

## Install on the Pi (first time)

Over SSH:

```bash
sudo apt install -y git
git clone https://github.com/jakeUpBrown/sleep-tracker.git ~/sleeptracker/code
~/sleeptracker/code/install.sh
```

`install.sh` asks for your password once (for `sudo`). It turns Bluetooth on, puts the Python packages in `~/snooz-env`, sets the web app and the smart alarm (the `sleepwake` service) to start at boot, and prints the web app's address. The alarm is on for 6:30 AM from the start: change the time or switch it off on the web app's Alarm card.

If the repo is private, the Pi needs to sign in to GitHub before `git clone` will work. Run `sudo apt install -y gh && gh auth login`, pick GitHub.com, HTTPS and "Login with a web browser", and enter the code it shows on your phone.

### Coming from the pasted-in copies

If you installed things by pasting the old `install_*.sh` scripts, those copies still work but won't get updates. After `install.sh` has run, delete them:

```bash
rm -f ~/snooz_pair.py ~/snooz_ctl.py ~/snooz_menu.py \
      ~/sleeptracker/watch_logger.py ~/sleeptracker/sleepweb.py ~/sleeptracker/sleepweb.html
```

Your data, settings, SNOOZ pairing (`~/snooz.json`) and saved watch address (`~/bangle.json`) are not touched.

## Update

On the Pi:

```bash
cd ~/sleeptracker/code && git pull && ./install.sh
```

It restarts the web app and the alarm service. The alarm keeps the settings saved in the web app (on for 6:30 AM if none were ever saved). If the update changed something that it can't update for you, it says so at the end:

- **"the watch app changed"**: upload it again (see [Watch app](#watch-app-banglejs-2) below).
- **"watch_logger.py changed"**: the logger is running the old version. Stop it with `pkill -f watch_logger.py`, then start it again the next time you record.

## Watch app (Bangle.js 2)

The watch app is installed from a laptop, not the Pi. Use the same steps to install it the first time and to update it.

1. Open `watch/pilog_install.js` and copy all of it. On GitHub, use the "Raw" view or the copy button.
2. In Chrome or Edge, open https://www.espruino.com/ide, click the connect icon (top left) and pick your Bangle.js.
3. Paste it into the right-hand editor pane and press **Upload** (the arrow pointing at the watch). The watch shows "Pi Sleep Log" when it's done.
4. Disconnect the Web IDE. The watch takes only one Bluetooth connection, so the laptop and your phone must be disconnected for the Pi to connect.
5. Check once, on the watch: Settings > Bluetooth > Programmable should be on.

Recording starts by itself when you take the watch off the charger and stops when you put it back on. A bed icon shows at the top of the screen while it's recording. You can also open Pi Sleep Log and tap the screen to switch it on or off.

At the alarm time the Pi switches the watch to a "Wake up" screen that buzzes every 30 seconds. Press the button to stop it (touching the screen does nothing, so rolling over can't silence it). It gives up after 10 minutes. Recording keeps running underneath.

## Everyday use

### Record a night

At bedtime, start the logger so it keeps running after you close SSH:

```bash
nohup ~/snooz-env/bin/python -u ~/sleeptracker/code/watch/watch_logger.py >> ~/sleeptracker/data/logger.log 2>&1 &
```

- Check it: `tail ~/sleeptracker/data/logger.log`
- Stop it in the morning: `pkill -f watch_logger.py`
- If it picked the wrong watch: add `--forget` once to search again.

Each night goes to `~/sleeptracker/data/watch-YYYY-MM-DD.csv`, named for the evening it started. If the Pi missed part of the night, the watch keeps its own copy as `pilog.csv`, which you can download from the Web IDE's storage list (the drive icon).

### Smart alarm

`wake.py` runs all the time as the `sleepwake` service and reads the settings you save in the web app. For a 6:30 AM alarm with a 30-minute window and a 15-minute fade:

1. From 6:00 AM it follows the watch data the logger is saving. At the first sign of light sleep (a movement burst, turning over, or a heart-rate rise) it starts fading the SNOOZ out.
2. If it sees nothing by 6:15 AM, the fade starts then anyway. The fade is timed to end by the alarm time, so the room is quiet by 6:30 AM. If the fade fails (for example the SNOOZ wasn't found), it tries again while a fade can still end in time, up to 3 tries in all; a failed fade shows on that night's chart.
3. At 6:30 AM the watch shows "Wake up" and buzzes every 30 seconds until you press the button, for up to 10 minutes. Keep the watch's own alarm set a few minutes later as the backup.

For this to work the SNOOZ has to be on at bedtime (its own button, or `snooz_ctl.py on 40`), and the logger has to be running for the light-sleep part. Without watch data it still does step 2 and tries step 3. With the window set to "Off" there is no light-sleep watching: the fade runs for the last 15 minutes before the alarm. A window no longer than the fade doesn't watch either: the fade starts when the window opens, so a 15-minute window with a 30-minute fade gives a 15-minute fade from 6:15 AM. Settings you save take effect within seconds, even inside the window. A wake-up time that has already passed when you save it is for the next day. Switching the alarm off during the window or the fade and back on before 10 minutes past the alarm brings that morning's alarm back; switching it off while the watch is buzzing silences it for good.

Checks you can run over SSH:

```bash
~/snooz-env/bin/python ~/sleeptracker/code/wake/wake.py --status             # next alarm and its timeline
~/snooz-env/bin/python ~/sleeptracker/code/wake/wake.py --simulate 2026-10-13 # what it would have done on a recorded night
~/snooz-env/bin/python ~/sleeptracker/code/wake/wake.py --test-buzz          # buzz the watch once (logger must be running)
~/snooz-env/bin/python ~/sleeptracker/code/wake/wake.py --test-fade 20       # fade the SNOOZ out over 20 s
```

`--test-buzz` reports an error if the logger hasn't picked the command up within 20 seconds, which means the logger isn't running or the watch isn't connected. `systemctl status sleepwake` checks the service and `journalctl -u sleepwake -n 30` shows what it did. Each night it also writes `~/sleeptracker/data/wake-YYYY-MM-DD.json`, which the web app shows on that night's chart.

### Phone web app

Open the address `install.sh` printed (like `http://192.168.1.42:8000`) in Chrome on your phone, then tap ⋮ > **Add to Home screen**.

- Reserve the Pi's IP address in your router so the address doesn't change.
- **Alarm card**: turn the smart alarm on or off and set the wake-up time, smart wake window and fade length. Saving writes `~/sleeptracker/settings.json`, which `wake.py` picks up within seconds.
- **Sleep card**: shows "Recording now" while the logger is getting data, and charts each night's movement and heart rate. Shaded stretches are light sleep (gold) and awake (grey); dashed lines mark falling asleep and waking up; the gold "fade" line and band show when the fade would start and end with your current settings. If the alarm ran that night, purple lines labelled faded (or fade failed), buzz and button show what it really did. The sentence under the chart says the same in words.
- **Diary card**: for the night picked above, note melatonin, whether you were sober, a 1 to 10 rating and anything else. It takes a few seconds each morning and is the ground truth for judging whether the light-sleep detection is right. Saved to `~/sleeptracker/data/diary.json`.
- **Away from home**: install Tailscale on the Pi and your phone. Run `curl -fsSL https://tailscale.com/install.sh | sh`, then `sudo tailscale up`, then `sudo tailscale serve --bg 8000`. That gives the app a private HTTPS address, and on HTTPS it also opens full screen like a real app. Don't open a port on your router instead.
- **Service commands**: `systemctl status sleepweb` checks it, and `journalctl -u sleepweb -n 20` shows the log, including each settings change.

### SNOOZ

Pair it once. Close the SNOOZ phone app, hold the SNOOZ power button for about 5 seconds until all the buttons flash, then run:

```bash
~/snooz-env/bin/python ~/sleeptracker/code/snooz/snooz_pair.py
```

To control it:

```bash
~/snooz-env/bin/python ~/sleeptracker/code/snooz/snooz_ctl.py on 40       # on at volume 40
~/snooz-env/bin/python ~/sleeptracker/code/snooz/snooz_ctl.py fadeout 60  # fade out over 60 s
~/snooz-env/bin/python ~/sleeptracker/code/snooz/snooz_menu.py            # interactive, type 'help'
```

## Where things live on the Pi

| Path | What |
|---|---|
| `~/sleeptracker/code/` | This repo |
| `~/sleeptracker/data/` | Nightly watch CSVs and `logger.log` |
| `~/sleeptracker/data/diary.json` | Your diary entries, one per night |
| `~/sleeptracker/data/wake-YYYY-MM-DD.json` | What the alarm did each night |
| `~/sleeptracker/settings.json` | Alarm settings from the web app |
| `~/sleeptracker/watch-status.json` | Written by the logger: is the watch connected, when did data last arrive, when was the button pressed |
| `~/sleeptracker/watch-cmd.json` | Written by `wake.py` when it wants the watch to buzz; the logger sends it on and deletes it |
| `~/snooz-env/` | Python packages for the Bluetooth scripts |
| `~/snooz.json` | SNOOZ address and pairing password (keep private) |
| `~/bangle.json` | The watch's Bluetooth address |
| `/etc/systemd/system/sleepweb.service` | Starts the web app at boot (written by `install.sh`) |
| `/etc/systemd/system/sleepwake.service` | Starts the smart alarm at boot (written by `install.sh`) |

`settings.json` looks like this. `alarm_time` is 24-hour Pi local time, the time you want to be awake by. `window_minutes` is the smart wake window before it and `fade_minutes` is how long the SNOOZ takes to fade out. When `window_minutes` is no longer than `fade_minutes` there is no light-sleep watching, and the fade lasts `window_minutes`.

```json
{"enabled": true, "alarm_time": "06:30", "window_minutes": 30, "fade_minutes": 15, "updated": 1792025223}
```

The light-sleep detection has a handful of thresholds with defaults listed at the top of `analysis/sleep_analysis.py`. To change one, add a `"tuning"` object to `settings.json`, for example `"tuning": {"active_ratio": 3.0}`; the web app keeps it when you save, and both the chart and the alarm use it. No nights had been recorded when the defaults were chosen, so expect to adjust them after a week of charts and diary entries.

The night CSV columns are `time`, `watch_unix`, `samples`, `mov_sum`, `mov_max`, `ax`, `ay`, `az`, `hr`, `hr_conf`, `battery` and `pi_unix`. `mov_sum` is the movement signal. Ignore `hr` when `hr_conf` is low, and treat `battery` as a rough trend only.

### Copying data to your computer

Everything the Pi records is in `~/sleeptracker/data`. To analyse it on a laptop, copy that folder into `data/` in this repo (it is gitignored, so it stays private). Put your Pi username and address in place of `<user>` and `<pi-address>`; copying into the repo folder itself refreshes `data/` each time instead of nesting a new `data/data/`:

```bash
scp -r <user>@<pi-address>:sleeptracker/data "D:/Coding Projects/Github/sleep-tracker/"
```

## Changing the code

- **Watch app**: edit the readable sources in `watch/src/`, then run `python3 watch/build_installer.py` to rebuild `watch/pilog_install.js`, and commit both. Bump `VERSION` in the build script when the app changes.
- **Web app**: it uses only the Python standard library. To try it on a laptop, run `python3 web/sleepweb.py --port 8000`. It reads and writes `~/sleeptracker` on whatever machine it runs on. To have something to look at, make a synthetic night first: `python3 analysis/make_test_night.py --date 2026-10-13 --out ~/sleeptracker/data --turnover-at 06:08` (the turnover time has to fall inside the night, and it won't overwrite an existing file).
- **Alarm and analysis**: `wake.py` and the web app share `analysis/sleep_analysis.py`, so a change to the detection shows up in both the chart overlay and the live alarm. `wake.py --dry-run` runs the alarm loop on a laptop with the SNOOZ and watch replaced by printed messages; its wake logs go to a `sleepwake-dry-run` folder in the system temp directory, not to `~/sleeptracker/data`.
- **Tests**: `python3 -m unittest discover -s tests -v` from the repo root. No packages needed.
- **After any change**: push to GitHub, then follow [Update](#update) on the Pi.
