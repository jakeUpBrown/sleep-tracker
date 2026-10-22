// Sunrise sconce: wall enclosure for the Pi, LED strip and diffuser.
//
// Hangs on the wall beside the headboard, above your pillow. The 1 m strip is
// cut into rows on a white light box behind a diffuser. In front of the
// diffuser a louver of parallel fins lets light out only within a band of
// angles: aimed at your pillow, with your partner's pillow outside it. The
// Pi and a Perma-Proto HAT sit behind the light box.
//
// Open in OpenSCAD, set the values in the Customizer panel, pick a `part`,
// render (F6) and export an STL. `part = "assembly"` shows everything
// exploded. Units are millimetres. See README.md in this folder.

/* [Part] */
part = "assembly"; // [assembly, housing, wall_plate, light_box, louver, diffuser, coupons]
// Gap between parts in the assembly view
explode = 30; // [0:5:80]

/* [LED strip] */
// Number of rows the strip is cut into
rows = 5; // [1:8]
// LEDs per row (rows x leds_per_row = 60 for the whole strip)
leds_per_row = 12; // [6:30]
// LED spacing along the strip (60 LEDs/m)
led_pitch = 16.667;
// Strip width, measured (Adafruit 60/m RGBW: 12.6 with its silicone sleeve)
strip_w = 12.6;
// Centre-to-centre distance between rows
row_pitch = 20;
// Room at each row end for the wire jumpers between rows
row_end_room = 9;

/* [Light and louver] */
// LED face to the back of the diffuser. More is smoother, less is brighter
diffuse_gap = 15;
// Diffuser thickness (printed white, or 2-3 mm opal acrylic)
diffuser_t = 1.2;
// Which way the fins steer the light. On the headboard wall your head and
// your partner's are side by side, so lean upright fins toward the bed
// (bed_right if the bed is to the sconce's right). On a side wall, use down.
louver_dir = "bed_right"; // [bed_right, bed_left, down, none]
// Fin lean: from straight out from the wall toward the bed, or below horizontal for down
louver_angle = 50; // [0:1:60]
// Fin depth, front to back
louver_depth = 17;
// Distance between fins. Smaller = tighter band, less light
louver_pitch = 20;
// Fin thickness
slat_t = 1.2;

/* [Box] */
// Depth for the Pi with a Perma-Proto HAT on it
elec_depth = 36;
wall = 2.4;
plate_t = 3;
// Front lip around the louver
lip = 6;
// Fit clearance between nested parts
fit = 0.4;
// Self-tapping hole for M3 screws (housing to wall plate)
m3_tap = 2.8;
m3_clear = 3.4;
// Self-tapping hole for M2.5 screws (Pi standoffs)
m25_tap = 2.2;
// Wall screws: shank and head diameter
wall_screw = 4.5;
wall_screw_head = 9;
// Power cord notch in the bottom wall, x from the centre (- is left)
cord_x = 45;
cord_w = 7;

/* [Hidden] */
$fn = 40;
eps = 0.01;

// ---------------------------------------------------------------- sizes
led_len   = leds_per_row * led_pitch;
// light box inside (x = along the wall, z = up)
lb_in_w   = led_len + 2 * row_end_room;
lb_in_h   = (rows - 1) * row_pitch + strip_w + 2 * 6;
lb_out_w  = lb_in_w + 2 * wall;
lb_out_h  = lb_in_h + 2 * wall;
// housing inside = light box outside + fit
h_in_w    = lb_out_w + fit;
h_in_h    = lb_out_h + fit;
h_out_w   = h_in_w + 2 * wall;
h_out_h   = h_in_h + 2 * wall;
// front window
win_w     = h_in_w - 2 * (lip - wall);
win_h     = h_in_h - 2 * (lip - wall);
// depth stack, front to back: lip, louver, diffuser, light box, Pi, plate
lb_depth  = wall + diffuse_gap;          // light box plate + walls
h_depth   = wall + louver_depth + diffuser_t + lb_depth + elec_depth + plate_t;
// plate sits inside the housing back
pl_w      = h_in_w - fit;
pl_h      = h_in_h - fit;

// Pi 3B+ (board 85 x 56, holes 58 x 49 from the SD-card corner)
pi_w = 85; pi_h = 56;
pi_holes = [[3.5, 3.5], [61.5, 3.5], [3.5, 52.5], [61.5, 52.5]];
// Pi position on the plate: lower-left corner of the board, SD card edge left,
// GPIO header along the top, micro-USB along the bottom edge. The wall screws
// sit above it
pi_x = -pl_w / 2 + 8;
pi_z = -pl_h / 2 + 30;   // leaves room below for a right-angle micro-USB plug
standoff_h = 6;

// tabs that take the housing screws, at top and bottom of the plate
tab_x  = [-(pl_w / 2 - 34), pl_w / 2 - 34];
tab_d  = 10;   // how far the tab reaches forward from the plate
tab_y0 = 6;    // tab hole distance in front of the plate

