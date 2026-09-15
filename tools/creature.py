#!/usr/bin/env python3
"""
NE's creatures as Serious Engine 1 models: every animation baked into
vertex frames, the way an SE1 `.mdl` animates.

For a model named in LevelModels.cfg (`DumDumlarge`, `monkeyman`...) this
writes, under the game root's `Models/NextEncounter/Enemies/<name>/`:

    <name>.scr            the modeler script: mip model, one animation per NE clip
    Frames/<clip>_NNN.obj one OBJ per frame, every frame the same vertices and faces
    <name>.tga            the texture (WldWriter --tex makes the .tex)

and `WldWriter --mdl <name>.scr <name>.mdl` builds the model with the engine's
own `CEditModel::LoadFromScript_t`, as Serious Modeler does.

The model and its animation come from the disc (tools/objmodel.py,
docs/image-format.md, "Object models" and "Animation"): each frame is one
skinning matrix per bone, applied to the bind pose on the CPU skin groups or
through a GPU list's matrix palette. NE plays a clip at 25 frames a second:
IEngResAnimator_QueueClip (0x80127e68) sets its length to frames / 25.0
(0x8021d5a8), so every SE1 frame lasts 0.04 s.

Axes. The OBJ is written the way the designers' own Maya exports are
(dev/Models/enemies/China/minion/MonkeyMan/MonkeyMan.obj beside the .mdl the
modeler built from it): NE's model space with x negated, faces wound outward.
NE's creature is 2.5 times the size of that export, 1.7 units tall for the
MonkeyMan, which is the size it has in NE's levels, so SIZE stays 1.0.

    python tools/creature.py export DumDumlarge
    python tools/creature.py check DumDumlarge   # after WldWriter --mdl
    python tools/creature.py list
"""

from __future__ import annotations

import argparse
import json
import math
import re
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ssg import Container                                        # noqa: E402
from level import _parse_prims, _to_triangles                   # noqa: E402
from gxtex import decode                                         # noqa: E402
import objmodel                                                  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
GAME = ROOT / "pc" / "engine" / "SamTSE"
MODELS = Path("Models") / "NextEncounter" / "Enemies"
INDEX = ROOT / "build" / "objmodels" / "index.json"
SECONDS_PER_FRAME = 1.0 / 25.0          # IEngResAnimator_QueueClip: frames / 25.0


def find_model(name):
    """(Models, model) for the first container that carries `name`."""
    index = json.loads(INDEX.read_text())
    wanted = name.lower()
    for label, info in index["models"].items():
        if info["name"].lower() != wanted:
            continue
        path = ROOT / "orig" / "files" / "Levels" / (info["from"] + ".ssw")
        if not path.exists():
            path = next((ROOT / "orig" / "files").rglob(info["from"] + ".ss?"), None)
        M = objmodel.Models(Container(path), path.stem)
        for m in M.models:
            if m["name"].lower() == wanted and m["kind"]:
                return M, m
    raise SystemExit("no animated model named %s on the disc" % name)


def topology(M, m):
    """The model's drawn vertices and faces, fixed for every frame.

    Returns (keys, uvs, faces): keys[i] is ("cpu", position index) for a
    vertex of a type-5 list, posed through the CPU skin groups, or
    ("gpu", bone, position index) for one of a type-0 list, posed by its
    palette's bone; faces are (material, (vertex, uv), (vertex, uv), (vertex, uv))."""
    img = M.img
    keys, key_index, uvs, uv_index, faces = [], {}, [], {}, []
    maps = M.slot_maps(m) if any(x["lists"][0] for x in m["meshes"]) else {}

    def vertex(key):
        if key not in key_index:
            key_index[key] = len(keys)
            keys.append(key)
        return key_index[key]

    def uv(i):
        if i not in uv_index:
            uv_index[i] = len(uvs)
            uvs.append(m["uv"][i] if i < len(m["uv"]) else (0.0, 0.0))
        return uv_index[i]

    for mi, mesh in enumerate(m["meshes"]):
        if mesh["lists"][0]:
            work = [(lst, maps.get(lst[2], {}), 0) for lst in mesh["lists"][0]]
        else:
            work = [(lst, None, 5) for lst in mesh["lists"][5]]
        for (p, used, _o), slots, vt in work:
            stride, fm, fp, _fn, fu = objmodel.LAYOUT[vt]
            for op, cnt, bs in _parse_prims(img, p, used, stride) or []:
                corners = []
                for k in range(cnt):
                    pi = objmodel.u16(img, bs + stride * k + fp)
                    ui = objmodel.u16(img, bs + stride * k + fu)
                    if vt == 0:
                        key = ("gpu", slots.get(img[bs + stride * k + fm]), pi)
                    else:
                        key = ("cpu", pi)
                    corners.append((vertex(key), uv(ui)))
                for a, b, c in _to_triangles(op, list(range(cnt))):
                    faces.append((mi, corners[a], corners[b], corners[c]))
    return keys, uvs, faces


