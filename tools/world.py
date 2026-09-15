#!/usr/bin/env python3
"""
Stage 1 of the Next Encounter -> Serious Engine 1 world conversion.

Emits a converter-ready **world description** per level: everything an SE1
world needs that an OBJ cannot carry -- which geometry forms which sector and
with what properties, which geometry becomes a separate brush entity, every
light as an SE1 `Light` property set, and the force fields. Geometry itself
stays in the OBJ that `tools/level.py mesh` writes; this file refers to its
groups (`mat12`, `sector7`) by name.

The division of labour is deliberate. This stage records raw NE data plus the
*mapping decisions*. Every convention-sensitive conversion -- a 3x4 matrix to
an SE1 placement, a direction vector to heading/pitch -- is left to the Stage 2
writer, which calls SE1's own `DecomposeRotationMatrixNoSnap` and
`DirectionVectorToAngles`. Nothing here re-implements an SE1 convention, so
nothing here can silently disagree with one.

Method, field-level mappings and the evidence behind each are in
docs/world-conversion.md.

Usage:
    python tools/world.py describe orig/files/Levels/Rlevel1_1.ssw -o build/world/
    python tools/world.py render   orig/files/Levels/Rlevel1_1.ssw -o build/world/
    python tools/world.py verify   orig/files/Levels
"""

from __future__ import annotations

import argparse
import json
import math
import re
import struct
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ssg import Container                                        # noqa: E402
from level import (batches, lights, materials, sectors, positions,  # noqa: E402
                   apply_xform, world_triangles, F_POS, SEC_XFORM)
from entity import entities, _fill_tri, _decode                  # noqa: E402
from gxtex import walk as texwalk, write_png                     # noqa: E402
from bsp import region_cells                                     # noqa: E402
from model import Level as ModelLevel                            # noqa: E402
import space                                                     # noqa: E402
from collision import bouncer_volumes, brush_meshes, touch_volumes                              # noqa: E402

# SE1 sector content types, from EntitiesMP/WorldBase.es.
AIR, WATER, LAVA = 0, 1, 2
CONTENT_NAME = {AIR: "Air", WATER: "Water", LAVA: "Lava"}

# CcMaterial liquid flag. +0x28 is 1 on exactly the 40 materials labelled
# "Custom Material - Water"/"- Lava" and 0 on all 3,773 others; water then
# carries 6 at +0x3c and lava 5 at +0x40.
MAT_LIQUID, MAT_WATER_TAG, MAT_LAVA_TAG = 0x28, 0x3C, 0x40

# SE1 environment (reverb) type 13 is "Underwater" (EntitiesMP/WorldBase.es:710);
# NE's water region leaves carry exactly 13 in the byte after the content type.
UNDERWATER = 13

# NE's colour is the light's SE1 colour byte / 127.5, not / 255. The level's
# baked lightmap (the 1024x1024 atlas at CcMaterial+0x20, docs/image-format.md)
# is an SE1 shadow map: surfaces the sun cannot reach hold the sun fill x 127.5
# ((39,38,31) predicted, (36,36,32) baked on Rlevel1_1) and sunlit floors that
# plus sun x 127.5 x N.L, within a few levels (docs/world-conversion.md,
# "Lights"). 1.0 is SE1's neutral 127, so 10,825 colours over 1.0 are not
# overbright. The runtime path's 255.0 (main.dol 0x8021e65c) only lights models.
COLOUR_SCALE = 127.5

# NE's light type (CcLight +0x04) -> EntitiesMP/Light.es LightType. Type 1 is
# SE1's ambient light: no shadows, no angle term. Baked that way, the lamp
# glows NE's lightmap shows come out (docs/world-conversion.md, "Lights").
# Type 2 is a point light: its shadows are in the lightmap.
NE_LIGHT_TYPES = {1: "LT_AMBIENT", 2: "LT_POINT", 3: "LT_DIRECTIONAL"}

MOVER_CLASS = 4070
FORCE_CLASS = 5020
TOUCHFIELD_CLASS, BOUNCER_CLASS = 4170, 4220

# Every image+0x48 brush shares its transform with the entity that owns it:
# all 958 Movers and all 116 DestroyableArch entities have one. SE1 builds
# both as brush entities of the same name.
BRUSH_CLASS = {4070: "MovingBrush", 4240: "DestroyableArchitecture"}


def u32(img, o):
    return struct.unpack_from(">I", img, o)[0]


def f32(img, o):
    return struct.unpack_from(">f", img, o)[0]


def _cstr(img, o, limit=96):
    end = img.find(b"\x00", o, o + limit)
    return img[o:end if end >= 0 else o + limit].decode("ascii", "replace")


def _labels(img, ptrs, start, end):
    """Printable strings a node points at from inside its own extent."""
    out = []
    for o in range(start, min(end, start + 0x400), 4):
        if o in ptrs:
            v = u32(img, o)
            if 0 < v < len(img):
                s = _cstr(img, v)
                if len(s) > 3 and s.isprintable():
                    out.append(s)
    return out


def liquid_kind(img, ptrs, group):
    """'water', 'lava', 'liquid' or None for a material or sector node."""
    off = group["offset"]
    if group["kind"] == "material":
        if u32(img, off + MAT_LIQUID) != 1:
            return None
        if u32(img, off + MAT_WATER_TAG) == 6:
            return "water"
        if u32(img, off + MAT_LAVA_TAG) == 5:
            return "lava"
        return "liquid"
    # Sector nodes carry no flag we have typed; their custom label is the
    # only evidence (2 cases on the disc), so it is used and marked as such.
    for s in _labels(img, ptrs, off, group["end"]):
        if s.startswith("Custom Material - Water"):
            return "water"
        if s.startswith("Custom Material - Lava"):
            return "lava"
    return None


def se1_light(l):
    """One NE light as an SE1 EntitiesMP/Light.es property set."""
    r, g, b = l["colour"]
    dark = max(r, g, b) < 0          # dark lights are all-negative (52/52)
    mag = (abs(r), abs(g), abs(b))
    rgb = [max(0, min(255, int(round(c * COLOUR_SCALE)))) for c in mag]
    t = l["type"]
    props = {
        "Name": "NE light %d" % l["index"],
        "Type": NE_LIGHT_TYPES[t],
        "Color": rgb,
        "Dark light": dark,
        "Dynamic": False,
    }
    out = {"position": list(l["pos"]), "props": props,
           "ne": {"type": t, "index": l["index"], "flags": l["flags"],
                  "colour": [r, g, b], "clamped": max(mag) * COLOUR_SCALE > 255.5}}
    if t == 3:
        # Writer: DirectionVectorToAngles(dir) -> orientation. SE1 lights a
        # model from pos - dir*1000, so dir is the direction light travels,
        # the same sense as NE's.
        out["direction"] = list(l["dir"])
    else:
        # as/ae are authored values -- the game's debug print names them
        # attenuation start/end -- but the shipping runtime never passes them
        # to the light object, so this preserves designer intent rather than
        # a traced GameCube falloff.
        props["Hot-spot"] = l["att_start"]
        props["Fall-off"] = l["att_end"]
        if l["att_end"] <= 0:
            # 4 lights on the disc, all in Rlevel2_2: zero authored reach.
            out["skip"] = "fall-off <= 0: authored with no reach"
    return out


def _drop_unbaked(raw, ls):
    """Only lights with 0 at +0x3c are in the level's baked lightmap.

    13 levels list every light twice, the second half repeating the first
    node for node with 2 at +0x3c (5,439 lights). The same 2 marks 3 lights
    of their own, and re-baked without them ClevelDM_1 matches NE's lightmap
    on 84% of pixels instead of 0.1%, RlevelDM_3 on 79% instead of 35%. 3
    marks one light on 25 levels (without it Rlevel0_1 and Rlevel3_2 match a
    point or two better), 1 a sun that lights nothing (Clevel5_3)."""
    seen = set()
    for r, l in zip(raw, ls):
        key = (r["type"], r["index"], r["colour"], r["pos"], r["dir"],
               r["att_start"], r["att_end"], r["flags"])
        if r["kind"] == 0:
            seen.add(key)
        elif key in seen:
            l["skip"] = "second copy of NE light %d (+0x3c = %d)" % (r["index"], r["kind"])
        else:
            l["skip"] = "+0x3c = %d: not in the baked lightmap" % r["kind"]


