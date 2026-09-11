# Strategy — porting NE content onto Serious Engine 1

## The decision

Route chosen (2026-09-09): **rebuild Next Encounter's levels and game logic on
top of Serious Engine 1**, using the GameCube build purely as the specification
to copy from. The engine is **[tx00100xt/SeriousSamClassic-VK](https://github.com/tx00100xt/SeriousSamClassic-VK)**
in `pc/engine` — a maintained GPL-2.0 fork of Croteam's SE1 1.10 (step 12
below, and `pc/README.md`). Croteam's own release stays in `ref/serious-engine`
as reference only.

This was picked over static recompilation and matching decompilation with the
trade-off understood and accepted: because the GameCube binary is Climax's own
engine and shares no code with SE1 (see `docs/findings.md`), nothing decompiled
from `main.dol` drops into SE1 directly. Every system has to be *reimplemented*
against SE1's APIs rather than transplanted. The result is closer to a faithful
remake than a port, and exact-fidelity behaviour (frame-accurate AI, original
physics feel) is explicitly not a goal of this route.

What this buys: a codebase that builds and runs on PC from day one, an engine
with a level editor and entity system already working, and no need to
reimplement GX, the Dolphin SDK, or a renderer.

## What that makes important

Because SE1 supplies the engine, the value is concentrated in **content and
rules**, not engine code. Priorities in order:

1. **Asset formats** — get NE's geometry, textures, animation and level data out
   of the `CLIMAX-` containers and into SE1's `.wld` / `.mdl` / `.tex`. This is
   the critical path; everything else is blocked behind it.
2. **Gameplay rules** — enemy stats, weapon behaviour, scoring, difficulty
   scaling. Much of this is in plain text already (below).
3. **Entity logic** — AI state machines (`cWickerManAI`, `cWereBullAI`,
   `cWarriorAI`), vehicles (`CcBaseVehicle`), pickups. Reimplemented as SE1
   entities (`.es` files under `Sources/EntitiesMP`).
4. **Level layout** — the 49 `.ssw` worlds rebuilt as SE1 worlds.

`main.dol` reversing serves 1–3. It is *not* an end in itself on this route:
decompile a function when it defines a rule or a format we need, not to complete
a map of the binary.

## The lucky break: config is plain text

`base.cfg`, `MinionStats.cfg`, `LevelModels.cfg`, `Combine.cfg` and `Jeep.cfg`
are human-readable and hold a large slice of the game's tuning already —
`MinionStats.cfg` alone is 81 KB of enemy stats with the developers' own
comments. They are declarative "stash" trees:

```
stash MinionStats
{
    float  DeathAliveTime       4.0  0.0  100.0   # how long to stay dead for
    float  DiffScaleEasy        0.25 0    2
    stash  DiffScaleTable { flag Health true  flag Damage true ... }
}
```

This is the `CcStash` system (cf. `CcStash::LoadFromMemory()` in the binary).
These files are directly readable as the specification for step 2 — no
reversing required. Start here; it is the cheapest real progress available.

## Container format: solved

`.ssg` and `.ssw` are both the `CLIMAX` container — a 0x40 big-endian header
followed by **two** concatenated zlib streams: the memory image, then the
relocation table. `tools/ssg.py` reads both, and all 63 containers on the disc
verify exactly (577 MB of image, 4,943,495 pointer fields). Full field layout in
`docs/findings.md`.

The image is a **serialized memory image**, not a tagged format: it was loaded to
a fixed address and its internal pointers fixed up at runtime. The relocation
table names every one of those pointer fields, which is what makes the object
graph walkable — that, not the inflation, is the part that matters.

The loader has been reversed: `CcImport_ReadOld` at `0x801206b8`, with 19
functions in its chain named in the Ghidra database. So inflation *and* the
pointer map are solved; what remains is **typing the nodes** — deciding what the
structs at the far end of those pointers actually are.

Asset names survive as plain strings inside the image, which gives a way in:
`FrontEnd.ssg` yields `SeriousSamLogo`, `GTCooperative`, `GTHoldTheFlag`,
`LevelFilmCell`; levels carry the developers' VSS metadata
(`AutoVSS::User = Jamesl`, `21\01\2004`).

## Other formats

