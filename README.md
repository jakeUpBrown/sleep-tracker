# Sleep Tracker

A DIY sleep tracker and smart alarm. A Bangle.js 2 watch records movement and heart rate all night and streams it to a Raspberry Pi 3B+. The Pi saves each night, controls a SNOOZ white noise machine over Bluetooth, and serves a small web app on the home network where you set the alarm and look at your sleep. The goal is to wake you during light sleep inside a window before your alarm time, by brightening an LED strip and fading out the SNOOZ, with the watch buzz as the last resort.

| Folder | Runs on | What it does |
|---|---|---|
| `snooz/` | Pi | Pair with the SNOOZ and control it: on, off, volume, fades |
| `watch/` | Watch and Pi | **Pi Sleep Log** watch app, plus `watch_logger.py`, which saves its data on the Pi |
| `web/` | Pi | Phone web app: alarm settings and sleep charts |
| `install.sh` | Pi | Installs or updates everything on the Pi |

Not built yet: `sunrise.py` (LED light ramp) and `wake.py` (the smart alarm that reads the web app's settings).

## Install on the Pi (first time)

Over SSH:

```bash
sudo apt install -y git
git clone https://github.com/jakeUpBrown/sleep-tracker.git ~/sleeptracker/code
~/sleeptracker/code/install.sh
```

`install.sh` asks for your password once (for `sudo`). It turns Bluetooth on, puts the Python packages in `~/snooz-env`, sets the web app to start at boot, and prints the web app's address.

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

It restarts the web app. If the update changed something that it can't update for you, it says so at the end:

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

### Phone web app

Open the address `install.sh` printed (like `http://192.168.1.42:8000`) in Chrome on your phone, then tap ⋮ > **Add to Home screen**.

- Reserve the Pi's IP address in your router so the address doesn't change.
- **Alarm card**: turn the smart alarm on or off and set the wake-up time and smart wake window. Saving writes `~/sleeptracker/settings.json`, which `wake.py` will read.
- **Sleep card**: shows "Recording now" while the logger is getting data, and charts each night's movement and heart rate.
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
| `~/sleeptracker/settings.json` | Alarm settings from the web app |
| `~/snooz-env/` | Python packages for the Bluetooth scripts |
| `~/snooz.json` | SNOOZ address and pairing password (keep private) |
| `~/bangle.json` | The watch's Bluetooth address |
| `/etc/systemd/system/sleepweb.service` | Starts the web app at boot (written by `install.sh`) |

`settings.json` looks like this. `alarm_time` is 24-hour Pi local time, and `window_minutes` is the smart wake window before it.

```json
{"enabled": true, "alarm_time": "06:30", "window_minutes": 30, "updated": 1792025223}
```

The night CSV columns are `time`, `watch_unix`, `samples`, `mov_sum`, `mov_max`, `ax`, `ay`, `az`, `hr`, `hr_conf`, `battery` and `pi_unix`. `mov_sum` is the movement signal. Ignore `hr` when `hr_conf` is low, and treat `battery` as a rough trend only.

## Changing the code

- **Watch app**: edit the readable sources in `watch/src/`, then run `python3 watch/build_installer.py` to rebuild `watch/pilog_install.js`, and commit both. Bump `VERSION` in the build script when the app changes.
- **Web app**: it uses only the Python standard library. To try it on a laptop, run `python3 web/sleepweb.py --port 8000`. It reads and writes `~/sleeptracker` on whatever machine it runs on.
- **After any change**: push to GitHub, then follow [Update](#update) on the Pi.