def _drop_unlit_suns(ls):
    """Two directional lights, white with no fill and pointing exactly level
    (Alevel12_3, Alevel12_4; Clevel5_3's is marked unbaked), leave no trace
    in the baked lightmap: on Alevel12_3 surfaces facing one would hold 127
    and hold 0. Converted, one would light every wall facing it."""
    for l in ls:
        if ("direction" in l and not l.get("skip") and abs(l["direction"][1]) < 1e-6
                and not any(l["props"].get("Directional ambient", (0, 0, 0)))):
            l["skip"] = "horizontal directional light with no ambient: not in the baked lightmap"


def _fold_sun_fills(ls):
    """A level's sun is authored as a pair: a directional light and a type-1
    (ambient) fill with infinite reach (as = ae = 999999) at exactly the same
    position. All 48 such fills on the disc sit on a directional light. SE1
    expresses that pair as ONE directional Light whose "Directional ambient"
    is the fill: SetupLightSource copies m_colAmbient only for directional
    lights. So the fill folds into its partner instead of becoming an ambient
    light with no shadows that would light the whole world, where NE's
    lightmap is black indoors.
    """
    dirs = [l for l in ls if "direction" in l and not l.get("skip", "").startswith("second copy")]
    used = set()
    for l in ls:
        if l.get("skip") or l["ne"]["type"] != 1 or l["props"].get("Fall-off", 0) < 1e5:
            continue
        mate = next((d for d in dirs if id(d) not in used and all(
            abs(a - b) < 1e-3 for a, b in zip(d["position"], l["position"]))),
            None)
        if mate and mate.get("skip"):
            used.add(id(mate))
            l["skip"] = ("fill of directional light %d, which is not in the baked lightmap"
                         % mate["ne"]["index"])
        elif mate:
            used.add(id(mate))
            mate["props"]["Directional ambient"] = l["props"]["Color"]
            mate["ne"]["ambient_from"] = l["ne"]["index"]
            l["skip"] = ("folded into directional light %d as its ambient"
                         % mate["ne"]["index"])
        else:                        # none on the disc; stays an ambient light
            l["ne"]["unpaired_fill"] = True


_SAMPLES = {}


def _sound(h):
    """A sample hash -> the WAVs tools/sound.py extracts for it (docs/sound.md).
    0 is "none"; a hash missing from the game's own table plays nothing in
    the game either."""
    if not h:
        return None
    if not _SAMPLES:
        from sound import hash_table
        _SAMPLES.update(hash_table())
    idx = _SAMPLES.get(h)
    if idx is None:
        return {"hash": "%08x" % h, "missing_in_game": True}
    return {"hash": "%08x" % h,
            "samples": ["build/sound/sfx_%03d.wav" % i for i in idx]}


MUSIC_CLASS = 4090
# base.cfg LevelMusic.ThreshMedium / ThreshHeavy -- and, exactly, SE1
# MusicHolder's own "Score Medium" / "Score Heavy" defaults.
SCORE_MEDIUM, SCORE_HEAVY = 100.0, 1000.0
_STREAM_ROWS = []


def music_description(img, ents):
    """NE Music entities (class 4090, props {u32 stream hash, u32 flag}).

    A hash names the first row of a six-voice track in the stream table
    (tools/sound.py): three stereo layers, which the game mixes by fight
    intensity against LevelMusic's thresholds. That is SE1's MusicHolder --
    Music Light / Medium / Heavy with Score Medium / Heavy -- so a level's first
    Music entity becomes the MusicHolder. Later ones change the music
    mid-level ("Changing music stream..", 0x8008123c); SE1's MusicChanger
    takes a single file, so those record all three layers and the writer
    decides.
    """
    if not _STREAM_ROWS:
        from sound import stream_table
        _STREAM_ROWS.extend(stream_table())
    row_of = {r["hash"]: r["index"] for r in _STREAM_ROWS if r["music"]}
    out = []
    for e in ents:
        if e["cls"] != MUSIC_CLASS or e["props_size"] < 8:
            continue
        h, flag = u32(img, e["props"]), u32(img, e["props"] + 4)
        rec = {"entity_id": e["id"], "name": e["name"], "hash": "%08x" % h,
               "flag": flag,
               "position": list(e["pos"]) if e["pos"] else None}
        i = row_of.get(h)
        if i is None:
            rec["missing"] = True
        else:
            t = i // 6
            layers = ["build/sound/music/track%02d_layer%d.wav" % (t, k) for k in range(3)]
            rec.update(track=t, layers=layers)
            rec["se1"] = ({"class": "MusicHolder", "Music Light": layers[0],
                           "Music Medium": layers[1], "Music Heavy": layers[2],
                           "Score Medium": SCORE_MEDIUM, "Score Heavy": SCORE_HEAVY}
                          if not any("se1" in x and x["se1"]["class"] == "MusicHolder"
                                     for x in out)
                          else {"class": "MusicChanger", "layers": layers})
        out.append(rec)
    return out


SOUND_CLASS = 4100
MESSAGE_CLASS = 4130
_BANK_LOOPED = []
_SPEECH_LINE = {}


def _speech(h):
    """A SoundHolder's stream hash -> its speech line. The hash names a row
    of the English block; Stream_PlayIndex adds language x 125, so line j
    has one file per language (docs/sound.md)."""
    if not h:
        return None
    if not _SPEECH_LINE:
        from sound import stream_table, SPEECH_BLOCK
        sp = [r for r in stream_table() if not r["music"]]
        _SPEECH_LINE.update({r["hash"]: j for j, r in enumerate(sp[:SPEECH_BLOCK])})
    j = _SPEECH_LINE.get(h)
    if j is None:
        return {"hash": "%08x" % h, "missing_in_game": True}
    from text import LANGS
    return {"hash": "%08x" % h, "line": j,
            "files": {lang: "build/sound/speech/lang%d_%03d.wav" % (k, j)
                      for k, lang in enumerate(LANGS)}}


def sound_description(ents):
    """NE SoundHolders (class 4100) -> SE1 SoundHolder, field for field:
    Sound, Fall-off, Hot-spot, Volume, Looping, Auto start, Destroyable.

    NE corrects the values at init (0x800833a8) and so does this: a fall-off
    below the hot-spot becomes hot-spot + 1, one within 0.1 of it gains 0.1,
    and the volume is held to 0..1. Speech is a stream with no position --
    the play call takes only the hash -- so it is kept beside the positional
    sample, for the writer to play globally.
    """
    if not _BANK_LOOPED:
        from sound import load_bank
        _BANK_LOOPED.extend(e["looped"] for e in load_bank())
    out = []
    for e in ents:
        if e["cls"] != SOUND_CLASS or "sample" not in e:
            continue
        fall, hot, vol = e["falloff"], e["hotspot"], e["volume"]
        if fall < hot:
            fall = hot + 1.0
        if fall < hot + 0.1:
            fall += 0.1
        vol = min(max(vol, 0.0), 1.0)
        snd = _sound(e["sample"])
        rec = {"entity_id": e["id"], "name": e["name"],
               "position": list(e["pos"]) if e["pos"] else None,
               "sample": snd, "speech": _speech(e["speech"]),
               "clamped": (fall, vol) != (e["falloff"], e["volume"])}
        idx = _SAMPLES.get(e["sample"]) if snd else None
        if idx:
            rec["se1"] = {"class": "SoundHolder", "Name": e["name"],
                          "Sound": snd["samples"][0], "Fall-off": fall,
                          "Hot-spot": hot, "Volume": vol,
                          "Looping": _BANK_LOOPED[idx[0]],
                          "Auto start": e["active"],
                          "Destroyable": e["destroyable"]}
            if len(idx) > 1:
                rec["note"] = ("NE picks one of %d variations per play; SE1's "
                               "SoundHolder takes one" % len(idx))
        out.append(rec)
    return out


def message_description(ents, stem):
    """NE MessageHolders (class 4130) -> SE1 MessageHolder. Both hand the
    player a NETRICSA message when triggered. Title and body come from the
    .tdb by hash (tools/text.py); `text.py messages` writes the SE1 message
    files named here, one per language."""
    from text import lookup, resolves, message_file, LANGS
    out = []
    for e in ents:
        if e["cls"] != MESSAGE_CLASS or "title_hash" not in e:
            continue
        title = lookup(e["title_hash"])
        out.append({
            "entity_id": e["id"], "name": e["name"], "key": e.get("text"),
            "position": list(e["pos"]) if e["pos"] else None,
            "title": title, "text": lookup(e["text_hash"]),
            "resolved": resolves(e["title_hash"]) and resolves(e["text_hash"]),
            "files": {lang: message_file(stem, e["id"], lang) for lang in LANGS},
            "se1": {"class": "MessageHolder", "Name": title,
                    "Message": message_file(stem, e["id"]),
                    "Active": e["active"]}})
    return out