| Ext | What | Status |
|---|---|---|
| `.ssg` | CLIMAX container, GLOB resource | textures typed and extracting; materials typed; **object models decoded** (nested images, also embedded in every `.ssw`): `tools/objmodel.py` |
| `.ssw` | CLIMAX container, WORLDMESH resource | textures, vertex arrays, lights, geometry (98%), UVs, materials and entities all decoded |
| `.cfg` | CcStash config | fully parsed, `build/cfg/*.json` |
| `.tdb` | text databases (EnglishUSA, German, french; EnglishEUR unused) | **decoded**: 858 records `{text, hasVars, hash}`, looked up by a hash the entity stores (`Text_FindHashedString`); all 63 MessageHolders resolve; `tools/text.py`, `docs/text.md` |
| `.sst` | standalone textures (17 files) | loader named (`CcRes_Texture_Load`); same `CcTexture` node |
| `.spt` / `.spd` | DSP-ADPCM effects bank, 540 samples | **decoded**: `tools/sound.py` writes WAVs, verified exactly against the stored loop history; entities name sounds by hash (`docs/sound.md`) |
| `.bik` | Bink video (35 files) | standard RAD format, playable with existing tools |
| `Game.Gui` | `DXFF` magic | unexamined |
| `StreamData.dat` | music and speech, 441 DSP-ADPCM channels | **decoded**: table at `main.dol 0x802411f8`; 11 music tracks as 3 stereo intensity layers (voices interleaved in 0x2380-byte chunks), 375 speech lines in 3 language blocks, of which German is a copy of English; `tools/sound.py streams` |
| `opening.bnr` | `BNR1` GameCube banner | standard format |

## How much of NE's ruleset SE1 already has

`tools/sediff.py` compares NE's ruleset against `pc/engine`'s
`EntitiesMP` (144 classes, the same set as `ref/serious-engine`), weighting every gap by how often NE actually uses
it — an unmapped class with 6,665 placements matters far more than one with 1.

|  | mapped / total | by usage |
|---|---|---|
| level entity classes | **34 / 43** | 26,003 / 26,823 — **96%** |
| item types | **51 / 62** | 4,845 / 5,583 — **86%** |
| enemy actors | 18 / 46 | 7,044 / 11,930 — **59%** |

**The level entity model is nearly a 1:1 match, and that is the finding that
matters most for this route.** It is not coincidence: Climax's engine was built
to run a Serious Sam game, and the two entity vocabularies converge to the
point of sharing names. NE's own designer labels for class 4070 are "Moving
Brush" — SE1's class is `MovingBrush`. The same goes for `DoorController`,
`TouchField`, `Damager`, `ModelDestruction`, `DestroyableArchitecture`,
`WorldLink`, `Copier`, `Watcher`, `Bouncer`, `Trigger`, `MessageHolder`.
Every SE1 class named in the mapping exists in the tree.

Items line up almost as well, and in places *exactly*: NE's health types are
`Health5/10/25/50/100` at indices 0–4, SE1's are `Pill/Small/Medium/Large/Super`
at 0–4; NE's armour is `Armour5/25/50/100/200` at its indices 0–4 against SE1's
`Shard/Small/Medium/Strong/Super`. All 11 NE weapons map onto SE1 weapons.

The lineage shows plainest in the enemy names: NE's `e_RocketMan` and
`e_FireCracker` are literally SE1 `Headman` subtypes `Rocketman` and
`Fire Cracker`, and `e_KamikazeMarine` is `Headman`'s `Kamikaze`. The Kleer is
`Boneman` in both codebases' terms.

### Where the work actually is

Enemies. NE's cast is mostly Climax's own invention — DumDums, Tweedles,
Gladiators, Monkeymen, Atlanteans, CrabMen, Squids, Mermen — and **41% of all
scripted spawns are creatures SE1 has no equivalent for**. Ranked by NE usage,
what has to be written:

| spawns | actor | |
|---|---|---|
| 1,090 | DumDumlarge | |
| 889 | monkeyman | |
| 799 | atlantean | |
| 612 | TweedleDumDum | |
| 234 | LegionnaireAnts | |
| 217 | Gladiator | |
| 174 | Warrior | |
| 171 | attacksquid | |

Everything below ~90 spawns is a long tail of 20 more creatures.

