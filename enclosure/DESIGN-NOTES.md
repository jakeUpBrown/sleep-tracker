# Sconce design notes (handoff)

Notes for picking up the enclosure design in a new session. `README.md` in this folder is the build guide. This file covers how the design got here, what was assumed, what has been checked and what is still open. Written October 22, 2026.

## Brief from the owner

- One enclosure for the electronics: Pi 3B+, DS3231 clock, LED level shifter, power and the LED strip with a diffuser.
- Prototype on the 3D printer. The final version may be woodworked. Wall mounting is fine.
- It goes on the wall to the left of the headboard, in about 3 ft (914 mm) of free wall.
- The light should point mostly at the left side of the bed (the owner's side) and disturb the partner as little as possible.
- The SNOOZ stays across the room on Bluetooth. It is **not** in the enclosure.
- Strip: Adafruit, 1 m, 60 LEDs ("I think"). Adafruit's 60/m RGBW strip is product 2837 (black PCB) or 2842 (white PCB), 12.6 mm wide and 4.2 mm thick in its silicone sleeve. RGBW and the exact width are **not confirmed**.

## Decisions and why

| Decision | Why |
|---|---|
| Small sconce, not a long bar or shelf | 1 m of strip doesn't fit in 914 mm in one line. A compact light is easier to aim and to shade from the partner. |
| Strip cut into 5 rows of 12 (200 mm rows, 20 mm apart), zig-zag, wire jumpers at the turns | Uses all 60 LEDs, and the housing (228 × 115 mm) fits a 250 × 210 mm bed. Costs about 12 solder joints. 4 × 15 would need a 270 mm housing. |
| Diffuser 15 mm in front of the LEDs | For an even glow the gap should be about one LED spacing (16.7 mm). Can be printed white (choose the thickness with the coupons) or cut from opal acrylic. |
| Partner shielding with a louver (parallel fins) in front of the diffuser, not by turning the whole box | A diffuser spreads light in every direction, so the face's angle alone can't aim it. Turning a 220 mm wide face 25° would make the box about 100 mm deeper at one end. Parallel fins pass only a band of angles, and the louver is cheap to reprint while tuning. |
| Fins upright, leaning toward the bed (`louver_dir = "bed_right"`) | See "A geometry correction" below. |
| Pi behind the light box, not beside it | Smaller footprint. The LEDs run about 15 minutes a morning, so heat is negligible. |
| Electronics on a Perma-Proto HAT on the Pi | Level shifter, resistor and DS3231 all on one board, with nothing extra to mount. |
| One external 5 V 10 A brick, barrel-to-screw-terminal adapter inside, Pi fed by a right-angle micro-USB pigtail | No mains wiring in a home-built box and one cord to the outlet. The pigtail keeps the Pi's input protection (feeding the 5 V GPIO pins would bypass it). |
| SNOOZ not enclosed | It makes its sound with a real fan and you twist its shell to set the tone. Inside a box it would sound wrong and be out of reach. The owner keeps it across the room anyway. |
| Bedroom darkness | No windows over the Pi. The light box is closed. The DS3231 board's red LED gets removed or covered. The Pi's LEDs get switched off in software (not done yet; see open items). |

## A geometry correction

My first louver had horizontal slats tilted down. That only separates the two sleepers when the light is on a **side wall**: the owner is closer to that wall than the partner, so the partner sees it at a shallower angle. On the **headboard wall** both heads are about the same distance out from the wall and differ only sideways. Horizontal slats can't tell them apart there, so the fins have to be upright and lean toward the bed. The model now has `louver_dir = bed_right | bed_left | down | none`.

Which wall it actually goes on is **not confirmed**. "To the left of the headboard" was read as the headboard wall.

## Louver arithmetic

For parallel fins of depth `L`, spacing `P`, leaning at angle θ: light at angle a passes when `|tan a − tan θ| < P / L`. It is full strength at a = θ and falls linearly to zero at the band edges.

Assumed layout, **not measured** (queen bed, pillow centres about 38 cm in from each edge, the light about 20 cm left of the headboard, eyes about 35 cm out from the wall and about 40 cm below the light's centre):

- Owner: about 60 cm to the side → about 60° toward the bed, about 80 cm away.
- Partner: about 135 cm to the side → about 75°, about 145 cm away.
- With no louver, distance and viewing angle alone give the partner roughly 15–20% of the owner's light.
- Defaults (θ = 50°, P = 20, L = 17): the band is 1–67°. The owner gets about 54% transmission at 60° and the partner gets 0 at 75°.
- Trade-off: a tighter band protects the partner more but costs the owner light. Moving the light closer to the owner's pillow helps both.
- Fins leaning more than about 50° won't print face-down (the model warns). For more lean, slats would have to be printed separately and slotted into a comb, which is also how a wood version would do it.

The brightness target is **not verified**. Commercial wake-up lights reach about 300 lux at the pillow. Whether 60 LEDs through a diffuser and a louver get there is unknown. Test with a phone light-meter app before the full build.

## Model structure (`sconce.scad`)

Coordinates: x along the wall (+ toward the headboard and bed), y out from the wall (wall at y = 0), z up. Each part is modelled in its assembled place, and `print_part()` lays it flat.

Depth stack, wall outward: plate 3 → electronics 36 → light box 2.4 plate + 15 gap → diffuser 1.2 → louver 17 → housing lip 2.4, about 77 mm in all. The housing clamps louver + diffuser + light box against the plate's corner posts, and four M3 screws go through the housing's top and bottom into tabs on the plate.

Parts: `housing`, `wall_plate` (Pi standoffs with the SD card edge on the left, GPIO at the top and room below for a micro-USB plug; corner posts; screw tabs; zip-tie loops; two countersunk wall-screw slots above the Pi), `light_box`, `diffuser`, `louver` (with a "this way up" notch), `coupons` (diffuser test tiles 0.6–1.6 mm).

Rendering and checks I used (OpenSCAD 2021.01):

```bash
openscad -o housing.stl -D 'part="housing"' enclosure/sconce.scad          # console echoes size and louver band
xvfb-run -a openscad -o view.png --imgsize=1200,800 --camera=-10,40,0,62,0,210,640 enclosure/sconce.scad
```

For a cross-section, make a file that `include`s `sconce.scad`, sets `explode = 0`, and intersects `assembly()` with a thin slab. Render it with `-D 'part="none"'` and an orthographic camera (`--projection=o`): `--camera=0,40,0,90,0,90,300` looks from the side, `0,40,0,0,0,0,330` from the top.

All parts rendered without errors and STL sizes were checked: housing 228 × 115 × 77, the plate, light box, louver and diffuser 223 × 109. Cross-sections confirmed the fin lean direction and the stack. **Nothing has been printed or test-fitted.**

## Weak spots and open items

1. **Confirm the wall** (headboard wall or side wall) and measure the real angles (see the build guide's "Measure first"). Then set `louver_angle` and `louver_pitch`.
2. **Confirm the strip**: width, RGBW or RGB, sleeved or bare, and whether it came with JST plugs.
3. **Printer model and bed size** are unknown. The housing needs 228 × 115 mm, so a Prusa Mini needs a split housing.
4. **The depth stack has no slack.** Print tolerances may stop the screws lining up or let parts rattle. Consider a little compliance, such as foam pads or a sprung rim on the light box.
5. **Assembly is awkward.** The louver, diffuser and light box are loaded into the face-down housing, then the strip is plugged into the HAT while holding the housing up to the wall. Possible fixes: clips that hold the light box in the housing, or a longer pigtail.
6. **The housing's top screws** can be seen from above. A hook or cleat at the top would leave screws only at the bottom.
7. **Electronics dimensions are guesses.** The HAT stack height (`elec_depth = 36`), the barrel adapter size (only zip-tied) and the clearance below the Pi for the micro-USB plug (30 mm, which assumes a right-angle pigtail) all need checking against the real parts.
8. **The louver costs light.** About half the owner's light is lost at the assumed angles. A brightness test decides whether that's acceptable, or whether to move the light closer, widen the band or skip the louver.
9. **Fin print quality** at a 50° lean, with 1.2 mm fins in PLA or PETG, is untested. The fins may need to be thicker.
10. **Turning off the Pi's LEDs** in `config.txt` belongs with the LED wiring (task 2.2). It isn't done, and `install.sh`/README don't mention it yet.
11. **Possible extras not designed:** a physical button (for example to start the logger or stop the alarm) and a watch-charger dock. Recording starts when the watch comes off its charger, so a dock is a natural fit, but it suits a nightstand better than a wall.
12. **Wood version:** a mitred hardwood box with a rabbet for the front, a small French cleat, the printed internals reused, and possibly wood slats as the louver. Not modelled.

## Related tasks (Linear)

- 1.4: check the LED strip parts (level shifter 74AHCT125, 1000 µF capacitor, 330 Ω resistor).
- 2.1: install the DS3231 (pins 1/3/5/6).
- 2.2: wire the LED strip (GPIO10 / pin 19 via SPI, `core_freq=250`).
- 1.6: `sunrise.py`.

No enclosure task exists yet. If one is added, it would be a 2.x task.
