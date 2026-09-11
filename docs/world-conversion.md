# Converting a level to a Serious Engine 1 world — sectors and lights

**Status.** Stage 1 is implemented and verified on all 49 levels
(`tools/world.py`). Stage 2 — the writer that produces a `.wld` — is specified
below against the `pc/engine` headers, with every call checked against a
file and line, but it is **not compiled**: the machine has no C++ toolset yet
(`pc/README.md`).

## The method

Three artefacts per level feed one writer:

| artefact | made by | carries |
|---|---|---|
| `build/mesh/<level>.obj` + `.mtl` + PNGs | `tools/level.py mesh` | geometry with UVs and textures; movable geometry already in world space; one OBJ group per material (`mat12`) and per movable node (`sector7`) |
| `build/entities/<level>.json` | `tools/entity.py json` | the 26,823 entity placements and the 10,036 placed models |
| `build/models/<level>/<model>.obj` + `.mtl` + PNGs | `tools/model.py meshes` | every prop mesh and each of its damage stages, in local space, one group per material |
| `build/world/<level>.json` | `tools/world.py describe` | **what OBJ cannot carry**: which groups form which sector and with what properties, which groups are separate brush entities, every light as an SE1 `Light` property set, and every prop as a model holder |

The writer builds SE1 sectors through the same `CObject3D` →
`FromObject3D_t` path that WorldEditor's own import uses, and creates `Light`
entities by setting properties by name.

**All convention-sensitive math happens in the writer, using SE1's own
functions** — `DecomposeRotationMatrixNoSnap` for matrix → placement,
`DirectionVectorToAngles` for direction → orientation. Stage 1 records raw NE
data plus the mapping *decisions*, and never re-implements an SE1 convention,
so it has nothing to silently disagree with.

### Why this route

- **Editor import is not available.** SE1's `CObject3D::LoadAny3DFormat_t`
  (`Engine/Math/Object3D_IO.cpp:210`) loads through the closed Exploration3D
  DLL via `GetProcAddress`. The fork does not ship it — the only
  `3dexploration` in the tree is a folder of test headers.
- **Writing `.wld` from Python** would mean re-implementing
  `CWorld::Write_t`, brush and BSP serialisation included. The engine already
  does that correctly.
- So the world is built in memory with the engine's own types and the engine
  serialises it.

## Sectors

### What SE1 needs

- **One `CObjectSector` becomes one `CBrushSector`.**
  `CBrushMip::AddFromObject3D_t` allocates exactly `ob_aoscSectors.Count()`
  sectors (`Engine/Brushes/BrushImport.cpp:81`), and
  `CBrushSector::FromObjectSector_t` copies colour, ambient, all three flag
  words and the name straight across (lines 104–109). So every sector property
  the writer sets on a `CObjectSector` survives into the world.
- **Sector properties are packed into `bsc_ulFlags`** (`Brush.h:515–518`):

  | bits | field | resolves through |
  |---|---|---|
  | 24–31 | content type | `WorldBase` content table |
  | 16–23 | force (gravity) type | `WorldBase.m_penGravity0…9` |
  | 12–15 | fog type | `WorldBase.m_penFog0…9` |
  | 8–11 | haze type | `WorldBase` haze markers |

  Environment (reverb) is in `bsc_ulFlags2` (`Brush.h:666`).
- **Content types** (`EntitiesMP/WorldBase.es:582–645`): 0 Air, 1 Water,
  2 Lava, 3 Cold Water, 4 Spikes, 5 Desert heat, 6 Lava (−10 HP/sec).

### What NE has, and where it goes

| SE1 | NE source | on the disc | status |
|---|---|---|---|
| `WorldBase` sector "static", content Air | every non-liquid `CcMaterial` group at `image+0x08` | 49 levels | geometry verified by render |
| content **Water** / **Lava** sectors | NE's **region BSP** — one convex cell per path to a typed leaf | **2,285 cells on 22 levels** — 2,062 water, 223 lava | verified by classification and render |
| liquid *surfaces*, render only | `CcMaterial +0x28 == 1`; water `+0x3c == 6`, lava `+0x40 == 5` | 28 water + 12 lava; the flag is set on 0 of the other 3,773 materials | exact |
| brush entities (`MovingBrush`, `DestroyableArchitecture`, extra `WorldBase` brushes) | the `image+0x48` list; its `+0x08` points at the **owning entity's own `Mtx`** | 1,209 brushes — see "Brush entities" | verified: 1,048 of 1,059 Mover pivots fall inside their own transformed geometry |
| sector ambient | NE authors level ambient as a *light* — see "the sun is a pair" below | 48 | mapped to `Directional ambient` |
| environment (reverb) | region leaf `+0x05` | 13 on every water cell — SE1 **"Underwater"** | exact |
| haze | region fog block `{on, start, end, rgb}` | 10 distinct, used by 643 cells | mapped to `HazeMarker` |
| force (gravity) | not in the region leaves | — | open; class 5020 is the candidate |

Every `+0x48` node's vertices are in local space — `Rlevel1_1`'s span
x[−6.00, 5.75] before the transform and x[−69.38, 243.12] after — and the OBJ
writes them in world space. The writer therefore takes
`local = inverse(matrix) · world` for the brush and uses the matrix itself as
the entity placement. An SE1 placement cannot carry scale, so any scale in the
matrix (recorded per brush as `scale`) is baked into the local geometry.

### Two things the writer has to handle

