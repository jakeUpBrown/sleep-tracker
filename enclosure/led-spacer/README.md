# LED sheet spacers

Four printed spacers that hold the LED's aluminum sheet 1/4 in off the box panel, so air can pass behind it.

- `led_spacer.stl`: all four, ready to print.
- `led_spacer.scad`: the parametric model. Open it in [OpenSCAD](https://openscad.org), change values in the Customizer (Window > Customizer), render (F6) and export an STL.

Each spacer is 3/8 in across and 1/4 in tall, with a 5/32 in (4 mm) clearance hole for a #6 screw.

## Printing

- PETG, standing on end, no supports. PLA softens at about 55 °C, too close to the sheet's temperature.
- 100% infill or at least 4 walls, so they don't crush.

## Fitting

Fix the sheet with four #6 × 1/2 in wood screws: through the sheet, through a spacer and into the panel. In 1/4 in plywood that leaves about 3/16 in in the wood, without the tips poking through. Tighten only until the spacers stop spinning, because PETG can slowly compress if it's clamped too hard.

## Changing it

| Setting | Default | Notes |
|---|---|---|
| `od` | 9.525 | Outside diameter (3/8 in) |
| `height` | 6.35 | Air gap behind the sheet (1/4 in) |
| `hole_d` | 4 | Clearance hole. Raise it if the screw binds |
| `count` | 4 | How many to lay out on the bed |