CAMERA_CLASS = 4120


def se1_rotation(h, p, b):
    """SE1's MakeRotationMatrix (Engine/Math/Geometry.cpp), angles in degrees."""
    h, p, b = (math.radians(x) for x in (h, p, b))
    sh, ch, sp, cp, sb, cb = (math.sin(h), math.cos(h), math.sin(p),
                              math.cos(p), math.sin(b), math.cos(b))
    return [[ch * cb + sp * sh * sb, sp * sh * cb - ch * sb, cp * sh],
            [cp * sb, cp * cb, -sp],
            [sp * ch * sb - sh * cb, sp * ch * cb + sh * sb, cp * ch]]


def ne_rotation(h, p, b):
    """SE1 editor angles -> the rotation rows of the Mtx Climax's converter
    wrote for them: MakeRotationMatrix at (180 - h, p, -b), which is
    S_z . R(h, p, b) . S_x. Exact on every camera and marker placement on the
    disc; tools/space.py takes it back to R(h, p, b)."""
    return se1_rotation(180.0 - h, p, -b)


def camera_description(ents):
    """NE cameras (class 4120) -> SE1 Camera + a CameraMarker chain.

    Built in NE space like everything else here, then taken to the original
    space by describe(): the position is the marker's placement translation
    (what the game reads), and the rotation is built from its stored angles.
    On records that carry their own Mtx that equals the Mtx, and in the
    original space it is the editor's own MakeRotationMatrix(h, p, b). Bias
    and continuity are 0 on the whole disc, which of +0x24 / +0x28 is which
    is therefore moot."""
    ids = {e["id"] for e in ents}
    out = []
    for e in ents:
        if e["cls"] != CAMERA_CLASS or "markers" not in e:
            continue
        ms = e["markers"]
        rec = {"entity_id": e["id"], "name": e["name"],
               "position": list(e["pos"]) if e["pos"] else None,
               "rotation": ne_rotation(*e["angles"]), "angles": e["angles"],
               "static": not ms,
               "loops": e["loops"],
               "se1": {"class": "Camera", "Name": e["name"], "FOV": e["fov"],
                       "Time": e["time"], "Target": 0 if ms else None},
               "markers": []}
        for i, m in enumerate(ms):
            nxt = i + 1 if i + 1 < len(ms) else (0 if e["loops"] else None)
            rec["markers"].append({
                "position": list(m["pos"]) if m["pos"] else None,
                "rotation": ne_rotation(*m["angles"]), "angles": m["angles"],
                "own_matrix": m["own_matrix"], "placement_rot": m["placement_rot"],
                "trigger_resolves": None if m["trigger"] is None else m["trigger"] in ids,
                "se1": {"class": "CameraMarker", "Delta time": m["delta_time"],
                        "Tension": m["tension"], "Bias": 0.0, "Continuity": 0.0,
                        "Stop moving": m["stop_moving"],
                        "Skip to next": m["skip_to_next"], "FOV": m["fov"],
                        "Trigger": m["trigger"], "Target": nxt}})
        out.append(rec)
    return out


SE1_ENTITIES = Path(__file__).resolve().parent.parent / "pc" / "engine" / "SamTSE" / "Sources" / "EntitiesMP"
_ENUMS = {}


def se1_enum(source, enum):
    """{label: value, NAME: value} of `enum` in EntitiesMP/<source>.es."""
    key = (source, enum)
    if key not in _ENUMS:
        body = (SE1_ENTITIES / (source + ".es")).read_text(encoding="latin-1")
        m = re.search(r"^enum\s+%s\s*\{(.*?)^\};" % enum, body, re.S | re.M)
        vals = {}
        for num, name, label in re.findall(r'^\s*(\d+)\s+(\w+)\s+"([^"]*)"', m.group(1) if m else "", re.M):
            vals[label] = vals[name] = int(num)
        _ENUMS[key] = vals
    return _ENUMS[key]


# The item classes and the enum each one's Type takes.
ITEM_ENUM = {"HealthItem": ("HealthItem", "HealthItemType"),
             "ArmorItem": ("ArmorItem", "ArmorItemType"),
             "AmmoItem": ("AmmoItem", "AmmoItemType"),
             "WeaponItem": ("WeaponItem", "WeaponItemType"),
             "PowerUpItem": ("PowerUpItem", "PowerUpItemType")}
ITEM_CLASSES = {2100, 2200, 3100, 3200, 3201, 3210, 3220}
# NE entity classes script_description leaves out, and why.
NOT_SCRIPTED = {
    3215: "SE1 has no scoring pickups",
    4260: "GameCube-only objective arrow",
    4270: "GameCube-only lockdown",
    4280: "GameCube-only warp of every player to a start",
    4330: "GameCube-only par times and medal scores",
    4340: "GameCube-only movie player",
    4320: "SE1 has no CTF flag",
    4190: "SE1 has no drivable vehicles",
    4180: "SE1's Switch is a model holder; NE's switch model is not converted",
    4310: "NE's particle types are its own; not mapped yet",
    4200: "props are not written yet",
    4210: "props are not written yet",
    4230: "props are not written yet",
    5010: "light flares are not mapped yet",
}
LEVEL_STEMS = {p.stem.lower(): p.stem for p in
               (Path(__file__).resolve().parent.parent / "orig" / "files" / "Levels").glob("*.ssw")}


def _item(e):
    """(SE1 class, Type value or None, confidence) of a pickup, from the judged
    tables in tools/sediff.py; (None, None, reason) when SE1 has no such item."""
    from sediff import ITEM_MAP, ITEM_MAP_BY_ID
    from entity import ITEM_TYPES
    idx = e.get("item")
    if idx is None:
        return None, None, "no item type"
    cls, label, conf = (ITEM_MAP.get(ITEM_TYPES[idx], (None, None, "none")) if idx < len(ITEM_TYPES)
                        else ITEM_MAP_BY_ID.get(idx, (None, None, "none")))
    if cls is None:
        return None, None, "SE1 has no %s" % e.get("item_name")
    if cls in ITEM_ENUM:
        if label is None:
            return None, None, "%s resolves to a game-mode slot, not a type" % e.get("item_name")
        return cls, se1_enum(*ITEM_ENUM[cls])[label], conf
    return cls, None, conf


# NE's creatures ported to SE1 classes of their own (pc/entities, docs/enemies.md),
# named as the designers named them in their Entities.dll.
PORTED_ENEMIES = {"e_DumDumLarge": "GCEnemyGenericDumDumLarge"}

# The property that picks a stand-in's variant, and its enum.
ENEMY_SUBTYPE = {"Headman": ("Type", "HeadmanType"), "Walker": ("Character", "WalkerChar"),
                 "Scorpman": ("Type", "ScorpmanType")}


def _enemy(e):
    """(SE1 class, {property: value}, confidence, NE actor) of an enemy
    template, from tools/sediff.py's judged table; class None when TSE has no
    such creature and it is to be ported."""
    from sediff import ENEMY_MAP, actor_enums
    actor = actor_enums().get(e.get("model"))
    if actor is None:
        return None, {}, "no actor", e.get("model_name")
    name, enum = actor
    if enum in PORTED_ENEMIES:
        return PORTED_ENEMIES[enum], {}, "ported", name
    cls, sub, conf, _note = ENEMY_MAP.get(enum, (None, None, "none", ""))
    props = {}
    if cls in ENEMY_SUBTYPE and sub:
        prop, en = ENEMY_SUBTYPE[cls]
        props[prop] = se1_enum(cls, en)[sub]
    return cls, props, conf, name