1. **Content comes from region volumes, not liquid surfaces.** An SE1
   content sector must be a closed volume, and NE's region cells are exactly
   that (below), so nothing has to be extruded. Liquid-material sectors stay as
   render-only surfaces with content Air. The two genuinely differ:
   `Alevel10_2`'s lava field is a liquid surface with no region at all. Cells
   are bounded only by the planes the tree needed plus the level's box, so
   some reach past walls into space no one can enter — harmless, but untidy.
2. **Open static geometry.** `BrushImport.cpp` has no rejection path, so a
   triangle-soup sector imports. Whether SE1's BSP and collision behave on a
   sector that is not closed is the first thing to test once the writer
   compiles. SE1 has `BSCF_OPENSECTOR` (`Brush.h:510`) for outward-facing
   sectors.

### NE's region system — decoded

The game's "region" is SE1's sector properties by another name, and it lives in
a BSP tree under root slot `+0x74` (`tools/bsp.py`).

- **The query.** A manager hangs off the game object at `+0x298`; its virtual
  slot `+0xa4`, called with a position, returns a region record whose byte at
  `+4` is the type. `cOctochop::Spawn` (`0x80031830`) treats 1 and 3 as water.
- **The data.** `+0x74` leads to `{planes, collision tree, header, region
  tree}`. A node is `{Plane*, child0, child1}`; a plane record is
  `{Vec3* normal, float d}`. A point goes to the child at `+4` when
  `normal·p + d ≥ 0`, otherwise to the child at `+8`.
- **The leaf** is the record the query returns:

  | offset | field | on the disc |
  |---|---|---|
  | `+0x04` | content | 0 none, **1 water (23 leaves), 2 lava (5)** — SE1's own content numbering |
  | `+0x05` | environment | **13 on every water leaf** — SE1 environment type 13 is "Underwater" (`WorldBase.es:710`) |
  | `+0x08` | fog | `{on, start, end, r, g, b}` — e.g. water `{1, 200, 1500, …}`, lava `{1, 10, 70, …}` |
  | `+0x0c` | effect | a 64×4 colour-ramp `CcTexture`, a projection matrix and a tint; no SE1 equivalent, kept raw |

**Verified two ways.**

- *Classification.* Points just under `Rlevel3_1`'s lava: 20 of 20 land in
  lava leaves. Under `Rlevel1_1`'s and `Alevel10_2`'s water: 8 of 12 and 6 of 7
  land in water leaves. Points above dry geometry: 0 of 4,500 land in a liquid
  leaf. The other three sign/child conventions score close to zero.
- *Render.* Each cell's footprint drawn under the liquid surfaces: the lava
  moat, pool and channel of `Rlevel3_1` and the pools of `Rlevel1_1` and
  `Alevel10_2` each sit inside their cells.

**As SE1 data.** Each path from the root to a typed leaf is a closed convex
cell — the half-spaces along the path plus the level's box. `world.py` builds
them as vertices and faces (CCW seen from inside): **2,285 cells on 22
levels**, put in one extra `WorldBase` brush whose sectors carry

| sector flag | from |
|---|---|
| content (`bsc_ulFlags` bits 24–31) | leaf `+0x04` |
| environment (`bsc_ulFlags2`) | leaf `+0x05` |
| haze (`bsc_ulFlags` bits 8–11) | a slot 1–9 per distinct fog; slot 0 stays empty for "no haze" |

Each distinct fog becomes a `HazeMarker` — Attenuation `FA_LINEAR`,
Near = start, Far = end, Base Color = rgb × 255 — bound as
`WorldBase.m_penHaze<slot>`. SE1's `FogMarker` is height-layered fog; NE's is
plain distance fog, which is SE1's haze.

**Not in the region leaves:** force (gravity) — every leaf's remaining words
are zero. The two further trees in the collision header are unidentified, and
they are *not* keyed by TouchField volume index: only 43 of 444 fields land in
a leaf carrying their own index, about one per level. Ruled out along the way:
the numbers in material names, root slots `+0x5c` and `+0x7c`, and the literal
stores to `+0x298`.

### Visibility

Fifteen materials are labelled `Custom Material - PVS`. They own **no display
lists and no texture** — a visibility construct whose geometry is not in the
render data. Not mapped.

## Brush entities

SE1 builds seven classes as brushes — `MovingBrush`, `DestroyableArchitecture`,
`Bouncer`, `TouchField` (a *field* brush: an invisible volume), `Pendulum`,
`Ship` and `WorldBase` (`InitAsBrush` / `InitAsFieldBrush` in `EntitiesMP`).
SE1's `Trigger` and `Damager` are point entities, so NE's 3,632 triggers and
134 damagers need no volume at all.

| NE class | on the disc | SE1 class | geometry | status |
|---|---|---|---|---|
| 4070 Mover | 958 | `MovingBrush` + `MovingBrushMarker`s | `image+0x48` — all 958 | **ready, with motion** |
| 4240 DestroyableArch | 116 | `DestroyableArchitecture` | `image+0x48` — all 116 | **ready** |
| none | 135 | extra `WorldBase` brush | `image+0x48` | **ready** |
| 4170 TouchField | 444 | `TouchField` | not in `+0x48` | properties ready, **volume not** |
| 4220 Bouncer | 64 | `Bouncer` | not in `+0x48` | properties ready, **volume not** |

Every `image+0x48` brush is tied to its owner the same way — the node's
`+0x08` points at the owning entity's own matrix — and the tie is total:
**958 of 958 Movers and 116 of 116 DestroyableArch entities have a brush, and
no brush points at any other class.** An earlier version of `world.py` only
matched Movers and filed the 108 visible DestroyableArch brushes as unlinked.

15 of the 1,209 brushes (7 Mover, 8 DestroyableArch) have **no render
triangles**: collision-only volumes. They are recorded with
`invisible: true`, but their geometry lives in the per-node collision records,
which the OBJ does not export yet.

