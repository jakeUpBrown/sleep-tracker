# Sleep Tracker

A DIY sleep tracker and smart alarm. A Bangle.js 2 watch records movement and heart rate all night and streams it to a Raspberry Pi 3B+. The Pi saves each night, controls a SNOOZ white noise machine over Bluetooth, and serves a small web app on the home network where you set the alarm, keep a sleep diary and look at your sleep. Inside a window before your alarm time it waits for a sign of light sleep, then fades the SNOOZ out to wake you gently. An LED strip ramps up a 30-minute sunrise that reaches full brightness at the alarm time. The watch buzz at the alarm time is the last resort. During the day the Pi keeps the watch's battery at a cap (80% by default) by switching its dock's power, so the battery isn't held at 100% for hours.

| Folder | Runs on | What it does |
|---|---|---|
| `snooz/` | Pi | Pair with the SNOOZ and control it: on, off, volume, fades |
| `watch/` | Watch and Pi | **Pi Sleep Log** watch app (recording and the alarm screen), plus `watch_logger.py`, which saves its data on the Pi, and `merge_pilog.py`, which fills a night the Pi missed from the watch's copy |
| `analysis/` | Pi | `sleep_analysis.py`: finds sleep onset, light sleep and the moment to start the fade. Shared by the web app and the alarm so they always agree |
| `wake/` | Pi | `wake.py`: the smart alarm service |
| `sunrise/` | Pi | `sunrise.py`: drives the LED strip, for the alarm's sunrise and for tests |
| `charge/` | Pi | `charge.py`: keeps the watch's battery at a cap while it sits in its dock, by switching the Pi's USB power |
| `web/` | Pi | Phone web app: alarm settings, sleep diary and sleep charts |
| `tests/` | Laptop | Unit tests, run with `python3 -m unittest discover -s tests` |
| `docs/` | | `wake-prediction-research.md`: what the evidence says about timing a wake-up, which factors to add from the data already collected, and which new non-intrusive data is worth it. `watch-charging-design.md`: how the dock power control works and what the evidence says about the charge cap |
| `install.sh` | Pi | Installs or updates everything on the Pi |

## Install on the Pi (first time)

Over SSH:

```bash
sudo apt install -y git
git clone https://github.com/jakeUpBrown/sleep-tracker.git ~/sleeptracker/code
~/sleeptracker/code/install.sh
```