def script_description(ents):
    """NE's scripting and pickups -> the SE1 classes that do the same
    (docs/world-conversion.md, "Scripting as written"). Each record keeps its
    NE id; `links` name an SE1 entity property and the NE id it points at, and
    the writer resolves them against every entity it writes. A Trigger slot
    keeps NE's event (trigger / on / off): which SE1 event that is depends on
    what the target listens for, which the writer knows."""
    out, skipped = [], Counter()
    for e in ents:
        cls, k, L = e["cls"], e["kind"], e.get("links") or {}
        if not e["pos"]:
            continue
        rec = {"entity_id": e["id"], "name": e["name"], "kind": k,
               "position": list(e["pos"]), "rows": [list(r) for r in e["rot"]],
               "links": []}
        props = {}
        if cls == 4080:
            se, props = "Trigger", {"Name": e["name"], "Active": e.get("active") is not False,
                                    "Count use": bool(e.get("use_count")), "Count": e.get("count", 1),
                                    "Max trigs": e.get("max_trigs", -1), "Wait": e.get("wait", 0.0)}
            for n, t in enumerate(e.get("targets") or [], 1):
                rec["links"].append({"property": "Target %02d" % n, "id": t["target"],
                                     "event_property": "Event type Target %02d" % n,
                                     "event": t["event"]})
        elif cls == 4150:
            se, props = "EnemyMarker", {"Name": e["name"]}
            rec["links"].append({"property": "Target", "id": L.get("next")})
        elif cls == 4060:
            se, props = "Marker", {"Name": e["name"]}
        elif cls == 4030:
            # SE1's WatchPlayers sends its close event to Owner/Target every
            # Wait time while a player is within Watch distance; its far event
            # is cleared, since NE's watcher has one target and one event.
            se, props = "WatchPlayers", {"Name": e["name"], "Active": e.get("active") is not False,
                                         "Watch distance": e.get("distance", 100.0),
                                         "Wait time": e.get("wait", 0.1),
                                         "Far Event type": se1_enum("Global", "EventEType")["EET_IGNORE"]}
            rec["links"].append({"property": "Owner/Target", "id": L.get("target")})
        elif cls == 4010:
            se, props = "Copier", {"Name": e["name"]}
            rec["links"].append({"property": "Target", "id": L.get("target")})
        elif cls == 4110:
            se, props = "Damager", {"Name": e["name"], "Ammount": e.get("amount", 1000.0)}
            rec["links"].append({"property": "Entity to Damage", "id": L.get("entity")})
        elif cls == 4020:
            se, props = "Teleport", {"Name": e["name"], "Active": e.get("active") is not False,
                                     "Width": e.get("width", 2.0), "Height": e.get("height", 3.0)}
            rec["links"].append({"property": "Target", "id": L.get("target")})
        elif cls == 4050:
            # all 26 on the disc are type 1, DT_TRIGGERED in SE1's own order
            se, props = "DoorController", {"Name": e["name"], "Active": e.get("active") is not False,
                                           "Width": e.get("width", 2.0), "Height": e.get("height", 3.0),
                                           "Type": e.get("type", 0)}
            rec["links"] += [{"property": "Target1", "id": L.get("target1")},
                             {"property": "Target2", "id": L.get("target2")}]
        elif cls == 4140:
            se, props = "PlayerMarker", {"Name": e["name"]}
        elif cls == 4160:
            world = LEVEL_STEMS.get((e.get("world") or "").lower())
            if not world:
                skipped["WorldLink: " + ("ends the game (endofgame)" if e.get("end_game")
                                         else "names no level")] += 1
                continue
            # all 41 set are type 2, WLT_RELATIVE in SE1's own order
            se, props = "WorldLink", {"Name": e["name"], "World": "Levels\\NextEncounter\\%s.wld" % world,
                                      "Type": e.get("type") or 2}
            rec["end_game"] = e.get("end_game")
        elif cls == 4040:
            se, props = "EnemySpawner", {"Name": e["name"], "Count total": e.get("count", 1),
                                         "Delay initial": e.get("delay", 0.0),
                                         "Delay single": e.get("interval", 0.1)}
            rec["links"] += [{"property": "Template Target", "id": L.get("template")},
                             {"property": "Patrol target", "id": L.get("patrol")}]
        elif cls == 5000:
            # A template the spawners copy (EnemySpawner "Template Target").
            # Stand-ins where TSE has the creature (docs/world-conversion.md,
            # "Enemies"); the rest wait on their port.
            se, sub, conf, actor = _enemy(e)
            if se is None:
                skipped["EnemyTemplate: %s, to be ported" % actor] += 1
                continue
            props = dict({"Name": e["name"], "Template": True}, **sub)
            rec["enemy"] = {"ne": actor, "confidence": conf}
            rec["links"].append({"property": "Death target", "id": L.get("death_target"),
                                 "event_property": "Death event type", "event": "trigger"})
        elif cls in ITEM_CLASSES:
            se, value, conf = _item(e)
            if se is None:
                skipped["%s: %s" % (k, conf)] += 1
                continue
            props = {"Name": e["name"]}
            if value is not None:
                props["Type"] = value
            rec["item"] = {"ne": e.get("item_name"), "confidence": conf}
            rec["links"].append({"property": "Target", "id": L.get("target")})
        else:
            if cls in NOT_SCRIPTED:
                skipped["%s: %s" % (k, NOT_SCRIPTED[cls])] += 1
            continue
        rec["links"] = [x for x in rec["links"] if x["id"] is not None]
        rec["se1"] = dict(props, **{"class": se})
        out.append(rec)
    return {"entities": out, "skipped": dict(skipped)}


def mover_motion(d):
    """NE MovingBrush -> SE1 MovingBrush properties plus one MovingBrushMarker
    per keyframe, chained in order and looped from the last back to the first,
    since NE heads for keyframe 0 after the last one.

    Values copy straight across. SE1's Speed is a time, as NE's is:
    velocity = (vTarget - vSource) / m_fSpeed (MovingBrush.es:598). Both
    engines load a marker's time and wait on *arriving* at it -- the wait is
    held there, the time drives the next segment -- and keep the current value
    when the marker's is negative (LoadMarkerParameters, MovingBrush.es:421-433;
    NE update 0x8007f0b8). The brush's own Speed and Wait are what NE's init
    computes: keyframe 0's when >= 0, else the header's.
    """
    keys = d.get("keyframes") or []
    if not keys:
        return None
    k0 = keys[0]
    markers = []
    for i, k in enumerate(keys):
        markers.append({
            "position": list(k["pos"]) if k["pos"] else None,
            "rows": [list(r) for r in k["rot"]] if k["rot"] else None,
            "se1": {"class": "MovingBrushMarker", "Speed": k["time"],
                    "Wait time": k["wait"], "Stop moving": k["stop"],
                    "Target": (i + 1) % len(keys)},   # marker index
        })
    return {
        "se1": {"Auto start": d["auto_start"],
                "Speed": k0["time"] if k0["time"] >= 0 else d["time"],
                "Wait time": k0["wait"] if k0["wait"] >= 0 else d["wait"],
                "Move on damage": d["move_on_damage"],
                "Target": 0},                          # marker index
        "markers": markers,
        # SE1 MovingBrush takes sound *entities* ("Sound start/stop/follow
        # entity"); the writer makes one SoundHolder per sample set.
        "sounds": dict({k: _sound(h) for k, h in d["sounds"].items()},
                       loop_enabled=d["loop_sound"]),
    }


def field_entities(container, ents):
    """TouchField and Bouncer: SE1 brush entities. A TouchField's volume is
    the group of trigger triangles (flag 0x40) in the level collision mesh
    whose value names its props+0x10 index (tools/collision.py); 428 of the
    444 fields have one, a closed mesh in world space. A Bouncer's is the
    group of solid pad triangles whose value is 0x200 | its index; 64 of 64
    have one, 29 closed.
    """
    img = container.image
    ids = {e["id"] for e in ents}
    vols = touch_volumes(container)
    bvols = bouncer_volumes(container)
    tf, bo = [], []
    for e in ents:
        if e["cls"] == TOUCHFIELD_CLASS and e["props_size"] >= 36:
            w = [u32(img, e["props"] + i * 4) for i in range(9)]
            link = None if w[3] == 0xFFFFFFFF else w[3]
            vol = vols.get(w[4])
            se1 = {"class": "TouchField", "Enter Target": link if link in ids else None,
                   # the init sets its active flag from props +0x00
                   "Active": bool(w[0]),
                   # props words 5 and 6 ride on the volume's collision
                   # triangles (bits 4 and 8 of their value): SE1 GC's two
                   # collision properties, by that and by their order (likely)
                   "Block Walking Enemy": bool(w[5]), "Block Flying Enemy": bool(w[6])}
            tf.append({
                "entity_id": e["id"], "name": e["name"],
                "position": list(e["pos"]) if e["pos"] else None,
                "se1": se1,
                "target_resolves": link in ids if link is not None else None,
                "volume_index": w[4],
                # The field's trigger triangles from the level collision mesh,
                # in world space: the brush's geometry. describe() converts
                # them to the original space like every other vertex list.
                "volume": ({"vertices": vol["vertices"], "faces": vol["faces"],
                            "closed": vol["closed"], "geometry_space": "world"}
                           if vol else None),
                "raw": w,
            })
        elif e["cls"] == BOUNCER_CLASS and e["props_size"] >= 32:
            f = [f32(img, e["props"] + i * 4) for i in range(3)]
            # The Bouncer's init (0x8007782c) builds its push direction as
            # Ry(-heading) . Rx(-pitch) . (0, 0, 1), angles in degrees
            # (x 0.0174533). That world vector is recorded; the writer turns it
            # into SE1's Direction with DirectionVectorToAngles, as for lights,
            # so no SE1 angle convention is guessed here.
            h, pt = math.radians(f[1]), math.radians(f[2])
            vec = [-math.sin(h) * math.cos(pt), math.sin(pt), math.cos(h) * math.cos(pt)]
            k = u32(img, e["props"] + 0x10)
            vol = bvols.get(k)
            bo.append({
                "entity_id": e["id"], "name": e["name"],
                "position": list(e["pos"]) if e["pos"] else None,
                "se1": {"class": "Bouncer", "Speed": f[0]},
                "direction": vec,
                "ne": {"speed": f[0], "heading": f[1], "pitch": f[2]},
                "volume_index": k,
                # The pad's solid collision triangles (value 0x200 | index), in
                # world space. Often open: a quad, or a box without its bottom.
                "volume": ({"vertices": vol["vertices"], "faces": vol["faces"],
                            "closed": vol["closed"], "geometry_space": "world"}
                           if vol else None),
            })
    return {"touch_fields": tf, "bouncers": bo}


