#!/usr/bin/env python3
"""
Entity and spawn data for Next Encounter levels (.ssw / WORLDMESH).

The entity list is a linked list off `image + 0x40`. Nodes are variable
length -- the name and the transform are stored inline -- so every field that
would otherwise move is reached through a pointer:

    struct CcEntity {
        CcEntity* next;      // +0x00
        Mtx*      xform;     // +0x04  -> the inline 3x4 matrix, past the name
        char*     name;      // +0x08  designer's label; pooled when repeated
        u32       classId;   // +0x0c  selects the props struct (see CLASSES)
        void*     props;     // +0x10  == xform + 48, the class payload
        u32       id;        // +0x14  unique within the level; link target
        u32       flags;     // +0x18
        ...                  // +0x2c  name inline here when not pooled
                             //        Mtx  (3x4 floats, row-major)
                             //        props
    };

`props` is always `xform + 48` (25,125 of 26,774 entities; the rest have a
larger gap, never a smaller one), and the props blob runs to the next node, so
its length is `next - props`. That length is **constant per classId**, which is
the evidence that classId selects a struct type rather than just labelling one.

The matrix is a GameCube `Mtx` -- f32[3][4], row-major, translation in column
3. Checked against each level's own vertex bounds: 98% of the 3x3 parts are
orthonormal (the rest carry scale) and 99.5% of translations land inside the
level.

Two enumerations are resolved from outside the level file:

  * **Minion type** (class 5000, props +0x00) is not the `ObjectModel<N>`
    index. `main.dol` holds a 61-entry permutation at 0x8021ab2c mapping one to
    the other; it is a bijection onto 0..60, and it agrees with the designers'
    names on all 41 indices the levels actually use. Baked in as MINION_MODEL.

  * **Item type** (the pickup classes, props +0x00) is the order of
    `base.cfg`'s `RespawnTimes` stash, which lists the item types as a run.
    Entries 0..45 are confirmed by class id: each pickup class occupies exactly
    one contiguous range (health 0-4, armour 5-9, powerup 10-12, ammo pack
    17-18, ammo 19-34, weapon 35-45). Past 45 `RespawnTimes` skips the items
    that never respawn, so keys/treasure/DM slots are labelled from the
    designers' names instead and marked with a trailing '?'.

Usage:
    python tools/entity.py list     orig/files/Levels/Rlevel1_1.ssw
    python tools/entity.py classes  orig/files/Levels
    python tools/entity.py models   orig/files/Levels
    python tools/entity.py graph    orig/files/Levels
    python tools/entity.py graph    orig/files/Levels/Rlevel1_1.ssw --chain "drillAmbush"
    python tools/entity.py enemies  orig/files/Levels/Rlevel1_1.ssw
    python tools/entity.py json     orig/files/Levels/Rlevel1_1.ssw -o build/entities/
    python tools/entity.py map      orig/files/Levels/Rlevel1_1.ssw -o build/maps/
    python tools/entity.py verify   orig/files/Levels
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
from ssg import Container                                    # noqa: E402
from level import positions, world_triangles                 # noqa: E402
from gxtex import write_png                                  # noqa: E402
import space                                                     # noqa: E402

ENTITY_LIST = 0x40
E_NEXT, E_XFORM, E_NAME, E_CLASS = 0x00, 0x04, 0x08, 0x0C
E_PROPS, E_ID, E_FLAGS = 0x10, 0x14, 0x18

# classId -> (short name, category). Names come from what the designers called
# the entities of that class -- every class here has a dominant label, often
# the bare class name ('Trigger', 'Damager', 'DoorController', 'LevelPar').
# Category is only used for grouping and colouring.
CLASSES = {
    2100: ("Health",           "pickup"),
    2200: ("Armour",           "pickup"),
    3100: ("Weapon",           "pickup"),
    3200: ("Ammo",             "pickup"),
    3201: ("AmmoPack",         "pickup"),
    3210: ("Key",              "pickup"),
    3215: ("Treasure",         "pickup"),
    3220: ("Powerup",          "pickup"),
    4010: ("Copier",           "logic"),
    4020: ("Teleport",         "logic"),
    4030: ("Watcher",          "logic"),
    4040: ("Wave",             "spawn"),
    4050: ("DoorController",   "logic"),
    4060: ("TeleportTarget",   "marker"),
    4070: ("Mover",            "world"),
    4080: ("Trigger",          "logic"),
    4090: ("Music",            "sound"),
    4100: ("Sound",            "sound"),
    4110: ("Damager",          "logic"),
    4120: ("Camera",           "logic"),
    4130: ("MessageHolder",    "logic"),
    4140: ("PlayerStart",      "marker"),
    4150: ("Marker",           "marker"),
    4160: ("WorldLink",        "logic"),
    4170: ("TouchField",       "logic"),
    4180: ("Switch",           "logic"),
    4190: ("Vehicle",          "world"),
    4200: ("Prop",             "world"),
    4210: ("ModelDestruction", "world"),
    4220: ("Bouncer",          "world"),
    4230: ("RollingStone",     "world"),
    4240: ("DestroyableArch",  "world"),
    4260: ("Arrow",            "pickup"),
    4270: ("LockDown",         "pickup"),
    4280: ("WarpPlayers",      "logic"),
    4310: ("ParticleHolder",   "fx"),
    4320: ("Flag",             "pickup"),
    4330: ("LevelPar",         "logic"),
    4340: ("FMVPlayer",        "logic"),
    5000: ("EnemyTemplate",    "enemy"),
    5010: ("LightFlare",       "fx"),
    5020: ("Unknown5020",      "logic"),
    6002: ("LevelRoot",        "logic"),
}

# main.dol 0x8021ab2c (file offset 0x217b0c): minion type index ->
# ObjectModel<N>. A bijection onto 0..60; see the module docstring.
MINION_MODEL = (
    0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12,
    22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34,
    35, 36, 37, 38, 39, 40, 45, 42, 15, 16, 17, 18, 19, 20, 21,
    46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60,
    41, 43, 44, 13, 14,
)

# base.cfg RespawnTimes order, confirmed against the pickup class ranges.
ITEM_TYPES = (
    "Health5", "Health10", "Health25", "Health50", "Health100",
    "Armour5", "Armour25", "Armour50", "Armour100", "Armour200",
    "SeriousSkates", "SeriousUnderpants", "SeriousDamage", "SeriousSmartbomb",
    "SeriousBubblegum", "SeriousRing", "SeriousGlasses",
    "GreenAmmoPack", "RedAmmoPack",
    "9MMBullets", "RicochetBullets", "HomingBullets", "ShotgunShells",
    "SniperBullets", "Napalm", "LiquidNitrogen", "LaughingGas",
    "LimpetGrenades", "SpiderMines", "Grenades", "StandardRockets",
    "HeatSeekerRocket", "SonicRocket", "PowerCells", "CannonBalls",
    "Drill", "DesertHawk", "Uzi", "Shotgun", "Minigun", "RocketLauncher",
    "GrenadeLauncher", "GasGun", "SniperRifle", "PowerGun", "Cannon",
)
# Past 45, RespawnTimes only lists what respawns, so these come from the
# designers' labels rather than the config, and keep a trailing '?'.
ITEM_TYPES_OBSERVED = {
    46: "Key?", 47: "Key?", 48: "Key?", 49: "Key?", 54: "Treasure?",
    55: "SeriousBomb?", 57: "DMWeapon1?", 58: "DMWeapon2?", 59: "DMWeapon3?",
    60: "DMWeapon4?", 61: "DMWeapon5?", 67: "DMAmmo1?", 68: "DMAmmo2?",
    69: "DMAmmo3?", 70: "DMAmmo4?", 71: "DMAmmo5?",
}
# ParticlesHolder type (+0x02) -> SE1 ParticlesHolderType, by the designers'
# names for each value (LockTwinkle, Particles fountain, PHold_Teleport,
# VentSmoke, Steam Particle, particle holder lightning). NE numbers them its
# own way; 3 is not used on the disc. The init (0x8008133c) maps each to an
# effect flag.
PARTICLE_TYPES = {0: "Lock Twinkle", 1: "Waterfall Spray1", 2: "Teleport",
                  4: "Smoke", 5: "Steam", 6: "Lightning"}
# ModelDestruction debris type (+0x04) is SE1's ModelDebrisType unchanged:
# urns 0, statues 1, barrels and furniture 2, trees 3, alien plants 4,
# pearl globes 5.
DEBRIS_TYPES = ("Pottery", "Stone", "Wood", "Tree", "Vegetation", "Ice", "Honeycomb")

PICKUP_CLASSES = {2100, 2200, 3100, 3200, 3201, 3210, 3215, 3220,
                  4260, 4270, 4320}


def u32(img, o):
    return struct.unpack_from(">I", img, o)[0]


def f32(img, o):
    return struct.unpack_from(">f", img, o)[0]


def cstr(img, o, limit=96):
    end = img.find(b"\x00", o, o + limit)
    return img[o:end if end >= 0 else o + limit].decode("ascii", "replace")


def item_name(idx):
    if idx < len(ITEM_TYPES):
        return ITEM_TYPES[idx]
    return ITEM_TYPES_OBSERVED.get(idx, "item%d" % idx)


def minion_model(idx):
    return MINION_MODEL[idx] if 0 <= idx < len(MINION_MODEL) else None


def transform(img, off):
    """The 3x4 Mtx at `off` -> (3x3 rows, translation)."""
    f = struct.unpack_from(">12f", img, off)
    return ((f[0:3], f[4:7], f[8:11]), (f[3], f[7], f[11]))


def entities(container):
    """Walk the entity list. Returns one dict per entity, in list order."""
    img = container.image
    n = len(img)
    node = u32(img, ENTITY_LIST)
    seen = set()
    out = []
    while node and node + 0x20 <= n and node not in seen:
        seen.add(node)
        nm = u32(img, node + E_NAME)
        cls = u32(img, node + E_CLASS)
        xf = u32(img, node + E_XFORM)
        rot = pos = None
        if 0 < xf and xf + 48 <= n:
            rot, pos = transform(img, xf)
        out.append({
            "offset": node,
            "id": u32(img, node + E_ID),
            "cls": cls,
            "kind": CLASSES.get(cls, ("Class%d" % cls, "unknown"))[0],
            "category": CLASSES.get(cls, (None, "unknown"))[1],
            "name": cstr(img, nm) if 0 < nm < n else "",
            "pos": pos,
            "rot": rot,
            "props": u32(img, node + E_PROPS),
            "flags": u32(img, node + E_FLAGS),
        })
        node = u32(img, node + E_NEXT)

    # The props blob runs from `props` to the next node in address order.
    order = sorted(e["offset"] for e in out)
    nxt = dict(zip(order, order[1:]))
    for e in out:
        end = nxt.get(e["offset"])
        e["props_size"] = (end - e["props"]) if end and end > e["props"] else 0
        e.update(_decode(img, e))
    return out


def _opt(v):
    return None if v == 0xFFFFFFFF else v


def _inline_text(img, p, size):
    """Longest printable NUL-terminated run inside a props blob.

    The classes whose props size varies are exactly the ones that append a
    string: MessageHolder carries a localisation key (NETRISCA_*, which is what
    the .tdb databases are keyed on) and Key carries an asset path.
    """
    best = ""
    for part in img[p:p + size].split(b"\x00"):
        if len(part) > len(best) and len(part) >= 4 and all(
                32 <= b < 127 for b in part):
            best = part.decode("ascii")
    return best or None


# Trigger event codes. NE uses three values, not SE1's numbering: 0 goes to
# things that get fired (6,489 of 6,501 slots aimed at a Wave), 1 and 2 to
# things that get switched (watchers, objective arrows, particles, lockdowns).
# Triggers named "...deactivate/stop/off" send 2 in 132 of their 167 switch
# slots; triggers named "...activate/start/on" lean to 1 (136 vs 109). So
# 1 = on, 2 = off -- likely, not proven.
EVENTS = {0: "trigger", 1: "on", 2: "off"}

# Entity-id link fields, per class: name -> props offset. Each resolves to a
# real entity id on >= 98% of its non-null values disc-wide, and points at the
# classes its name says -- `tools/entity.py graph` re-checks both.
LINKS = {
    4040: {"template": 0x14, "patrol": 0x24},      # Wave -> EnemyTemplate / Marker
    5000: {"death_target": 0x08},                  # EnemyTemplate -> Trigger
    4030: {"target": 0x08},                        # Watcher -> Trigger
    4010: {"target": 0x00},                        # Copier -> template entity
    4020: {"target": 0x0C},                        # Teleport -> TeleportTarget
    4050: {"target1": 0x10, "target2": 0x14},      # DoorController
    4180: {"target": 0x04},                        # Switch -> Trigger
    4110: {"entity": 0x04},                        # Damager -> what it damages
    4150: {"next": 0x18, "target": 0x2C},          # Marker -> next Marker (paths)
    4170: {"enter_target": 0x0C},                  # TouchField -> Trigger
    4070: {"switch": 0x24},                        # Mover -> Switch
    4200: {"destruction": 0x08},                   # Prop -> ModelDestruction
    4100: {"parent": 0x08},                        # SoundHolder -> its parent
    4190: {"link_04": 0x04, "link_0c": 0x0C},      # Vehicle -> Trigger / WorldLink
}
for _c in PICKUP_CLASSES - {4270, 4320}:          # LockDown and Flag are not
    LINKS.setdefault(_c, {})["target"] = 0x08     # pickups; fired on pickup
LINKS[4260] = {"points_at": 0x08}                  # Arrow -> the item it marks


def _decode(img, e):
    """Class-specific fields. Only what is established goes in here."""
    p, size, cls = e["props"], e["props_size"], e["cls"]
    if not p or size < 4 or p + size > len(img):
        return {}
    out = {}
    text = _inline_text(img, p, size)
    if text:
        out["text"] = text
    links = {}
    for name, off in LINKS.get(cls, {}).items():
        if off + 4 <= size:
            v = _opt(u32(img, p + off))
            # id 0 is "no target": the level root and every unnamed class-5020
            # entity carry id 0, so a link of 0 is null rather than a pointer.
            if v:
                links[name] = v

    # Arrow and LockDown sit in the pickup list for their links, but their
    # +0x00 is Active (their inits test it), not an item index.
    if cls in PICKUP_CLASSES and cls not in (4260, 4270):
        idx = u32(img, p)
        if idx == 0xFFFFFFFF:
            out.update(item=None, item_name=None)
        else:
            out.update(item=idx, item_name=item_name(idx))
    elif cls == 5000 and size >= 12:
        t = u32(img, p)
        out.update(minion=t, model=minion_model(t))
    elif cls == 4040 and size >= 0x28:
        out.update(template=u32(img, p + 0x14), count=u32(img, p + 0x0C),
                   delay=f32(img, p), interval=f32(img, p + 4))
    elif cls == 4080 and size >= 0x7C:
        # SE1 Trigger's own shape: 10 targets, 10 event types, a count, a
        # max-trigs that defaults to -1, a wait time.
        targets = []
        for i in range(10):
            t = u32(img, p + 0x14 + 4 * i)
            if t not in (0, 0xFFFFFFFF):
                ev = u32(img, p + 0x3C + 4 * i)
                targets.append({"target": t, "event": EVENTS.get(ev, ev)})
                links["target%02d" % (i + 1)] = t
        out.update(active=bool(u32(img, p)), targets=targets,
                   use_count=bool(u32(img, p + 0x64)),
                   count=u32(img, p + 0x68),
                   max_trigs=struct.unpack_from(">i", img, p + 0x74)[0],
                   wait=f32(img, p + 0x78))
    elif cls == 4030 and size >= 0x10:            # -> SE1 WatchPlayers
        out.update(active=bool(u32(img, p)), distance=f32(img, p + 4),
                   wait=f32(img, p + 0x0C))
    elif cls == 4020 and size >= 0x10:            # -> SE1 Teleport
        out.update(active=bool(u32(img, p)), width=f32(img, p + 4),
                   height=f32(img, p + 8))
    elif cls == 4050 and size >= 0x18:            # -> SE1 DoorController
        out.update(active=bool(u32(img, p)), width=f32(img, p + 4),
                   height=f32(img, p + 8), type=u32(img, p + 0x0C))
    elif cls == 4110 and size >= 8:               # -> SE1 Damager
        out.update(amount=f32(img, p))
    elif cls == 4120 and size >= 0x30:            # -> SE1 Camera + CameraMarkers
        # SE1's Camera -> CameraMarker chain, packed into the camera's props.
        # Header {f32 fov, f32 time, marker* first, 0, 0, f32 h, p, b, 0 x4}:
        # a 48-byte camera has no markers and holds its own view for `time`.
        # Each marker, as the update (0x80078a38) and its loader (0x8007992c)
        # read it:
        #   +0x00 f32 fov         +0x04 skip to next    +0x08 stop moving
        #   +0x0c trigger id      +0x10 f32 delta time  +0x14 Mtx* placement
        #   +0x18 next marker*    +0x1c f32 (0..0.7)    +0x20 f32 tension
        #   +0x24, +0x28 f32 bias and continuity (0 on the whole disc)
        #   +0x2c f32 (-1 on the whole disc)
        #   +0x30 f32 h, p, b     SE1 editor angles
        #   +0x48 Mtx             own placement, 0x78-byte records only
        # The game takes the position from the placement's translation and
        # the orientation from the angles, never the Mtx rotation. The spline
        # is Kochanek-Bartels over a 4-marker window.
        markers, first = [], u32(img, p + 8)
        rec = first
        while rec and rec + 0x48 <= len(img) and len(markers) < 64:
            xf = u32(img, rec + 0x14)
            rot, pos = transform(img, xf) if 0 < xf and xf + 48 <= len(img) else (None, None)
            trig = _opt(u32(img, rec + 0x0C))
            markers.append({
                "pos": pos, "angles": [f32(img, rec + 0x30 + 4 * k) for k in range(3)],
                "fov": f32(img, rec), "delta_time": f32(img, rec + 0x10),
                "skip_to_next": bool(u32(img, rec + 4)),
                "stop_moving": bool(u32(img, rec + 8)), "trigger": trig,
                "tension": f32(img, rec + 0x20),
                "bias_continuity": [f32(img, rec + 0x24), f32(img, rec + 0x28)],
                "unknown_1c": f32(img, rec + 0x1C), "unknown_2c": f32(img, rec + 0x2C),
                "own_matrix": xf == rec + 0x48,
                "placement_rot": rot})
            if trig:
                links["marker%02d_trigger" % len(markers)] = trig
            rec = u32(img, rec + 0x18)
            if rec == first:
                break
        out.update(fov=f32(img, p), time=f32(img, p + 4),
                   angles=[f32(img, p + 0x14 + 4 * k) for k in range(3)],
                   markers=markers, loops=bool(markers) and rec == first)
    elif cls == 4330 and size >= 0x14:            # -> GCLevelPar
        # The ParWatcher init (0x80081750) reads the five in GCLevelPar's order.
        out.update(par_time=f32(img, p), par_kills=f32(img, p + 4),
                   bronze_score=f32(img, p + 8), silver_score=f32(img, p + 0x0C),
                   gold_score=f32(img, p + 0x10))
    elif cls == 4160 and size >= 0x0C:            # -> SE1 WorldLink
        # Its init logs "WorldName %s" (+0x04) and "EndGameFlag %d" (u16 +0x08).
        # +0x00 is 2 on the whole disc: SE1's WorldLinkType "Relative".
        w = u32(img, p + 4)
        out.update(type=u32(img, p), world=cstr(img, w) if 0 < w < len(img) else None,
                   end_game=bool(struct.unpack_from(">H", img, p + 8)[0]))
    elif cls == 4340 and size >= 4:               # -> GCFMVPlayer
        w = u32(img, p)
        out.update(fmv=cstr(img, w) if 0 < w < len(img) else None)
    elif cls == 4280 and size >= 8:               # -> GCWarpPlayers
        # +0x00 Active (its init tests it); u16 +0x04 the PlayerStart to warp to.
        out.update(active=bool(u32(img, p)))
        t = struct.unpack_from(">H", img, p + 4)[0]
        if t != 0xFFFF:
            links["target"] = t
    elif cls == 4270 and size >= 8:               # -> GCLockDown
        out.update(active=bool(u32(img, p)), scale=f32(img, p + 4))
    elif cls == 4260 and size >= 0x14:            # -> GCArrow
        # The init picks "yellowarrow" / "greenarrow" from +0x10 (1 / 2).
        out.update(active=bool(u32(img, p)), inherit_pos=bool(u32(img, p + 4)),
                   do_not_scale=bool(u32(img, p + 0x0C)), type=u32(img, p + 0x10))
    elif cls == 4310 and size >= 0x28:            # -> SE1 ParticlesHolder
        # s16 +0x00 Active and +0x02 type, as the init (0x8008133c) reads them,
        # then GCN's Count, StretchAll, StretchX/Y/Z, Size, Param1-3.
        act, t, n = struct.unpack_from(">HHH", img, p)
        out.update(active=bool(act), type=t, type_name=PARTICLE_TYPES.get(t), count=n,
                   stretch_all=f32(img, p + 8),
                   stretch=[f32(img, p + 0x0C + 4 * k) for k in range(3)],
                   size=f32(img, p + 0x18),
                   params=[f32(img, p + 0x1C + 4 * k) for k in range(3)])
    elif cls == 4210 and size >= 0x18:            # -> SE1 ModelDestruction
        # The init (0x8007d7bc) loads +0x04..+0x10 and the s16 at +0x14.
        d = u32(img, p + 4)
        out.update(debris_type=d,
                   debris_name=DEBRIS_TYPES[d] if d < len(DEBRIS_TYPES) else None,
                   debris_count=u32(img, p + 8), debris_size=f32(img, p + 0x0C),
                   health=f32(img, p + 0x10))
        t = struct.unpack_from(">h", img, p + 0x14)[0]
        if t != -1:
            links["trigger_on_destruction"] = t & 0xFFFF
    elif cls == 4190 and size >= 0x10:            # -> GCVehicle
        # +0x00 NE's own type (Jeeps 2, submarines 3, the Combine 0), u16 +0x08
        # active (the init tests it); +0x04 and +0x0c are links.
        out.update(type=u32(img, p), active=bool(struct.unpack_from(">H", img, p + 8)[0]))
    elif cls == 4170 and size >= 0x14:            # -> SE1 TouchField
        out.update(volume_index=u32(img, p + 0x10))
    elif cls == 4220 and size >= 0x14:            # -> SE1 Bouncer (likely)
        out.update(speed=f32(img, p), direction=[f32(img, p + 4),
                                                 f32(img, p + 8), 0.0],
                   volume_index=u32(img, p + 0x10))
    elif cls == 4100 and size >= 0x24:            # -> SE1 SoundHolder
        # NE's own name is "SoundHolder" (its init, 0x800833a8, logs it), and
        # the init's warnings name the fields: "hotspot radius larger than
        # falloff", "volume greater than 1.0". docs/sound.md.
        out.update(active=bool(u32(img, p)),          # plays on its first update
                   destroyable=bool(u32(img, p + 4)),  # "Parent %d died and I am destroyable"
                   sample=u32(img, p + 0x0C),         # effect hash; 0 = none
                   falloff=f32(img, p + 0x10), hotspot=f32(img, p + 0x14),
                   volume=f32(img, p + 0x18),
                   speech=u32(img, p + 0x1C),         # stream hash, English block
                   subtitle=u32(img, p + 0x20))       # .tdb hash; 0 on the whole disc
    elif cls == 4130 and size >= 0x10:            # -> SE1 MessageHolder
        # Init (0x8007d3a4) resolves both through Text_FindHashedString
        # (docs/text.md); the NETRISCA_* key string is only the designers' name.
        out.update(active=bool(u32(img, p)), text_hash=u32(img, p + 4),
                   title_hash=u32(img, p + 0x0C))
    elif cls == 4070 and size >= 0x40:            # -> SE1 MovingBrush + markers
        # NE's own class name is "MovingBrush" (its init logs it). Fields as
        # read by its init (0x8007edcc) and update (0x8007f0b8).
        # Keyframes: +0x10 count, +0x14 first record. Each 0x58-byte record is
        # {f32 time, f32 wait, u32 stop, next*, 0, Mtx*, ...}, the Mtx usually
        # its own +0x28. Arriving at a record loads its time and wait when
        # >= 0 (-1 keeps the current value) and halts there if `stop` is set;
        # after the last record the mover heads for the first. All 958 walks
        # end on a null next after exactly `count` records.
        n, rec, keys = u32(img, p + 0x10), u32(img, p + 0x14), []
        while rec and len(keys) < min(n, 64) and rec + 0x18 <= len(img):
            xf = u32(img, rec + 0x14)
            rot, pos = transform(img, xf) if 0 < xf and xf + 48 <= len(img) else (None, None)
            keys.append({"pos": pos, "rot": rot, "time": f32(img, rec),
                         "wait": f32(img, rec + 4), "stop": bool(u32(img, rec + 8))})
            rec = u32(img, rec + 0x0C)
        out.update(
            auto_start=bool(u32(img, p)),           # init sets "start moving"
            time=f32(img, p + 4),                   # seconds per segment: t += dt/time
            wait=f32(img, p + 8),                   # seconds held before leaving a key
            brush_index=u32(img, p + 0x0C),         # its image+0x48 node, 958/958
            move_on_damage=bool(u32(img, p + 0x1C)),
            # sample hashes, found in the 479-entry table at 0x8023adf4
            # ("FindHash"); 0 = none
            sounds={"start": u32(img, p + 0x28), "loop": u32(img, p + 0x2C),
                    "end": u32(img, p + 0x30)},
            loop_sound=bool(u32(img, p + 0x34)),
            keyframes=keys)
        # s16: the entity sent TRIGGER when damaged ("Sending TRIGGER to
        # TriggerOnDamage %d"); -1 = none
        dmg = struct.unpack_from(">h", img, p + 0x3C)[0]
        if dmg > 0:
            links["damage_target"] = dmg
    if links:
        out["links"] = links
    return out


MODEL_LIST = 0x58
M_NEXT, M_XFORM, M_NAME = 0x00, 0x04, 0x08


def model_instances(container):
    """The placed-model list at `image + 0x58`.

    Same header shape as the entity list -- next, transform pointer, name --
    but no class id. Each node also carries the model's asset path, a Climax
    `.clm` model name, at an offset that drifts because the node's name is
    stored inline; it is found by scanning the node's own pointer fields.

    This is what class 4200 (Prop) entities name: **every one of the 1,443
    props on the disc has its name in its own level's model list**. The list is
    much larger than the prop count (10,036 nodes disc-wide), so it holds the
    level's scenery generally, not only what gameplay exposes as an entity.

    100% of nodes have a readable transform and 99% land inside the level's own
    vertex bounds -- the same signature the entity list gives.
    """
    img = container.image
    n = len(img)
    ptrs = set(container.pointer_offsets())
    node = u32(img, MODEL_LIST)
    seen = set()
    nodes = []
    while node and node + 0x20 <= n and node not in seen:
        seen.add(node)
        nodes.append(node)
        node = u32(img, node + M_NEXT)
    order = sorted(nodes)
    nxt = dict(zip(order, order[1:]))
    out = []
    for off in nodes:
        xf = u32(img, off + M_XFORM)
        rot = pos = None
        if 0 < xf and xf + 48 <= n:
            rot, pos = transform(img, xf)
        nm = u32(img, off + M_NAME)
        path = None
        end = nxt.get(off, off + 0x140)
        for o in range(0, min(end - off, 0x140), 4):
            if off + o not in ptrs:
                continue
            v = u32(img, off + o)
            if 0 < v < n:
                s = cstr(img, v, 96)
                if ".clm" in s.lower():
                    path = s
                    break
        out.append({
            "offset": off,
            "name": cstr(img, nm) if 0 < nm < n else "",
            "path": path,
            "pos": pos,
            "rot": rot,
        })
    return out


def roster_names():
    """ObjectModel<N> -> model name, from build/cfg/LevelModels.json if built."""
    path = Path("build/cfg/LevelModels.json")
    if not path.exists():
        return {}
    out = {}

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                m = re.fullmatch(r"ObjectModel(\d+)", k)
                if m and isinstance(v, dict) and "value" in v:
                    out[int(m.group(1))] = v["value"]
                else:
                    walk(v)
    walk(json.loads(path.read_text()))
    return out


# --- subcommands ------------------------------------------------------------

def cmd_list(args):
    models = roster_names()
    for path in args.files:
        es = entities(Container(path))
        if args.kind:
            want = args.kind.lower()
            es = [e for e in es if want in e["kind"].lower()]
        print("=== %s: %d entities ===" % (path, len(es)))
        for e in es[:args.limit]:
            x, y, z = e["pos"] or (0.0, 0.0, 0.0)
            extra = ""
            if e.get("item_name"):
                extra = "  item=%s" % e["item_name"]
            elif e.get("model") is not None:
                extra = "  minion=%d -> %s" % (
                    e["minion"], models.get(e["model"], e["model"]))
            elif e.get("template") is not None:
                extra = "  template=#%d count=%s" % (e["template"], e["count"])
            elif e.get("text"):
                extra = "  %r" % e["text"]
            print("  [%5d] %-16s (%8.2f,%7.2f,%8.2f) %-34r%s"
                  % (e["id"], e["kind"], x, y, z, e["name"][:34], extra))
        if len(es) > args.limit:
            print("  ... %d more" % (len(es) - args.limit))
    return 0


def cmd_classes(args):
    files = _expand(args.paths)
    hist = Counter()
    names = {}
    for f in files:
        for e in entities(Container(f)):
            hist[e["cls"]] += 1
            names.setdefault(e["cls"], Counter())[e["name"]] += 1
    print("%d levels, %s entities, %d classes\n"
          % (len(files), format(sum(hist.values()), ","), len(hist)))
    for cls in sorted(hist):
        kind = CLASSES.get(cls, ("Class%d" % cls, "?"))
        top = ", ".join(repr(k) for k, _ in names[cls].most_common(3) if k)
        print("  %-6d %-17s %-7s n=%-6d %s"
              % (cls, kind[0], kind[1], hist[cls], top[:70]))
    return 0


def cmd_enemies(args):
    models = roster_names()
    for path in args.files:
        es = entities(Container(path))
        byid = {e["id"]: e for e in es}
        waves = Counter()
        spawned = Counter()
        for e in es:
            if e["cls"] != 4040:
                continue
            t = byid.get(e.get("template"))
            if t and t["cls"] == 5000:
                waves[t["id"]] += 1
                spawned[t["id"]] += e.get("count") or 0
        tmpl = [e for e in es if e["cls"] == 5000]
        print("=== %s: %d enemy templates, %d waves, %d spawns ==="
              % (path.name, len(tmpl), sum(waves.values()),
                 sum(spawned.values())))
        rows = sorted(tmpl, key=lambda e: -spawned[e["id"]])
        for e in rows[:args.limit]:
            mdl = models.get(e.get("model"), e.get("model"))
            print("  [%5d] %-22s waves=%-4d spawns=%-5d %r"
                  % (e["id"], str(mdl), waves[e["id"]], spawned[e["id"]],
                     e["name"][:40]))
        if len(rows) > args.limit:
            print("  ... %d more" % (len(rows) - args.limit))
    return 0


def cmd_models(args):
    files = _expand(args.paths)
    hist = Counter()
    tot = withpath = 0
    for f in files:
        ms = model_instances(Container(f))
        tot += len(ms)
        for m in ms:
            if m["path"]:
                withpath += 1
                hist[m["path"].split("\\")[-1]] += 1
        if len(files) == 1:
            print("=== %s: %d placed models ===" % (f.name, len(ms)))
            for m in ms[:args.limit]:
                x, y, z = m["pos"] or (0.0, 0.0, 0.0)
                print("  (%8.2f,%7.2f,%8.2f) %-28r %s"
                      % (x, y, z, m["name"][:28], m["path"] or ""))
            if len(ms) > args.limit:
                print("  ... %d more" % (len(ms) - args.limit))
    if len(files) > 1:
        print("%d levels, %s placed models, %s with a .clm path, %d distinct"
              % (len(files), format(tot, ","), format(withpath, ","), len(hist)))
        for k, v in hist.most_common(args.limit):
            print("  %5d  %s" % (v, k))
    return 0


def _chain(es, start, depth):
    byid = {e["id"]: e for e in es}
    seen = set()

    def label(e):
        return "%s #%d %r" % (e["kind"], e["id"], e["name"][:36])

    def walk(e, d, indent):
        if e["id"] in seen:
            print("%s(%s, already shown)" % (indent, label(e)))
            return
        seen.add(e["id"])
        if d >= depth:
            return
        if e["cls"] == 4080:
            edges = [("target", t["target"], t["event"]) for t in e.get("targets", [])]
        else:
            edges = [(k, v, None) for k, v in e.get("links", {}).items()]
        for name, tid, ev in edges:
            t = byid.get(tid)
            arrow = "--%s%s-->" % (name, "[%s]" % ev if ev is not None else "")
            if not t:
                print("%s%s #%d (unresolved)" % (indent, arrow, tid))
                continue
            print("%s%s %s" % (indent, arrow, label(t)))
            walk(t, d + 1, indent + "    ")

    print(label(start))
    walk(start, 0, "    ")


def cmd_graph(args):
    """The scripting graph: every entity-id link, checked for resolution and
    for what it points at. With --chain, follow one entity's links."""
    files = _expand(args.paths)
    n = res = 0
    per, tgt = Counter(), {}
    for f in files:
        es = entities(Container(f))
        byid = {e["id"]: e for e in es}
        for e in es:
            for name, t in e.get("links", {}).items():
                key = (e["kind"], name.rstrip("0123456789"))
                n += 1
                per[key] += 1
                if t in byid:
                    res += 1
                    tgt.setdefault(key, Counter())[byid[t]["kind"]] += 1
        if args.chain and len(files) == 1:
            want = args.chain.lower()
            start = next((e for e in es if str(e["id"]) == args.chain
                          or want in e["name"].lower()), None)
            if start is None:
                print("no entity matching %r" % args.chain)
            else:
                _chain(es, start, args.depth)
            print()
    print("%d levels: %s links, %s resolve to an entity (%.2f%%)"
          % (len(files), format(n, ","), format(res, ","), 100.0 * res / max(1, n)))
    for key in sorted(per, key=lambda k: -per[k]):
        got = tgt.get(key, Counter())
        top = ", ".join("%s %d%%" % (k, v * 100 // max(1, sum(got.values())))
                        for k, v in got.most_common(3))
        print("  %-16s %-13s %6d  resolved %3d%%  -> %s"
              % (key[0], key[1], per[key],
                 sum(got.values()) * 100 // max(1, per[key]), top))
    return 0 if res == n else 1


def cmd_json(args):
    args.output.mkdir(parents=True, exist_ok=True)
    models = roster_names()
    for path in args.files:
        es = entities(Container(path))
        for e in es:
            e.pop("rot", None)
            if e.get("model") is not None:
                e["model_name"] = models.get(e["model"])
        ms = model_instances(Container(path))
        for m in ms:
            m.pop("rot", None)
        dest = args.output / (path.stem + ".json")
        dest.write_text(json.dumps({"level": path.stem,
                                    "space": "NE, raw (tools/space.py converts)",
                                    "count": len(es),
                                    "entities": es, "models": ms}, indent=1))
        print("%s: %s entities, %s placed models -> %s"
              % (path.name, format(len(es), ","), format(len(ms), ","), dest))
    return 0


CATEGORY_COLOUR = {
    "enemy":   (255, 70, 70),
    "spawn":   (255, 150, 40),
    "pickup":  (80, 230, 120),
    "marker":  (250, 230, 90),
    "world":   (150, 150, 170),
    "logic":   (110, 170, 255),
    "sound":   (200, 110, 255),
    "fx":      (255, 110, 200),
    "unknown": (255, 255, 255),
}
DRAW_ORDER = ("world", "logic", "sound", "fx", "marker", "pickup",
              "spawn", "enemy")


def _fill_tri(put, px, rgb):
    """Scanline-fill a screen-space triangle. Small and dependency-free."""
    (ax, ay), (bx, by), (cx, cy) = px
    lo, hi = min(ay, by, cy), max(ay, by, cy)
    if hi - lo > 4096:
        return
    for y in range(lo, hi + 1):
        xs = []
        for (x0, y0), (x1, y1) in (((ax, ay), (bx, by)), ((bx, by), (cx, cy)),
                                   ((cx, cy), (ax, ay))):
            if (y0 <= y < y1) or (y1 <= y < y0):
                xs.append(x0 + (x1 - x0) * (y - y0) / (y1 - y0))
        if not xs:
            for x in range(min(ax, bx, cx), max(ax, bx, cx) + 1):
                put(x, y, rgb)
            continue
        for x in range(int(min(xs)), int(max(xs)) + 1):
            put(x, y, rgb)


def cmd_map(args):
    """Top-down render: level geometry in grey, entities coloured by category.

    Coverage numbers have been misleading on this project before; looking at
    the picture is the check that has actually caught bad structure.
    """
    args.output.mkdir(parents=True, exist_ok=True)
    for path in args.files:
        cont = Container(path)
        img = cont.image
        pts = [space.world(p) for p in positions(img)]
        if not pts:
            print("%s: no position array" % path)
            continue
        es = entities(cont)
        placed = [e for e in es if e["pos"]]
        props = [m for m in model_instances(cont) if m["pos"]]

        def band(vals, lo=1.0, hi=99.0):
            v = sorted(vals)
            n = len(v) - 1
            return v[int(n * lo / 100)], v[int(n * hi / 100)]

        x0, x1 = band([p[0] for p in pts])
        z0, z1 = band([p[2] for p in pts])
        W = args.size
        s = (W - 20) / max(1e-6, x1 - x0)
        H = max(64, min(4096, int((z1 - z0) * s) + 20))   # keep 1:1 scale
        buf = bytearray(b"\x0e\x10\x16\xff" * (W * H))

        def put(x, z, rgb):
            if 0 <= x < W and 0 <= z < H:
                i = (z * W + x) * 4
                buf[i:i + 3] = bytes(rgb)

        def to_px(p):                                  # NE point -> original space
            q = space.world(p)
            return int(10 + (q[0] - x0) * s), int(10 + (q[2] - z0) * s)

        # Geometry as filled triangles, shaded by height, so the entity dots
        # can be judged against actual floors and walls rather than a haze of
        # vertices.
        ys = sorted(p[1] for p in pts)
        y0, y1 = ys[len(ys) // 100], ys[-len(ys) // 100 - 1]
        for vs in world_triangles(cont):
            px = [to_px(v) for v in vs]
            t = min(1.0, max(0.0, (sum(v[1] for v in vs) / 3 - y0)
                             / max(1e-6, y1 - y0)))
            rgb = (int(38 + 62 * t), int(42 + 62 * t), int(52 + 66 * t))
            _fill_tri(put, px, rgb)

        for m in props:                                # scenery underneath
            cx, cz = to_px(m["pos"])
            put(cx, cz, (215, 175, 110))

        for cat in DRAW_ORDER:                         # enemies drawn last
            rgb = CATEGORY_COLOUR[cat]
            r = 2 if cat in ("enemy", "spawn") else 1
            for e in placed:
                if e["category"] != cat:
                    continue
                cx, cz = to_px(e["pos"])
                for dx in range(-r, r + 1):
                    for dz in range(-r, r + 1):
                        if dx * dx + dz * dz <= r * r + 1:
                            put(cx + dx, cz + dz, rgb)

        dest = args.output / (path.stem + "_entities.png")
        write_png(dest, W, H, bytes(buf))
        cats = Counter(e["category"] for e in placed)
        print("%s: %s/%s placed  %s -> %s"
              % (path.name, format(len(placed), ","), format(len(es), ","),
                 " ".join("%s=%d" % kv for kv in cats.most_common()), dest))
    return 0


def cmd_verify(args):
    """Structural checks that would fail loudly if the layout were wrong."""
    files = _expand(args.paths)
    tot = bad_x = ortho = inb = links = badlinks = 0
    unknown = Counter()
    sizes = {}
    for f in files:
        cont = Container(f)
        img = cont.image
        es = entities(cont)
        pts = positions(img)
        tot += len(es)
        byid = {e["id"]: e for e in es}
        bb = None
        if pts:
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            zs = [p[2] for p in pts]
            bb = (min(xs), max(xs), min(ys), max(ys), min(zs), max(zs))
        for e in es:
            if e["cls"] not in CLASSES:
                unknown[e["cls"]] += 1
            if e["props_size"]:
                sizes.setdefault(e["cls"], Counter())[e["props_size"]] += 1
            if not e["pos"]:
                bad_x += 1
                continue
            rows = e["rot"]
            lens = [math.sqrt(sum(v * v for v in r)) for r in rows]
            if all(abs(l - 1.0) < 0.02 for l in lens):
                dots = [abs(sum(a * b for a, b in zip(rows[i], rows[j])))
                        for i, j in ((0, 1), (0, 2), (1, 2))]
                if all(d < 0.02 for d in dots):
                    ortho += 1
            if bb:
                t = e["pos"]
                pad = 200.0
                if (bb[0] - pad <= t[0] <= bb[1] + pad
                        and bb[2] - pad <= t[1] <= bb[3] + pad
                        and bb[4] - pad <= t[2] <= bb[5] + pad):
                    inb += 1
            if e["cls"] == 4040:
                links += 1
                if not byid.get(e.get("template")):
                    badlinks += 1
    pct = lambda a: a * 100 // max(1, tot)             # noqa: E731
    print("%d levels, %s entities" % (len(files), format(tot, ",")))
    print("  transform readable      %s/%s"
          % (format(tot - bad_x, ","), format(tot, ",")))
    print("  3x3 orthonormal         %s/%s (%d%%; the rest carry scale)"
          % (format(ortho, ","), format(tot, ","), pct(ortho)))
    print("  translation in bounds   %s/%s (%d%%)"
          % (format(inb, ","), format(tot, ","), pct(inb)))
    print("  wave -> template links  %s/%s resolve"
          % (format(links - badlinks, ","), format(links, ",")))
    multi = {c: dict(v) for c, v in sizes.items() if len(v) > 1}
    print("  props size constant     %d/%d classes%s"
          % (len(sizes) - len(multi), len(sizes),
             ("  varying: %s" % multi) if multi else ""))
    if unknown:
        print("  UNKNOWN class ids: %s" % dict(unknown))
    return 1 if (badlinks or unknown) else 0


def _expand(paths):
    roots = paths or [Path("orig/files/Levels")]
    files = []
    for r in roots:
        r = Path(r)
        files.extend(sorted(r.rglob("*.ssw")) if r.is_dir() else [r])
    return sorted(set(files))


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="entities with class, name and position")
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument("--kind", help="filter by class name substring")
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("classes", help="class id histogram with example names")
    p.add_argument("paths", nargs="*", type=Path)
    p.set_defaults(func=cmd_classes)

    p = sub.add_parser("enemies",
                       help="enemy templates with wave and spawn counts")
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_enemies)

    p = sub.add_parser("models",
                       help="placed models from image+0x58, with .clm paths")
    p.add_argument("paths", nargs="*", type=Path)
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("graph", help="scripting links: resolution, targets, chains")
    p.add_argument("paths", nargs="*", type=Path)
    p.add_argument("--chain", help="entity id or name substring to follow")
    p.add_argument("--depth", type=int, default=4)
    p.set_defaults(func=cmd_graph)

    p = sub.add_parser("json", help="dump every entity to JSON")
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument("-o", "--output", type=Path, default=Path("build/entities"))
    p.set_defaults(func=cmd_json)

    p = sub.add_parser("map", help="render placements over the level")
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument("-o", "--output", type=Path, default=Path("build/maps"))
    p.add_argument("--size", type=int, default=900)
    p.set_defaults(func=cmd_map)

    p = sub.add_parser("verify", help="structural checks across levels")
    p.add_argument("paths", nargs="*", type=Path)
    p.set_defaults(func=cmd_verify)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