### Mover motion — NE's `MovingBrush` is SE1's

Class 4070's own name in the game is `MovingBrush` (its init,
`CcMovingBrush_Init` at `0x8007edcc`, logs it), and it moves the way SE1's
does. These are the fields as its init and update (`CcMovingBrush_Update`,
`0x8007f0b8`) read them:

```c
struct CcMovingBrushProps {    // classId 4070
    u32  autoStart;            // +0x00  init sets "start moving"; 68 movers
    f32  time;                 // +0x04  seconds per segment: t += dt / time
    f32  wait;                 // +0x08  seconds held at a key before leaving
    u32  brushIndex;           // +0x0c  its image+0x48 node, 958 of 958
    u32  keyCount;             // +0x10
    Key* first;                // +0x14  always props + 0x48
    u32  _18;                  // +0x18  1 on 79 lifts/drop tiles: move on touch?
    u32  moveOnDamage;         // +0x1c  damage accumulates and starts it
    f32  _20;                  // +0x20  -1.0 on 957
    u32  switchId;             // +0x24  entity id (LINKS)
    u32  sound[3];             // +0x28  START, LOOP, END sample hashes; 0 = none
    u32  loopSound;            // +0x34  play LOOP while moving
    f32  _38;                  // +0x38  5000 or 500 on 12: health?
    s16  damageTarget;         // +0x3c  sent TRIGGER when damaged; -1 = none
};
struct Key {                   // 0x58 bytes each, from props + 0x48
    f32  time;                 // +0x00  >= 0 replaces the current time on arrival
    f32  wait;                 // +0x04  >= 0 replaces the current wait on arrival
    u32  stop;                 // +0x08  halt here ("Stopped moving at marker")
    Key* next;                 // +0x0c  null on the last key
    u32  _10;
    Mtx* xform;                // +0x14  usually &mtx below; elsewhere on 46 keys
    u32  _18[4];
    Mtx  mtx;                  // +0x28  the key's full placement
};
```

The mapping is one to one (`tools/world.py` `mover_motion`):

- **Keys become `MovingBrushMarker`s**, chained in order, with the last one
  pointing back at the first: after its last key NE heads for key 0. Each key's
  matrix is its marker's placement.
- **Time becomes Speed, unconverted.** SE1's Speed is also seconds per segment:
  velocity = `(vTarget − vSource) / m_fSpeed` (`MovingBrush.es:598`).
- **The arrival rule is the same in both engines.** Arriving at a marker loads
  its time, wait and stop flag: the wait is held there, and the time drives the
  next segment. A negative value keeps the current one (`LoadMarkerParameters`,
  `MovingBrush.es:421–433`). So a two-key door with `stop` on both keys opens
  and shuts on alternate triggers in both.
- **The brush's own Speed and Wait time** are what NE's init computes: key 0's
  value when it is ≥ 0, otherwise the header's. **Auto start** and **Move on
  damage** copy across.

Evidence:

- All 958 key walks end on a null `next` after exactly `keyCount` keys.
- All 2,016 key matrices are valid.
- Key 0 is the brush's own placement on 945 movers, and within 0.8 units on
  the other 13.
- All 14 damage targets resolve to Triggers.
- On `Rlevel1_1`, rendered at key 1, every door sits in its own opening and the
  gates rise by their own height.
- Disc-wide, a two-key sliding door travels a median **0.86× its own extent**
  along its direction of travel, with 65% between 0.8× and 1.2×. Doors open by
  about their own size, as doors do.
- 52 movers never stop (cogs, pistons).

### TouchField and Bouncer: properties yes, volumes not yet

**Properties map cleanly.**

- TouchField `props + 0x0c` is its **Enter Target**: all 156 fields that set
  one point at a real entity id. `+0x00`/`+0x04` look like *Active* and
  *Players only* — (1,1) on 423 of 444 — but that is unconfirmed.
- Bouncer props read `(speed, heading, pitch)` — `(2000, 0, 90)` is straight
  up, the same convention as SE1's `Direction` default `ANGLE3D(0,90,0)` — so
  they become *Speed* and *Direction*. Likely, not proven.

**The volume is the gap.** Ruled out, in turn:

- `image+0x48` — no node points at either class.
- Transform scale — all 444 touch fields and 64 bouncers are unit scale.
- Props — 36 and 32 bytes, no extents.
- The space between matrix and props — neither class has one.
- The constructors — `0x80084154` and `0x800777e8` only call a shared base
  initialiser (`0x8009b090`) that leaves the object's bounding box at its
  empty sentinel, so the volume is attached later.

Both classes carry an **index at `props + 0x10`** that counts 0, 1, 2 … per
level, and 30 of the 40 levels with touch fields hold a sorted table under
root `+0x74` with exactly one entry per field.

**Root `+0x74` is the level's collision tree**: a pool of plane records — a
pointer to a shared unit normal plus a distance — 7,838 planes over 1,424
normals on `Rlevel1_1`, alongside dense webs of tree nodes. Touch and bouncer
volumes are very likely convex leaves of it. Decoding it is a job of its own.

Until then the writer can place `TouchField` and `Bouncer` entities with
their properties, but has **no honest size** for their volumes — and should
not invent one.

**What the game's code says (read after the above).**

- **Registration.** A TouchField's init (`0x80084198`) stores the entity in a
  128-slot global table at `0x802d970c`, indexed by `props + 0x10`. A
  Bouncer's (`0x8007782c`) uses a 32-slot table at `0x802d8708`. The only
  readers are two getters, `0x80084618` and `0x80077a88`.
