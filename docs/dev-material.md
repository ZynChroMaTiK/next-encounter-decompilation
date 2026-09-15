# `dev/` — developer material from NE's production

`dev/` holds files from a developer on *Next Encounter*, from the period
when the levels were built in Serious Engine 1.04's Serious Editor, before
Climax converted them to their own GameCube format. There are 6,466 files
(2.5 GB). None of this is on the disc.

It is proprietary, and it is **git-ignored and read-only**, like `orig/`.
Tools read it in place and write what they derive under `build/dev/`.

What it gives the project:
1. **The source of every character model.** The Maya exports explain NE's
   object models, including the vertices no bone moves.
2. **The entity classes the designers used**, with every property named and
   typed. NE's per-class property blocks are Climax's packing of these.

## Inventory

| Path | Files | What it is |
|---|---|---|
| `Bin/` | 44 | SE1 1.04 tools (Jan 2002): `SeriousEditor.exe`, `SeriousModeler.exe`, `Ecc.exe`, `Engine.dll`. Also four entity DLLs, below. |
| `Bin/Debug/` | 17 | Debug builds and `EntitiesD.map`, a linker map with public symbols of a Jan 2002 build. |
| `classes/` | 182 | `.ecl` stubs, `Package: TFNM Bin\Entities.dll` / `Class: C<Name>`, one per class. |
| `Models/` | 6,012 | Per-model folders. `.mb` is Maya; `.clm` is Climax's Maya export (below); `.cclm` lists the `.clm` files that make one animation set. `.mdl` `.tex` `.scr` `.ini` `.h` `.obj` `.tbn` are SE1 Modeler files. `.max`, `.avi` and `.rar` hold effect sources. |
| `Models/FMA/FMARequirements.doc` | 1 | The list of models and animations for the six cutscenes. |
| `Venice/` | 228 | A **PS2 deathmatch** level: the model scripts say `Levels\PS2DM\Venice`. There are 18 SE1 `.wld` worlds (magic `BUIV`, build 10000); no GameCube level matches it. |
| `Models/items/Level/levelRlevel1_2_Waterfall/waterfallArea.wld` | 1 | A Serious Editor world of `Rlevel1_2`'s waterfall area, an early version (Jan 2003): 1,934 polygons. The measure for rebuilding polygons, below. |

The model folder names match NE's object model names (`KleerKnight`,
`merman`, `TweedleDumDum`, the `Rlevel1_2_Stalactites`-style level items).
Each source can therefore be paired with its disc model by name.

## `.clm` — the Maya export (`tools/clm.py`)

The file is a tree of chunks, little-endian. The root is a bare header:
`"ClimaxModel\0"` and one u32 (0). The top-level chunks follow it to the end
of the file. Each chunk is:

```
char name[]      NUL-terminated, zero-padded to 4 bytes, counted from the
                 chunk's own start (data can be odd-sized, so chunks need
                 not be aligned in the file)
u32  nchildren
u32  size        payload bytes
u8   data[size]
     nchildren chunks follow
```

The chunks seen:

```
Mesh
  Vertex List            f32[3] × n
    Weights List         one Bone Bind per vertex, in vertex order
      Bone Bind          one Weighting child per influence
        Weighting        bone name (padded), f32 weight
  Normal List, Name, Matrix (f32[16]), Local Pivots
  Face                   × faces
    Material ID, Normal List, Texturing → UV Set ("map1")
Material                 Texture, Diffuse/Ambient/Specular/Emissive/Transparency,
                         Plug Bool / Plug Int (TwoSided, Blend, EngineTexture,
                         EngineBump, Env, AlphaSort)
Animation Clip           Name ("idle1Source", ...)
  Skeleton
    Bone                 nested as the hierarchy; payload is the bone name
      Translate / Rotate / Scale → Key Frame, LayerID, LocatorID, Matrix
      Mesh Ref           a rigid mesh carried by this bone
Camera, Transform Node   Maya scene leftovers
```

`python tools/clm.py verify` finds 0 problems:

| check | result |
|---|---|
| files that parse exactly to their end | 1,049 of 1,049 |
| meshes | 1,291: 692 weighted, 599 rigid |
| rigid meshes named by a Bone's `Mesh Ref` | 14 |
| files with a Skeleton | 687 |

