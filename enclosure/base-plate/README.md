# Base plate

A printed plate that holds the Pi and the breadboard (with the LED level shifter) inside the plywood box, and screws to the box floor once. The Pi sits on four standoffs 6 mm above the plate, so air reaches its underside. The breadboard sits in a shallow tray beside the Pi's GPIO edge, so the jumpers stay short.

- `base_plate.stl`: ready to print with the defaults.
- `base_plate.scad`: the parametric model. Open it in [OpenSCAD](https://openscad.org), change values in the Customizer (Window > Customizer), render (F6) and export an STL. The console prints the plate size and the space left above the Pi.

With the defaults the plate is 103 × 140 mm (4.1 × 5.5 in) and 3 mm thick. Windows under the Pi and the breadboard halve the plastic. The plate doesn't need to be stiff, because it's screwed to the floor. The tallest part, the Pi's GPIO jumpers, reaches about 34 mm above the floor, which leaves about 17 mm below the lid of the 2 in box.

## Before printing

Measure your breadboard. The tray is sized for a half-size breadboard of 83 × 56 mm. If yours differs, set `bb_l` and `bb_w` and export a new STL.

## Printing

- Flat, no supports, 0.2 mm layers.
- PETG if the LED strip is inside the box: PLA softens at about 55 °C.

## Fitting

1. Place the plate with the Pi's USB and Ethernet end towards a side of the box you can reach, and away from the LED bar, so the strip's heat flows away from the Pi.
2. Screw it to the floor with four #4 × 1/2 in flat-head wood screws through the corner holes (drill 1/16 in pilot holes).
3. Fix the Pi with four M2.5 × 6 mm pan-head screws, which thread straight into the printed standoffs. The holes only line up one way round. Don't overtighten: the plastic threads strip.
4. Peel the breadboard's backing and press it into the tray. Its adhesive sticks to the plywood through the window, and to the ledge around the window's edge.

## Changing it

| Setting | Default | Notes |
|---|---|---|
| `inside_h` | 50.8 | Inside height of the box (2 in), for the clearance check |
| `bb_l`, `bb_w` | 83, 56 | Your breadboard's size |
| `standoff_h` | 6 | Air gap under the Pi |
| `standoff_hole` | 2.2 | For M2.5 screws threaded into the plastic. 3.5 for heat-set M2.5 inserts |
| `bb_gap` | 8 | Gap between the Pi and the tray |
| `margin` | 8 | Border where the floor screws go |
| `windows` | true | false prints a solid plate |
| `window_keep` | 3 | Solid plastic left around each standoff and inside the tray walls |