Outside enemies the gaps are small and mostly systemic rather than per-entity:
treasure/scoring pickups (454 placements, SE1 has no scoring items), the
`Arrow` and `LockDown` classes (purpose still unclear in NE itself), CTF flags,
par times, FMV playback, drivable vehicles (8 placements — Jeep, Combine,
submarine), and eight ammo variants NE adds that SE1 lacks (ricochet, homing,
liquid nitrogen, laughing gas, limpet grenades, spider mines, heat-seeker and
sonic rockets).

### What this says about the route

The route holds. The expensive part was never going to be the level plumbing,
and the diff confirms it: 96% of what the levels place already has an SE1
entity waiting. The cost is concentrated in **enemy AI for ~28 creatures**,
which is exactly what `MinionStats.cfg` already specifies numerically —
81 KB of stats with the developers' own comments, parsed into
`build/cfg/MinionStats.json`.

One caveat on the numbers: the class/item/enemy mapping tables in
`tools/sediff.py` are a **judgement call**, labelled `exact` / `likely` / `new`
in the tool's own output. Nothing in them is derived from the binary. They are
a planning aid, not a finding, and the `likely` rows (Gizmo, Fish, Woman,
Walker, Devil) are inference from creature descriptions rather than evidence.

## Immediate next steps

Done:

1. ~~Container format~~ — solved and implemented (`tools/ssg.py`); all 63
   containers verified, 4,943,495 pointer fields.
2. ~~Container loader in `main.dol`~~ — `CcImport_ReadOld` (`0x801206b8`);
   27 functions named in the Ghidra database.
3. ~~Parse the `.cfg` stash files~~ — `tools/cfg.py`, 2,796 settings in
   `build/cfg/*.json`, including the 51-entry enemy roster.
4. ~~Image structure~~ — root at `image+0x08`, five resource loaders named.
5. ~~Textures~~ — `CcTexture` typed; `tools/gxtex.py` extracts all 8,118
   texture nodes across the disc, visually verified.
6. ~~Vertex arrays and lights~~ — `image+0xb4` table and `image+0x34` light
   list; 536,024 vertices and 20,210 lights, verified on all 49 levels.
7. ~~Geometry~~ — GX display lists via `{data, paddedSize, usedSize,
   vertexType}` descriptors, each holding a *sequence* of primitives.
   **884,313 triangles, 98% of positions**, verified by rendering.
8. ~~Materials~~ — material nodes at `image+0x08` contain their own display-list
   descriptors, so ownership is exact: **17,115/17,115 lists linked, 0 unowned**;
   3,813 materials, 3,788 with a diffuse texture. `tools/level.py mesh` writes
   OBJ + MTL whose `map_Kd` names resolve against `gxtex.py extract` output.
9. ~~Entity and spawn data~~ — the list at `image+0x40` is the placement
   system: **26,823 entities across 49 levels, 43 class ids**, each with a 3x4
   transform, the designer's own name, and a per-class props struct.
   Enemies are placed as *templates* (class 5000) referenced by *wave
   spawners* (class 4040); **all 6,665 wave links resolve**. Minion type needs
   a 61-entry permutation table found at `main.dol:0x8021ab2c` to reach
   `ObjectModel<N>`, and item type is `base.cfg`'s `RespawnTimes` order.
   `tools/entity.py` reads it all; `build/entities/*.json` has every level.

10. ~~Texcoords~~ — the vertex field order is uniform across levels and both
    vertex types: field 3 indexes array 4 (TEX0). Verified two ways — zero
    out-of-range indices over 478 (level, field) pairs with 429 exact fits,
    and near-zero variation in texture scale within a material (0.00-0.02
    against 0.5-0.9 for every wrong pairing). `tools/level.py mesh` now writes
    `vt` lines and `v/vt` faces: **all 884,313 triangles carry UVs**, and
    `tools/level.py render` samples the diffuse texture through them. The
    export now also writes the PNGs its MTL names, so all `map_Kd` references
    across the 49 levels resolve.
   Movable geometry (doors, gates, moving brushes) is a separate list at
   `image+0x48` whose vertices are in local space; **43,281 triangles were
   being exported at the world origin** and are now placed by their sector
   transform, each a named OBJ object. The last material node's extent was
   swallowing them, which is why they read as correctly owned.