The clip names are the ones NE keeps: merman's anims on the disc are
`attack1Source`, `death1Source`, `idle1Source`, and so on.

Unlike NE's models, the `.clm` stores a **skeleton**, as nested bones with
bind matrices.

## The unbound vertices, explained

`docs/image-format.md` left one question open. On three characters, NE draws
vertices that no skin group moves. The sources answer it. Across the disc
there are 50 such model instances, but only 3 distinct models. In every one,
the skinned vertices are a prefix of the position array and the unbound ones
all come after it:

| NE model | skinned prefix | tail | the tail in the source |
|---|---|---|---|
| merman | 464 | 49 | `propellorShape`, 55 vertices, **rigid**, carried by the `propellor` bone. Its bounding box equals the tail's to 0.01. |
| TweedleDumDum | 572 | 19 | `PropShape`, 20 vertices, **rigid**, carried by the `Prop` bone. Its box matches the tail's at another spin angle. |
| KleerKnight | 602 | 807 | A second, newer mesh: `KleerKnight_meshShape`, 809 vertices, used by `death2` and the `scaled_*` clips. The 602 are `kleer_meshShape` from the other clips. Both are weighted in the source, but NE carries weights only for the old mesh. |

So the exporter appended meshes to the position array that the skin
records do not cover:
- the propellers, which are rigidly parented rather than skinned;
- on KleerKnight, a leftover second body.

NE merman has 25 bones where the source has 29. The four missing are
`shoulders`, `locator1`, `locatorShape1` and `propellor`. The propeller's
own bone is among them, so nothing in NE's data can move it.

**What the game does with them.** `CcMesh_SkinCPU` calls
`DCZeroRange(buf->ptr, buf->size)` before accumulating. The model's position
array is a `CcArray {ptr, byteSize, count, stride}`, and its `byteSize`
covers every position:
- merman: `0x180c` = 513 × 12;
- KleerKnight: `0x420c` = 1,409 × 12.

If the per-instance skin buffer copies that descriptor, the tail is zeroed
every frame and collapses onto the origin, so its triangles degenerate and
never show. I haven't yet found the code that builds the buffer, so this is
likely but not proven.

**For the port:**
- Dropping the tail reproduces what the game most likely shows.
- To show the parts instead, the source is enough:
  - bind the propellers rigidly to the nearest surviving ancestor of their
    bone;
  - skin KleerKnight's second body with its own source weights, matched by
    bone name.
- Positions on the disc are X-mirrored against Maya. merman's asymmetric box
  is `[-2.02, 2.32]` on the disc and `[-2.32, 2.02]` in the source. Every
  model on the disc is mirrored this way (`docs/world-conversion.md`,
  "Axes"). The exporters undo it, so exported models match their sources as
  they are. Only raw disc data needs X negated to match.

## The entity classes (`tools/se1dll.py`)

The four entity DLLs export every class as `C<Name>_DLLClass`, a
`CDLLEntityClass`. It holds the class id at `+0x20` and the base class at
`+0x24`.

In 1.04 the property arrays (`CEntityProperty`, 32 bytes each) and the enum
tables all have constructors. So in the file they are zero, and static
initializers fill them when the DLL loads. `se1dll.py` recovers them by
emulating those initializers with capstone. It follows constant registers,
stores to `[abs]` and `[reg+disp]`, and `thiscall` constructor calls, whose
arguments are pushed right to left with `ecx` = the record.

The check: a property id is `class id << 8 | n`. That holds for all but 3 of
the 4,282 properties in the three `Entities.dll` builds. None comes out
untyped. Properties with no name are genuine: SE1 hides a property that has
no editor name.

`python tools/se1dll.py dump` writes `build/dev/se1_classes.json`.

| DLL | date | classes | properties | enums |
|---|---|---|---|---|
| `Entities.dll` | May 2003 | 156 | 1,446 | 64 |
| `Entities.dll.orig` | Aug 2003 | 102 | 1,514 | 64 |
| `Entities.dll.new` | Sep 2003 | 108 | 1,322 | 50 |
| `GCEntities.dll` | Sep 2003 | 49 | 11 | 5 |