echo(str("Outside size: ", h_out_w, " wide x ", h_out_h, " tall x ", h_depth, " deep"));
echo(str("LEDs used: ", rows * leds_per_row, " of 60"));
// Light at angle a gets through parallel fins only while
// |tan(a) - tan(louver_angle)| < pitch / depth, fully at louver_angle and
// falling to nothing at the band edges.
louver_k = louver_pitch / louver_depth;
if (louver_dir != "none")
    echo(str("Louver passes light between ",
             round(atan(tan(louver_angle) - louver_k)), " and ",
             round(atan(tan(louver_angle) + louver_k)), " degrees ",
             louver_dir == "down" ? "below horizontal" : "toward the bed from straight out"));
if (louver_dir != "none" && louver_angle > 50)
    echo("WARNING: fins leaning more than 50 deg overhang too far to print face-down");
assert(rows * leds_per_row <= 60, "more LEDs than the strip has");

// ---------------------------------------------------------------- helpers
// Coordinates: x along the wall (+ toward the headboard), y out from the
// wall (wall at y = 0), z up. Each part is modelled in place, and
// print_* modules lay it flat for printing.

module box(w, d, h, y0 = 0) {   // centred in x and z, from y0 to y0 + d
    translate([-w / 2, y0, -h / 2]) cube([w, d, h]);
}

module hole_y(d, y0, len, x, z) {   // hole along y
    translate([x, y0, z]) rotate([-90, 0, 0]) cylinder(d = d, h = len);
}

// ---------------------------------------------------------------- wall plate
// Screwed to the wall. Carries the Pi, the posts the light box rests on, the
// tabs the housing screws into and zip-tie loops for the power cord.
module wall_plate() {
    difference() {
        union() {
            box(pl_w, plate_t, pl_h);
            // Pi standoffs
            for (p = pi_holes)
                translate([pi_x + p[0], plate_t - eps, pi_z + p[1]])
                    rotate([-90, 0, 0]) cylinder(d = 6, h = standoff_h);
            // corner posts the light box plate rests on
            for (sx = [-1, 1], sz = [-1, 1])
                translate([sx * (pl_w / 2 - 5) - 4, plate_t - eps, sz * (pl_h / 2 - 5) - 4])
                    cube([8, elec_depth + eps, 8]);
            // housing screw tabs
            for (x = tab_x, sz = [-1, 1])
                translate([x - 6, plate_t - eps, sz > 0 ? pl_h / 2 - 9 : -pl_h / 2])
                    cube([12, tab_d + eps, 9]);
            // zip-tie loops beside the cord notch
            for (dx = [-14, 14])
                translate([cord_x + dx - 3, plate_t - eps, -pl_h / 2 + 12])
                    difference() {
                        cube([6, 6, 10]);
                        translate([-1, 0, 2.5]) cube([8, 3.5, 5]);
                    }
        }
        // Pi screw holes
        for (p = pi_holes)
            hole_y(m25_tap, plate_t, standoff_h + 1, pi_x + p[0], pi_z + p[1]);
        // housing screw holes, vertical, through the tabs
        for (x = tab_x, sz = [-1, 1])
            translate([x, plate_t + tab_y0, sz * pl_h / 2])
                cylinder(d = m3_tap, h = 22, center = true);
        // wall screw slots near the top, countersunk on the front
        for (x = [-1, 1] * (pl_w / 2 - 60))
            translate([x, 0, pl_h / 2 - 14]) {
                hull() for (dx = [-3, 3])
                    hole_y(wall_screw, -1, plate_t + 2, dx, 0);
                hull() for (dx = [-3, 3])
                    translate([dx, plate_t - 2.2, 0]) rotate([-90, 0, 0])
                        cylinder(d1 = wall_screw, d2 = wall_screw_head, h = 2.2 + eps);
            }
    }
}

// ---------------------------------------------------------------- light box
// White open box: LED rows on its plate, the diffuser rests on its rim.
// Sits on the wall plate's corner posts.
module light_box() {
    y0 = plate_t + elec_depth;
    difference() {
        box(lb_out_w, lb_depth, lb_out_h, y0);
        box(lb_in_w, lb_depth, lb_in_h, y0 + wall);
        // shallow grooves that line up the strip rows
        for (r = [0 : rows - 1])
            translate([0, 0, row_z(r)])
                box(led_len + 4, 0.6 + eps, strip_w + 0.6, y0 + wall - 0.6);
        // wire hole to the electronics, at the start of the top row
        translate([-led_len / 2 - row_end_room / 2, y0 - 1, row_z(0)])
            rotate([-90, 0, 0]) cylinder(d = 8, h = wall + 2);
    }
}
function row_z(r) = ((rows - 1) / 2 - r) * row_pitch;   // row 0 is the top

// ---------------------------------------------------------------- diffuser
module diffuser() {
    y0 = plate_t + elec_depth + lb_depth;
    box(h_in_w - fit, diffuser_t, h_in_h - fit, y0);
}