- **Volumes are collision geometry.** Both getters are called from collision
  response (`0x80090b74`, `0x800ab4ac`). That code queries the world
  collision object (`CcLevel + 0x29c`, also `DAT_802ab3a4`) and gets back
  0x30-byte hit records, each with a **type at `+0x24` and an index at
  `+0x28`**:

  | type | meaning | index |
  |---|---|---|
  | 1 | bouncer | bouncer volume index |
  | 2 | moving brush | sector index |
  | 3 | touch field | touch volume index |
  | 4 | hazard damage | — |

  So a TouchField's volume is whatever collision primitives carry
  (3, its index).
- **The collision world is filled at load.** `CcLevel_LoadByName`
  (`0x8010b5f8`) has the engine manager create the object. It is not a
  static class, which is why its query has not been found by vtable scans.
  The collision cells in `+0x74` index two object arrays whose sizes the
  image declares but whose contents are zero on disc (129 objects on
  `Rlevel1_1`). No sum of up to three level-list sizes matches those counts
  on all 49 levels.
- **The two header trees are not volumes.** Their leaves have the collision
  tree's shape `{0, id, (u16 index, u16 flags)*, n, next*}`, so they are
  spatial indices too. Their leaf count equals the touch-field count on only
  3 of 94 trees.
- **Bouncer direction, confirmed by code.** The init builds the push
  direction as `Ry(−heading)·Rx(−pitch)·(0,0,1)`, with angles in degrees.
  `world.py` records that world vector, and the writer turns it into SE1's
  `Direction` with `DirectionVectorToAngles`.

**Where the chain now ends (a second pass at the code).**

- **Collision objects are render objects, tagged with an owner.** The code
  that sets the hit type is four small virtual methods. They sit side by side
  in the vtable at `0x8021e878`, which belongs to the class whose strings are
  `RO::ActualRender` and "Object Being Completely hidden by mask":

  | method | type | extra state |
  |---|---|---|
  | `RO_SetBouncer` (`0x8013de90`) | 1 | stores the owner raw |
  | `RO_SetMovingBrush` (`0x8013def0`) | 2 | copies a float from the owner, may register with `FUN_8013fef0` |
  | `RO_SetTouchField` (`0x8013dfb0`) | 3 | none |
  | `RO_SetHazard` (`0x8013e02c`) | 4 | sets a constant float |

  Each one resets the object, then stores the owner at `+0x1c` and the type
  at `+0x24`. So a touch field's volume is a render object's geometry, and
  the tag only says which field it belongs to.
  - `CcTouchField_Init` only registers the entity in its index table.
  - Render objects are made by `RO_CreateInList` (`0x8013ecc0`), which
    links each new one into a list at `owner + 0x28`. It is itself virtual
    and has no static caller, so the level code that decides which geometry
    becomes a touch volume has not been reached.
- **The collision leaves index those runtime objects.** A leaf is `{0, id,
  entries*, n, next*, …}`, and its entries are `(u16 object, u16 flags)`
  with one flag bit each: `(0x0b, 0x8000)`, `(0x15, 2)`. The object index
  stays inside the second runtime array (129 on `Rlevel1_1`).
- **Ruled out:**
  - The runtime counts match no level list, or sum of up to four lists, on
    all 49 levels (materials, sectors, meshes, placed models, touch fields,
    bouncers, movers, destructibles, props, damagers, lights).
  - The first count equals the prop-mesh count on only 2 levels.
  - The `cmpwi …, 1000` sites are GX render-state setup, not the type-1000
    collision factory.
  - No level string names a trigger volume; every "Touch Field" string is
    an entity name.
- **Next.** Statically, the scene class whose vtable holds `RO_CreateInList`
  would lead to the level code that builds the render objects. Dynamically,
  a Dolphin RAM dump taken inside a level would show the runtime render
  objects directly: those tagged 3, their owners and their geometry. That
  is likely the shorter road.

## Props — placed models

The `+0x58` scenery list becomes SE1 model holders. The mesh format is in
`docs/image-format.md` ("The mesh data — typed").

| NE | on the disc | SE1 | rule |
|---|---|---|---|
| instance, mesh with one distinct texture | 6,694 | `ModelHolder2` | `Model` `.mdl`, `Texture`, `StretchX/Y/Z` |
| instance, mesh with 2–4 distinct textures | 477 | `ModelHolder3` | `.smc` with one surface per texture, `StretchXYZ` |
| instance with no mesh | 2,860 | no stock class | each carries `smallflame` (1,502) or `bigflame` (1,358); the game spawns particle effect 2 or 4 there (`CcLevel_SpawnFlames`, `0x8010b790`). SE1's `ParticlesHolder` has no flame type, so this needs a flame model on a `ModelHolder2`, or a new class built with Ecc |
| `models\editor\axis.clm` | 5 | `Marker` | an editor gizmo whose mesh the shipped image lacks |

- **Placement.** A GameCube `Mtx` sends local axis k onto column k, so each
  axis's stretch is that column's length and the rotation is the matrix with
  its columns normalised. As for brushes, the writer calls
  `DecomposeRotationMatrixNoSnap`. Stretch is not clamped. The smallest
  (0.01) belong to Atlantis energy beams — `enr_cylinder` squashed thin and
  stretched along its axis — and 1,077 props are non-uniform, mostly trees and
  bushes varied by hand.
- **Texture count, not material count, picks the class.** 3,202 props have
  more than one material, but most repeat one texture across them (the birch
  tree: two materials, one texture), which a single-texture MDL still
  carries. Only 477 need `ModelHolder3`.