**`Entities.dll` is the build that made NE.** It is the only one with the
GameCube classes:
- `CGCEnemy*`, class ids 1101–1561, every enemy and FMA actor NE has;
- `GCLevelPar`, `GCFMVPlayer`, `CGCWarpPlayers`, `CGCArrow`, `CGCLockDown`,
  `CGCMPFlag`, `CGCTreasureItem`, `CGCVehicle`, `CGCWeather`, and
  `CGCDBCreate` (1000);
- the Climax additions to the stock classes, such as SoundHolder's
  `SoundID` / `StreamId` / `SubtitleId`.

The other three builds differ:
- `.orig` and `.new` have none of these, but do have stock SE1 enemies such
  as `CBeast` and `CWerebull`.
- `GCEntities.dll` reuses ids 1000–1802 for a different roster: a jeep,
  combine and submarine; wildlife; rocket pack and scuba gear; enemies such
  as `GunWasp` and `RazorFish`. It is not the shipped game.

SE1 class ids are not NE's: SoundHolder is 204 in SE1 and 4100 in NE. The
pairing is by name, by the field lists, and where decoded, by NE's own
values:

| NE class | SE1 class (`Entities.dll`) | fields, in SE1 order | fit |
|---|---|---|---|
| 4100 Sound | `CSoundHolder` | Sound, Fall-off, Hot-spot, Volume, Looping, Surround, Volumetric, Name, Auto start, Destroyable, **SoundID, StreamId, SubtitleId** | confirmed: NE's decoded `destroyable`, sample, speech and subtitle hashes |
| 4130 MessageHolder | `CMessageHolder` | Name, Distance, Active, Next, **MessageID**, ForceNetrisca, **TitleID**, FirstMessage, GfxFilename | confirmed: NE `+4` text hash and `+0xc` title hash |
| 4170 TouchField | `CTouchField` | Name, Enter Target, Enter Event, Exit Target, Exit Event, Active, Players only, Exit check time, *(hidden)*, Block Walking Enemy, Block Flying Enemy | NE's 9 words; `+0xc` is the decoded Enter Target |
| 4220 Bouncer | `CBouncer` | Name, Speed, Direction (h, p, b), Control time, Max exit speed, Normal / Parallel component multiplier | NE `(speed, h, p, b, volume)`, e.g. `(65, 90, 10, 0, 1)` |
| 4120 Camera | `CCamera` + `CCameraMarker` | Camera: Time, FOV, Target, OnBreak, WideScreen, Motion blur, skippable, HideSam. Marker: Delta time, **Bias, Tension, Continuity**, Stop moving, FOV, Skip to next, Fade Color, Trigger, Motion blur, GameTime | confirmed by NE's camera code: the markers are packed into the props, and the path is a Kochanek–Bartels (TCB) spline (`docs/world-conversion.md`, "Cameras") |
| 4330 LevelPar | `GCLevelPar` | Name, ParTime, ParKills, BronzeScore, SilverScore, GoldScore | confirmed: `CcParWatcher_Init` reads the five in this order |
| 4340 FMVPlayer | `GCFMVPlayer` | Name, FMV | NE `+0x00` points at the movie name (`12_2_FMV`) |
| 4280 WarpPlayers | `CGCWarpPlayers` | Name, Active, Target | confirmed: the init tests Active; the u16 target is a PlayerStart on 139 of 139 |
| 4260 Arrow | `CGCArrow` | Active, Inherit Pos, Do Not Scale, Type (Yellow / Green Arrow) | confirmed: the init tests Active and picks `yellowarrow` / `greenarrow` from `+0x10`; NE adds a points-at link at `+0x08` |
| 4270 LockDown | `CGCLockDown` | Active, Inherit Pos, Scale | confirmed Active (the init tests it); NE `(1, 1.5, …)` |
| 4110 Damager | `CDamager` | Name, Type (DamageType), Ammount, Entity to Damage, DamageFromTriggerer | NE `(1000, -1, 1, …)` |
| 4010 Copier | `CCopier` | Name, Target, Spawn Effect | NE `(target id, 1, …)` |
| 4050 DoorController | `CDoorController` | Name, Target1, Target2, Width, Height, Players Only, Type (Auto / Triggered / Locked / Triggered Auto), Locked message, Locked target, Key, Trigger on anything, Active | |
| 4190 Vehicle | `CGCVehicle` | Type (Jeep / Combine / Submarine / HoverBoard), DeathTarget | NE's type numbers are its own (Jeeps 2, submarines 3, the Combine 0); u16 Active at `+0x08`; two links |
| 3215 Treasure, 4320 Flag | `CGCTreasureItem`, `CGCMPFlag` | StringID, ModelName, Value | |
| 4310 ParticleHolder | `CParticlesHolder` | Type (Lock Twinkle, Waterfall Spray1/2, Teleport, Steam, Smoke, Lightning), Count, Stretch…, Size, Param1–3, Active | confirmed: the init reads s16 Active and type; NE numbers the types its own way (0 Lock Twinkle … 6 Lightning) |
| 4210 ModelDestruction, 4240 DestroyableArch | `CModelDestruction`, `CDestroyableArchitecture` | …, Debris Type, **SoundID, DestructionSoundID**, NoDestructionSound | confirmed: the init loads debris type (SE1's values unchanged), count, size, health and an s16 trigger |
| 4230 RollingStone | `CRollingStone` | Bounce, Health, Damage, Fixed damage, Stretch, Deceleration, Start Speed, Start Direction | the class is confirmed by `EntityFactory_Create`; the field order is only a fit |

"Fit" means NE's words sit where the SE1 fields predict. Only the rows
marked confirmed rest on decoded NE code.

Enums worth knowing (`Entities.dll`):
- **`DamageType`**: EXPLOSION, PROJECTILE, CLOSERANGE, BULLET, DROWNING,
  IMPACT, BRUSH, BURNING, ACID, TELEPORT, FREEZING, CANNONBALL, …,
  CHAINSAW. Values run 1–18, and 9999 is NONE.
- **`CustomEffectGCN_Type`**: None, WaterFall, Fountain,
  RotateZ Slow (SpitRoast), Energy Pulse. This is a GameCube-only effect
  hook on models and moving brushes.
- **`EventEType`**: SE1's stock list, Start / Stop / Trigger / …. NE's own
  event codes (`docs/world-conversion.md`) are not this list.

## `waterfallArea.wld` — the designers' polygons

`python tools/devworld.py <world>.wld` lists a dev world's polygons into
`build/dev/wld/`: flags, plane, and for each of the three texture layers its
texture, mapping, scroll, blend, flags and colour per polygon, its edges, and
the triangles stored in the file. Its layers are the evidence for how NE's
material labels pack the layer settings (docs/image-format.md, "Texture
layers"). The engine reads
it (`WldWriter --dump`, brushes only, so NE's missing entity classes don't
matter); the script copies the world under the game root for the run and
gets the texture names through by standing an installed texture in for each.

The waterfall area is an early version. Its geometry matches `Rlevel1_2`
under no rotation, mirror or scale (3 of 911 vertices at best), so it cannot
be laid over the disc. What it does give is the shape of polygons as the
designers built them, in their order:

| | |
|---|---|
| brushes | 2, one empty; 11 sectors |
| polygons | 1,934: 1,741 triangles, 167 quads, 26 with 5–11 corners |
| concave, of those with more than 3 corners | 44 of 193 |
| holes | none |
| textures | 12, all `Levels\roman\...` and two detail maps |
| consecutive pairs coplanar, sharing an edge, same texture, mapping and flags | 179 |

That last row is why a rebuilt polygon can be a union of originals:
`tools/polygons.py replay` puts this list through the rebuild rule and gets
1,550 of 1,918 back exactly, 367 inside 122 unions, and 1 split
(`docs/world-conversion.md`, "Polygons").

## Not yet used

- **The Venice `.wld` files.** They are PS2-only, but they are complete SE1
  1.04 worlds, useful as reference for the Stage 2 writer's output.
- **`EntitiesD.map`.** It holds the symbols of a Jan 2002 `EntitiesD.dll`,
  from before the GameCube classes.
- **The SE1 `.mdl` / `.scr` for each model.** The designers placed these
  stand-ins in Serious Editor. They are candidates for the port's first
  model form.