# After loading, CcLevel_SpawnFlames (main.dol 0x8010b790) walks the placed
# models and spawns a particle effect wherever a node's effect string is one of
# these: 2 for "smallflame", 4 for "bigflame". All 2,860 meshless instances
# carry one; the designer names ("Brazierflame", "wall lamp flame") are labels.
FLAME_EFFECT = {"smallflame": 2, "bigflame": 4}


def _flame_kind(L, lo, hi):
    for o in range(lo, hi, 4):
        if o in L.pset:
            v = u32(L.img, o)
            if 0 < v < len(L.img) and _cstr(L.img, v) in FLAME_EFFECT:
                return _cstr(L.img, v)
    blob = L.img[lo:hi]
    for s in FLAME_EFFECT:
        if s.encode() + b"\0" in blob:
            return s
    return None


def props_description(container, stem):
    """Placed models -> SE1 ModelHolder2 entities; meshless instances (the
    flames) are recorded raw as effects.

    A GameCube Mtx maps local axis k onto column k, so the per-axis stretch is
    each column's length. The writer normalises the columns before handing the
    rotation to DecomposeRotationMatrixNoSnap. Stretches are not clamped: the
    smallest on the disc (0.01) are Atlantis energy beams, enr_cylinder
    squashed thin and stretched along its axis, which is what they are.
    """
    L = ModelLevel(container, stem)
    meshes, props, effects = {}, [], []
    order = sorted(m["offset"] for m in L.instances)
    ends = {a: min(b, a + 0x140) for a, b in zip(order, order[1:])}
    for inst in L.instances:
        if not inst["rot"]:
            continue
        rows, trans = inst["rot"], inst["pos"]
        rec = {"name": inst["name"],
               "matrix": {"rows": [list(r) for r in rows],
                          "translation": list(trans)}}
        if inst["mesh"] is None:
            if inst["path"] and "editor" in inst["path"].lower():
                # models\editor\axis.clm: an editor gizmo whose mesh is not
                # in the shipped image (5 instances on the disc). Kept as an
                # invisible marker; nothing renders here on the GameCube.
                rec["note"] = "editor placeholder %s: invisible marker" % inst["path"]
                rec["se1"] = {"class": "Marker", "Name": inst["name"]}
            else:
                kind = _flame_kind(L, inst["offset"], ends.get(inst["offset"],
                                                               inst["offset"] + 0x140))
                rec["effect"] = kind
                rec["note"] = ("CcLevel_SpawnFlames (0x8010b790) spawns particle "
                               "effect %d at this translation"
                               % FLAME_EFFECT[kind]) if kind else "no mesh, no effect string"
            effects.append(rec)
            continue
        if inst["mesh"] not in meshes:
            meshes[inst["mesh"]] = L.mesh(inst["mesh"])
        m = meshes[inst["mesh"]]
        stretch = [math.sqrt(sum(rows[j][k] ** 2 for j in range(3)))
                   for k in range(3)]
        # distinct textures, in material order: several meshes split one
        # texture across materials (Rlevel1_1's birch tree: 2 materials, 1
        # texture), which a single-texture MDL still carries
        tex = list(dict.fromkeys(L.texname(x["diffuse"]) for x in m["materials"]
                                 if x["diffuse"] is not None))
        rec.update({
            "model": m["name"],
            "file": "build/models/%s/%s.obj" % (stem, m["name"]),
            "materials": len(m["materials"]),
            "se1": {"class": "ModelHolder2",
                    "Name": inst["name"] or m["name"],
                    "Model": m["name"] + ".mdl",
                    "Texture": tex[0] if tex else None,
                    "StretchX": stretch[0], "StretchY": stretch[1],
                    "StretchZ": stretch[2]},
        })
        if inst["damage"]:
            # _d1, _d2 ...: the model's damage stages (1,080 of the 1,105
            # carriers are breakable Prop entities). SE1 swaps a broken model
            # through ModelHolder2 "Destruction" -> ModelDestruction
            # "Model 0..4", template holders; a second stage hangs off the
            # first stage's own Destruction. docs/world-conversion.md "Props".
            rec["damage_states"] = [
                {"model": L.mesh_name(n),
                 "file": "build/models/%s/%s.obj" % (stem, L.mesh_name(n))}
                for n, _ in inst["damage"]]
        if len(tex) > 1:
            # An MDL model carries a single texture; an SKA mesh (.smc, read by
            # EntitiesMP/ModelHolder3.es) has a surface per material, each with
            # its own texture.
            rec["se1"] = {"class": "ModelHolder3",
                          "Name": rec["se1"]["Name"],
                          "Model file (.smc)": m["name"] + ".smc",
                          "textures": tex,
                          "StretchXYZ": stretch}
        props.append(rec)
    return props, effects


def region_description(container):
    """NE's region BSP -> one extra SE1 brush whose sectors are the region
    cells, plus one HazeMarker per distinct region fog.

    Each cell is a closed convex volume, which is exactly what an SE1 content
    sector must be. Sector flags: content (Water 1 / Lava 2), environment
    (Underwater 13) and a haze *slot*. Slot 0 is kept empty so a sector with
    no haze can point at it -- WorldBase.m_penHaze0..4 are the slots (TSE),
    and no level uses more than 2.
    """
    cells = region_cells(container)
    haze, slot_of = [], {}
    out = []
    for i, c in enumerate(cells):
        slot = 0
        f = c["fog"]
        if f and f["on"]:
            key = (round(f["start"], 3), round(f["end"], 3),
                   tuple(round(v, 4) for v in f["rgb"]))
            if key not in slot_of:
                slot_of[key] = len(haze) + 1
                haze.append({"slot": len(haze) + 1, "se1": {
                    "class": "HazeMarker", "Attenuation Type": "FA_LINEAR",
                    "Near": f["start"], "Far": f["end"],
                    "Base Color": [max(0, min(255, int(round(v * 255))))
                                   for v in f["rgb"]]}})
            slot = slot_of[key]
        out.append({
            "name": "%s region %d" % (CONTENT_NAME.get(
                c["content"], "type%d" % c["content"]).lower(), i),
            "content": c["content"], "environment": c["environment"],
            "haze": slot, "effect": c["effect"],
            "vertices": c["vertices"], "faces": c["faces"],
        })
    return {"brush": {"class": "WorldBase", "sectors": out},
            "haze_markers": haze}