- **Damage stages become SE1's destruction chain.** 1,105 instances carry
  `_d1`/`_d2` damage meshes, and 1,080 of them are breakable Prop entities
  (class 4200) that already link a `ModelDestruction` (`LINKS`). In SE1 a
  `ModelHolder2`'s `Destruction` (`EntitiesMP/ModelHolder2.es:94`) points at
  a `ModelDestruction`, whose `Model 0`…`Model 4`
  (`EntitiesMP/ModelDestruction.es:49–53`) are template model holders swapped
  in when it breaks. A second stage is the `_d1` template's own
  `Destruction`, with the `_d2` template as its `Model 0`. Each prop lists its
  stage files under `damage_states`.
  These looked like LODs when rendered — statues and trees keep their outline
  and roughly halve in polygons per stage — but the barrel and cabinets *gain*
  triangles, and the Prop correlation above is what decides it.
- **Collision** (`Colliding`) is not set yet: which instances collide is
  unread. Health and debris come from NE's ModelDestruction entity, whose
  props are not decoded.

## Scripting — triggers, waves, watchers and the rest

NE's fight scripting is the same thing SE1's is: entities pointing at each
other by id. Each class keeps its links at fixed props offsets (`LINKS` in
`tools/entity.py`), and `tools/entity.py graph` checks every one. **27,151
links across the 49 levels; 27,146 (99.98%) resolve to a real entity** — the
5 that don't are all trigger slots. Id 0 means "no target": the level root and
every unnamed class-5020 entity carry id 0.

| NE link | n | points at | SE1 property |
|---|---|---|---|
| Trigger targets 1–10 | 12,845 | Wave 50%, Trigger 15%, Mover 7%, … | `Trigger` Target 01–10 |
| Wave `template` | 6,660 | EnemyTemplate 100% | `EnemySpawner` Template Target |
| Wave `patrol` | 1,684 | Marker 99% | `EnemySpawner` Patrol target |
| Prop `destruction` | 1,395 | ModelDestruction 100% | the prop's destruction |
| Watcher `target` | 1,146 | Trigger 87% | `WatchPlayers` Owner/Target |
| EnemyTemplate `death_target` | 1,145 | Trigger 84%, Wave 15% | `EnemyBase` Death target |
| Copier `target` | 864 | Ammo, Health, Armour templates | `Copier` Target |
| Marker `next` | 728 | Marker 100% | `Marker` Target — patrol paths |
| TouchField `enter_target` | 156 | Trigger 98% | `TouchField` Enter Target |
| Damager `entity` | 104 | DestroyableArch 75%, Prop 25% | `Damager` Entity to Damage |
| Teleport `target` | 65 | TeleportTarget 100% | `Teleport` Target |
| Switch `target` / Mover `switch` | 46 / 46 | Trigger / Switch | `Switch` ON-OFF Target |
| Mover `damage_target` | 14 | Trigger 100% | none on `MovingBrush`; open |
| DoorController `target1/2` | 28 | Trigger, Mover | `DoorController` Target1/2 |
| pickup `target` | 180 | Trigger | `Item` Target, fired on pickup |
| Arrow `points_at` | 38 | Weapon, Key, AmmoPack | none — an objective arrow |

### Trigger: SE1's shape, NE's event codes

```c
struct CcTriggerProps {      // classId 4080, 180 bytes
    u32 active;              // +0x00
    ...
    u32 target[10];          // +0x14  entity ids; -1 = empty slot
    u32 event[10];           // +0x3c  0 trigger, 1 on, 2 off
    u32 useCount;            // +0x64  set on all 526 triggers with count > 1
    u32 count;               // +0x68
    ...
    s32 maxTrigs;            // +0x74  -1 by default, as in SE1
    f32 wait;                // +0x78
};
```

Across 3,632 triggers: 12,845 target/event pairs — 11,132 trigger, 1,138 off,
575 on; 630 use a count and 585 wait before firing.

The event codes are NE's own, not SE1's `EventEType` numbering. Code 0 goes to
things that get fired — 6,489 of 6,501 slots aimed at a Wave. Codes 1 and 2 go
to things that get switched: watchers, objective arrows, particles, lockdowns.
**1 = on, 2 = off is likely, not proven**: triggers named "…deactivate/stop/off"
send 2 in 132 of their 167 switch slots. For SE1, 0 becomes `EET_TRIGGER`, and
1/2 become `EET_ACTIVATE`/`EET_DEACTIVATE` or `EET_START`/`EET_STOP` depending
on what the target class listens for.

One encounter, from `tools/entity.py graph Rlevel1_1.ssw --chain
"drillAmbushWave: DumDumSpawner"`:

```
Wave 'drillAmbushWave: DumDumSpawner'
 └─template→ EnemyTemplate 'drillAmbushWave: DumDum Template'
    └─death_target→ Trigger 'drillAmbushWave: DumDum Death Counter'
        ├─[trigger]→ Small Door 8 L, Small Door 8 R, Small Door 7 L/R
        ├─[off]→     LockDown
        ├─[on]→      Watcher 'watcher room re-entry'
        └─[trigger]→ Copier, 'fly through hallway Camera'
```

Kill the ambush and the doors open, the lockdown lifts, the next watcher arms
and a camera flies down the hallway — which is also why the on/off reading
looks right in context.

