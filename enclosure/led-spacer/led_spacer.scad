// Spacers that hold the LED sheet off the box panel, so air can pass behind it.
//
// Print them standing on end, no supports, in PETG: PLA softens too close to
// the sheet's temperature. Units are millimetres. See README.md.

/* [Spacer] */
// Outside diameter (3/8 in)
od = 9.525;
// Height (1/4 in): the air gap behind the sheet
height = 6.35;
// Clearance hole for a #6 screw (5/32 in nominal)
hole_d = 4;
// Bevel on both ends, so the first layer's squish doesn't stop it sitting flat
bevel = 0.4;

/* [Printing] */
count = 4;
// Gap between spacers on the bed
spacing = 6;

/* [Hidden] */
$fn = 64;

echo(str(count, " spacers, ", od, " mm across, ", height, " mm tall; wall ", (od - hole_d) / 2, " mm"));

module spacer() {
    difference() {
        rotate_extrude()
            polygon([[0, 0], [od / 2 - bevel, 0], [od / 2, bevel],
                     [od / 2, height - bevel], [od / 2 - bevel, height], [0, height]]);
        translate([0, 0, -1]) cylinder(d = hole_d, h = height + 2);
    }
}

for (i = [0 : count - 1])
    translate([i * (od + spacing), 0, 0]) spacer();