def force_fields(container):
    """Class 5020: a local vector, a zero vector, then a 3x4 transform.

    The entity's own transform pointer is null -- its placement lives in these
    props. Read as a parallel force of strength |g| along the transformed
    local vector. The fit to SE1's GravityMarker is inference: the shape
    matches and the levels that use it are the odd-gravity Atlantis ones, but
    no code has been read that consumes this class.
    """
    img = container.image
    out = []
    for e in entities(container):
        if e["cls"] != FORCE_CLASS or e["props_size"] < 72:
            continue
        w = [f32(img, e["props"] + i * 4) for i in range(18)]
        vec, vec2 = w[0:3], w[3:6]
        rows = (w[6:9], w[10:13], w[14:17])
        trans = (w[9], w[13], w[17])
        mag = math.sqrt(sum(v * v for v in vec)) or 1.0
        unit = [v / mag for v in vec]
        world = [sum(r[k] * unit[k] for k in range(3)) for r in rows]
        out.append({
            "entity_id": e["id"],
            "matrix": {"rows": [list(r) for r in rows], "translation": list(trans)},
            "local_vector": vec, "second_vector": vec2,
            "se1": {"class": "GravityMarker", "Type": "Parallel",
                    "Strength": mag, "direction": world},
            "confidence": "inference",
            # Only 14 of 23 translations fall inside their level, and
            # AlevelDM_2's miss by up to 3.7x the level's span -- so either
            # this matrix reading or the class's meaning is wrong there.
            # Recorded, not to be created until the consuming code is read.
            "emit": False,
        })
    return out


def describe(container, path):
    img = container.image
    stem = path.stem
    ptrs = set(container.pointer_offsets())
    texs = list(texwalk(img))

    def texname(i):
        if i is None or i >= len(texs):
            return None
        t = texs[i]
        return "%s_%03d_%s_%dx%d.png" % (stem, i, t.name, t.width, t.height)

    tris = Counter()
    for owner, tl in batches(container):
        if owner:
            tris[(owner["kind"], owner["index"])] += len(tl)

    static, liquids = [], []
    for m in materials(container):
        n = tris.get(("material", m["index"]), 0)
        if not n:
            continue
        kind = liquid_kind(img, ptrs, m)
        group = "mat%d" % m["index"]
        if kind:
            # A liquid *surface*: render-only. Gameplay content is carried by
            # the region volumes -- Alevel10_2's lava field is a liquid
            # surface with no region at all, so the two must not be conflated.
            liquids.append({
                "name": "%s %s" % (kind, group),
                "content": AIR,
                "liquid_surface": kind,
                "groups": [group], "triangles": n,
                "evidence": "CcMaterial +0x28 == 1",
            })
        else:
            static.append(group)

    world_sectors = [{"name": "static", "content": AIR, "groups": static,
                      "triangles": sum(tris[("material", int(g[3:]))]
                                       for g in static)}] + liquids
    for s in world_sectors:
        # Properties NE has not yielded yet; see docs/world-conversion.md.
        s.update({"ambient": None, "force": 0, "fog": 0, "haze": 0})

    ents = entities(container)
    mtx_to_entity = {u32(img, e["offset"] + 0x04): e for e in ents}
    brushes = []
    bmesh = brush_meshes(container)
    for s in sectors(container):
        if not s["xform"]:
            continue
        n = tris.get(("sector", s["index"]), 0)
        rows, trans = s["xform"]
        owner = mtx_to_entity.get(u32(img, s["offset"] + SEC_XFORM))
        kind = liquid_kind(img, ptrs, s)
        brushes.append({
            # 15 brushes on the disc are empty: no render triangles, and a
            # collision record with no vertices. Their owners work as logic,
            # so they carry no group and no geometry.
            "group": ("sector%d" % s["index"]) if n else None,
            "invisible": not n,
            "name": owner["name"] if owner else ("sector%d" % s["index"]),
            "class": BRUSH_CLASS.get(owner["cls"] if owner else None, "WorldBase"),
            "entity_id": owner["id"] if owner else None,
            "entity_class": owner["kind"] if owner else None,
            # a brush node's rotation: its local space is mirrored in Z, not
            # X, so it converts as S_z.R.S_z (tools/space.py)
            "matrix": {"brush_rows": [list(r) for r in rows],
                       "translation": list(trans)},
            "scale": [math.sqrt(sum(v * v for v in r)) for r in rows],
            "texture": texname(s["diffuse"]),
            "content": (LAVA if kind == "lava" else WATER) if kind else AIR,
            "triangles": n,
            # Its collision record (node +0x28): 0 on the 15 empty brushes and
            # on 135 visible ones that have vertices but no triangles.
            "collision_triangles": len(bmesh[s["index"]]["tris"]) if bmesh.get(s["index"]) else 0,
            # A visible brush with no collision triangles never blocks in NE:
            # 134 of the 135 extra WorldBase brushes (all alpha-textured
            # decoration) and one water-flow mover. The writer sets SE1's
            # BPOF_PASSABLE on its polygons.
            "passable": bool(n) and not (bmesh.get(s["index"]) or {}).get("tris"),
            "motion": (mover_motion(_decode(img, owner))
                       if owner and owner["cls"] == MOVER_CLASS else None),
            # level.py mesh writes these groups in WORLD space; the writer
            # takes local = inverse(matrix) * world for the brush, and the
            # matrix itself becomes the entity placement.
            "geometry_space": "world",
        })

    raw = list(lights(img))
    ls = [se1_light(l) for l in raw]
    _drop_unbaked(raw, ls)
    _fold_sun_fills(ls)
    _drop_unlit_suns(ls)
    fields = field_entities(container, ents)
    regions = region_description(container)
    props, effects = props_description(container, stem)
    return space.to_original({
        "level": stem,
        "geometry": "build/mesh/%s.obj" % stem,
        "axes": ("original space, y-up, unscaled: NE's world z and model-local x "
                 "negated, rotations S_z.R.S_x (tools/space.py)"),
        "worldbase": {"sectors": world_sectors},
        "region_brush": regions["brush"],
        "haze_markers": regions["haze_markers"],
        "props": props,
        "effects": effects,
        "brush_entities": brushes,
        "lights": ls,
        "force_fields": force_fields(container),
        "touch_fields": fields["touch_fields"],
        "bouncers": fields["bouncers"],
        "music": music_description(img, ents),
        "sounds": sound_description(ents),
        "messages": message_description(ents, stem),
        "cameras": camera_description(ents),
        "scripts": script_description(ents),
        "entities":"build/entities/%s.json" % stem,
    })


# --- subcommands ------------------------------------------------------------

def cmd_describe(args):
    args.output.mkdir(parents=True, exist_ok=True)
    for path in args.files:
        d = describe(Container(path), path)
        dest = args.output / (path.stem + ".json")
        dest.write_text(json.dumps(d, indent=1))
        ws = d["worldbase"]["sectors"]
        print("%s: %d world sectors (%d liquid), %d brush entities "
              "(%d movers), %d lights, %d force fields -> %s"
              % (path.name, len(ws), len(ws) - 1, len(d["brush_entities"]),
                 sum(1 for b in d["brush_entities"] if b["entity_id"] is not None),
                 len(d["lights"]), len(d["force_fields"]), dest))
    return 0


WATER_RGB, LAVA_RGB, MOVER_RGB = (60, 120, 230), (240, 110, 30), (170, 110, 210)


def _circle(put, cx, cz, r, rgb):
    steps = max(12, int(r * 6))
    for i in range(steps):
        a = 2 * math.pi * i / steps
        put(int(cx + r * math.cos(a)), int(cz + r * math.sin(a)), rgb)


def _hull2d(ps):
    ps = sorted(set(ps))
    if len(ps) < 3:
        return ps

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lo, hi = [], []
    for p in ps:
        while len(lo) >= 2 and cross(lo[-2], lo[-1], p) <= 0:
            lo.pop()
        lo.append(p)
    for p in reversed(ps):
        while len(hi) >= 2 and cross(hi[-2], hi[-1], p) <= 0:
            hi.pop()
        hi.append(p)
    return lo[:-1] + hi[:-1]


