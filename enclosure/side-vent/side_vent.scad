// Side vent grille for the plywood box prototype.
//
// Fits a rectangular hole cut in a box side: the flange sits on the outside
// face and the plug goes into the hole. The slats slope down on the way out,
// so from level or above you can't see in (the Pi's LEDs stay out of the
// bedroom) and dust doesn't fall in.
//
// Print it flange face down, no supports. Units are millimetres. The console
// echoes the size, the slat spacing and the open area. See README.md.

/* [Hole in the box side] */
// Hole width (6 in)
hole_w = 152.4;
// Hole height (1 1/4 in). Keep it clear of the top and bottom panels behind the side
hole_h = 31.75;
// Plug corner radius. If you drill the hole's corners, at least the drill's radius
hole_r = 5;
// Gap between the plug and the hole, each side
fit = 0.2;
// How far the plug goes into the hole. Less than the plywood thickness
plug_depth = 4;

/* [Flange] */
flange_t = 2.4;
// Flange width above and below the hole
flange_side = 7;
// Flange width at each end, where the screws go
flange_end = 10;
screws = true;
// #4 flat-head wood screw: shank clearance and head diameter
screw_d = 3.2;
screw_head_d = 6;


/* [Slats] */
// Slope of the slats below horizontal, on the way out. 0 = straight slots
slat_angle = 45; // [0:5:50]
slat_t = 2;
// Number of openings between the slats. 0 = the fewest that still hide the inside from level view
channels = 0;
// Upright ribs tying the slats together across the width, so they don't flex
ribs = 3; // [0:6]
rib_t = 2;
// Wall around the slats
frame = 2;

/* [Hidden] */
$fn = 40;
eps = 0.01;

depth  = flange_t + plug_depth;
plug_w = hole_w - 2 * fit;
plug_h = hole_h - 2 * fit;
in_w   = plug_w - 2 * frame;
in_h   = plug_h - 2 * frame;
out_w  = hole_w + 2 * flange_end;
out_h  = hole_h + 2 * flange_side;

// Every opening is a full channel from the outside face to the inside face,
// sloping with the slats, so none dead-ends against the frame. At the outside
// face the channels span y_lo..y_hi: the floor starts below the plug's inner
// wall (the flange is solid there) and the roof ends low enough to clear the
// frame at the inside face.
drop   = depth * tan(slat_angle);       // rise of a channel from outside to inside
slat_v = slat_t / cos(slat_angle);      // slat thickness measured vertically
y_lo   = -in_h / 2 - flange_t * tan(slat_angle);
y_hi   = in_h / 2 - drop;
span   = y_hi - y_lo;
n      = channels > 0 ? channels
       : slat_angle > 0 ? ceil((span + slat_v) / (drop + slat_v))
       : max(1, round((span + slat_t) / 5));
gap    = (span - (n - 1) * slat_v) / n;  // channel height, measured vertically
// A level view is blocked when each channel rises at least its own height
hides  = slat_angle > 0 && gap <= drop + eps;
// Open area measured square to the airflow
open_cm2 = (in_w - ribs * rib_t) * n * gap * cos(slat_angle) / 100;

echo(str("Vent ", out_w, " x ", out_h, " x ", depth, " mm; hole ", hole_w, " x ", hole_h, " mm"));
echo(str(n, " openings ", round(gap * 10) / 10, " mm tall; open area about ",
         round(open_cm2), " cm2 (", round(open_cm2 / 6.4516 * 10) / 10, " in2)"));
echo(hides ? "Level view into the box: blocked" : "Level view into the box: OPEN (more channels or a steeper slat_angle)");
if (gap < 1.5) echo("WARNING: openings under 1.5 mm will clog with dust; use fewer channels");
if (slat_angle > 50) echo("WARNING: slats steeper than 50 degrees may not print without supports");

module rounded_rect(w, h, r) {
    offset(r) square([w - 2 * r, h - 2 * r], center = true);
}

// One opening, x0..x0+w across. +y is up on the box; z = 0 is the outside face.
module channel(y0, x0, w) {
    multmatrix([[1, 0, 0, 0], [0, 1, tan(slat_angle), 0], [0, 0, 1, 0]])
        translate([x0, y0, -1]) cube([w, gap, depth + 2]);
}

// The openings, cut between the ribs
module channels() {
    rib_x = [for (i = [1 : ribs]) -in_w / 2 + i * in_w / (ribs + 1)];
    starts = concat([-in_w / 2], [for (c = rib_x) c + rib_t / 2]);
    ends   = concat([for (c = rib_x) c - rib_t / 2], [in_w / 2]);
    for (s = [0 : len(starts) - 1], k = [0 : n - 1])
        channel(y_lo + k * (gap + slat_v), starts[s], ends[s] - starts[s]);
}

module screw_hole() {
    translate([0, 0, -1]) cylinder(d = screw_d, h = depth + 2);
    translate([0, 0, -eps]) cylinder(d1 = screw_head_d, d2 = screw_d, h = (screw_head_d - screw_d) / 2);
}

// Arrow on the back of the flange, pointing up, for fitting it the right way round
module up_arrow() {
    translate([0, hole_h / 2 + fit + 1, flange_t - 0.6])
        linear_extrude(1) polygon([[-3, 0], [3, 0], [0, 4]]);
}

module vent() {
    difference() {
        union() {
            linear_extrude(flange_t) rounded_rect(out_w, out_h, 3);
            linear_extrude(depth) rounded_rect(plug_w, plug_h, max(hole_r - fit, 0.5));
        }
        channels();
        if (screws)
            for (s = [-1, 1]) translate([s * (hole_w / 2 + flange_end / 2), 0, 0]) screw_hole();
        up_arrow();
    }
}

vent();
