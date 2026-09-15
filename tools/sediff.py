#!/usr/bin/env python3
"""
How much of Next Encounter's ruleset already exists in Serious Engine 1?

The chosen route rebuilds NE's content on SE1 (see docs/strategy.md), so the
question that decides how much work that is: for every thing NE places or
tunes, does SE1 already have an entity for it?

Three things get compared, each weighted by how often NE actually uses it --
an unmapped class with 6,665 placements matters far more than one with 1:

  * **Level entity classes** -- NE's 43 class ids against SE1's `EntitiesMP`.
  * **Items** -- NE's item type enum against SE1's `HealthItem`, `ArmorItem`,
    `WeaponItem`, `AmmoItem` and `PowerUpItem` enums.
  * **Enemies** -- NE's 46 actor models against SE1's `EnemyBase` subclasses.

SE1's side is read from the engine tree's `EntitiesMP/*.es` rather than
hardcoded, so the class list and the enums stay honest if that tree moves.
`pc/engine` (the fork being ported onto) is preferred; `ref/serious-engine`
is the fallback.

**The mapping tables below are a judgement call and are labelled as such.**
`exact` means the names or documented aliases agree (NE's `e_RocketMan` and
`e_FireCracker` are literally SE1 `Headman` subtypes `Rocketman` and
`Fire Cracker`); `likely` means the creature or object is recognisably the same
but the identification is inference; `none` means SE1 has no counterpart and
the behaviour has to be written. Nothing here is derived from the binary --
it is a planning aid, not a finding.

Usage:
    python tools/sediff.py classes
    python tools/sediff.py items
    python tools/sediff.py enemies
    python tools/sediff.py summary
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from entity import CLASSES, ITEM_TYPES, ITEM_TYPES_OBSERVED   # noqa: E402

# Prefer the fork the port is being built on; fall back to Croteam's stock
# tree. The two carry the same 144 entity classes (the fork adds only
# PlayerWeaponsHD and PlayerWeapons_old), so the comparison is unaffected --
# but read the tree we are actually targeting.
SE1_CANDIDATES = (
    Path("pc/engine/SamTSE/Sources/EntitiesMP"),
    Path("ref/serious-engine/Sources/EntitiesMP"),
)
SE1_DIR = next((p for p in SE1_CANDIDATES if p.is_dir()), SE1_CANDIDATES[-1])
ENTITIES_JSON = Path("build/entities")
LEVELMODELS = Path("orig/files/LevelModels.cfg")

EXACT, LIKELY, NONE = "exact", "likely", "none"

# NE class id -> (SE1 class, confidence, note)
CLASS_MAP = {
    2100: ("HealthItem", EXACT, ""),
    2200: ("ArmorItem", EXACT, ""),
    3100: ("WeaponItem", EXACT, ""),
    3200: ("AmmoItem", EXACT, ""),
    3201: ("AmmoPack", EXACT, ""),
    3210: ("KeyItem", EXACT, ""),
    3215: (None, NONE, "treasure/score pickup; SE1 has no scoring items"),
    3220: ("PowerUpItem", EXACT, ""),
    4010: ("Copier", EXACT, ""),
    4020: ("Teleport", EXACT, ""),
    4030: ("WatchPlayers", LIKELY, "SE1 Watcher is enemy-AI; WatchPlayers is the trigger"),
    4040: ("EnemySpawner", EXACT, ""),
    4050: ("DoorController", EXACT, ""),
    4060: ("Marker", EXACT, ""),
    4070: ("MovingBrush", EXACT, "NE's own label for these is 'Moving Brush'"),
    4080: ("Trigger", EXACT, ""),
    4090: ("MusicChanger", EXACT, ""),
    4100: ("SoundHolder", EXACT, ""),
    4110: ("Damager", EXACT, ""),
    4120: ("Camera", EXACT, "plus CameraMarker for the path"),
    4130: ("MessageHolder", EXACT, ""),
    4140: ("PlayerMarker", EXACT, ""),
    4150: ("EnemyMarker", EXACT, "EntityFactory_Create builds an EnemyMarker; patrol and path points"),
    4160: ("WorldLink", EXACT, ""),
    4170: ("TouchField", EXACT, ""),
    4180: ("Switch", EXACT, ""),
    4190: (None, NONE, "drivable vehicles; SE1 has none"),
    4200: ("ModelHolder", EXACT, ""),
    4210: ("ModelDestruction", EXACT, ""),
    4220: ("Bouncer", EXACT, ""),
    4230: ("RollingStone", EXACT, "EntityFactory_Create case 0x1086 builds a RollingStone"),
    4240: ("DestroyableArchitecture", EXACT, ""),
    4260: (None, NONE, "GameCube-only CGCArrow: a yellow or green arrow pointing at a pickup"),
    4270: (None, NONE, "GameCube-only CGCLockDown: Active, Scale"),
    4280: ("Teleport", LIKELY, "GameCube CGCWarpPlayers: warps every player to a PlayerStart"),
    4310: ("ParticlesHolder", EXACT, ""),
    4320: (None, NONE, "CTF flag; SE1 has no flag entity"),
    4330: (None, NONE, "GameCube-only GCLevelPar: par time, par kills, medal scores"),
    4340: (None, NONE, "GameCube-only GCFMVPlayer: plays a named movie"),
    5000: ("EnemyBase", EXACT, "the template; the creature itself is separate"),
    5010: ("Light", EXACT, ""),
    5020: (None, NONE, "force fields, not built by EntityFactory_Create (docs/world-conversion.md)"),
    6002: ("WorldSettingsController", LIKELY, "one per level"),
}

# NE item type -> (SE1 class, enum label, confidence)
ITEM_MAP = {
    "Health5": ("HealthItem", "Pill", EXACT),
    "Health10": ("HealthItem", "Small", EXACT),
    "Health25": ("HealthItem", "Medium", EXACT),
    "Health50": ("HealthItem", "Large", EXACT),
    "Health100": ("HealthItem", "Super", EXACT),
    "Armour5": ("ArmorItem", "Shard", EXACT),
    "Armour25": ("ArmorItem", "Small", EXACT),
    "Armour50": ("ArmorItem", "Medium", EXACT),
    "Armour100": ("ArmorItem", "Strong", EXACT),
    "Armour200": ("ArmorItem", "Super", EXACT),
    "SeriousSkates": ("PowerUpItem", "SeriousSpeed", EXACT),
    "SeriousUnderpants": ("PowerUpItem", "Invulnerability", EXACT),
    "SeriousDamage": ("PowerUpItem", "SeriousDamage", EXACT),
    "SeriousSmartbomb": ("PowerUpItem", "SeriousBomb", EXACT),
    "SeriousBubblegum": (None, None, NONE),
    "SeriousRing": (None, None, NONE),
    "SeriousGlasses": ("PowerUpItem", "Invisibility", LIKELY),
    "GreenAmmoPack": ("AmmoPack", None, EXACT),
    "RedAmmoPack": ("AmmoPack", None, EXACT),
    "9MMBullets": ("AmmoItem", "Bullets", EXACT),
    "RicochetBullets": (None, None, NONE),
    "HomingBullets": (None, None, NONE),
    "ShotgunShells": ("AmmoItem", "Shells", EXACT),
    "SniperBullets": ("AmmoItem", "Sniper bullets", EXACT),
    "Napalm": ("AmmoItem", "Napalm", EXACT),
    "LiquidNitrogen": (None, None, NONE),
    "LaughingGas": (None, None, NONE),
    "LimpetGrenades": (None, None, NONE),
    "SpiderMines": (None, None, NONE),
    "Grenades": ("AmmoItem", "Grenades", EXACT),
    "StandardRockets": ("AmmoItem", "Rockets", EXACT),
    "HeatSeekerRocket": (None, None, NONE),
    "SonicRocket": (None, None, NONE),
    "PowerCells": ("AmmoItem", "Electricity", EXACT),
    "CannonBalls": ("AmmoItem", "IronBalls", EXACT),
    "Drill": ("WeaponItem", "Chainsaw", EXACT),
    "DesertHawk": ("WeaponItem", "Colt", EXACT),
    "Uzi": ("WeaponItem", "Tommygun", EXACT),
    "Shotgun": ("WeaponItem", "Single shotgun", EXACT),
    "Minigun": ("WeaponItem", "Minigun", EXACT),
    "RocketLauncher": ("WeaponItem", "Rocket launcher", EXACT),
    "GrenadeLauncher": ("WeaponItem", "Grenade launcher", EXACT),
    "GasGun": ("WeaponItem", "Flamer", LIKELY),
    "SniperRifle": ("WeaponItem", "Sniper", EXACT),
    "PowerGun": ("WeaponItem", "Laser", LIKELY),
    "Cannon": ("WeaponItem", "Cannon", EXACT),
}

# Items past 45 are keyed by id, not name -- four distinct key slots all read
# as "Key?", so a name-keyed table would collapse them and triple-count.
# The DM slots are indirection, not new item types: each is a game-mode slot
# that resolves to a real weapon or ammo type, so they map to the same SE1
# entity once resolved.
ITEM_MAP_BY_ID = {
    46: ("KeyItem", None, EXACT), 47: ("KeyItem", None, EXACT),
    48: ("KeyItem", None, EXACT), 49: ("KeyItem", None, EXACT),
    54: (None, None, NONE),                       # treasure / scoring pickup
    55: ("PowerUpItem", "SeriousBomb", EXACT),
    57: ("WeaponItem", None, LIKELY), 58: ("WeaponItem", None, LIKELY),
    59: ("WeaponItem", None, LIKELY), 60: ("WeaponItem", None, LIKELY),
    61: ("WeaponItem", None, LIKELY),
    67: ("AmmoItem", None, LIKELY), 68: ("AmmoItem", None, LIKELY),
    69: ("AmmoItem", None, LIKELY), 70: ("AmmoItem", None, LIKELY),
    71: ("AmmoItem", None, LIKELY),
}

# NE actor enum -> (SE1 class, subtype, confidence, note)
ENEMY_MAP = {
    "e_KamikazeMarine": ("Headman", "Kamikaze", EXACT, ""),
    "e_KamikazeMarineMKII": ("Headman", "Kamikaze", LIKELY, "armoured variant"),
    "e_MiniSam": (None, None, NONE, ""),
    "e_MarshHopper": ("Gizmo", None, LIKELY, "SE1's Marsh Hopper"),
    "e_GiantHornet": (None, None, NONE, ""),
    "e_WereBull": ("Werebull", "Summer", EXACT, ""),
    "e_ToothFish": ("Fish", None, LIKELY, "SE1's Reeban Electro-Fish"),
    "e_Scorpian": ("Scorpman", "Soldier", EXACT, ""),
    "e_DemonA": ("Demon", None, EXACT, "Aludran Reptiloid"),
    "e_DemonB": ("Demon", None, LIKELY, "larger variant"),
    "e_MechA": ("Walker", "Soldier", LIKELY, "Biomechanoid"),
    "e_MechB": ("Walker", "Sergeant", LIKELY, "Biomechanoid"),
    "e_RocketMan": ("Headman", "Rocketman", EXACT, "same name in both"),
    "e_FireCracker": ("Headman", "Fire Cracker", EXACT, "same name in both"),
    "e_Grenadier": ("Headman", "Bomberman", LIKELY, ""),
    "e_BabyWerebull": ("Werebull", None, LIKELY, "smaller variant"),
    "e_KleerKnight": ("Boneman", None, EXACT, "SE1 calls the Kleer 'Boneman'"),
    "e_TongueHarpy": ("Woman", None, LIKELY, "SE1's Scythian Witch-Harpy"),
    "e_ChariotHarpy": ("Woman", None, LIKELY, "mounted variant"),
    "e_DarkLord": ("Devil", None, LIKELY, "SirianDarkLord; SE1's Devil is Sirian"),
    "e_Gladiator": (None, None, NONE, ""),
    "e_LegionnaireAnt": (None, None, NONE, ""),
    "e_LegionnaireAntLeader": (None, None, NONE, ""),
    "e_DevilStallion": (None, None, NONE, ""),
    "e_ElephantGunner": (None, None, NONE, ""),
    "e_WickerMan": (None, None, NONE, ""),
    "e_TempleGuardian": (None, None, NONE, ""),
    "e_Warrior": (None, None, NONE, ""),
    "e_CraneBomber": (None, None, NONE, ""),
    "e_MonkeyMan": (None, None, NONE, ""),
    "e_Sorcerer": (None, None, NONE, ""),
    "e_Octochop": (None, None, NONE, ""),
    "e_Atlantean": (None, None, NONE, ""),
    "e_CrabMan": (None, None, NONE, ""),
    "e_AttackSquid": (None, None, NONE, ""),
    "e_BikerSquid": (None, None, NONE, ""),
    "e_MerMan": (None, None, NONE, ""),
    "e_DibDibDumDum": (None, None, NONE, ""),
    "e_DumDumLarge": (None, None, NONE, ""),
    "e_DumDumSmall": (None, None, NONE, ""),
    "e_TweedleDumDum": (None, None, NONE, ""),
    "e_Minotaur": (None, None, NONE, ""),
    "e_DragonDrill": (None, None, NONE, ""),
    "e_DragonFire": (None, None, NONE, ""),
    "e_DragonCannon": (None, None, NONE, ""),
    "e_Dragon": (None, None, NONE, ""),
}


def se1_classes():
    if not SE1_DIR.is_dir():
        return set()
    return {p.stem for p in SE1_DIR.glob("*.es")}


def se1_enum(cls):
    """Enum labels declared in an SE1 entity source."""
    path = SE1_DIR / (cls + ".es")
    if not path.exists():
        return []
    body = path.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"^enum\s+\w+\s*\{(.*?)^\};", body, re.S | re.M)
    return re.findall(r'"([^"]+)"', m.group(1)) if m else []


def ne_usage():
    """How often NE actually places each class and item type."""
    cls = Counter()
    item = Counter()
    minion = Counter()
    spawns = Counter()
    if not ENTITIES_JSON.is_dir():
        return cls, item, minion, spawns
    for f in sorted(ENTITIES_JSON.glob("*.json")):
        d = json.loads(f.read_text())
        byid = {e["id"]: e for e in d["entities"]}
        for e in d["entities"]:
            cls[e["cls"]] += 1
            if e.get("item") is not None:
                item[e["item"]] += 1
            if e["cls"] == 5000 and e.get("model") is not None:
                minion[e["model"]] += 1
            if e["cls"] == 4040:
                t = byid.get(e.get("template"))
                if t and t.get("model") is not None:
                    spawns[t["model"]] += e.get("count") or 0
    return cls, item, minion, spawns


# ObjectModel 0..45 are the creatures (the cfg's Minions/Rome/Feudal China/
# Legendary Atlantis/Bosses sections). 46..60 are FMA cutscene actors and 61+
# are items, weapons, ammo, projectiles and vehicles -- none of them enemies,
# and none reachable as a class 5000 minion type in any shipped level.
ACTOR_MAX = 45


def actor_enums():
    """ObjectModel<N> -> (model name, e_ enum name) from LevelModels.cfg."""
    out = {}
    if not LEVELMODELS.exists():
        return out
    for line in LEVELMODELS.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.search(r'ObjectModel(\d+)\s+"([^"]*)".*?#\s*(e_\w+)', line)
        if m and int(m.group(1)) <= ACTOR_MAX:
            out[int(m.group(1))] = (m.group(2), m.group(3))
    return out


def _item_ids():
    return list(range(len(ITEM_TYPES))) + sorted(ITEM_TYPES_OBSERVED)


def _item_name(idx):
    return (ITEM_TYPES[idx] if idx < len(ITEM_TYPES)
            else ITEM_TYPES_OBSERVED.get(idx, "item%d" % idx))


def _item_target(idx):
    if idx < len(ITEM_TYPES):
        return ITEM_MAP.get(ITEM_TYPES[idx], (None,))[0]
    return ITEM_MAP_BY_ID.get(idx, (None,))[0]


def _bar(conf):
    return {EXACT: "exact ", LIKELY: "likely", NONE: "NEW   "}[conf]


# --- subcommands ------------------------------------------------------------

def cmd_classes(args):
    have = se1_classes()
    usage, _, _, _ = ne_usage()
    print("NE level entity class -> SE1 EntitiesMP  (%d SE1 classes on disk)\n"
          % len(have))
    tot = mapped = tot_n = mapped_n = 0
    for cid in sorted(CLASSES):
        name, cat = CLASSES[cid]
        se1, conf, note = CLASS_MAP.get(cid, (None, NONE, "unmapped"))
        n = usage.get(cid, 0)
        tot += 1
        tot_n += n
        if se1:
            mapped += 1
            mapped_n += n
        missing = "" if (se1 is None or se1 in have) else "  !! not in tree"
        print("  %-6d %-17s %6d  %s  %-26s %s%s"
              % (cid, name, n, _bar(conf), se1 or "-", note, missing))
    print("\n  %d of %d classes map to an existing SE1 entity" % (mapped, tot))
    print("  by placements: %s of %s (%d%%)"
          % (format(mapped_n, ","), format(tot_n, ","),
             mapped_n * 100 // max(1, tot_n)))
    return 0


def cmd_items(args):
    _, usage, _, _ = ne_usage()
    print("NE item type -> SE1 item entity\n")
    tot = mapped = tot_n = mapped_n = 0
    for idx in _item_ids():
        nm = _item_name(idx)
        if idx < len(ITEM_TYPES):
            se1, label, conf = ITEM_MAP.get(nm, (None, None, NONE))
        else:
            se1, label, conf = ITEM_MAP_BY_ID.get(idx, (None, None, NONE))
        n = usage.get(idx, 0)
        tot += 1
        tot_n += n
        if se1:
            mapped += 1
            mapped_n += n
        target = se1 + (' "%s"' % label if label else "") if se1 else "-"
        print("  %3d %-20s %6d  %s  %s" % (idx, nm, n, _bar(conf), target))
    print("\n  %d of %d item types map" % (mapped, tot))
    print("  by placements: %s of %s (%d%%)"
          % (format(mapped_n, ","), format(tot_n, ","),
             mapped_n * 100 // max(1, tot_n)))
    return 0


def cmd_enemies(args):
    have = se1_classes()
    _, _, minion, spawns = ne_usage()
    actors = actor_enums()
    print("NE actor -> SE1 enemy class  (ObjectModel 0..%d, the creatures)\n" % ACTOR_MAX)
    rows = []
    for idx in sorted(actors):
        model, enum = actors[idx]
        se1, sub, conf, note = ENEMY_MAP.get(enum, (None, None, NONE, ""))
        rows.append((spawns.get(idx, 0), minion.get(idx, 0), idx, model,
                     enum, se1, sub, conf, note))
    rows.sort(key=lambda r: -r[0])
    tot = mapped = tot_n = mapped_n = 0
    for sp, tm, idx, model, enum, se1, sub, conf, note in rows:
        tot += 1
        tot_n += sp
        if se1:
            mapped += 1
            mapped_n += sp
        target = se1 + (' "%s"' % sub if sub else "") if se1 else "-"
        flag = "" if (se1 is None or se1 in have) else "  !! not in tree"
        print("  %-24s spawns=%-5d tmpl=%-4d %s  %-22s %s%s"
              % (model[:24], sp, tm, _bar(conf), target, note, flag))
    print("\n  %d of %d actors map to an existing SE1 enemy" % (mapped, tot))
    print("  by scripted spawns: %s of %s (%d%%)"
          % (format(mapped_n, ","), format(tot_n, ","),
             mapped_n * 100 // max(1, tot_n)))
    return 0


def cmd_summary(args):
    have = se1_classes()
    usage, items, minion, spawns = ne_usage()
    actors = actor_enums()

    def tally(pairs):
        n = m = wn = wm = 0
        for key, se1, weight in pairs:
            n += 1
            wn += weight
            if se1:
                m += 1
                wm += weight
        return m, n, wm, wn

    c = tally([(k, CLASS_MAP.get(k, (None,))[0], usage.get(k, 0))
               for k in CLASSES])
    i = tally([(k, _item_target(k), items.get(k, 0)) for k in _item_ids()])
    e = tally([(k, ENEMY_MAP.get(actors[k][1], (None,))[0], spawns.get(k, 0))
               for k in actors])

    print("SE1 tree: %d EntitiesMP classes\n" % len(have))
    print("                      mapped/total      by usage")
    for label, t in (("level entity classes", c), ("item types", i),
                     ("enemy actors", e)):
        print("  %-20s %5d/%-5d %12s/%-9s  %3d%%"
              % (label, t[0], t[1], format(t[2], ","), format(t[3], ","),
                 t[2] * 100 // max(1, t[3])))

    gaps = [(usage.get(k, 0), CLASSES[k][0], "class")
            for k in CLASSES if not CLASS_MAP.get(k, (None,))[0]]
    gaps += [(spawns.get(k, 0), actors[k][0], "enemy")
             for k in actors if not ENEMY_MAP.get(actors[k][1], (None,))[0]]
    gaps.sort(reverse=True)
    print("\nBiggest gaps -- what has to be written, by NE usage:")
    for n, name, kind in gaps[:14]:
        print("  %7d  %-26s %s" % (n, name, kind))
    return 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn, help_ in (
            ("classes", cmd_classes, "entity classes vs EntitiesMP"),
            ("items", cmd_items, "item types vs SE1 item entities"),
            ("enemies", cmd_enemies, "actors vs SE1 enemy classes"),
            ("summary", cmd_summary, "totals and the biggest gaps")):
        p = sub.add_parser(name, help=help_)
        p.set_defaults(func=fn)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