def cmd_render(args):
    """Top-down check of the description: liquid sectors tinted by content,
    brush entities in purple, point and ambient lights as fall-off rings in their own
    colour (dark lights in magenta), directional lights as an arrow."""
    args.output.mkdir(parents=True, exist_ok=True)
    for path in args.files:
        cont = Container(path)
        img = cont.image
        pts = positions(img)
        d = describe(cont, path)
        content = {g: (LAVA if s.get("liquid_surface") == "lava" else
                       WATER if s.get("liquid_surface") else s["content"])
                   for s in d["worldbase"]["sectors"] for g in s["groups"]}
        content.update({b["group"]: b["content"] for b in d["brush_entities"]
                        if b["group"]})
        movable = {b["group"] for b in d["brush_entities"] if b["group"]}

        def band(vals):
            v = sorted(vals)
            n = len(v) - 1
            return v[n // 100], v[n - n // 100]

        # Bound by what is actually drawn: some levels (Alevel11_3) carry
        # far-flung vertices that no display list uses, and bounding by the
        # whole position array squashes the level into a corner.
        drawn = [space.world(q) for tri in world_triangles(cont) for q in tri]
        x0, x1 = band([p[0] for p in drawn])
        z0, z1 = band([p[2] for p in drawn])
        W = args.size
        s = (W - 20) / max(1e-6, x1 - x0)
        H = max(64, min(4096, int((z1 - z0) * s) + 20))
        buf = bytearray(b"\x0e\x10\x16\xff" * (W * H))

        def put(x, z, rgb):
            if 0 <= x < W and 0 <= z < H:
                i = (z * W + x) * 4
                buf[i:i + 3] = bytes(rgb)

        def px(p):
            return int(10 + (p[0] - x0) * s), int(10 + (p[2] - z0) * s)

        layers = ([], [])                 # draw liquids and movers on top
        for owner, tl in batches(cont):
            if not owner:
                continue
            g = ("mat%d" if owner["kind"] == "material" else "sector%d") % owner["index"]
            c = content.get(g, AIR)
            rgb = (WATER_RGB if c == WATER else LAVA_RGB if c == LAVA
                   else MOVER_RGB if g in movable else (58, 62, 72))
            (layers[1] if rgb != (58, 62, 72) else layers[0]).append(
                (owner["xform"], tl, rgb))
        def draw(layer):
            for xf, tl, rgb in layer:
                for tri in tl:
                    if max(v[F_POS] for v in tri) < len(pts):
                        _fill_tri(put, [px(space.world(apply_xform(xf, pts[v[F_POS]])))
                                        for v in tri], rgb)

        draw(layers[0])
        # region volumes: footprint of each convex cell, darker than the
        # liquid surfaces drawn over them
        for cell in d["region_brush"]["sectors"]:
            h = _hull2d([px(v) for v in cell["vertices"]])
            rgb = (30, 60, 150) if cell["content"] == WATER else (150, 60, 20)
            for k in range(1, len(h) - 1):
                _fill_tri(put, [h[0], h[k], h[k + 1]], rgb)
        draw(layers[1])

        for l in d["lights"]:
            p = l["props"]
            if l.get("skip"):
                continue
            if "direction" in l:
                dx, _, dz = l["direction"]
                n = math.hypot(dx, dz) or 1.0
                for k in range(40):
                    put(int(W - 50 + dx / n * k), int(50 + dz / n * k), (255, 240, 120))
                continue
            cx, cz = px(l["position"])
            rgb = (255, 0, 255) if p["Dark light"] else tuple(p["Color"])
            _circle(put, cx, cz, max(1.0, p["Fall-off"] * s), rgb)
            put(cx, cz, (255, 255, 255))
        for f in d["force_fields"]:
            cx, cz = px(f["matrix"]["translation"])
            dx, _, dz = f["se1"]["direction"]
            for k in range(14):
                put(int(cx + dx * k), int(cz + dz * k), (120, 255, 160))

        dest = args.output / (path.stem + "_world.png")
        write_png(dest, W, H, bytes(buf))
        print("%s -> %s" % (path.name, dest))
    return 0


def cmd_verify(args):
    roots = args.paths or [Path("orig/files/Levels")]
    files = []
    for r in roots:
        r = Path(r)
        files.extend(sorted(r.rglob("*.ssw")) if r.is_dir() else [r])
    tot = Counter()
    problems = []
    for f in sorted(set(files)):
        d = describe(Container(f), f)
        ws = d["worldbase"]["sectors"]
        tot["levels"] += 1
        tot["liquid surfaces"] += len(ws) - 1
        tot["water surfaces"] += sum(1 for x in ws if x.get("liquid_surface") == "water")
        tot["lava surfaces"] += sum(1 for x in ws if x.get("liquid_surface") == "lava")
        rc = d["region_brush"]["sectors"]
        tot["region cells"] += len(rc)
        tot["levels with regions"] += bool(rc)
        tot["water cells"] += sum(1 for x in rc if x["content"] == WATER)
        tot["lava cells"] += sum(1 for x in rc if x["content"] == LAVA)
        tot["underwater env"] += sum(1 for x in rc if x["environment"] == UNDERWATER)
        tot["cells with haze"] += sum(1 for x in rc if x["haze"])
        tot["haze markers"] += len(d["haze_markers"])
        if len(d["haze_markers"]) > 4:
            problems.append("%s: more than 4 haze slots" % f.stem)
        for x in rc:
            if len(x["vertices"]) < 4 or len(x["faces"]) < 4:
                problems.append("%s: degenerate region cell" % f.stem)
        tot["brush entities"] += len(d["brush_entities"])
        for b in d["brush_entities"]:
            tot["brush: " + b["class"]] += 1
            tot["invisible brushes"] += b["invisible"]
            tot["brushes with collision triangles"] += bool(b["collision_triangles"])
            tot["empty brushes"] += b["invisible"] and not b["collision_triangles"]
            tot["passable brushes"] += b["passable"]
            tot["passable WorldBase brushes"] += b["passable"] and b["class"] == "WorldBase"
            mo = b.get("motion")
            if b["class"] == "MovingBrush":
                tot["movers with motion"] += mo is not None
                if mo is None:
                    problems.append("%s: MovingBrush %s has no keyframes" % (f.stem, b["name"]))
            if mo:
                ms = mo["markers"]
                tot["markers"] += len(ms)
                tot["auto-start movers"] += mo["se1"]["Auto start"]
                tot["movers that never stop (loop)"] += not any(
                    m["se1"]["Stop moving"] for m in ms)
                tot["zero-time keyframes"] += sum(1 for m in ms if m["se1"]["Speed"] == 0)
                for k in ("start", "loop", "end"):
                    snd = mo["sounds"][k]
                    if snd:
                        tot["mover sounds resolved" if "samples" in snd
                            else "mover sounds missing in game"] += 1
                if any(m["position"] is None for m in ms):
                    problems.append("%s: marker without a position" % f.stem)
                br, m0 = b["matrix"]["brush_rows"], ms[0]["rows"]
                if m0 and all(abs(m0[i][j] - br[i][j] / (math.sqrt(sum(x * x for x in br[i])) or 1)) < 2e-3
                              for i in range(3) for j in range(3)):
                    tot["key 0 rotation == brush rotation"] += 1
                if math.dist(ms[0]["position"], b["matrix"]["translation"]) > 1e-2:
                    tot["key 0 off the brush placement"] += 1
        for x in d["scripts"]["entities"]:
            tot["scripts: %s" % x["se1"]["class"]] += 1
            tot["script links"] += len(x["links"])
            tot["scripts"] += 1
        for why, n in d["scripts"]["skipped"].items():
            tot["scripts not written: %s" % why] += n
        tot["touch fields"] += len(d["touch_fields"])
        tot["touch targets resolved"] += sum(1 for t in d["touch_fields"] if t["target_resolves"])
        tot["touch targets set"] += sum(1 for t in d["touch_fields"] if t["target_resolves"] is not None)
        tot["touch fields with a volume"] += sum(1 for t in d["touch_fields"] if t["volume"])
        tot["touch volumes closed"] += sum(1 for t in d["touch_fields"]
                                           if t["volume"] and t["volume"]["closed"])
        tot["bouncers"] += len(d["bouncers"])
        tot["bouncers with a volume"] += sum(1 for b in d["bouncers"] if b["volume"])
        tot["bouncer volumes closed"] += sum(1 for b in d["bouncers"]
                                             if b["volume"] and b["volume"]["closed"])
        tot["music entities"] += len(d["music"])
        tot["music -> MusicHolder"] += sum(1 for m in d["music"] if m.get("se1", {}).get("class") == "MusicHolder")
        tot["music -> MusicChanger"] += sum(1 for m in d["music"] if m.get("se1", {}).get("class") == "MusicChanger")
        tot["music hash not a track"] += sum(1 for m in d["music"] if m.get("missing"))
        tot["levels with music"] += bool(d["music"])
        for s in d["sounds"]:
            smp, sp = s["sample"], s["speech"]
            tot["sound holders"] += 1
            if smp:
                tot["sound: sample resolved" if "samples" in smp
                    else "sound: sample missing in game"] += 1
            if sp:
                tot["sound: speech resolved" if "line" in sp
                    else "sound: speech missing in game"] += 1
            tot["sound: silent"] += not smp and not sp
            tot["sound -> SoundHolder"] += "se1" in s
            tot["sound: looping"] += s.get("se1", {}).get("Looping", False)
            tot["sound: corrected like the game"] += s["clamped"]
            want = ([s["se1"]["Sound"]] if "se1" in s else []) + list(
                (sp or {}).get("files", {}).values())
            if not all(Path(p).exists() for p in want):
                problems.append("%s: sound file missing for %s" % (f.stem, s["name"]))
        for m in d["messages"]:
            tot["messages"] += 1
            tot["messages resolved"] += m["resolved"]
            if not m["resolved"]:
                problems.append("%s: MessageHolder %d text unresolved" % (f.stem, m["entity_id"]))
            if not all(Path(p).exists() for p in m["files"].values()):
                problems.append("%s: message file missing (tools/text.py messages)" % f.stem)
        for c in d["cameras"]:
            tot["cameras"] += 1
            tot["cameras: static" if c["static"] else "cameras: with a path"] += 1
            ed = se1_rotation(*c["angles"])
            tot["camera rotation == editor angles"] += max(
                abs(c["rotation"][i][j] - ed[i][j]) for i in range(3) for j in range(3)) < 2e-3
            tot["camera paths that stop at the end"] += bool(
                c["markers"] and c["markers"][-1]["se1"]["Stop moving"])
            for m in c["markers"]:
                tot["camera markers"] += 1
                if m["position"] is None:
                    problems.append("%s: camera %d marker without a position"
                                    % (f.stem, c["entity_id"]))
                ed = se1_rotation(*m["angles"])
                tot["marker rotation == editor angles"] += max(
                    abs(m["rotation"][i][j] - ed[i][j]) for i in range(3) for j in range(3)) < 2e-3
                if m["own_matrix"]:
                    tot["markers with their own Mtx"] += 1
                    tot["marker rotation == own Mtx"] += max(
                        abs(m["rotation"][i][j] - m["placement_rot"][i][j])
                        for i in range(3) for j in range(3)) < 2e-3
                if m["trigger_resolves"] is not None:
                    tot["marker triggers"] += 1
                    tot["marker triggers resolved"] += m["trigger_resolves"]
                    if not m["trigger_resolves"]:
                        problems.append("%s: camera %d trigger %s unresolved"
                                        % (f.stem, c["entity_id"], m["se1"]["Trigger"]))
        tot["props"] += len(d["props"])
        tot["multi-material props"] += sum(1 for x in d["props"] if x["materials"] > 1)
        for x in d["props"]:
            tot["props: " + x["se1"]["class"]] += 1
        tot["effects"] += len(d["effects"])
        for x in d["effects"]:
            tot["effects: %s" % (x.get("effect") or x.get("se1", {}).get("class") or "unknown")] += 1
        tot["props with damage states"] += sum(
            1 for x in d["props"] if x.get("damage_states"))
        for x in d["props"]:
            files = [x["file"]] + [s["file"] for s in x.get("damage_states", [])]
            gone = [p for p in files if not Path(p).exists()]
            if gone:
                problems.append("%s: model file missing: %s" % (f.stem, gone[0]))
                break
        tot["liquid brushes"] += sum(1 for b in d["brush_entities"] if b["content"])
        tot["lights"] += len(d["lights"])
        tot["directional"] += sum(1 for l in d["lights"] if "direction" in l)
        for l in d["lights"]:
            if not l.get("skip"):
                tot["created as %s" % l["props"]["Type"]] += 1
        tot["dark"] += sum(1 for l in d["lights"] if l["props"]["Dark light"])
        tot["clamped"] += sum(1 for l in d["lights"] if l["ne"]["clamped"])
        tot["force fields"] += len(d["force_fields"])
        tot["force fields emitted"] += sum(1 for x in d["force_fields"] if x["emit"])
        tot["sun fills folded"] += sum(1 for l in d["lights"]
                                       if "folded into" in l.get("skip", ""))
        tot["no-reach skipped"] += sum(1 for l in d["lights"]
                                       if l.get("skip", "").startswith("fall-off"))
        tot["second copies dropped"] += sum(1 for l in d["lights"]
                                            if l.get("skip", "").startswith("second copy"))
        tot["other unbaked dropped"] += sum(1 for l in d["lights"]
                                            if l.get("skip", "").startswith("+0x3c"))
        tot["unlit suns dropped"] += sum(1 for l in d["lights"]
                                         if l.get("skip", "").startswith("horizontal"))
        tot["unpaired fills"] += sum(1 for l in d["lights"]
                                     if l["ne"].get("unpaired_fill"))
        tot["lights to create"] += sum(1 for l in d["lights"] if not l.get("skip"))
        for l in d["lights"]:
            if not all(0 <= c <= 255 for c in l["props"]["Color"]):
                problems.append("%s: light colour out of range" % f.stem)
            if ("direction" not in l and not l.get("skip")
                    and l["props"]["Fall-off"] <= 0):
                problems.append("%s: point or ambient light with no fall-off" % f.stem)
            if l["props"]["Dark light"] and "direction" in l:
                problems.append("%s: dark directional (SE1 refuses)" % f.stem)
        # every referenced OBJ group must exist in the exported mesh
        obj = Path(d["geometry"])
        if obj.exists():
            have = {line.split()[1] for line in obj.read_text().splitlines()
                    if line.startswith("usemtl ")}
            want = {g for x in ws for g in x["groups"]}
            want |= {b["group"] for b in d["brush_entities"] if b["group"]}
            missing = want - have
            if missing:
                problems.append("%s: %d groups missing from the OBJ"
                                % (f.stem, len(missing)))
        else:
            problems.append("%s: no OBJ at %s" % (f.stem, obj))
    for k in ("levels", "liquid surfaces", "water surfaces", "lava surfaces",
              "levels with regions", "region cells", "water cells",
              "lava cells", "underwater env", "cells with haze",
              "haze markers", "brush entities",
              "brush: MovingBrush", "brush: DestroyableArchitecture",
              "brush: WorldBase", "invisible brushes", "empty brushes",
              "brushes with collision triangles", "passable brushes",
              "passable WorldBase brushes", "movers with motion",
              "markers", "auto-start movers", "movers that never stop (loop)",
              "zero-time keyframes", "key 0 off the brush placement",
              "key 0 rotation == brush rotation",
              "mover sounds resolved", "mover sounds missing in game",
              "touch fields",
              "touch targets set", "touch targets resolved",
              "touch fields with a volume", "touch volumes closed", "bouncers",
              "bouncers with a volume", "bouncer volumes closed",
              "music entities", "levels with music", "music -> MusicHolder",
              "music -> MusicChanger", "music hash not a track",
              "sound holders", "sound -> SoundHolder", "sound: sample resolved",
              "sound: sample missing in game", "sound: speech resolved",
              "sound: speech missing in game", "sound: silent", "sound: looping",
              "sound: corrected like the game", "messages", "messages resolved",
              "cameras", "cameras: static", "cameras: with a path",
              "camera rotation == editor angles", "marker rotation == editor angles",
              "camera paths that stop at the end", "camera markers",
              "markers with their own Mtx", "marker rotation == own Mtx",
              "marker triggers", "marker triggers resolved",
              "props",
              "multi-material props", "props: ModelHolder2",
              "props: ModelHolder3", "props with damage states", "effects",
              "effects: smallflame", "effects: bigflame", "effects: Marker",
              "effects: unknown",
              "liquid brushes", "lights", "directional",
              "dark", "clamped", "sun fills folded", "no-reach skipped", "second copies dropped", "other unbaked dropped", "unlit suns dropped",
              "unpaired fills", "lights to create", "created as LT_POINT",
              "created as LT_AMBIENT", "created as LT_DIRECTIONAL", "force fields",
              "force fields emitted", "scripts", "script links"):
        print("  %-16s %s" % (k, format(tot[k], ",")))
    for k in sorted(k for k in tot if k.startswith("scripts: ") or k.startswith("scripts not written")):
        print("  %-16s %s" % (k, format(tot[k], ",")))
    for p in problems[:20]:
        print("  PROBLEM " + p)
    print("%d problems" % len(problems))
    return 1 if problems else 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("describe", help="write the SE1 world description JSON")
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument("-o", "--output", type=Path, default=Path("build/world"))
    p.set_defaults(func=cmd_describe)
    p = sub.add_parser("render", help="top-down check of sectors and lights")
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument("-o", "--output", type=Path, default=Path("build/world"))
    p.add_argument("--size", type=int, default=1100)
    p.set_defaults(func=cmd_render)
    p = sub.add_parser("verify", help="invariants across levels")
    p.add_argument("paths", nargs="*", type=Path)
    p.set_defaults(func=cmd_verify)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
