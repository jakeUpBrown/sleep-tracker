# Side vent grille

A printed vent for the sides of the plywood box prototype (sides 9 in wide, 2 in tall). It fits a 6 × 1 1/4 in hole: the flange covers the cut edge on the outside and a short plug sits in the hole. The slats slope down on the way out. From level or above you can't see into the box, so the Pi's LEDs don't light the bedroom at night, and dust doesn't fall in.

- `side_vent.stl`: ready to print with the defaults.
- `side_vent.scad`: the parametric model. Open it in [OpenSCAD](https://openscad.org), change values in the Customizer (Window > Customizer), render (F6) and export an STL. The console prints the vent's size, the openings and the open area.

With the defaults the vent is 172 × 46 × 6.4 mm and has three openings with about 18 cm² (2.8 in²) of open area. Each opening slopes with the slats all the way through, so none dead-ends against the frame. The slats are 2 mm thick, and three upright ribs tie them together across the width so they don't flex. On the 2 in side the flange comes within about 2.5 mm (3/32 in) of the top and bottom edges.

## How many

Use one on each of two opposite sides, so air can cross the box. A box only 2 in tall gets little chimney draft, so its walls shed much of the heat themselves. Two vents are plenty for the Pi alone. If the LED strip is inside the box, fit one on all four sides, then run the 90-minute soak test below. If the Pi still runs hot, add a fan.

## Printing

- Flange face down, no supports. PLA is fine, because the inside of the box stays well under 50 °C.
- The slats lean 45°, so part cooling should be on.
- 0.2 mm layers.

## Fitting

1. Mark a 6 × 1 1/4 in rectangle centred on the side. That leaves 3/8 in above and below the hole. If the top and bottom panels sit inside the sides and are thicker than that, use a 1 in hole (`hole_h = 25.4`, about 11 cm² of open area).
2. Drill a 3/8 in hole inside each corner of the rectangle. Cut between them with a jigsaw or coping saw, then file the edges until the plug pushes in. The plug's corners are rounded to suit the drilled corners.
3. Fit it with the small arrow on the back of the flange pointing **up**. Fitted upside down, the slats aim the view upward and the inside shows.
4. Fix it with two #4 × 1/4 in flat-head wood screws (drill 1/16 in pilot holes through the flange holes), or a bead of hot glue on the inside.

The plug is 4 mm deep, which suits 1/4 in plywood or thicker. For thinner plywood, set `plug_depth` about 1 mm less than its thickness.

## Changing it

| Setting | Default | Notes |
|---|---|---|
| `hole_w`, `hole_h` | 152.4, 31.75 | The hole you cut (6 × 1 1/4 in) |
| `fit` | 0.2 | Gap around the plug. Raise it if the plug is too tight in a test cut |
| `slat_angle` | 45 | 0 gives straight slots: more airflow (about 25 cm²), but you can see in. Above 50° the slats need supports |
| `slat_t` | 2 | Slat thickness. Thicker is stiffer, with less open area |
| `ribs`, `rib_t` | 3, 2 | Upright ribs across the slats. More ribs make it stiffer |
| `channels` | 0 (auto) | Number of openings. Auto is the fewest that still block a level view |
| `screws` | true | false drops the screw holes, for gluing |

## Checking it

Run the strip at full brightness for 90 minutes with the box closed, and log the Pi's temperature every minute:

```bash
nohup ~/snooz-env/bin/python ~/sleeptracker/code/sunrise/sunrise.py run --start $(date +%s) --full $(( $(date +%s) + 60 )) --off $(( $(date +%s) + 5400 )) --brightness 100 > ~/soak.log 2>&1 &
while true; do echo "$(date +%r) $(vcgencmd measure_temp) $(vcgencmd get_throttled)"; sleep 60; done
```

It passes if the Pi stays under about 70 °C and `throttled` stays `0x0`.
