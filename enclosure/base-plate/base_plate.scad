// Base plate for the plywood box prototype: holds the Pi and the
// breadboard with the LED level shifter, and screws to the box floor once.
//
// The Pi sits on four printed standoffs; the breadboard sits in a shallow tray
// beside the Pi's GPIO edge, so the jumpers stay short. Print it flat, no
// supports. Units are millimetres. The console echoes the plate size and the
// height left above the Pi. See README.md.

/* [Box] */
// Inside height of the box (2 in)
inside_h = 50.8;

/* [Plate] */
plate_t = 3;
// Border around the Pi and the tray, where the floor screws go
margin = 8;
// #4 flat-head wood screw: shank clearance and head diameter
screw_d = 3.2;
screw_head_d = 6;
// Cut windows under the Pi and the breadboard to save filament
windows = true;
// Solid plastic left around each standoff and inside the tray walls
window_keep = 3;

/* [Pi] */
// Height of the standoffs above the plate: air gap under the Pi
standoff_h = 6;
standoff_d = 6;
// Hole for an M2.5 screw to thread into. 3.5 for a heat-set M2.5 insert instead
standoff_hole = 2.2;

/* [Breadboard] */
// Measure yours: a half-size breadboard is about 83 x 56 mm
bb_l = 83;
bb_w = 56;
// Gap around the breadboard in its tray, each side
bb_fit = 0.5;
tray_wall = 1.6;
tray_h = 4;
// Gap between the Pi's GPIO edge and the tray
bb_gap = 8;

/* [Hidden] */
$fn = 40;
eps = 0.01;

// Raspberry Pi 3B+ and 4 mechanical drawing: 85 x 56 board, M2.5 holes 58 x 49 apart,
// 3.5 mm in from the microSD end and the long edges. GPIO header on the y = 56 edge.
pi_l = 85;
pi_w = 56;
pi_holes = [[3.5, 3.5], [61.5, 3.5], [3.5, 52.5], [61.5, 52.5]];
pi_pcb = 1.6;
pi_ports_h = 16;      // USB-A stack above the board
pi_jumpers_h = 23.5;  // GPIO header (8.5) plus Dupont plugs with a wire bend (15)

tray_l = bb_l + 2 * (bb_fit + tray_wall);
tray_w = bb_w + 2 * (bb_fit + tray_wall);
inner_l = max(pi_l, tray_l);
plate_l = inner_l + 2 * margin;
plate_w = pi_w + bb_gap + tray_w + 2 * margin;

pi_x = margin + (inner_l - pi_l) / 2;
pi_y = margin;
tray_x = margin + (inner_l - tray_l) / 2;
tray_y = margin + pi_w + bb_gap;

top_of_pi = plate_t + standoff_h + pi_pcb + max(pi_ports_h, pi_jumpers_h);

echo(str("Plate ", plate_l, " x ", plate_w, " x ", plate_t, " mm (",
         round(plate_l / 25.4 * 10) / 10, " x ", round(plate_w / 25.4 * 10) / 10, " in)"));
echo(str("Tallest part above the floor about ", top_of_pi, " mm; clearance under the lid ",
         inside_h - top_of_pi, " mm"));
if (inside_h - top_of_pi < 5) echo("WARNING: under 5 mm above the Pi's jumpers; lower standoff_h or use right-angle plugs");

module screw_hole() {
    translate([0, 0, -1]) cylinder(d = screw_d, h = plate_t + 2);
    translate([0, 0, plate_t - (screw_head_d - screw_d) / 2 + eps])
        cylinder(d1 = screw_d, d2 = screw_head_d, h = (screw_head_d - screw_d) / 2);
}

module plate() {
    linear_extrude(plate_t)
        offset(4) offset(-4) square([plate_l, plate_w]);
}

module standoffs() {
    for (h = pi_holes)
        translate([pi_x + h[0], pi_y + h[1], plate_t - eps])
            cylinder(d = standoff_d, h = standoff_h + eps);
}

module standoff_holes() {
    for (h = pi_holes)
        translate([pi_x + h[0], pi_y + h[1], 1])
            cylinder(d = standoff_hole, h = plate_t + standoff_h);
}

module tray() {
    translate([tray_x, tray_y, plate_t - eps])
        difference() {
            cube([tray_l, tray_w, tray_h + eps]);
            translate([tray_wall, tray_wall, -1])
                cube([tray_l - 2 * tray_wall, tray_w - 2 * tray_wall, tray_h + 2]);
        }
}

module floor_screws() {
    for (x = [margin / 2 + 1, plate_l - margin / 2 - 1], y = [margin / 2 + 1, plate_w - margin / 2 - 1])
        translate([x, y, 0]) screw_hole();
}

// The breadboard's adhesive sticks to the plywood through its window, and the
// rim around each standoff joins the plate's frame, so nothing floats free.
module windows() {
    translate([0, 0, -1]) linear_extrude(plate_t + 2) {
        difference() {
            translate([pi_x + 2, pi_y + 2]) offset(2) offset(-2) square([pi_l - 4, pi_w - 4]);
            for (h = pi_holes)
                translate([pi_x + h[0], pi_y + h[1]]) circle(d = standoff_d + 2 * window_keep);
        }
        inset = tray_wall + window_keep;
        translate([tray_x + inset, tray_y + inset])
            offset(2) offset(-2) square([tray_l - 2 * inset, tray_w - 2 * inset]);
    }
}

module base_plate() {
    difference() {
        union() {
            plate();
            standoffs();
            tray();
        }
        standoff_holes();
        floor_screws();
        if (windows) windows();
    }
}

base_plate();
