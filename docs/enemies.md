# Enemies — stand-ins, and the port of NE's own creatures

NE places 46 creature models (`LevelModels.cfg` ObjectModel 0–45). Each
level's enemies are **templates** (class 5000) that spawners (Wave, class
4040) copy; `docs/world-conversion.md`, "Scripting as written", covers the
spawners and the rest of the scripting.

- **18 creatures TSE already has** go in as TSE classes: the stand-ins below.
  They cover 7,044 of NE's 11,930 scripted spawns.
- **The other 28 are ported**: each becomes an SE1 entity class of its own in
  `pc/entities`, with NE's model, animations, stats and behaviour.

## Stand-ins

`tools/world.py` (`_enemy`) takes the class from `tools/sediff.py`'s judged
table (`ENEMY_MAP`, labelled exact or likely there) and writes the template
with `Template` set, its `Death target`, and the variant where the class has
one:

| NE | spawns | TSE class | variant |
|---|---|---|---|
| KleerKnight | 1,826 | `Boneman` | |
| KamikazeMarines, KamikazeMarinesMKII | 1,635, 57 | `Headman` | Type Kamikaze |
| TongueHarpy, ChariotHarpy | 1,051, 80 | `Woman` | |
| Grunt | 662 | `Headman` | Type Rocketman |
| mechA, mechB | 542, 236 | `Walker` | Character Soldier, Sergeant |
| werebull, BabyWereBull | 310, 26 | `Werebull` | (TSE's Werebull has no character property) |
| Grenade_Grunt | 163 | `Headman` | Type Bomberman |
| demonA, demonB | 146, 51 | `Demon` | |
| toothfish | 125 | `Fish` | |
| firecracker_Grunt | 101 | `Headman` | Type Fire Cracker |
| scorpions | 32 | `Scorpman` | Type Soldier |
| SirianDarkLord | 1 | `Devil` | |
| MarshHopper | 0 | `Gizmo` | |

## The port

Three layers per creature. The first creature through all of them is
**DumDumLarge**, the most spawned of the 28 (1,090 spawns).

| creature | spawns | model | class | behaviour |
|---|---|---|---|---|
| DumDumlarge | 1,090 | done | `GCEnemyGenericDumDumLarge` | chase and lunge, from `cDumDumLargeAI` (not yet played) |
| monkeyman, atlantean, TweedleDumDum, LegionnaireAnts, Gladiator, Warrior, attacksquid, Merman, octochop, bikersquid, sorcerer, devilstallion, TempleGuardian, crabman, WickerMen, DibDibDumDum, cranebomber, ElephantGunner, minisam, MinotaurGladiator, Dragon, GiantHornet, DumDumsmall, the three ThreeHeadedDragons | 889 … 0 | to do | to do | to do |

### 1. Models (`tools/creature.py`, `WldWriter --mdl`)

An SE1 `.mdl` is vertex-animated, so each NE clip is baked into frames.

1. `python tools/creature.py export DumDumlarge` poses the disc model
   (`tools/objmodel.py`: one skinning matrix per bone per frame, through the
   CPU skin groups or a GPU list's palette) and writes, under the game root's
   `Models/NextEncounter/Enemies/<name>/`:
   - `Frames/bind.obj` and one OBJ per frame, every frame the same vertices
     and faces;
   - `<name>.scr`, the modeler script: the bind pose as the mip model, one
     `ANIMATION` per NE clip (`recoilback1Source` becomes `RECOILBACK1`);
   - `<name>.tga`, the texture, and `<name>.json`, what was exported.
2. `WldWriter --mdl <name>.scr <name>.mdl` builds the model with the
   engine's own `CEditModel::LoadFromScript_t`, as Serious Modeler does, and
   writes `<name>.h`, the animation ids a class includes
   (`DUMDUMLARGE_ANIM_IDLE1`). `WldWriter --tex` makes the `.tex`.

What each choice rests on:

- **The importer.** The fork is built without Exploration 3D, so
  `CObject3D::LoadAny3DFormat_t` loaded nothing and batch loading threw. A
  patch (`pc/patches/obj-model-import.patch`) gives it a Wavefront OBJ
  reader that fills the same conversion arrays: vertices through the
  script's transform with x and z negated, v negated, faces flipped when the
  transform mirrors. **Checked against the designers' own toolchain:**
  rebuilt from their `MonkeyMan.obj` and `.scr` (`dev/Models/enemies/China/
  minion/MonkeyMan`), the model equals their `MonkeyMan.mdl` in all 361
  vertices, in order, and, with the importer's mapping as it was, in all 706
  polygons with every corner's winding and texture coordinates; only the
  order polygons are stored in differs. Without negating v, every V came out
  with the opposite sign. (With the seam fix below, the corners their
  importer mapped wrong now differ from their model, and match their OBJ.)
- **UV seams.** SE1's modeler kept one texture coordinate per vertex per
  surface, in three places: `RemapVertices` built the opened and unwrapped
  copies per vertex (the first polygon's UV won), `CEditModel::AddMipModel`
  made one texture vertex per vertex a surface, and
  `CalculateUnwrappedMapping` found each texture vertex's UV by searching the
  opened copy for the same position (the last match won). With several
  materials, seams mostly fall on material boundaries, which get separate
  texture vertices; with one, every polygon along a seam inside it was
  mapped with the other side's coordinates. The original importer did the
  same: the designers' `MonkeyMan.mdl` has **324 of its 706 polygons** off
  their OBJ's UVs, by up to 1,880 mex, and 187 of its 361 vertices sit on a
  seam. The patch keys the copies on vertex and texture vertex, and a model
  mapped from one object takes each polygon corner's coordinates from the
  same corner of the unwrapped copy (the three copies come from one file,
  in the same polygon and corner order), splitting a texture vertex per
  distinct UV. Checked by matching every model polygon to the OBJ triangle
  with the same vertices: MonkeyMan rebuilt 0 of 706 wrong, DumDumlarge 187
  of 1,070 before and 0 after, and a two-material copy of it 0, with its
  surfaces split as assigned. Vertices and frames are unchanged.
- **Axes and size.** The frames are written as the designers' Maya exports
  are: NE's model space with x negated, faces wound outward, `vt` = 1 − NE's
  v. Their MonkeyMan's head sits at x −0.057 where NE's sits at +0.142, and
  its top vertex has v 0.755 against NE's 0.245. NE's creature is 2.5 times
  the size of that export, 1.7 units tall, the size it has in NE's levels,
  so `SIZE` stays 1.0. In the built model a vertex at NE's (x, y, z) sits
  at (x, y, −z): DumDumlarge's first frame is within 0.017 units of NE's
  idle pose there (the dump interpolates toward frame 1), against 0.16 or
  more for any other clip.
- **25 frames a second.** `IEngResAnimator_QueueClip` (`0x80127e68`) sets a
  clip's length to frames / 25.0 (`0x8021d5a8`), so every frame lasts 0.04 s.

DumDumlarge: 535 vertices, 1,070 faces, 5 animations, 170 frames.

**Not handled yet:** models with more than one texture (TweedleDumDum has
two; SE1 models take one), and the rigid tails NE's skin records leave out
on KleerKnight, merman and TweedleDumDum (`docs/image-format.md`, "Still open
for the models").

### 2. Classes (`pc/entities`)

`pc\build-windows.ps1 -Entities` builds `pc/entities/EntitiesNE.vcxproj` like
the fork's `EntitiesMP`: each `.es` goes through Ecc, and the DLL derives from
EntitiesMP's classes (`CEnemyBase` is exported from `EntitiesMP.dll`; its
headers are read as imports, `StdH.h`). It is written as
`Bin/EntitiesNEMP.dll`: a class file naming `Bin\EntitiesNE.dll` gets the mod
extension `MP` inserted when it loads (`CEntityClass::Read_t`). The build
copies `pc/entities/Classes/*.ecl` to the game root. A class file is text, and
the filename carries the stream keyword: `Package: TFNM Bin\EntitiesNE.dll`,
as in the retail archive's own.

**Names and ids are the designers'**: their `Entities.dll` has every NE
creature as `CGCEnemy<Group><Name>` on `CEnemyBase`, ids 1101–1307
(`docs/dev-material.md`). DumDumLarge is `CGCEnemyGenericDumDumLarge`, 1120.

**Stats come from `MinionStats.cfg`** (`build/cfg/MinionStats.json`), the
creature's `Object<N>` over `Generic`: health, normal and fast speed, sight
and hearing, field of view, turn time, attack delays, score. What a creature
does to you is in the same file's `Extras` section, one block per creature
(`Extras.DumDumLarge`: `Damage` 5); the code reads it through the minion's
stats method (below).

`GCEnemyGenericDumDumLarge`: health 30, speed 10 walking and 14 charging, a
full turn in 1 s, view 90°, score 100. NE's clips play as SE1's standing,
walking, running and rotating animations, and its two recoils by which side
it was hit from. It bursts when it dies (NE's `SplatterType` 1; there is no
death clip). Written into the worlds as the template for its 144 spawner
templates; the writer creates, saves and loads it.

### 3. Behaviour (from `main.dol`)

NE's creatures are C++ classes with their own AI, reached only through
vtables (`cDumDumLarge::Collided`, `0x800154c4`, has no direct caller). Two
halves per creature: a **minion** (the body: movement, collision, health,
animation) and an **AI** object that thinks for it by pushing **directives**
onto the minion's queue.

**The classes name themselves.** Every AI and directive class has a virtual
method that returns its own name (`lis r3; addi r3; blr` to a string such as
`"cMoveToObject"`). `python tools/aiclasses.py [--json build/ai_classes.json]`
finds all 85 such functions in `main.dol` and the 84 vtables that list them,
with no Ghidra:

- **AI classes**, one per creature (`cDumDumLargeAI` `0x801ecbf0`,
  `cMonkeyManAI` `0x801f5d90`, `cAtlanteanAI` `0x801f1148`, …), on
  `cAIDirective` (`0x801e20a0`), 11 methods (12 for the legionnaire ants).
- **Directive classes**, 4 methods (destructor, name, update, interrupt):
  `cWalkTo`, `cMoveTo`, `cMoveToObject` `0x801e2de8`, `cJump`, `cLaugh`,
  `cGetHit` `0x801e29c0`, `cPatrol`, `cFindObject`, `cAttackObject`,
  `cCircleObject`, `cPlayAnim`, `cThrow`, `cBlock`, `cPauseStack`
  `0x801e37b0`, `cWalkDirection`, `cEndMove`, …

CodeWarrior's vtable entries are 8 bytes, `{s16 this-adjust, 0, function}`;
a call through slot `+0x11c` reads the function at `+0x11c` and the
adjustment at `+0x118` (`ghidra/scripts/ghtool.py vtable ADDR` lists one).

**`cAIDirective`, the shared AI.** Fields: `+0x10` target, `+0x14` the
minion, `+0x18` state (short), `+0x1c` the stats, `+0x28` a 1 s timer.

- **Update** `0x800064b0`, every AI's method 2: in state 0 or 3 it sets
  state 1; with no target, state 3; otherwise it calls the creature's
  **Think** (method 6, `+0x3c`). Each second it runs `0x8000682c`, which
  deactivates a minion left out of range longer than `ActiveTime`.
- **SetState** `0x80006570`: entering state 1 finds the player as target,
  state 2 calls the minion's `+0x134`, state 3 pushes a `cPauseStack` for a
  random time between `AttackPostDelayMIN` and `MAX`; then the creature's
  **OnEnterState** (method 7, `+0x44`).
- **The stats** (`cMinionStats`), in the loader's string order:

  | offset | field | offset | field |
  |---|---|---|---|
  | `+0x00` | FOV | `+0x28` | Score |
  | `+0x04` | SeeDist | `+0x2c` | Health |
  | `+0x08` | HearDist | `+0x30` | UpdateRate60hz |
  | `+0x0c` | PatrolSeeDist | `+0x34` | SplatterType |
  | `+0x10` | PatrolHearDist | `+0x38` | CollisionRadius |
  | `+0x14` | ActiveTime | `+0x3c` | AttackTime |
  | `+0x18` | NormalSpeed | `+0x40` | AttackFrequency |
  | `+0x1c` | FastSpeed | `+0x44` | TurnSpeed |
  | `+0x20` | AttackPostDelayMIN | `+0x48` | WalkTime |
  | `+0x24` | AttackPostDelayMAX | `+0x4c` | MoveTimeOut |

- **The minion base** (`0x8008xxxx`–`0x8009xxxx`): `+0x20` position,
  `+0x5c` velocity, `+0xd4` heading (normalized by `0x8008ae74`), `+0x1c`
  flags, `+0xfc` the directive queue. `0x80096ad0` (`+0x174`) is true while
  the queue is empty and `+0x10c` is 0. `0x8008a3f4` sets the velocity
  toward a point at a speed (no arrival test). `0x80096424` is the stuck
  recovery: more than 10 frames in a row blocked, it resets the AI (`+0x134`)
  and queues a directive. `+0x11c` (`0x80096b84`) returns the creature's
  `Extras` block by its type (`+0x3c`: DumDumLarge 17), whose `+0x6c` is
  `Damage`.

**`cDumDumLargeAI`** (constructor `0x80021dd4`, starts in state 2):

- **Think** `0x80021e34`: state 1 goes to 5; state 5 goes to 6; state 6
  lunges while the queue is empty (`0x80096ad0`), else goes to state 1 and
  disarms (clears flag `0x20`).
- **OnEnterState** `0x80022040`: state 5 pushes a `cMoveToObject` on the
  target that stops at 10 units (`0x801ecbe4`) and gives up after
  `MoveTimeOut`; state 6 resets the animation, stops, stores the target's
  position and zeroes the lunge clock (`+0x38`).
- **The lunge** (`0x80021ed8`–`0x8002202c`, read from the instructions: the
  decompile stops at `PSVECAdd`): every update it aims at the stored position
  plus the heading × 2.0 (`0x801ecbd8`) and moves there at the running speed
  (`0x8008a3f4`); when the clock passes 12/25 s it plays its attack sound
  (`+0x30c`, `0x80095c58`, creature sound 4), sets flag `0x20` and sets the
  clock to −1000, so it arms once a lunge.

**`cDumDumLarge`, the minion.** Its own code is a handful of short methods
(`0x800151d8`–`0x80015834`) over the minion base.

- **Constructor** `0x800151d8`: calls the base constructor `0x800904c4`,
  then installs four vtables, one per base: `0x801e5fb0` at `+0x48` (the
  main one, CodeWarrior's `{method, adjustment}` pairs), `0x801e5f90` at
  `+0x58`, `0x8020d040` at `+0x108`, and `0x801e3e48`, then `0x801e4808`, at
  `+0x228` around a member constructed at `+0x22c`. It sets bit `0x400` of
  the flags at `+0x1c`.
- **Destructor** `0x800152b0`: the vtables back, then the base destructor
  `0x800908f0`.
- **`Collided`** `0x800154c4` (main vtable entry at `0x801e613c`): while
  `+0x244` is set and flag `0x100` is up, a contact more than 0.75
  (`[r13-0x7f68]`) beyond both collision radii (vtable `+0x194`) is ignored;
  otherwise the base's collision handler `0x80093f90` runs, and with flag
  `0x20` up (armed) and the two within 1.0 (`0x801e5f80`) beyond both radii
  it takes the other's damage receiver (`+0xec`), hands it (`+0x98`) a hit
  of its `Extras` `Damage` from its own position, and clears flag `0x20`.
- `0x80015708` and `0x80015834` are two more overrides (a hit or death
  effect spawn, and a push direction away from what hit it).

**The port** (`GCEnemyGenericDumDumLarge.es`) keeps SE1's `CEnemyBase` for
sight, chase and pain, and puts NE's numbers and lunge in it:

| NE | port |
|---|---|
| state 5, `cMoveToObject` stopping at 10 | `m_fCloseDistance` 10: the base chases, then calls `Hit` |
| state 6 lunge toward stored position + heading × 2 | `Hit` sets `m_vDesiredPosition` each tick at `FastSpeed` |
| armed at 12/25 s, once | `m_bArmed` after 0.48 s, `m_bStruck` stops a second arming |
| `Collided`: armed and within 1.0 beyond both radii | the gap to the target polled each tick (SE1 sends no touch between two walking models, only `EBlock` to the mover), `InflictDirectDamage` 5 |
| stuck recovery after 10 blocked frames | `EBlock` for more than 10/60 s ends `Hit` |
| a hit reaction queued ends the lunge | `AnimForDamage` disarms; the base's wound handling interrupts `Hit` |

Still open: what ends a lunge that misses in NE, when nothing blocks or
hits the DumDum (it keeps aiming 2 units ahead of the stored spot, so it
would circle it); the port gives up after `MoveTimeOut` (5 s). The attack
sound (creature sound 4) is not played yet. Not yet tried in play.