`install.sh` asks for your password once (for `sudo`). It turns Bluetooth on, puts the Python packages in `~/snooz-env`, sets the web app and the smart alarm (the `sleepwake` service) to start at boot, installs the watch charge control switched off (see [Watch charging](#watch-charging)), and prints the web app's address. The alarm is on for 6:30 AM from the start: change the time or switch it off on the web app's Alarm card.

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

It restarts the web app, the alarm service and the charge service. The alarm keeps the settings saved in the web app (on for 6:30 AM if none were ever saved). If the update changed something that it can't update for you, it says so at the end:

- **"the watch app changed"**: upload it again (see [Watch app](#watch-app-banglejs-2) below).
- **"watch_logger.py changed"**: the logger is running the old version. Stop it with `pkill -f watch_logger.py`, then start it again the next time you record.
- **"reboot the Pi"**: the LED strip's boot settings changed (the first time only). Run `sudo reboot`.

## Watch app (Bangle.js 2)

The watch app is installed from a laptop, not the Pi. Use the same steps to install it the first time and to update it.

1. Open `watch/pilog_install.js` and copy all of it. On GitHub, use the "Raw" view or the copy button.
2. In Chrome or Edge, open https://www.espruino.com/ide, click the connect icon (top left) and pick your Bangle.js.
3. Paste it into the right-hand editor pane and press **Upload** (the arrow pointing at the watch). The watch shows "Pi Sleep Log" when it's done.
4. Disconnect the Web IDE. The watch takes only one Bluetooth connection, so the laptop and your phone must be disconnected for the Pi to connect.
5. Check once, on the watch: Settings > Bluetooth > Programmable should be on.

Recording starts by itself once you take the watch off the charger and it moves (a watch left in a dock whose power the Pi has cut stays quiet), and stops when you put it back on a live charger. A bed icon shows at the top of the screen while it's recording. You can also open Pi Sleep Log and tap the screen to switch it on or off; a watch stopped by hand stays off until it has been back on a live charger.

The app also puts the battery level and charger state in the watch's Bluetooth advertisement, which is how the Pi watches the charge from across the room without connecting.

At the alarm time the Pi switches the watch to a "Wake up" screen that buzzes every 30 seconds. Press the button to stop it (touching the screen does nothing, so rolling over can't silence it). It gives up after 10 minutes. Recording keeps running underneath.

## LED sunrise strip

The strip is an Adafruit NeoPixel RGBW strip (SK6812, 60 LEDs on 1 m) with its own 5 V 4 A power supply. The Pi only sends it data, through a 74AHCT125 level shifter on a breadboard, because the Pi's 3.3 V signal is too weak for a 5 V strip. Wire it with everything unplugged:

| From | To |
|---|---|
| Pi pin 2 (5 V) | Breadboard **+** rail: powers the chip only, never the strip |
| Pi pin 20 (GND) | Breadboard **–** rail |
| Pi pin 19 (GPIO10, SPI data) | Chip pin 2 |
| Chip pins 1 and 7 | **–** rail |
| Chip pins 14, 4, 10 and 13 | **+** rail (14 powers the chip, the others switch off its unused channels) |
| Chip pin 3 | 330 Ω resistor, then the strip's **Din** |
| Power supply **+** | Strip **5V**, and the long leg of a 1000 µF capacitor |
| Power supply **–** | Strip **GND**, the capacitor's striped leg, and the breadboard **–** rail |

Pin 1 of the chip is next to its notch; pin 1 of the Pi is at the end of the header away from the USB ports. Connect the strip at its **Din** end (the end its arrows point away from); the clip-on connector needs the clear sleeve trimmed back about 2 cm so it touches bare pads. The wires at the other (DO) end stay unused. Never connect the supply's + to the Pi. Plug in the LED supply before the Pi.

`install.sh` switches on SPI and adds `core_freq=250` (steadies the Pi 3's SPI clock) and `spidev.bufsiz=32768` to the boot files, then asks for a reboot the first time. To check the strip by hand over SSH:

```bash
~/snooz-env/bin/python ~/sleeptracker/code/sunrise/sunrise.py test              # dim warm white for 5 s
~/snooz-env/bin/python ~/sleeptracker/code/sunrise/sunrise.py ramp --minutes 2  # the whole sunrise in 2 minutes
~/snooz-env/bin/python ~/sleeptracker/code/sunrise/sunrise.py off               # all LEDs off
```

Don't run these while the alarm's sunrise is on: both would be writing to the strip. Add `--fake` to print the colours instead of lighting the strip. A different strip goes in `settings.json` (see [below](#where-things-live-on-the-pi)).

For the plywood box prototype, [`enclosure/side-vent/`](enclosure/side-vent/) has a printable vent grille for the box sides, with fitting steps and a heat soak test, and [`enclosure/base-plate/`](enclosure/base-plate/) has a printable plate that holds the Pi and the breadboard on the box floor, and [`enclosure/led-spacer/`](enclosure/led-spacer/) has printable spacers for the LED's aluminum sheet.

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

### Fill in a night the Pi missed

If the logger stopped during the night, put the watch's copy back into that night's file:

1. In the Web IDE, open the storage list (the drive icon), download `pilog.csv`, and copy it to the Pi:

   ```bash
   scp pilog.csv pi@<pi-address>:~/pilog.csv
   ```

2. On the Pi, see what would change, then do it:

   ```bash
   python3 ~/sleeptracker/code/watch/merge_pilog.py ~/pilog.csv --dry-run
   python3 ~/sleeptracker/code/watch/merge_pilog.py ~/pilog.csv
   ```

   Rows are matched on the watch's timestamp, so running it twice is harmless. Each night that gains rows is rewritten with the original kept as `watch-YYYY-MM-DD.csv.bak`, and the report lists any stretch still without data: those are the watch's own gaps (on the charger, or recording switched off), which nothing can fill. Reload the web app to see the result. The watch keeps appending to `pilog.csv`, so delete it from the storage list now and then to keep it small.

### Smart alarm

`wake.py` runs all the time as the `sleepwake` service and reads the settings you save in the web app. For a 6:30 AM alarm with a 30-minute window and a 15-minute fade:

1. From 6:00 AM it follows the watch data the logger is saving. At the first sign of light sleep (a movement burst, turning over, or a heart-rate rise) it starts fading the SNOOZ out.
2. If it sees nothing by 6:15 AM, the fade starts then anyway. The fade is timed to end by the alarm time, so the room is quiet by 6:30 AM. If the fade fails (for example the SNOOZ wasn't found), it tries again while a fade can still end in time, up to 3 tries in all; a failed fade shows on that night's chart.
3. At 6:30 AM the watch shows "Wake up" and buzzes every 30 seconds until you press the button, for up to 10 minutes. Keep the watch's own alarm set a few minutes later as the backup.

The sunrise light runs alongside, on its own clock: from 6:00 AM it ramps from deep red through orange to warm white, full by 6:30 AM, whatever the light-sleep watching does, and stays on until 7:30 AM. It turns off sooner with **Turn light off** in the web app, or when you switch the alarm off. Pressing the watch button doesn't turn it off. If the strip fails, it tries again up to 3 times while there is time.

For this to work the SNOOZ has to be on at bedtime (its own button, or `snooz_ctl.py on 40`), and the logger has to be running for the light-sleep part. Without watch data it still does step 2 and tries step 3. With the window set to "Off" there is no light-sleep watching: the fade runs for the last 15 minutes before the alarm. A window no longer than the fade doesn't watch either: the fade starts when the window opens, so a 15-minute window with a 30-minute fade gives a 15-minute fade from 6:15 AM. Settings you save take effect within seconds, even inside the window. A wake-up time that has already passed when you save it is for the next day. Switching the alarm off during the window or the fade and back on before 10 minutes past the alarm brings that morning's alarm back; switching it off while the watch is buzzing silences it for good.

Checks you can run over SSH:

```bash
~/snooz-env/bin/python ~/sleeptracker/code/wake/wake.py --status             # next alarm and its timeline
~/snooz-env/bin/python ~/sleeptracker/code/wake/wake.py --simulate 2026-10-13 # what it would have done on a recorded night
~/snooz-env/bin/python ~/sleeptracker/code/wake/wake.py --test-buzz          # buzz the watch once (logger must be running)
~/snooz-env/bin/python ~/sleeptracker/code/wake/wake.py --test-fade 20       # fade the SNOOZ out over 20 s
```

`--test-buzz` reports an error if the logger hasn't picked the command up within 20 seconds, which means the logger isn't running or the watch isn't connected. `systemctl status sleepwake` checks the service and `journalctl -u sleepwake -n 30` shows what it did. Each night it also writes `~/sleeptracker/data/wake-YYYY-MM-DD.json`, which the web app shows on that night's chart.

### Watch charging

Plug the watch's charge cable into one of the Pi's USB ports and leave the watch in its dock during the day. The `sleepcharge` service (`charge.py`) reads the battery level from the watch's Bluetooth advertisement every minute and switches the 5 V on the Pi's USB ports off when the level reaches the cap you set in the web app (**Charge the watch to**, 80% by default) and back on when it is 10 points below it (70% for the default cap). An idle watch loses about a quarter of a percent an hour, so the dock is off nearly all day and charges for half an hour after the watch goes back in the morning. The Pi 3B+ can only switch all four USB ports together, so nothing else can be powered from them. While the logger is connected the level comes from it instead. A watch that is still recording when it goes back in the dock, or one stopped by hand, gets a powered dock whatever its level, because only a live charger makes the watch app stop recording or forget a hand stop.

**It is off until you turn it on.** `install.sh` installs the service but doesn't start it, because it hasn't been run on the Pi yet. Do the hand checks in `docs/watch-charging-design.md` first, then turn it on once; later updates keep it running:

```bash
sudo systemctl enable --now sleepcharge
```

To turn it off again, `sudo systemctl disable --now sleepcharge`.

Whenever the service can't be sure (no reading for 10 minutes, at start, at exit) it powers the dock, so a problem never leaves the watch unable to charge; if `uhubctl` itself fails it says so in the web app and keeps trying. Which cap is right is in `docs/watch-charging-design.md`: lower is better for the battery as long as the night is covered, so pick the worst night's drain plus 20 points.

```bash
~/snooz-env/bin/python ~/sleeptracker/code/charge/charge.py --status   # last reading, dock power, any problem
~/snooz-env/bin/python ~/sleeptracker/code/charge/charge.py --off      # hold the dock power off for an hour
~/snooz-env/bin/python ~/sleeptracker/code/charge/charge.py --on       # hold it on for an hour
~/snooz-env/bin/python ~/sleeptracker/code/charge/charge.py --auto     # end the hold
```

`--off` is also the way to check the hardware: the watch's charging icon should go out within a minute. `systemctl status sleepcharge` checks the service and `journalctl -u sleepcharge -n 30` shows what it did. To switch the ports by hand, `uhubctl -l 1-1 -p 2 -a off` (or `on`), but not while the service is running: it sends its own state again every 10 minutes. The Sleep card in the web app shows the latest reading.

### Phone web app

Open the address `install.sh` printed (like `http://192.168.1.42:8000`) in Chrome on your phone, then tap ⋮ > **Add to Home screen**.

- Reserve the Pi's IP address in your router so the address doesn't change.
- **Alarm card**: turn the smart alarm on or off and set the wake-up time, smart wake window and fade length, switch the sunrise light on or off and set its peak brightness, and set how far to charge the watch. While the light is on, a **Turn light off** button appears; `wake.py` acts on it within about 10 seconds. Saving writes `~/sleeptracker/settings.json`, which `wake.py` picks up within seconds.
- **Sleep card**: shows "Recording now" while the logger is getting data, the watch's battery and whether the dock is powered, and charts each night. The **Sleep depth** chart is the night at a glance: one line from Deep at the bottom to Awake at the top, built from how far movement and heart rate sit above their resting levels, averaged over a few minutes, with awake stretches at the top and detected light sleep never below the Light line. The dashed green line is a textbook sleep cycle (90 minutes by default, deep sleep early and REM late) laid from the time you fell asleep, to compare the night against. It is a reference, not a measurement: real cycles vary and wrist data cannot tell REM from deep sleep. The **Movement and heart rate** chart below shows the raw signals; shaded stretches are light sleep (gold) and awake (grey). On both, dashed lines mark falling asleep and waking up and the gold "fade" line and band show when the fade would start and end with your current settings. If the alarm ran that night, purple lines labelled faded (or fade failed), buzz and button show what it really did. The sentence under the charts says the same in words.
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
| `~/sleeptracker/watch-status.json` | Written by the logger: is the watch connected, when did data last arrive, when was the button pressed, battery level and charger state |
| `~/sleeptracker/watch-cmd.json` | Written by `wake.py` when it wants the watch to buzz; the logger sends it on and deletes it |
| `~/sleeptracker/light-cmd.json` | Written by the web app's **Turn light off**; `wake.py` turns the light off and deletes it |
| `~/sleeptracker/watch-charge.json` | Written by `charge.py` every minute: the watch's battery, whether the dock is powered, any problem |
| `~/sleeptracker/charge-cmd.json` | Written by `charge.py --on`, `--off` and `--auto`; the service acts on it and deletes it |
| `~/snooz-env/` | Python packages for the Bluetooth scripts |
| `~/snooz.json` | SNOOZ address and pairing password (keep private) |
| `~/bangle.json` | The watch's Bluetooth address |
| `/etc/systemd/system/sleepweb.service` | Starts the web app at boot (written by `install.sh`) |
| `/etc/systemd/system/sleepwake.service` | Starts the smart alarm at boot (written by `install.sh`) |
| `/etc/systemd/system/sleepcharge.service` | Starts the watch charge control at boot (written by `install.sh`) |
| `/etc/udev/rules.d/52-usb.rules` | Lets `charge.py` switch the Pi's USB hub without sudo (written by `install.sh`) |

`settings.json` looks like this. `alarm_time` is 24-hour Pi local time, the time you want to be awake by. `window_minutes` is the smart wake window before it and `fade_minutes` is how long the SNOOZ takes to fade out. When `window_minutes` is no longer than `fade_minutes` there is no light-sleep watching, and the fade lasts `window_minutes`.

`sunrise_enabled` switches the sunrise light on or off and `sunrise_brightness` is its peak, as a percentage of the strip's safe maximum (10 to 100). `charge_max` is the watch's charge cap (50 to 100).

```json
{"enabled": true, "alarm_time": "06:30", "window_minutes": 30, "fade_minutes": 15,
 "sunrise_enabled": true, "sunrise_brightness": 100, "charge_max": 80, "updated": 1792025223}
```

For a different LED strip, add a `"sunrise"` object by hand: `"sunrise": {"leds": 60, "pixel_order": "GRBW", "max_amps": 3.0}` are the defaults. `pixel_order` is `GRBW` or `RGBW` for RGBW strips and `GRB` or `RGB` for RGB ones (WS2812B is `GRB`). `max_amps` caps the strip's estimated current: keep it at about three quarters of the power supply's rating, and at most 5 for the barrel jack adapter. The web app keeps this object when you save. For a different USB hub or port, add `"watch_charger": {"hub": "1-1", "port": 2}` the same way (the defaults, which are the Pi 3B+'s four ports; `sudo uhubctl` lists what your Pi has).

The light-sleep detection has a handful of thresholds with defaults listed at the top of `analysis/sleep_analysis.py`. To change one, add a `"tuning"` object to `settings.json`, for example `"tuning": {"active_ratio": 3.0}`; the web app keeps it when you save, and both the chart and the alarm use it. The same object sets the depth chart's `cycle_minutes` (the textbook cycle length) and `depth_smooth_minutes` (how many minutes the depth line averages). The movement thresholds were set from the first recorded night (October 2026); the rest had no real nights behind them yet, so expect to adjust them after a week of charts and diary entries.

The night CSV columns are `time`, `watch_unix`, `samples`, `mov_sum`, `mov_max`, `ax`, `ay`, `az`, `hr`, `hr_conf`, `battery` and `pi_unix`. `mov_sum` is the movement signal. Ignore `hr` when `hr_conf` is low, and treat `battery` as a rough trend only.

### Copying data to your computer

Everything the Pi records is in `~/sleeptracker/data`. To analyse it on a laptop, copy that folder into `data/` in this repo (it is gitignored, so it stays private). Put your Pi username and address in place of `<user>` and `<pi-address>`; copying into the repo folder itself refreshes `data/` each time instead of nesting a new `data/data/`:

```bash
scp -r <user>@<pi-address>:sleeptracker/data "D:/Coding Projects/Github/sleep-tracker/"
```

## Changing the code

- **Watch app**: edit the readable sources in `watch/src/`, then run `python3 watch/build_installer.py` to rebuild `watch/pilog_install.js`, and commit both. Bump `VERSION` in the build script when the app changes.
- **Web app**: it uses only the Python standard library. To try it on a laptop, run `python3 web/sleepweb.py --port 8000`. It reads and writes `~/sleeptracker` on whatever machine it runs on. To have something to look at, make a synthetic night first: `python3 analysis/make_test_night.py --date 2026-10-13 --out ~/sleeptracker/data --turnover-at 06:08` (the turnover time has to fall inside the night, and it won't overwrite an existing file).
- **Sunrise**: `sunrise.py ... --fake` prints the colours instead of driving the strip, so the ramp can be checked on a laptop. `wake.py` is the only process that drives the strip during an alarm; the web app asks it to turn the light off through `light-cmd.json`.
- **Watch charging**: `charge.py --dry-run` runs the controller on a laptop with the USB switch replaced by printed messages (it still scans for the watch, so it needs `bleak`, and it reads the real `settings.json` and logger status); its state and command files go to a `charge-dry-run` folder under `~/sleeptracker`. The decision rules are in `desired_power()` and tested in `tests/test_charge.py`.
- **Alarm and analysis**: `wake.py` and the web app share `analysis/sleep_analysis.py`, so a change to the detection shows up in both the chart overlay and the live alarm. `wake.py --dry-run` runs the alarm loop on a laptop with the SNOOZ and watch replaced by printed messages; its wake logs go to a `sleepwake-dry-run` folder in the system temp directory, not to `~/sleeptracker/data`.
- **Tests**: `python3 -m unittest discover -s tests -v` from the repo root. No packages needed.
- **After any change**: push to GitHub, then follow [Update](#update) on the Pi.