// ---------------------------------------------------------------- louver
// Frame with parallel fins leaning by louver_angle, toward the bed or down.
// Light passes only within a band of angles around louver_angle (the console
// prints the band). Print it in black or another dark colour so the fins
// absorb stray light instead of glowing. Also holds the diffuser in place, so
// it is needed even with louver_dir = "none".
module louver() {
    y0 = plate_t + elec_depth + lb_depth + diffuser_t;
    w = h_in_w - fit;
    h = h_in_h - fit;
    lean = louver_depth * tan(louver_angle);   // how far a fin moves, back to front
    difference() {
        box(w, louver_depth, h, y0);
        box(w - 2 * (lip - wall), louver_depth + 2, h - 2 * (lip - wall), y0 - 1);
        // "this way up" notch, top edge at the back: upside down the fins
        // would lean the wrong way
        translate([0, 0, h / 2 - 1.5]) box(6, 2, 3 + eps, y0 - eps);
    }
    if (louver_dir == "down")
        intersection() {
            box(w, louver_depth, h, y0);
            for (z = [-h / 2 : louver_pitch : h / 2 + lean])
                // side view: from (y0, z) at the back down to (y0 + depth, z - lean)
                translate([-w / 2, 0, 0]) rotate([90, 0, 90]) linear_extrude(w)
                    polygon([[y0, z], [y0 + louver_depth, z - lean],
                             [y0 + louver_depth, z - lean + slat_t], [y0, z + slat_t]]);
        }
    else if (louver_dir != "none") {
        s = louver_dir == "bed_right" ? 1 : -1;
        intersection() {
            box(w, louver_depth, h, y0);
            for (x = [-w / 2 - lean : louver_pitch : w / 2])
                // top view: from (x, y0) at the back over to (x + lean, y0 + depth)
                translate([0, 0, -h / 2]) linear_extrude(h)
                    polygon([[s * x, y0], [s * (x + lean), y0 + louver_depth],
                             [s * (x + lean + slat_t), y0 + louver_depth], [s * (x + slat_t), y0]]);
        }
    }
}

// ---------------------------------------------------------------- housing
// The visible shell. Slides over the wall plate, clamps the louver,
// diffuser and light box against the plate posts, and is held by four M3
// screws through its top and bottom into the plate tabs.
module housing() {
    difference() {
        box(h_out_w, h_depth, h_out_h);
        // inside, open at the back
        box(h_in_w, h_depth - wall + eps, h_in_h, -eps);
        // front window
        box(win_w, wall + 2, win_h, h_depth - wall - 1);
        // screw holes into the plate tabs
        for (x = tab_x, sz = [-1, 1])
            translate([x, plate_t + tab_y0, sz * (h_in_h / 2 + wall / 2)])
                cylinder(d = m3_clear, h = wall + 2, center = true);
        // power cord notch at the back of the bottom wall
        translate([cord_x - cord_w / 2, -eps, -h_out_h / 2 - 1])
            cube([cord_w, 8, wall + 2]);
        // vent slots, top and bottom, behind the light box
        for (sz = [-1, 1], i = [-3 : 3])
            translate([i * 14 - 26, plate_t + 6, sz * (h_in_h / 2 + wall / 2)])
                hull() for (dy = [0, elec_depth - 14])
                    translate([0, dy, 0]) cylinder(d = 4, h = wall + 2, center = true);
    }
}

// ---------------------------------------------------------------- coupons
// Diffuser test tiles: hold each over the lit strip to pick a thickness.
// Tile n has n notches.
coupon_t = [0.6, 0.8, 1.0, 1.2, 1.6];
module coupons() {
    for (i = [0 : len(coupon_t) - 1])
        translate([i * 34, 0, 0]) difference() {
            cube([30, 30, coupon_t[i]]);
            for (n = [0 : i])
                translate([3 + n * 5, -eps, -1]) cube([2, 3, 4]);
        }
}

// ---------------------------------------------------------------- print poses
// Each part laid flat in its best print orientation.
module flat_from_back() { rotate([90, 0, 0]) children(); }        // back on the bed
module flat_from_front(d) { translate([0, 0, d]) rotate([-90, 0, 0]) children(); }

module print_part(p) {
    if (p == "wall_plate")
        flat_from_back() wall_plate();
    else if (p == "light_box")
        flat_from_back() translate([0, -(plate_t + elec_depth), 0]) light_box();
    else if (p == "diffuser")
        flat_from_back() translate([0, -(plate_t + elec_depth + lb_depth), 0]) diffuser();
    else if (p == "louver")   // front face down, so the fins lean louver_angle from upright
        flat_from_front(louver_depth)
            translate([0, -(plate_t + elec_depth + lb_depth + diffuser_t), 0]) louver();
    else if (p == "housing")  // front face down
        flat_from_front(h_depth) housing();
    else if (p == "coupons")
        coupons();
}

module assembly() {
    color("Gainsboro") wall_plate();
    color("ForestGreen") translate([pi_x + pi_w / 2, plate_t + standoff_h, pi_z + pi_h / 2])
        box(pi_w, 1.4, pi_h);   // Pi board stand-in
    color("White") translate([0, explode, 0]) light_box();
    color("Ivory", 0.8) translate([0, 2 * explode, 0]) diffuser();
    color("DimGray") translate([0, 3 * explode, 0]) louver();
    color("Peru", 0.5) translate([0, 4 * explode, 0]) housing();
}

if (part == "assembly") assembly();
else print_part(part);
