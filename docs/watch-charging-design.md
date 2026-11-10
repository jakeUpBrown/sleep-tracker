# Watch charge control: design notes

Goal: while the Bangle.js 2 sits in its dock, the Pi watches its battery and cuts the dock's power once the charge passes a cap, so the battery is not held at 100% for 16 hours a day. The Pi stays on; only the watch cable loses its 5 V.

## What the hardware gives us

- **Battery**: 175 mAh LiPo. The gauge is a voltage divider on `D3` (1 MΩ / 3 MΩ, so `analogRead(D3)` is about a quarter of the cell voltage against the 3.3 V reference). `E.getBattery()` is linear in that reading: `(v - 0.246) * 100 / (0.3144 - 0.246)`, clamped to 0–100. In volts that is roughly 3.25 V = 0% and 4.15 V = 100%, so 80% is about 3.97 V and 70% about 3.88 V. While charging the terminal voltage sits high, so the percentage reads a few points optimistic until the cell has rested for a minute or two. Some watches never reach 100% on this scale (93–95% after a day on charge is common).
- **Charger detection**: `Bangle.isCharging()` is the charger IC's status pin (`D23`, active low, polled every 80 ms, no debounce in firmware). It cannot distinguish "charger unplugged" from "charge complete", which is why the current `pilog.boot.js` debounces it for 3 s. There is a separate "charging complete" line on `D25` that the firmware does not expose.
- **Charge cable**: a USB-A plug on the far end. Everything the Pi needs to do is cut the 5 V on that plug.
- **Advertising**: when not connected, the watch advertises every 375 ms. `NRF.setAdvertising({0x180F: [E.getBattery()]})` puts the battery in the advertisement as Battery Service data, and `manufacturerData` (Espruino's ID 0x0590) can carry a charging flag next to it. The Pi can read both with a passive `bleak` scan, with no connection, so the Web IDE and phone stay free and the one-connection rule in CLAUDE.md is untouched.

## What the research says about the cap

- Battery University's rule of thumb: each 0.10 V off the 4.20 V peak roughly doubles cycle life, and 3.92 V/cell is the longevity sweet spot. On this watch's gauge 3.92 V reads about 74%.
- Storage at 100% is the clear loser for calendar ageing in the graphite/NCA pouch-cell study (Werner 2021), with little difference below 80%. Newer silicon-oxide anode studies find a "spoon" shape, with a bump in ageing near 80% as well, so 80% is a convention from phones rather than a magic number. Lower is better as long as the night is covered.
- What matters for this watch is calendar ageing, not cycles: it is docked most of the day and does under one shallow cycle a night. So the cap should be as low as the night allows. The night's cost (HRM on, accelerometer at 12.5 Hz, Bluetooth streaming) has to come from the recorded CSVs; the `battery` column of each night file gives it directly:

  ```bash
  for f in ~/sleeptracker/data/watch-*.csv; do awk -F, -v f="$(basename "$f")" 'NR==2{a=$11} NR>1{b=$11} END{print f, a"% -> "b"%"}' "$f"; done
  ```

  Pick the cap as the worst night's drain plus a 20-point margin, rounded up. Start at 80 until there are a few nights to go on.

Sources: [Battery University BU-808](https://batteryuniversity.com/article/how-to-prolong-lithium-based-batteries), [Werner et al. 2021, Batteries](https://doaj.org/article/cc60a71d6de0422d88652650040f9b00), [Zülke et al., KIT](https://publikationen.bibliothek.kit.edu/1000133836), [Warwick silicon-oxide study](https://wrap.warwick.ac.uk/193742), [Bangle.js 2 technical page](https://www.espruino.com/Bangle.js2+Technical), [Espruino firmware source](https://github.com/espruino/Espruino/blob/master/libs/banglejs/jswrap_bangle.c), [Espruino discussion on E.getBattery never reaching 100%](https://github.com/orgs/espruino/discussions/3036), [NRF.setAdvertising reference](https://www.espruino.com/Reference#l_NRF_setAdvertising).

## How to switch the dock's power

| Option | Parts | How | Notes |
|---|---|---|---|
| **A. The Pi's own USB ports** (recommended, try first) | none | `uhubctl -l 1-1 -p 2 -a off` / `on` | The Pi 3B+ can switch the 5 V on its four USB-A ports, but only all four together. Nothing else on this Pi uses USB (Bluetooth and Wi-Fi are on-board, the LED strip is on SPI), so that is fine. Ethernet should survive, but the only way to be sure is to try it over Wi-Fi SSH. `install.sh` installs `uhubctl` from apt and a udev rule so the service does not need sudo. |
| B. A USB power switch board on a GPIO | one ~$3 board (8086 Consultancy "USB Power Switch", a SiP32508 high-side switch: USB-A in, USB-A out, `EN` and `GND` pins) | GPIO high = power on | Dedicated to the watch, no shared ports, and it defaults to off at boot. Needs two jumper wires. The service drives the pin with `gpiozero` (its `lgpio` build deps are already installed for the strip). |
| C. A Wi-Fi smart plug on a USB wall adapter | one plug (Kasa/TP-Link or Tasmota) | `python-kasa` | No wiring, but adds a cloud-ish device, Wi-Fi dependence and a plug with a relay click. |

A plain N-channel MOSFET module from a 3.3 V GPIO cannot do high-side switching of 5 V properly, so if B, use a switch IC board rather than a bare FET module.

To test option A on the Pi:

```bash
sudo apt install -y uhubctl && sudo uhubctl
```

```bash
sudo uhubctl -l 1-1 -p 2 -a off
```

The watch's charging icon should go out within a few seconds (and SSH over Wi-Fi should keep working), then `sudo uhubctl -l 1-1 -p 2 -a on` brings it back.

## Software design

As built (October 2026), where it differs from the plan below: the cap is a top-level `charge_max` in `settings.json` like the other web app settings, and power comes back at a fixed 10 points under it; only the `uhubctl` switch exists (option A was chosen), with a hand-edited `"watch_charger": {"hub", "port"}` for a different hub; with no reading for 10 minutes the dock is powered rather than left alone, since "on" is the safe state; the advert's manufacturer byte is a flags byte (1 charger live, 2 recording, 4 stopped by hand) and the controller powers the dock whenever the watch reports recording or a hand stop, because a watch put back in an unpowered dock would otherwise keep recording (only a live charger stops it); the service re-sends its state every 10 minutes; the motion gate wants 3 jolts over 0.15 g within 5 s.


### Watch (`watch/src/pilog.boot.js`)

1. **Broadcast the battery.** Once a minute, in every state (recording or not, before the early return): `NRF.setAdvertising({0x180F: [E.getBattery()]}, {manufacturerData: [charging ? 1 : 0, rawVoltageByte]})`. When connected, also `Bluetooth.println("PB," + battery + "," + charging)` every minute so the logger knows the level while the watch is docked but connected.
2. **Don't mistake "dock power cut" for "taken off the dock".** Today `charging` going false after 3 s means "start recording". With the Pi cutting power, a watch sitting in the dock would see the same thing, switch the HRM on and drain itself on the dock. Change the start rule to: charger gone **and** the watch has moved (a few accelerometer samples with `diff` above about 0.1 g within a short window). A watch picked up and put on always passes; a watch sitting in a dark dock never does. The stop rule (charger back) stays as it is. Putting the watch on the nightstand instead of the wrist still starts recording the moment it is picked up, as now.

Rebuild `watch/pilog_install.js` and bump `VERSION`.

### Logger (`watch/watch_logger.py`)

Parse `PB,` lines and the `battery` column of `PL,` rows, and add `battery` and `charging` to `watch-status.json`. Nothing else changes.

### New service: `charge/charge.py` as `sleepcharge` (runs in `~/snooz-env`, like `wake.py`)

Loop every 60 s:

1. **Read the battery.** If `watch-status.json` is fresh and says connected, take `battery`/`charging` from there (the watch is not advertising while connected). Otherwise do a 15 s passive `bleak` scan filtered to the address in `~/bangle.json` and read the 0x180F service data and the charging byte. No reading for 10 minutes: leave the power as it is and log it.
2. **Decide.** Two settings: `charge_max` (default 80) and `charge_resume` (default `charge_max - 10`). Power goes **off** when `battery >= charge_max`, **on** when `battery < charge_resume`, otherwise unchanged. Because the watch only self-discharges about a quarter of a percent an hour while idle, the dock is off nearly all day and turns on for the half hour after the watch goes back in the morning. Readings taken while charging run high, so after switching off wait two minutes and re-read before trusting a level, and never switch more than once every 5 minutes.
3. **Drive the switch** through a small `PowerSwitch` class with three backends picked by `settings.json`: `uhubctl` (subprocess), `gpio` (a `gpiozero` output on a configurable pin) and `fake` (prints, for `--dry-run` and tests). Power is restored on a clean shutdown and at service start if the reading is below the cap, so a crash never leaves the watch unable to charge; the systemd unit has `Restart=always`.
4. **Report.** Write `~/sleeptracker/watch-charge.json`: `{"battery", "charging", "power", "source", "updated"}`.

Settings, in `settings.json` as `"watch_charger": {"switch": "uhubctl", "max": 80, "resume": 70, "pin": 17}` (`pin` only for `gpio`). The web app keeps the object when it saves, like `sunrise` and `tuning`.

CLI: `charge.py --status` (last reading and power state), `--on` / `--off` (manual override for an hour), `--dry-run`.

### Web app

On the Sleep card, under "Recording now": "Watch battery 76%, charging" or "Watch battery 81%, dock power off", greyed with "no reading since …" when stale. The Alarm card gets a "Charge the watch to" number field (50–100, default 80).

### Install and docs

`install.sh` installs `uhubctl` and its udev rule (or `gpiozero` into the venv for option B), writes `sleepcharge.service`, and the README gets a section on the dock, the setting and the switch options. The CLAUDE.md status line gains the charger.

### Tests

`tests/test_charge.py`: the decision table (hysteresis, stale readings, the 5-minute minimum between switches, restore-on-start), the status file, and the `fake` switch. `tests/test_watch_logger.py` covers the `PB,` line. The watch-side motion gate is checked by hand: dock the watch, cut power with `uhubctl`, and confirm no bed icon appears after 10 minutes; pick it up and confirm it does.

## Later, not now

- **Just-in-time top-up**: hold the watch at 60% all day and charge to the cap an hour before the usual bedtime (learned from when the logger is normally started). Better for the battery than a fixed cap but needs a bedtime estimate; worth it once the fixed cap has run for a few weeks.
- Log the daily battery curve to a CSV so the cap can be tuned from data rather than guessed.