def pose(M, m, keys, mats):
    """Positions of `keys` with one 3x4 matrix per bone."""
    P = m["pos"]
    cpu = {}
    for bi, v, w, p in M.skin(m):
        q = objmodel._apply(mats[bi], p)
        a = cpu.setdefault(v, [0.0, 0.0, 0.0])
        for i in range(3):
            a[i] += w * q[i]
    out = []
    for key in keys:
        if key[0] == "cpu":
            out.append(tuple(cpu[key[1]]) if key[1] in cpu else P[key[1]])
        else:
            bone = key[1]
            out.append(objmodel._apply(mats[bone] if bone is not None else objmodel.IDENTITY, P[key[2]]))
    return out


def clip_name(name):
    """An SE1 animation name from an NE clip name: 'recoilback1Source' -> RECOILBACK1."""
    base = re.sub(r"source$", "", name, flags=re.I)
    return re.sub(r"[^A-Za-z0-9]", "_", base).upper() or "CLIP"


def write_obj(path, verts, uvs, faces, materials):
    """One frame in the designers' OBJ convention: x negated, faces wound the
    other way round, so they still face outward."""
    lines = ["# NE creature frame (tools/creature.py)"]
    lines += ["v %.6f %.6f %.6f" % (-x, y, z) for x, y, z in verts]
    # The designers' vt is 1 - NE's v (MonkeyMan: 0.755 against 0.245 on its
    # top vertex); some creatures keep v in -1..0, so -v is shifted by a whole
    # number into 0..1 rather than taken from 1.
    shift = -math.floor(min(-v for _u, v in uvs)) if uvs else 0
    lines += ["vt %.6f %.6f" % (u, shift - v) for u, v in uvs]
    current = None
    for mi, a, b, c in faces:
        if mi != current:
            lines.append("usemtl %s" % materials[mi])
            current = mi
        lines.append("f %d/%d %d/%d %d/%d" % (c[0] + 1, c[1] + 1, b[0] + 1, b[1] + 1, a[0] + 1, a[1] + 1))
    path.write_text("\n".join(lines) + "\n")


def write_tga(path, width, height, rgba):
    """Uncompressed 32-bit TGA, top-left origin."""
    header = struct.pack("<BBBHHBHHHHBB", 0, 0, 2, 0, 0, 0, 0, 0, width, height, 32, 0x28)
    px = bytearray(len(rgba))
    px[0::4], px[1::4], px[2::4], px[3::4] = rgba[2::4], rgba[1::4], rgba[0::4], rgba[3::4]
    path.write_bytes(header + bytes(px))


def export(name):
    M, m = find_model(name)
    pool = M.key_pool(m)
    if pool is None:
        raise SystemExit("%s: no key pool" % name)
    keys, uvs, faces = topology(M, m)
    textures = sorted({mesh["texture"] for mesh in m["meshes"] if mesh["texture"] is not None})
    if len(textures) > 1:
        raise SystemExit("%s has %d textures; an SE1 model takes one (not packed yet)" % (m["name"], len(textures)))
    materials = [re.sub(r"[^A-Za-z0-9_]", "_", mesh["texture_name"] or "Default") for mesh in m["meshes"]]
    label = re.sub(r"[^A-Za-z0-9_]", "_", m["name"])
    rel = MODELS / label
    dest = GAME / rel
    (dest / "Frames").mkdir(parents=True, exist_ok=True)

    bind = pose(M, m, keys, [objmodel.IDENTITY] * len(m["bones"]))
    write_obj(dest / "Frames" / "bind.obj", bind, uvs, faces, materials)
    clips, used = [], set()
    for a in m["anims"]:
        cn = clip_name(a["name"])
        while cn in used:
            cn += "_"
        used.add(cn)
        files = []
        for fr in range(a["frames"]):
            mats = [M.key_matrix(pool, k) for k in M.frame_keys(m, a, fr)]
            fn = "%s_%03d.obj" % (cn.lower(), fr)
            write_obj(dest / "Frames" / fn, pose(M, m, keys, mats), uvs, faces, materials)
            files.append(fn)
        clips.append((cn, a["name"], files))

    tex = None
    if textures:
        t = M.texs[textures[0]]
        tex = label + ".tga"
        write_tga(dest / tex, t.width, t.height, bytes(decode(M.img, t.pixels, t.width, t.height, t.gx)))

    d = str(rel).replace("/", "\\") + "\\"
    scr = [";******* NE creature %s from %s (tools/creature.py)" % (m["name"], M.stem),
           "TEXTURE_DIM 2.0 2.0", "SIZE 1.0", "MAX_SHADOW 0", "HI_QUALITY YES",
           "FLAT NO", "HALF_FLAT NO", "STRETCH_DETAIL NO", "",
           "DIRECTORY %sFrames\\" % d, "MIP_MODELS 1", "    bind.obj", "", "ANIM_START",
           # the animation block reads its own DIRECTORY, as the designers' scripts repeat it
           "DIRECTORY %sFrames\\" % d]
    for cn, _ne, files in clips:
        scr += ["", "ANIMATION %s" % cn, "SPEED %g" % SECONDS_PER_FRAME] + ["    " + f for f in files]
    scr += ["", "ANIM_END", "", "END", ""]
    (dest / (label + ".scr")).write_text("\r\n".join(scr))
    info = {"name": m["name"], "from": M.stem, "vertices": len(keys), "faces": len(faces),
            "uvs": len(uvs), "bones": len(m["bones"]), "texture": tex,
            "script": str(rel / (label + ".scr")), "model": str(rel / (label + ".mdl")),
            "animations": [{"se1": cn, "ne": ne, "frames": len(files)} for cn, ne, files in clips]}
    (dest / (label + ".json")).write_text(json.dumps(info, indent=1))
    print("%s from %s: %d vertices, %d faces, %d animations, %d frames -> %s"
          % (m["name"], M.stem, len(keys), len(faces), len(clips), sum(len(f) for _c, _n, f in clips), dest))
    return info


