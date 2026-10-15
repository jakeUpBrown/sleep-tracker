#!/usr/bin/env python3
"""Rebuild pilog_install.js from the readable watch sources in src/.

Run this after editing anything in watch/src/, then commit both:
  python3 watch/build_installer.py
"""
import json
from pathlib import Path

HERE = Path(__file__).parent
VERSION = "0.3"  # bump when the watch app changes
FILES = ["pilog.boot.js", "pilog.app.js", "pilog.wid.js"]
INFO = {"id": "pilog", "name": "Pi Sleep Log", "src": "pilog.app.js", "icon": "pilog.img", "type": "app", "version": VERSION}

HEADER = """// Pi Sleep Log installer. Paste into the right-hand side of the Espruino Web IDE,
// connect to the watch, and press the Upload button (it can go to RAM; it writes the files itself).
(function(){
  var S=require("Storage");
"""
FOOTER = """  var gi=Graphics.createArrayBuffer(48,48,1,{msb:true});
  gi.setColor(1).setFont("Vector",28).setFontAlign(0,0).drawString("Zz",24,26);
  S.write("pilog.img",gi.asImage("string"));
  if(!S.read("pilog.json"))S.writeJSON("pilog.json",{on:false});
  print("Pi Sleep Log installed. Rebuilding boot code...");
  try{eval(S.read("bootupdate.js"));}catch(e){print("bootupdate:",e);}
  setTimeout(function(){load("pilog.app.js");},1500);
})();
"""


def main() -> None:
    lines = [HEADER]
    for name in FILES:
        lines.append(f"  S.write({json.dumps(name)},{json.dumps((HERE / 'src' / name).read_text())});\n")
    lines.append(f'  S.writeJSON("pilog.info",{json.dumps(INFO)});\n')
    lines.append(FOOTER)
    out = HERE / "pilog_install.js"
    out.write_text("".join(lines))
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