11. ~~Diff the ruleset against SE1~~ — `tools/sediff.py`; 96% of level entity
    placements and 86% of item placements already have an SE1 entity, but only
    59% of scripted enemy spawns do. See "How much of NE's ruleset SE1 already
    has" above.

Next:

12. ~~Decide the SE1 baseline for `pc/`~~ — **tx00100xt/SeriousSamClassic-VK**,
    cloned to `pc/engine` (GPL-2.0, last push 2026-01-14). Vulkan renderer with
    an OpenGL fallback, x86-64 and ARM64, and — the deciding factor — the full
    tree including `Ecc`, the entity class compiler the route needs in order to
    add entity classes. Same 144 `EntitiesMP` classes as the reference tree, so
    the ruleset diff carries over. Rationale, the alternatives considered, and
    the build prerequisites are in `pc/README.md`. **Nothing is compiled yet**:
    the machine has no C++ toolset and no Vulkan SDK.
13. ~~Decode the `.clm` mesh format~~ — `tools/model.py`. `CcMesh` nodes at
    `image+0x5c` hold their own arrays and materials, and GX display lists of
    vertex type 2 (stride 8). 503 meshes (369 models plus 134 damage
    stages of breakable props), 666
    materials, all textured, 122,111 triangles, 0 bad index refs. 7,171 of
    7,176 `.clm` instances link to their mesh; the other 5 are an editor gizmo
    the shipped image lacks. Checked by rendering (a gallery per level, and
    every instance placed over the level). The props are in the world
    description as `ModelHolder2`/`ModelHolder3` (`docs/world-conversion.md`).
    Still open: prop collision, NE's ModelDestruction props, and the
    flame/particle instances.
14. **Gekko language extension** — still needed before trusting decompiler
    output in math-heavy code.
15. **World conversion** — the method for turning a level into an SE1 world
    with its sectors and lights is in `docs/world-conversion.md`. Stage 1
    (`tools/world.py`) is done and verified on all 49 levels: 2,285 water/lava
    region cells, 1,209 brush entities (all 958 Movers and all 116
    DestroyableArch), TouchField and Bouncer properties, 20,210 lights mapped onto
    `Light.es` properties (20,158 to create). Stage 2, the `.wld` writer, is
    specified against the engine headers but blocked on the C++ toolset.
    NE's region BSP is decoded — content, underwater reverb and fog become SE1
    content sectors and `HazeMarker`s. Still open: gravity, and TouchField and
    Bouncer volumes.
16. **Scripting graph** — decoded: 27,151 entity-id links (99.98% resolve)
    covering triggers (SE1's own 10-target layout), waves → enemy templates →
    death triggers, watchers, patrol paths, copiers and doors
    (`tools/entity.py graph`). **Mover motion is decoded**: class 4070 is the
    game's own `MovingBrush`, and its keyframes map one to one onto SE1's
    `MovingBrushMarker` chain, with time, wait and stop copied unconverted.
    That covers 958 movers and 2,016 markers, checked against the game's code
    and by rendering (`docs/world-conversion.md`). Camera paths are the largest
    props still undecoded.
17. **Object models** — the enemies, characters, pickups and weapon rigs are
    decoded as geometry: 2,226 nested images across the 63 containers, 261
    names, 711 distinct by content (exported as OBJ with textures), 0 bad
    index refs, checked by rendering (`tools/objmodel.py`,
    `docs/image-format.md`).

    **Animation and skinning are decoded too.**
    - Frames index a per-model pool of 20-byte keys. Each key decodes, as
      the game's own paired-single code does, to a 3×4 skinning matrix
      (bind pose → posed), so no bone hierarchy is needed.
    - Vertices are skinned either on the CPU, from weighted groups in the
      bone records, or on the GPU, through per-list matrix palettes whose
      state persists through the whole model.
    - Checked three ways: exact data tests, edge stretch against the wrong
      reading, and posed renders (`tools/objmodel.py pose`).

    **Open:** a few characters leave vertices in no skin group (KleerKnight
    807 of 1,409). And the SE1 form is still to choose: the keys bake
    straight into `.mdl` vertex animation, while SKA would need a skeleton
    NE never stores.