def check_mapping(obj_path, dump_path, mex_w=2048, mex_h=2048):
    """(polygons, unmatched, wrong, worst mex) of a built model against its OBJ.

    The dump is `WldWriter --mdlinfo`'s: each mip-0 polygon's corners as
    (vertex, U, V) in mex. OBJ faces are fanned into triangles as the importer
    fans them, and each polygon is matched to the triangle with the same three
    vertices (the model keeps the OBJ's vertex order). A corner's vt (u, v) is
    (u * W, -v * H) mex, W and H the script's TEXTURE_DIM x 1024. A polygon is
    wrong when a corner is more than 2 mex off. The modeler's UV seam bug
    (docs/enemies.md, "UV seams") showed as 187 wrong polygons of 1,070 on
    DumDumlarge before the fix."""
    vts, by_verts = [], {}
    for line in Path(obj_path).read_text().splitlines():
        t = line.split()
        if not t:
            continue
        if t[0] == "vt":
            vts.append((float(t[1]), float(t[2])))
        elif t[0] == "f":
            c = [tuple(int(x) - 1 for x in s.split("/")[:2]) for s in t[1:]]
            for i in range(1, len(c) - 1):
                tri = (c[0], c[i], c[i + 1])
                by_verts.setdefault(frozenset(v for v, _t in tri), []).append({v: vts[t] for v, t in tri})
    polys = unmatched = wrong = worst = 0
    for line in Path(dump_path).read_text().splitlines():
        if not line.startswith("poly"):
            continue
        t = line.split()
        corners = [tuple(int(x) for x in t[i:i + 3]) for i in range(4, len(t), 3)]
        polys += 1
        cands = by_verts.get(frozenset(c[0] for c in corners))
        if not cands:
            unmatched += 1
            continue
        best = min(max(max(abs(round(uv[v][0] * mex_w) - u), abs(round(-uv[v][1] * mex_h) - vv))
                       for v, u, vv in corners) for uv in cands)
        worst = max(worst, best)
        wrong += best > 2
    return polys, unmatched, wrong, worst


def check(name):
    """Dump the built model with WldWriter --mdlinfo and check its mapping."""
    import subprocess
    label = re.sub(r"[^A-Za-z0-9_]", "_", find_model(name)[1]["name"])
    rel = str(MODELS / label / (label + ".mdl")).replace("/", "\\")
    dump = ROOT / "build" / "creatures" / (label + "_mdlinfo.txt")
    dump.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(GAME / "Bin" / "WldWriter.exe"), "--mdlinfo", rel, str(dump)],
                   cwd=GAME / "Bin", check=True, capture_output=True)
    polys, unmatched, wrong, worst = check_mapping(GAME / MODELS / label / "Frames" / "bind.obj", dump)
    print("%s: %d polygons, %d unmatched, %d with a corner UV more than 2 mex off (worst %d)"
          % (label, polys, unmatched, wrong, worst))
    return 1 if unmatched or wrong else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("export", help="frames, texture and modeler script for creatures")
    p.add_argument("names", nargs="+")
    p = sub.add_parser("check", help="a built model's corner UVs against its bind-pose OBJ")
    p.add_argument("names", nargs="+")
    sub.add_parser("list", help="animated models on the disc")
    args = ap.parse_args(argv)
    if args.cmd == "list":
        index = json.loads(INDEX.read_text())
        for n in sorted({i["name"] for i in index["models"].values() if i["kind"]}, key=str.lower):
            print(n)
        return 0
    if args.cmd == "check":
        return max(check(n) for n in args.names)
    for n in args.names:
        export(n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