**Scalar fields decoded alongside the links:** Watcher active, watch distance
(10, 20 …) and wait (0.1) → `WatchPlayers`; Teleport and DoorController width
and height; Damager amount (1000.0 — SE1's own default); Wave count, delay and
interval; Bouncer speed and direction (likely).

**Not decoded yet:** camera paths (props up to 1,368 bytes of spline data),
Sound and Music (one reads like radius 100/50 and
volume 1.0), ParticleHolder, ModelDestruction, LevelPar (reads like a par
time and score thresholds — 400.0, 14, 130000, 140000), FMVPlayer, WorldLink,
Vehicle, LockDown, WarpPlayers, and classes 4230 and 5020. Two flags are
recorded raw because they are not links: EnemyTemplate `+0x0c` and pickup
`+0x30`/`+0x34`.

## Music — NE's dynamic music is SE1's `MusicHolder`

NE mixes three stereo layers of a track by fight intensity, and the
thresholds are `base.cfg` `LevelMusic.ThreshMedium` = 100 and
`ThreshHeavy` = 1000. SE1's `MusicHolder` has **Music Light / Medium /
Heavy**, and its **Score Medium / Score Heavy** default to exactly 100 and
1000. The stream format is in `docs/sound.md`.

- A Music entity (class 4090, props `{u32 stream hash, u32 flag}`) names a
  track. `world.py` records the track's three layer WAVs
  (`build/sound/music/trackNN_layerK.wav`).
- A level's first Music entity becomes its `MusicHolder`: the layers become
  Light/Medium/Heavy, with the thresholds above.
- Later ones change the music mid-level ("Changing music stream..",
  `0x8008123c`). SE1's `MusicChanger` takes a single file, so these keep all
  three layers and the writer decides.

Across the disc there are 47 Music entities on 43 levels: 43 become
`MusicHolder`s and 4 become changers, and every hash resolves to a track.
The layer order (layer 0 = Light) follows the voice order and has not been
checked against the game's intensity code.

## Sounds and messages — `SoundHolder` and `MessageHolder`, both one to one

- **SoundHolder (class 4100).** `SoundHolder` is NE's own name for the
  class, and its fields are SE1 `SoundHolder`'s: Sound, Fall-off, Hot-spot,
  Volume, Auto start, Destroyable. The struct is in `docs/sound.md`.
  - `world.py` applies the corrections NE's init makes (58 holders change)
    and takes Looping from the sample's bank entry (27 loop).
  - 108 of 213 holders become an SE1 `SoundHolder` naming a WAV.
  - Speech is recorded beside the sample, one WAV per language. NE plays
    speech with no position, so the writer should play it globally rather
    than inside a holder's range.
- **MessageHolder (class 4130)** is NETRICSA on both sides: SE1's computer
  and NE's `NETRISCA_*` messages are the same device.
  - Its props hold a text hash (`+0x04`) and a title hash (`+0x0c`) into
    the `.tdb` (`docs/text.md`).
  - `tools/text.py messages` writes each one as an SE1 message file
    (`SUBJECT`, `IMAGE none`, `TEXT`) per language, and `world.py` points
    the SE1 `MessageHolder`'s Message at it.
  - All 63 resolve.

Both classes act on a trigger, so the trigger graph carries over unchanged.

## Lights

### What NE has — from the game's own code

Field names are the game's: the debug print at `0x8013f314` reads
`LightDefault %d col=<..> pos=<..> dir=<..> as=%0.2f ae=%0.2f`. The node
layout is in `docs/image-format.md`.

| type | count | runtime behaviour |
|---|---|---|
| 1 | 8,666 | **never instantiated** by the level light setup (`0x801386cc`) |
| 2 | 11,496 | instantiated as runtime lights, **at most 270 per level**; colour × 255.0 (`0x8021e65c`) and clamped; the object also receives a constant 5000.0 (`0x8021e660`) |
| 3 | 48 | separate directional path (`0x8013f0ec`); colour × 256.0 and clamped; uses `dir` |

Three findings shape the mapping:

- **`as`/`ae` are authored, not consumed.** The runtime light object never
  receives them, and the per-material light binder the setup calls,
  `0x8013f6d8`, is an **empty stub** in the shipping build. Mapping them to
  SE1's hot-spot/fall-off preserves designer intent, not a traced GameCube
  falloff.
- **Colours go overbright.** 10,825 lights (53%) have a component above 1.0,
  up to 2.30. For types 2 and 3 the game clamps at 255 itself, so clamping is
  faithful.
- **Every level's sun is a pair.** All 48 non-directional lights with
  infinite reach (`as = ae = 999999`) are type 1, and **all 48 sit at exactly
  the position of a directional light**, across 38 levels. That is one
  authored object: a directional light plus its ambient fill.

### Mapping onto `EntitiesMP/Light.es`

| SE1 property | from NE | rule | why |
|---|---|---|---|
| Type | `type` | 1, 2 → `LT_POINT`; 3 → `LT_DIRECTIONAL` | |
| Color | `col` | `round(abs(c) × 255)`, clamped to 0–255 | the game's own scale and clamp for types 2 and 3; type 1 is never instantiated, so its clamp is our choice |
| Dark light | sign of `col` | TRUE when negative | all 52 are negative in every component; SE1 refuses dark *directional* lights and none are |
| Hot-spot | `as` | copied | SE1 copies it straight to `ls_rHotSpot` |
| Fall-off | `ae` | copied | SE1 copies it straight to `ls_rFallOff` |
| Directional ambient | the co-located sun fill | fill colour, on the directional light | `SetupLightSource`: *"only directional lights are allowed to have ambient component"* |
| orientation | `dir` | `DirectionVectorToAngles(dir)` in the writer | SE1 lights a model from `pos − dir·1000` (`RenderModels.cpp:430`): `dir` is the direction light travels in both engines |
| Dynamic | — | FALSE | NE lights never move; SE1 bakes shadow maps for static lights |
| — | `ae ≤ 0` | not created | 4 lights, all in `Rlevel2_2`: authored with no reach |

**20,210 NE lights become 20,158 SE1 `Light` entities** — the 48 sun fills
fold into their directional lights, and the 4 no-reach lights are dropped.

### Lighting is re-baked by SE1, not carried over

The obvious alternative was to carry NE's baked lighting across. There is
nothing to carry:

- **Array 7 is not vertex lighting.** Predicting per-vertex light from every
  combination of light types, with and without N·L, correlates with it at
  r ≈ 0. Its four bytes are each near-uniform over 0–255, mutually
  uncorrelated, and identical near and far from lights — two packed 16-bit
  values, not a colour.
- **There are no lightmaps.** No material has a second texture (261 of 261 on
  `Rlevel1_1`).

So the writer converts the lights and lets SE1 compute shadow maps from them.

## Force fields (class 5020) — recorded, not emitted

The 23 class-5020 entities, all in Atlantis levels, carry a vector `(0,0,g)`
with g ∈ {−10, −1, 0.5, 1, 2, 4}, a zero vector, then a 3×4 matrix; their own
transform pointer is null. That is the shape of SE1's `GravityMarker`
(`Parallel`, strength |g|). But only 14 of the 23 translations fall inside
their level, and `AlevelDM_2`'s miss by up to **3.7× the level's span** — so
either the matrix reading or the class's meaning is wrong there. They are kept
in the description with `emit: false` until the code that consumes them is
read.

## Stage 2 — the writer, as SE1 calls

Every name below was checked against `pc/engine/SamTSE/Sources`. Not
compiled.

```cpp
CWorld wo;

// 1. Static world: one CObjectSector per sector in the description.
CObject3D o3d;
for (const Sector &s : desc.worldbase.sectors) {
  CObjectSector &osc = *o3d.ob_aoscSectors.New(1);        // Object3D.h:364
  osc.osc_strName    = s.name;
  osc.osc_colAmbient = s.ambient;                          // Object3D.h:298
  osc.osc_ulFlags[0] = (s.content << BSCB_CONTENTTYPE)     // Brush.h:515-518
                     | (s.force   << BSCB_FORCETYPE)
                     | (s.fog     << BSCB_FOGTYPE)
                     | (s.haze    << BSCB_HAZETYPE);
  for (const Face &f : obj.faces(s.groups))                // the OBJ groups
    osc.CreatePolygon(3, f.vertices /* DOUBLE3D[] */,     // Object3D.h:324
                      material(f.texture), 0, FALSE);
}
CPlacement3D plOrigin(FLOAT3D(0,0,0), ANGLE3D(0,0,0));
CEntity *penBase = wo.CreateEntity_t(plOrigin,             // World.h:204
                     CTFILENAME("Classes\\WorldBase.ecl"));
penBase->Initialize();
penBase->GetBrush()->FromObject3D_t(o3d);                  // BrushImport.cpp:49,
penBase->GetBrush()->CalculateBoundingBoxes();             // as WorldEditor.cpp:2470

// 2. Brush entities: doors, gates, moving brushes.
for (const Brush &b : desc.brush_entities) {
  CPlacement3D pl;
  pl.pl_PositionVector = b.matrix.translation;
  DecomposeRotationMatrixNoSnap(pl.pl_OrientationAngle,    // Geometry.h:41
                                b.matrix.rows);
  CEntity *pen = wo.CreateEntity_t(pl, CTFILENAME("Classes\\MovingBrush.ecl"));
  // geometry: local = inverse(matrix) * the OBJ group, scale baked in,
  // then FromObject3D_t on the entity's own brush as above
  // motion: one MovingBrushMarker.ecl per b.motion.markers, placed by its key
  // matrix the same way. Each marker's "Target" is the next marker (the
  // last's is the first) and the brush's "Target" is marker 0. The se1
  // property sets go on by name, as for lights.
}

// 2b. Region volumes: one more WorldBase whose sectors are the region cells.
CObject3D o3dRegions;
for (const Cell &c : desc.region_brush.sectors) {
  CObjectSector &osc = *o3dRegions.ob_aoscSectors.New(1);
  osc.osc_strName    = c.name;
  osc.osc_ulFlags[0] = (c.content << BSCB_CONTENTTYPE)
                     | (c.haze    << BSCB_HAZETYPE);
  osc.osc_ulFlags[1] = c.environment << BSCB2_ENVIRONMENTTYPE;  // Brush.h:666
  for (const Face &f : c.faces)          // CCW seen from inside the cell
    osc.CreatePolygon(f.count, f.vertices, regionMaterial, 0, FALSE);
}
// ...then FromObject3D_t on a second WorldBase entity, and one HazeMarker
// per desc.haze_markers, stored in that WorldBase's "Haze <slot>" property.

// 3. Lights.
for (const Light &l : desc.lights) {
  if (l.skip) continue;
  CPlacement3D pl;
  pl.pl_PositionVector = l.position;
  if (l.has_direction)
    DirectionVectorToAngles(l.direction, pl.pl_OrientationAngle);  // Geometry.h:48
  CEntity *pen = wo.CreateEntity_t(pl, CTFILENAME("Classes\\Light.ecl"));
  for (const Prop &p : l.props) {                          // by editor name
    CEntityProperty *ep = pen->PropertyForName(p.name);    // Entity.h:226
    ENTITYPROPERTY(pen, ep->ep_slOffset, p.Type) = p.value;  // EntityProperties.h:132
  }
  pen->Initialize();
}

// 3b. Props: one model holder per desc.props entry, placed like a brush
// entity. Stretch comes from the matrix column lengths, so the rotation
// handed to DecomposeRotationMatrixNoSnap is the matrix with its columns
// normalised. The .mdl / .smc files are converted from build/models/ first.
for (const PropRec &p : desc.props) {
  CPlacement3D pl;
  pl.pl_PositionVector = p.matrix.translation;
  DecomposeRotationMatrixNoSnap(pl.pl_OrientationAngle,
                                normalise_columns(p.matrix.rows));
  CEntity *pen = wo.CreateEntity_t(pl, p.se1.cls == "ModelHolder3"
      ? CTFILENAME("Classes\\ModelHolder3.ecl")    // .smc, one surface per texture
      : CTFILENAME("Classes\\ModelHolder2.ecl"));  // .mdl, a single texture
  set_props_by_name(pen, p.se1);                   // as for lights
  pen->Initialize();
}

// 4. Bake and save.
wo.CalculateDirectionalShadows();                          // World.h:347
wo.CalculateNonDirectionalShadows();                       // World.h:348
wo.Save_t(CTFILENAME("Levels\\NextEncounter\\Rlevel1_1.wld"));  // World.h:253
```

`CreateWorldBaseEntity`, which WorldEditor uses, is an editor helper rather
than an Engine function, so the writer creates `WorldBase.ecl` itself.

Positions are copied unscaled — NE and SE1 are both y-up. Whether gameplay
needs a unit scale (player height against NE's door sizes) is open.

## Verification

`python tools/world.py verify` across all 49 levels:

| check | result |
|---|---|
| liquid surfaces, render only | 40 — 28 water, 12 lava |
| region cells | 2,285 on 22 levels — 2,062 water, all environment Underwater; 223 lava |
| haze markers | 10; 643 cells use one |
| brush entities | 1,209 — 958 `MovingBrush`, 116 `DestroyableArchitecture`, 135 `WorldBase`; 15 collision-only |
| mover motion | 958 of 958 movers, 2,016 markers; 68 auto-start, 52 never stop, 0 zero-time keys |
| touch fields / bouncers | 444 / 64; all 156 set TouchField targets resolve to an entity |
| lights | 20,210 — 48 directional, 52 dark, 10,825 clamped |
| sun fills folded into a directional light | 48, 0 left unpaired |
| no-reach lights dropped | 4 |
| SE1 `Light` entities to create | 20,158 |
| force fields | 23 recorded, 0 emitted |
| OBJ groups referenced but missing from the mesh | 0 |
| props | 7,171 — 6,694 `ModelHolder2`, 477 `ModelHolder3`; 1,105 with damage stages |
| prop model or damage-stage files missing | 0 |
| effects (instances with no mesh) | 2,865 — 1,502 `smallflame`, 1,358 `bigflame`, 5 editor placeholders, 0 unknown |
| bouncers with a code-derived direction | 64 of 64 |
| sound holders | 213. 108 become `SoundHolder` (27 looping, 58 corrected like the game); 93 of 96 speech hashes resolve; 8 samples and 3 speech hashes are missing in the game too |
| messages | 63 of 63 resolve title and text in all three languages |

`python tools/world.py render` draws each description over its level —
liquid sectors tinted by content, brush entities in purple, point lights as
fall-off rings in their own colour, dark lights in magenta, directional light
as an arrow. On `Rlevel1_1` the pool is its water sector, every door and gate
sits in its fence gap or doorway, and light rings cluster inside rooms. On
`Alevel10_2` the lava field and the octagonal pool are separate content
sectors and the pistons are movers.

## Open, in order

1. **Compile Stage 2** — needs the Visual Studio C++ toolset and the Vulkan
   SDK; then test open-sector BSP and collision first.
2. **TouchField and Bouncer volumes** — not in `+0x48`, and not keyed into the
   header trees by volume index; the collision tree and its two unidentified
   neighbours are what is left. Also export the 15 collision-only brushes.
3. **Force (gravity)** — absent from the region leaves; read the code that
   consumes class 5020 before emitting `GravityMarker`s.
4. **Region-cell polygons** — which SE1 polygon flags make them invisible and
   passable, and trimming cells to the level's geometry.
5. **Units** — whether NE's scale needs converting for gameplay.
6. **Props, the rest** — OBJ → `.mdl` / `.smc` conversion needs the same
   toolchain as Stage 2. Still unread: prop collision, NE's ModelDestruction
   props (health, debris), and the two pointers in each damage-stage
   record. The 2,860 flames are identified (small or big, at a known
   position) but have no stock SE1 class: `ParticlesHolder`'s 19 types
   include none. Either give a `ModelHolder2` an animated flame model, or add
   a flame class with Ecc. NE's effects 2 and 4 are themselves unread.
7. **Sounds** — mover START/LOOP/END sounds, and likely every other entity
   sound, are 32-bit hashes. `Sound_FindSampleByHash` (`0x8010ee70`) looks
   them up in a 479-row table at `0x8023adf4`. Each 24-byte row is
   `{hash, variation count, u32* sample indices, …}`, and the indices point
   into the one bank, `Sound/sfxbank1.spt` / `.spd`. 2,117 of the 2,173 mover
   hashes that are set are in the table. The other 56 are missing in the game
   too: it logs "Failed to find sample in FindHash" and plays nothing. **The
   bank is now extracted** (`tools/sound.py`, `docs/sound.md`), and each mover
   sound in the description names its WAVs. What remains is the writer: SE1's
   `MovingBrush` takes sound *entities*, so it creates one `SoundHolder` per
   sample set. NE's own SoundHolders are already mapped ("Sounds and
   messages").
