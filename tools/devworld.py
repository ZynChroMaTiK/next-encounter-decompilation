#!/usr/bin/env python3
"""
List the polygons of a Serious Editor world from dev/, for comparing the
designers' own geometry with what is rebuilt from the disc
(docs/world-conversion.md, "Polygons").

`WldWriter --dump` does the reading, with the engine's own brush loader. Two
things stand in its way, and this script handles both and cleans up after:

- The engine opens files under its game root only, so the world is copied to
  `pc/engine/SamTSE/Temp/devworld/` for the run.
- Loading a world loads every texture it names, and NE's are not installed.
  Missing ones would all become the editor default and lose their names, so a
  replacement table (`Data/BaseForReplacingFiles.txt`, the engine's own
  mechanism) stands a distinct installed texture in for each, and the dump is
  mapped back to the real names afterwards. The names come from the world's
  filename dictionary (`DICT`, then `DFNM` + u32 length + chars per name).

    python tools/devworld.py dev/Models/items/Level/levelRlevel1_2_Waterfall/waterfallArea.wld

writes build/dev/wld/<name>.txt (format: WldWriter.cpp, DumpBrushes).
"""

from __future__ import annotations

import argparse
import shutil
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GAME = ROOT / "pc" / "engine" / "SamTSE"
BS = chr(92)


def texture_names(data: bytes):
    pos = data.index(b"DICT")
    count = struct.unpack_from("<I", data, pos + 4)[0]
    o, names = pos + 8, []
    for _ in range(count):
        if data[o:o + 4] != b"DFNM":
            raise ValueError("bad filename dictionary at %d" % o)
        n = struct.unpack_from("<I", data, o + 4)[0]
        names.append(data[o + 8:o + 8 + n].decode("latin-1"))
        o += 8 + n
    return [n for n in names if n.lower().endswith(".tex")]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("world", type=Path)
    ap.add_argument("-o", "--output", type=Path, default=ROOT / "build" / "dev" / "wld")
    args = ap.parse_args(argv)

    data = args.world.read_bytes()
    names = texture_names(data)
    with zipfile.ZipFile(GAME / "SE1_00.gro") as z:
        stand = sorted(n for n in z.namelist()
                       if n.lower().endswith(".tex") and n.startswith("Textures/"))
    if len(stand) < len(names):
        sys.exit("not enough installed textures to stand in")
    table = {s.replace("/", BS): n for n, s in zip(names, stand)}

    temp = GAME / "Temp" / "devworld"
    base = GAME / "Data" / "BaseForReplacingFiles.txt"
    if base.exists():
        sys.exit("%s already exists; not touching it" % base)
    args.output.mkdir(parents=True, exist_ok=True)
    out = args.output / (args.world.stem + ".txt")
    try:
        temp.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.world, temp / args.world.name)
        base.write_text("".join('"%s" "%s"\n' % (n, s) for s, n in table.items()))
        rel = BS.join(["Temp", "devworld", args.world.name])
        subprocess.run([str(GAME / "Bin" / "WldWriter.exe"), "--dump", rel, str(out)],
                       cwd=GAME / "Bin", check=True)
    finally:
        base.unlink(missing_ok=True)
        shutil.rmtree(temp, ignore_errors=True)

    lines = out.read_text().splitlines()
    fixed = []
    for line in lines:
        if line.startswith("poly "):
            t = line.split(" ")
            t[13:16] = [table.get(x, x) for x in t[13:16]]
            line = " ".join(t)
        elif line.startswith("layer "):
            t = line.split(" ")
            t[12] = table.get(t[12], t[12])
            line = " ".join(t)
        fixed.append(line)
    out.write_text("\n".join(fixed) + "\n")
    print("%s: %d texture names, %d polygons -> %s" % (
        args.world.name, len(names), sum(l.startswith("poly ") for l in fixed), out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
