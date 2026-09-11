# Inside the CLIMAX image

The container (header, two zlib streams, relocation table) is covered in
`docs/findings.md`. This file is about what is *inside* the inflated image —
the serialised object graph — and how much of it is typed so far.

## Resource types

One container format, five resource types, each with its own loader. Found by
their `RES_*_load` debug strings; all five call the same
`CcImport_Load` → `CcImport_ReadOld` path, then interpret the image differently.

| Loader | Address | Extension | Notes |
|---|---|---|---|
| `CcRes_Glob_Load` | `0x8012d814` | `.ssg` | resource bundle (`Data.ssg`, GUI, skins) |
| `CcRes_WorldMesh_Load` | `0x80137b70` | `.ssw` | levels |
| `CcRes_Texture_Load` | `0x80131fd4` | `.sst` | standalone textures |
| `CcRes_Font_Load` | `0x8012c990` | `.ssf` | fonts (no `.ssf` on the disc) |
| `CcRes_Mesh_Load` | `0x8012e8ac` | — | meshes |

## Common image header

Every image starts the same way:

| Offset | Meaning |
|---|---|
| `+0x00` | `1` — version |
| `+0x04` | `0x64` (100) — version/type constant, identical across all files |
| `+0x08` | pointer to the root object |
| `+0x0c…` | type-specific root slots |

`+0x08` is confirmed by `CcRes_Font_Load`, which does
`*(*(image + 8) + 0x1c) = 0` before handing that object to the texture setup.

### Where the texture list lives — depends on resource type

This is the one thing that will silently give wrong answers:

| Resource | Texture list head | Walker |
|---|---|---|
| GLOB (`.ssg`) | `image + 0x18` | `0x8012d758` |
| MESH | `image + 0x0c` | `0x8012e2c0` |
| WORLDMESH (`.ssw`) | `image + 0x0c` | shares the mesh layout |

Both walkers are the same shape:

```c
for (node = *(Node**)(image + SLOT); node; node = node->next) {
    node->handle = 0;                 // +0x1c, runtime only
    CcTexture_Setup(node, ...);       // 0x80131cb4
}
```

Reading a `.ssw` at `+0x18` yields 2 nonsense nodes instead of failing, which is
exactly how an undercount hides. `tools/gxtex.py` auto-detects the slot by
scoring each candidate chain against the `CcTexture` invariants (power-of-two
dimensions are the strong signal), and confirms the split cleanly:
**14 containers use `0x18`, 49 use `0x0c`** — precisely the 14 `.ssg` and 49
`.ssw` on the disc.

### Materials at `image + 0x08` — and how geometry finds its texture

> Ownership by node extent is right, but the last node's extent has to be
> closed explicitly or it claims the whole tail of the image — see "Movable
> geometry at `image + 0x48`" below, which it was hiding.

`image + 0x08` heads a list of **materials**, not geometry — in MESH *and*
WORLDMESH images alike. From the setup at `0x8012e37c`, which drives the GX
TEV/blend state:

```c
struct CcMaterial {
    CcMaterial* next;      // +0x00
    CcTexture*  diffuse;   // +0x10  bound to TEV stage 0 via tex->handle
    CcTexture*  second;    // +0x18  bound to stage 4
    u32         pass;      // +0x24  0 or 1 selects the setup path
    u32         flags;     // +0x28  bits 0/1/2 toggle three render states
    u8          modeA;     // +0x32  0/1/2
    u8          modeB;     // +0x33  0/1/2
    void*       runtime;   // +0x48  shader object built at load
};
```

**Material nodes are variable-length and physically contain their own
display-list descriptors.** That is what ties geometry to its texture: a
descriptor belongs to the material whose extent `[offset, next)` contains it.
Exact, no proximity matching — **17,115 of 17,115 display lists across all 49
levels link to an owning material, 0 unowned**.

That mattered, because the obvious alternative fails quietly. Sector nodes also
carry `"Mat 135 (6,1,7) -1 2049 # 0"` name strings, and pairing each descriptor
with its nearest preceding name string *looks* reasonable — but it matched only
283 of 370 descriptors on `Rlevel1_1`, with gaps from 56 to 1,872 bytes and
wildly varying texture counts per batch. The material list was the right
structure to ask; the strings are just labels inside it.

Across all 49 levels: **3,813 materials, 3,758 named, 3,788 with a diffuse
texture.** `tools/level.py materials` lists them; `tools/level.py mesh` writes
OBJ + MTL with faces grouped by material and `map_Kd` pointing at the PNG names
`tools/gxtex.py extract` produces (checked: 97/97 references resolve).

### World mesh (`.ssw`) — vertex arrays and lights

The root is much richer: roughly 28 pointer slots between `+0x08` and `+0x7c`,
plus a table further in. Two structures are now decoded.

**Vertex array table at `image + 0xb4`** — 13 entries of

```c
struct CcArray { void* data; u32 byteSize; u32 count; u32 stride; };
```

This is the GX indexed-vertex setup: separate arrays for positions, normals,
texture coordinates and colours, addressed by index. It is self-verifying —
`count * stride == byteSize` holds exactly for all 13 entries on all 49 levels,
and the bytes immediately after entry 12 are the exporter's AutoVSS metadata
strings, which is what fixes the count at 13.

`Rlevel1_1.ssw`:

| # | count | stride | meaning |
|---|---|---|---|
| 0 | 19,359 | 12 | **positions**, float3 |
| 3 | 2,446 | 12 | normals, float3 |
| 4, 5, 6, 12 | 8,576 / 5,970 / 2,038 / 9,863 | 8 | texcoords, float2 |
| 7, 8 | 39,515 / 1,114 | 4 | 4-byte entries; **7 is not vertex colour** (see below) |
| 1, 2, 9, 10, 11 | 1–4 | 4 | tiny/constant |

**Lights at `image + 0x34`** — a linked list, 84-byte nodes. The field names are
the game's own: the debug print at `0x8013f314` reads `LightDefault %d
col=<..> pos=<..> dir=<..> as=%0.2f ae=%0.2f`.

```c
struct CcLight {
    CcLight* next;      // +0x00
    u32      type;      // +0x04  1 static, 2 runtime point, 3 directional
    float    col[3];    // +0x08  x255 and clamped at runtime (0x8021e65c = 255.0)
    float    pos[3];    // +0x14
    float    dir[3];    // +0x20  non-zero on exactly the 48 type-3 lights
    float    as;        // +0x2c  attenuation start
    float    ae;        // +0x30  attenuation end (999999 on directional)
    u32      index;     // +0x34
    u32      flags;     // +0x38  0, 1 or 2
    ...                 // +0x3c on: runtime; +0x40 receives the light object
};
```

An earlier version of this table called `+0x20` "always -0.0" and `+0x30`
"range". Both were read off point lights only; the directional lights show
`+0x20` is a direction and the printf names `+0x2c`/`+0x30` as an attenuation
pair.

Colour components are **signed**: negative values are subtractive "dark" lights
used to shade areas down (`rgb = (-2.3,-2.3,-2.3)` in `Alevel9_1`). All 52 on the
disc are negative in every component. Don't treat them as a decode error.

How each type is used, and how they map onto SE1's `Light` entity, is in
`docs/world-conversion.md`.

Across all 49 levels: **536,024 vertices and 20,210 lights**, 0 verification
failures.

Positions and lights corroborate each other: plotting positions top-down with
lights overlaid draws a readable floorplan — walled courtyards, pillar grids,
circular rooms — with lights sitting inside rooms rather than scattered. That is
much stronger evidence than a bounds check. `tools/level.py map` renders it.

### Display lists — the render geometry

Geometry *is* GX display lists, reached through descriptors:

```c
struct CcDisplayList {          // a pointer field plus the three words after it
    void* data;                 // [opcode][u16 vertexCount][vertex data]
    u32   paddedSize;           // usedSize rounded up to 32 bytes
    u32   usedSize;             // == 3 + vertexCount * stride
    u32   vertexType;           // selects the vertex format
};
```

**A display list holds a *sequence* of primitives, not one.** Assuming one was
the costliest mistake in this whole exercise: it silently discarded half the
descriptors — theirs simply did not satisfy `usedSize == 3 + n*stride` — and
pinned geometry coverage at 27% instead of 98%. Parse until `usedSize` bytes
are consumed:

```c
o = data; end = data + usedSize;
while (o + 3 <= end && img[o] != 0) {        // 0 = trailing padding
    opcode = img[o]; n = be16(o + 1);
    emit(opcode, n, o + 3);
    o += 3 + n * stride;
}
```

Requiring the primitives to tile exactly into `usedSize` is itself a strong
validity check — on `Rlevel1_1` all 370 descriptors parse with **zero**
rejections. Consecutive lists sit back to back: `0x452e60 + 384` is exactly
`0x452fe0`.

Each vertex is a tuple of u16 indices into the `CcArray` table. Two formats
carry essentially all the geometry: `vertexType` 1 is 7 fields (stride 14),
`vertexType` 3 is 11 fields (stride 22).

**Both vertex types share one field order, and it is the same on every level.**
Type 3 simply carries four more fields than type 1:

| field | array | meaning |
|---|---|---|
| 0 | 0 | position (float3) |
| 1 | 3 | normal (float3) |
| 2 | — | *not an index* — reads as packed bytes (`0x0300`, `0x0500`), consistent with GX's matrix-index attributes |
| 3 | 4 | **TEX0 — the diffuse texcoord set** |
| 4 | 5 | TEX1 |
| 5 | 6 | TEX2 |
| 6 | 7 | colour (u32) |
| 7…10 | 8…11 | type 3 only |

The array table itself has the same shape on every level: slot 0 positions
(stride 12), slot 3 normals (stride 12), slots 4/5/6 float2 texcoords (stride
8), slot 7 colours, slots 1/2/9/10/11 usually one dummy entry. Slot 12 is
stride 8 but is *not* a texcoord set — its first float is 0.0 in every entry on
every level.

Two independent checks, because the field map is exactly where this project has
gone wrong before:

1. **Range fit.** Across all 49 levels the map yields 478 (level, field) pairs
   with **zero** out-of-range indices, and **429 of them consume the array
   exactly to its last entry** (`max index == count - 1`).
2. **Texture scale consistency.** A correct UV set applies its texture at a
   constant scale within a material, so `sqrt(uvArea / worldArea)` should
   barely vary across a material's triangles. Scoring every candidate pairing
   by the median coefficient of variation of that ratio separates the right
   answer from the wrong ones by two orders of magnitude:

   | pairing | `Rlevel1_1` | `Clevel7_1` | `Alevel12_1` |
   |---|---|---|---|
   | **field 3 → array 4** | **0.000** | **0.024** | **0.002** |
   | **field 4 → array 5** | **0.000** | **0.002** | **0.000** |
   | **field 5 → array 6** | **0.000** | **0.000** | **0.028** |
   | field 3 → array 5 | 0.636 | 0.634 | 0.578 |
   | field 4 → array 4 | 0.597 | 0.668 | 0.653 |
   | field 6 → array 4 | 0.682 | 0.727 | 0.787 |

   Every correct pairing is ~0; every wrong one is 0.5–0.9. This test needs no
   rendering and no eyeballing, which is what makes it worth keeping.

#### The earlier conclusion here was wrong

This document previously said fields 1..10 "map to different arrays on
different levels, because which GX attributes are enabled varies per level",
and listed differing array counts as evidence. That inference was mistaken. The
counts do differ per level — array 3 holds 2,446 entries in `Rlevel1_1` against
20,572 in `Clevel7_1` — but that is just different levels having different
amounts of data in the *same* slot, not a different mapping.

What actually failed back then was a map fitted to `Rlevel1_1` that asserted
extra conditions (field 2 → array 8, type-3 slots 2/5/8/9/10 must be zero); it
rejected **404 of 428 valid descriptors** in `Clevel7_1`. The right response
was to drop the bogus constraints, not to conclude the mapping was per-level.
Validating on field 0 alone was a safe retreat that worked, and it is what kept
geometry usable — but it left texcoords unreachable for longer than necessary.

Primitives seen: `QUADS` (0x80) dominates, plus `TRIANGLES`, `TRISTRIP` and
`TRIFAN`. `tools/level.py mesh` converts them and writes OBJ + MTL with `vt`
lines and `v/vt` faces; **all 884,313 triangles across all 49 levels carry
UVs**, from 328,165 texcoords. OBJ's texture origin is bottom-left where GX's
is top-left, so the exporter writes `1 - v`.

The export is self-contained: `mesh` also decodes the textures its MTL names
into the same directory, because an OBJ whose `map_Kd` lines do not resolve is
not a usable export. All **3,786 `map_Kd` references across the 49 MTLs
resolve** (1,589 PNGs), and no OBJ has an out-of-range `v` or `vt` index.
`tools/level.py render` is the verification path -- a top-down view with the
diffuse texture sampled through the UVs.

### Movable geometry at `image + 0x48` — doors and moving brushes

Doors, gates and moving brushes are **not** part of the static world mesh. They
live in their own list, and their vertices are authored in **local space** —
every one within a few units of the origin — with a matrix that places them:

```c
struct CcSector {              // image + 0x48
    CcSector*  next;           // +0x00
    void*      _04;
    Mtx*       xform;          // +0x08  places the local-space vertices
    ...
    CcTexture* diffuse;        // +0x58  its own material, like CcMaterial
    ...                        //        plus its own display-list descriptors
};
```

**48 of `Rlevel1_1`'s 62 nodes point `xform` straight at a Mover entity's own
matrix** — the same `Mtx` the class 4070 entity uses — which is what keeps a
door's geometry and its gameplay entity in step. The other 14 carry an inline
matrix. Every node also owns its display-list descriptors and its own diffuse
texture, exactly as a `CcMaterial` node does.

Disc-wide the owner is not always a Mover: **all 958 Movers and all 116
DestroyableArch entities** own an `image+0x48` brush this way, and 135 brushes
have no owning entity. Of the 1,209 nodes, 15 have no render triangles and are
collision-only.

Verified against the entity list: after transforming, **1,048 of 1,059 Mover
pivots across all 49 levels fall inside their own geometry's bounding box**.
The 11 that do not are all named "Moving Brush" or "Moving Steps" — things
whose pivot sits off the mesh because they travel — and the worst is 14.5 units
out. Before the transform, all of this geometry sat at the world origin:
`Rlevel1_1`'s sector vertices spanned x[-6.00, 5.75] z[-6.00, 6.00] and now
span x[-69.38, 243.12] z[-21.88, 167.19], inside the level's x[-85, 257]
z[-186, 170].

**43,281 triangles across the 49 levels were being exported at the origin.**
`tools/level.py mesh` now writes them in world space, each sector as its own
named OBJ object (`o sector12`) so it stays separable — 1,194 movable objects
disc-wide.

#### The last material was silently swallowing them

Ownership is by node extent `[offset, next)`, but the *last* material node has
no `next`, and closing it at `len(image)` makes it claim everything downstream.
On `Rlevel1_1` that handed material 98 all 109 movable-geometry descriptors —
742 triangles filed under a static material, at the origin, and counted as
correctly owned. The earlier "17,115/17,115 display lists linked, 0 unowned"
was true but not meaningful for these, because an over-wide extent cannot
report a miss.

Two fixes: the last material's extent is clamped to the start of the sector
list, and sector ownership is checked *before* material ownership. A
zero-unowned result is only worth something when the extents are bounded.

### Two mistakes that both looked like progress

Figures for `Rlevel1_1.ssw` (19,359 positions) unless noted.

| Approach | Result | Verdict |
|---|---|---|
| Descriptors, multi-primitive, validate field 0 only | 370 lists / 2,208 prims, 30,180 tris, **99%** | **correct** |
| Same, but validating all fields against a fixed map | identical here, **17%** on `Clevel7_1` | map was overfit to one level |
| Descriptors, single-primitive | 185 lists, 3,307 tris, 16% | dropped half the descriptors |
| Aligned scan, every index validated | 430 lists, 9,405 tris, 45% | renders level-spanning slivers |
| Aligned scan, loose stride sweep | 421 lists, 129,576 verts, "90%" | over-fits; stride 6 swallows data |
| Linear walk of the DL region | 224 lists, 78,291 verts | padding makes stride choice ambiguous |

Both real defects were in the *parse*, and both were masked by a plausible
story:

1. **One primitive per list.** Half the descriptors failed
   `usedSize == 3 + n*stride` and were dropped as invalid. They were valid;
   the lists simply held several primitives.
2. **A field→array map fitted to one level.** It validated beautifully on the
   level it came from and silently rejected most descriptors elsewhere.

In both cases the tempting response was to widen the *search* — scan more
offsets, try more strides. Every version of that raised the coverage number
and made the output worse. Coverage is not the metric; rendering is.

Corollary: geometry cannot be found by scanning aligned offsets for primitive
opcodes alone, which matches ~16% of all offsets by chance.

### The 10-byte sector records are collision boxes

Sector nodes carry a tail of 10-byte records
`[u16 flag][u16 i0][u16 i1][u16 i2][u16 flag2]`. Extracted **within sector node
bounds** they behave exactly as expected: 48 of 62 sectors carry a run, 595
triangles total, ~12 per sector, over just 53 distinct positions — 49 of which
sit in the last 10% of the position array. Twelve triangles over eight
dedicated vertices is a box. These are per-sector **collision hulls**.

Scanning the *whole image* for the same pattern instead produced 36,029
"records" covering 99% of positions, with statistics that looked compelling —
~1.86 records per vertex, and coverage barely moving as criteria tightened from
2,207 runs to 44. All of it was an artifact of running the pattern over the
index arrays. Rendering it produced garbage fans. Bound structural scans to the
structure that owns them.

### Entities at `image + 0x40` — the placement list

The biggest content structure in a level. A linked list of variable-length
nodes; because the name and the transform are stored inline, everything that
would otherwise move is reached through a pointer:

```c
struct CcEntity {
    CcEntity* next;      // +0x00
    Mtx*      xform;     // +0x04  -> the inline 3x4 matrix, past the name
    char*     name;      // +0x08  designer's label; pooled when repeated
    u32       classId;   // +0x0c  selects the props struct
    void*     props;     // +0x10  == xform + 48, the class payload
    u32       id;        // +0x14  unique in the level; what links target
    u32       flags;     // +0x18
    ...                  // +0x2c  name inline here when not pooled,
                         //        then Mtx, then props
};
```

`props == xform + 48` for 25,125 of 26,774 entities (the rest have a larger
gap, never a smaller one), and the blob runs to the next node, so its length is
`next - props`. **That length is constant per `classId`** for 34 of 42 classes —
which is the evidence that `classId` selects a struct type rather than merely
labelling one. The 8 that vary have a dominant size plus a tail: those are the
classes that append a string or a path (see below).

`xform` is a GameCube `Mtx`: `f32[3][4]`, row-major, translation in column 3.
Across all 49 levels, 95% of the 3x3 parts are orthonormal (the rest carry
scale) and 99% of translations fall inside the level's own vertex bounds.

**Across the disc: 26,823 entities in 49 levels, 43 class ids.** Every class is
named from the label its entities dominantly carry — the designers used the bare
class name (`Trigger`, `Damager`, `DoorController`, `LevelPar`, `WarpPlayers`)
often enough to make this reliable.

| Class | Meaning | n |
|---|---|---|
| 2100 / 2200 | health / armour pickup | 1530 / 1009 |
| 3100 / 3200 / 3201 | weapon / ammo / ammo pack | 182 / 2005 / 68 |
| 3210 / 3215 / 3220 | key / treasure / powerup | 26 / 454 / 41 |
| 4010 | Copier (clones another entity) | 874 |
| 4020 / 4060 | Teleport / its target marker | 65 / 76 |
| 4030 | Watcher (checkpoint/condition) | 1148 |
| **4040** | **Wave spawner** | **6665** |
| 4050 | DoorController | 26 |
| 4070 | Mover (doors, moving brushes) | 958 |
| 4080 | Trigger | 3632 |
| 4090 / 4100 | Music / SoundHolder (sample + speech) | 47 / 213 |
| 4110 / 4120 / 4130 | Damager / Camera / MessageHolder | 134 / 171 / 63 |
| 4140 / 4150 / 4160 | PlayerStart / Marker / WorldLink | 456 / 2079 / 42 |
| 4170 / 4180 / 4190 | TouchField / Switch / Vehicle | 444 / 47 / 8 |
| 4200 / 4210 / 4220 / 4240 | Prop / ModelDestruction / Bouncer / DestroyableArch | 1443 / 101 / 64 / 116 |
| 4260 / 4270 / 4280 | Arrow / LockDown / WarpPlayers | 156 / 116 / 148 |
| 4310 / 4320 / 4330 / 4340 | ParticleHolder / Flag / LevelPar / FMVPlayer | 210 / 6 / 42 / 14 |
| **5000** | **Enemy template** | **1866** |
| 5010 | light flare | 28 |
| 4230, 5020, 6002 | unidentified (6002 is one per level) | 1 / 23 / 26 |

#### How enemies are placed

Enemies are not placed directly. A **class 5000** node is a *template* carrying
the minion type; a **class 4040** node is a *wave* that names a template by its
entity `id` and says how many to spawn and when:

```c
struct CcWaveProps {   // classId 4040, 60 bytes
    f32 delay;         // +0x00
    f32 interval;      // +0x04
    u32 _08;           // +0x08  almost always 1
    u32 count;         // +0x0c  1..12
    u32 _10;           // +0x10  0 or 1
    u32 templateId;    // +0x14  -> the class 5000 entity
    u32 _18;           // +0x18  always -1
    ...
};
```

**All 6,665 wave nodes resolve to a real entity id, and 6,660 of them to a
class 5000 template** (the other 5 point at the level root). The link is
self-evidently right in the data: the wave named
`drillAmbushWave: DumDumSpawner` points at the template named
`drillAmbushWave: DumDum Template`.

#### The two enumerations, both resolved outside the level

`props + 0x00` of a class 5000 node is the minion type — but **not** the
`ObjectModel<N>` index from `LevelModels.cfg`. `main.dol` holds a 61-entry
permutation at **`0x8021ab2c`** (file offset `0x217b0c`) mapping one to the
other. It is a bijection onto 0..60 and it agrees with the designers' names on
all 41 indices the levels actually use — minion 37 is `attacksquid` by naive
indexing but `KleerKnight` through the table, and every entity using it is
named "Kleer". Without the table the roster is silently wrong for two thirds of
the cast.

`props + 0x00` of a pickup class is an item type, and its order is
`base.cfg`'s `RespawnTimes` stash read as a run. Entries 0..45 are confirmed
independently by class id: each pickup class occupies exactly one contiguous
range — health 0-4, armour 5-9, powerup 10-12, ammo pack 17-18, ammo 19-34,
weapon 35-45. Past 45 `RespawnTimes` only lists what respawns, so keys (46-49),
treasure (54), `SeriousBomb` (55) and the DM slots (57-61, 67-71) are labelled
from the designers' names and marked with a trailing `?`.

One disagreement worth recording: at 27/28/29 the config order reads
`LimpetGrenades, SpiderMines, Grenades` while the designers' labels read
grenades, limpet mines, spider mines. Every neighbouring entry agrees exactly,
and the labels are demonstrably sloppy elsewhere (item 1 is `Health10` but is
called "Item 25 health"), so the config order is used.

#### Props link to their destruction controller

Class 4200 (Prop) has a link at `props + 0x08`: **1,395 of 1,443 resolve to an
entity id, and every single one lands on a class 4210 (ModelDestruction)
node** — a perfectly class-homogeneous link, which is what makes it convincing
rather than coincidental. So a prop names what happens when it is destroyed.

That was the second hypothesis. The first was that the field indexed a
per-level model table; it fails outright (1,321 of 1,443 values fall outside
that list). Worth recording because the field *looked* like a model index: its
values are level-local and each value carries one consistent prop name per
level — which is equally explained by props of one kind sharing a destruction
controller. The model table is real, but props reach it by name; see
"Placed models at `image + 0x58`" below.

#### Inline strings

The classes whose props size varies are exactly the ones that append a string.
MessageHolder carries a **localisation key** — `NETRISCA_RLEVEL_1_1_INTRO`,
`NETRISCA_CLEVEL_7_1_BOUNCER`; 61 of them across the game. The game never
reads it. The text comes from two hashes, at `props + 0x04` (body) and
`+ 0x0c` (title), looked up in the `.tdb` (`docs/text.md`). Key carries an
asset path (`\items\Level\Rlevel1_1_Tollgate_coins`). Camera (up to 1368 bytes)
appends spline data that is still unread. Mover (up to 776) appends its
keyframes: a linked list of 0x58-byte keys, each with a time, a wait, a stop
flag and a full matrix. It is typed in `docs/world-conversion.md` ("Mover
motion"), from the game's own `MovingBrush` code.

Verified by rendering placements over the level geometry, not by coverage:
pickups line corridors, waves sit in rooms and along the canyon floor of
`Clevel7_1`, and `RlevelDM_2` shows textbook deathmatch perimeter placement.

#### Links between entities

Most classes also keep entity-id links at fixed props offsets — 27,151 of them
across the disc, 99.98% resolving to a real entity. Together they are the
whole fight script: triggers with ten targets each, waves → enemy templates →
death triggers, watchers, patrol paths, copiers, doors. Id 0 means "no
target". Per-class layout and the SE1 mapping are in
`docs/world-conversion.md`, "Scripting"; `tools/entity.py graph` checks them.

### Placed models at `image + 0x58` — the scenery list

The level's scenery: **10,036 placed models across the 49 levels**, naming
**168 distinct `.clm` meshes**. Same header shape as the entity list, without a
class id:

```c
struct CcModelInstance {
    CcModelInstance* next;   // +0x00
    Mtx*             xform;  // +0x04  3x4, and carries scale (rows are not unit)
    char*            name;   // +0x08  'Bush', 'Urn_1', 'tourch flame'
    ...                      // inline: name, then the .clm asset path,
    //                          then the matrix, then mesh data pointers
};
```

The asset path sits at an offset that drifts, because the name is stored
inline; find it by scanning the node's own pointer fields for a target
containing `.clm`. **7,176 of 10,036 nodes carry one** —
`models\effects\grass\bush.clm`, `levels\roman\models\urn_1\urn_1.clm`,
`srcdata\Levels\roman\models\birtchtree\birtchtree.clm`. The most-used meshes
disc-wide are `torch.clm` (896), `fancy_lamp_a.clm` (685), `bush.clm` (570),
`wall_lamp_old.clm` (494) and `lanternpaper.clm` (345).

All 10,036 nodes have a readable transform and **10,026 (99%) land inside their
level's own vertex bounds** — the same signature the entity list gives.

Verified by rendering the instances over the level geometry, colour-coded by
asset name: greenery falls exactly on the lawn areas that the textured render
shows as grass, lamps trace walls and run evenly spaced down the colonnade,
and urns ring the pool court. Placement and asset association are both right.

#### How a Prop entity finds its model

By **name**, not by index. **All 1,443 class 4200 (Prop) entities have their
name present in their own level's model list — 1,443 hits, 0 misses.**

This corrects an earlier entry here. The `+0x58` list was rejected as "not the
model table" on the strength of one test: class 4200's `props + 0x08` does not
index it (1,321 of 1,443 values fall outside the list). That test was sound but
the conclusion drawn from it was too broad — `props + 0x08` is the prop's
ModelDestruction link, and the lookup that actually matters is by name.

#### The mesh data — typed (`tools/model.py`)

This replaces an entry that called the prop geometry unread and "not in the
display-list descriptor form". It *is* in that form: the descriptors hang off
each mesh's own material chain, and walking that chain finds them. Why the
earlier whole-map scan reported none in model land was not re-examined.

Root slot `image + 0x5c` chains one node per distinct mesh:

```c
struct CcMesh {
    CcMesh*     next;        // +0x00
    CcMaterial* materials;   // +0x04  the world's CcMaterial node shape; each
                             //        material owns its display lists
    CcArray*    posRef;      // +0x0c  -> +0x50
    CcArray*    nrmRef;      // +0x10  -> +0x60
    CcArray*    uvRef;       // +0x18  -> +0x70
    CcArray     positions;   // +0x50  float3, local space
    CcArray     normals;     // +0x60  float3
    CcArray     texcoords;   // +0x70  float2
    CcMaterial  first;       // +0x80  name = the Maya shading group ('bushSG')
};
```

- **Vertices: GX vertex type 2, stride 8** — `{u16 position, u16 normal,
  u16 packed, u16 texcoord}`. Fitted the way the world layout was, against
  each mesh's own arrays. Field 2 is not an index, exactly as in the world.
- **Extent.** A mesh ends at the next node *and* before its own array data.
  Without the second bound the last mesh in address order read the resource
  glob after it (Rlevel1_1's tollgate statue, Alevel11_1's submarine):
  4,130 bad index refs and 1,990 unparsed lists before, 0 and 0 after.
- **Instance → mesh.** The first mesh-node pointer inside a `+0x58` instance
  node. 7,171 of 7,176 `.clm` instances link. The other 5 are
  `models\editor\axis.clm`, an editor gizmo whose mesh the image lacks. No
  mesh is shared between two `.clm` names.
- **Damage stages.** Breakable props point at further meshes, each in a
  record of its own `{name*, path*, mesh*, ?*, ?*}` carrying its own path:
  `Wine_barrel_d1.clm`, `birtchtree_d1.clm`, `birtchtree_d2.clm`. 1,105
  instances carry them, over 134 meshes. **1,080 of those 1,105 are named by a
  class-4200 Prop entity** (the breakables, each linked to a
  ModelDestruction), against 302 of the 6,066 instances without. Rendering
  alone misread them as LODs: statues and trees keep their outline and
  roughly halve in polygons per stage (birch 209 → 104 → 50). But the barrel
  (216 → 224) and the cabinets gain triangles, and the Prop correlation
  settles it. The two pointers after `mesh*` are not `CcArray` headers
  (second word 0x140, 0x17c, 0x240 …) and are unread.
- **Instances with no mesh** (2,860) have no path. Each carries an effect
  string instead: `smallflame` (1,502) or `bigflame` (1,358), matching each
  level image's own string counts exactly. After loading,
  `CcLevel_SpawnFlames` (`0x8010b790`) walks the model list and spawns
  particle effect 2 or 4 at each one's translation. The designer names
  (`longhalltourch flame`, `Brazierflame`, `wall lamp flame`) are only
  labels.

Disc totals (`python tools/model.py verify`): 503 meshes (369 models plus 134
damage stages), 666 materials all textured, 122,111 triangles, 0 bad
position or texcoord refs, 0 unparsed lists, 228 distinct names. Checked by
rendering: a front-on gallery of every mesh per level, and every instance
placed through its matrix over the level's geometry.

### Still untyped

`+0x7c` (103) is *not* a simple chain — its first node's `next` slot is not a
pointer, so the earlier "103 nodes" count was an artifact of walking it as one.
`+0x70` (6 nodes) is a second material list. The `+0x48` list is typed above;
what remains unread inside those nodes is the pointer to the position array at
`+0x2a4` and a tail of packed 16-bit values.

`+0x74` holds the level's **BSP trees** (`tools/bsp.py`): a plane pool —
records of a pointer to a shared unit normal plus a distance, 7,838 of them
over 1,424 normals on `Rlevel1_1` — and several trees of `{Plane*, child0,
child1}` nodes. One is the **collision tree**, whose leaves list
`(u16 index, u16 flags)` entries and brush-like records. One is the **region
tree**, fully decoded: water and lava volumes with underwater reverb and fog —
see `docs/world-conversion.md`. Two more are unidentified.

## Object models — enemies, characters, pickups, weapons

`LevelModels.cfg` names about 150 models (`ObjectModel<N>` = `werebull`,
`KamikazeMarines`, `WeaponPickups/uzi` …). **None of them is a file.**
`CcLevel_LoadByName` (`0x8010b5f8`) tries `levels/%s.ssg`, which the disc
does not have. The models are embedded instead:

- each level image carries the ones the level uses;
- `Data.ssg` holds the shared ones (pickups, projectiles, first-person
  weapon rigs), 89 of them;
- `FrontEnd.ssg` and `Multiplayer/skin_*.ssg` hold the menu Sam and the
  deathmatch skins.

`tools/objmodel.py` reads them.

Each model is a **nested image**. It opens with the common header `{1,
0x64, root}`, and the pointer just before it names it:

```c
struct ObjModel {                // an image: +0x00 = 1, +0x04 = 0x64
    MeshNode* mesh;              // +0x08  chain via +0x00, one node per texture
    u32       _0c;
    f32*      bounds;            // +0x10  min xyz, max xyz (see below)
    u32       _14;
    u32       flag;              // +0x18  1 on debris: gibs, shards, bark
    CcArray*  pos, nrm, clr, uv; // +0x1c..+0x28  {data, size, count, stride}
    u32       kind;              // +0x2c  0 static; 3, 5 or 7 animated
    Bone*     bones;             // +0x30  {next, name*, u32 index, ...} 0x60 bytes
    Anim*     anims;             // +0x34  {next, name*, 0, u32 tracks, ...}
};
struct MeshNode {
    MeshNode*  next;             // +0x00
    char*      textureName;      // +0x04  'blinn2SG', 'new_demonA____Default1'
    ...
    CcTexture* texture;          // +0x10  a node in the container's texture list
    ...                          // its display-list descriptors follow it
};
```

- **Arrays.**
  - Static models share all four arrays with a library image, and index a
    slice of them.
  - Characters own their positions and normals (the skinned part), and
    share colours and UVs.
- **Geometry.** GX display lists with the level's descriptor shape
  `{ptr, paddedSize, usedSize, vertexType}`. The models use two vertex types
  the world never does:
  - **type 5, stride 7**: `{u16 pos, u16 nrm, u8 clr, u16 uv}`;
  - **type 0, stride 8**: `{u8 pnmtx, u16 pos, u16 nrm, u8 clr, u16 uv}`.

  Type 0's first byte only ever holds 0, 3, … 27. Those are exactly GX's
  `PNMTX0..9`, the matrix slots of GPU skinning. On every mesh that has both
  types, the type-0 lists draw exactly the type-5 triangles (464 of 464),
  cut into pieces that each use at most ten bone matrices. So the type-5
  lists alone give the model, and positions are the bind pose in model
  space.
- **Skeleton and animations.**
  - An animated model's bones are a chain numbered 0..n−1 (`root`,
    `pelvis`, `spine1..3`, `left_thigh` …).
  - Its animations are named records: `idle1`, `walk1`, `attack1`,
    `deathbackward1`, and on some models `…Source` names left over from the
    Maya export.
  - The bone records carry only a name and an index. Their other 0x50
    bytes are zero on disc, presumably runtime matrices.
- **Bounds.** Header `+0x10` matches the model's used positions exactly
  on 2,053 of 2,226 models, and contains them on 2,140. It is a bounding
  box on most models but not all of them.

Checked by rendering front-on galleries (`python tools/objmodel.py
render`). The enemies of `Alevel9_1`, `Data.ssg`'s pickups and weapon rigs,
`Clevel8_4`'s three dragon forms and the eleven deathmatch skins in T-pose
all come out as what their names say.

`python tools/objmodel.py verify`, all 63 containers:

| check | result |
|---|---|
| models / distinct names | 2,226 / 261 |
| distinct by content (`export`) | 711 OBJ + 617 textures, 67 MB, in `build/objmodels/` with an `index.json` of which container carries what. Most levels embed identical copies; a static model is compacted from the shared library arrays to the vertices it uses |
| mesh nodes, each with ≥ 1 display list | 2,701; 1 without a texture (`darklordgibs`) |
| display lists | 3,446 type 5, 914 type 0, 0 of another type, 0 that fail to tile |
| index refs out of bounds (pos, nrm, uv) | 0 of 10,511,478 |
| type-0 triangles = type-5 triangles | 464 of 464 meshes carrying both |
| animated models (kind > 0) with bones and animations | 580 of 580; 11,948 bones, all numbered 0..n−1; 4,155 animations |
| `LevelModels.cfg` names present | 131 of 155 |

The 24 names that are absent are:

- the cutscene props (`FMA0_*`), which went to video;
- `Dragon`, `MarshHopper`, `DumDumsmall` and a few pickups (`health1`,
  `health200`, `armor10`), which no level places;
- `sam` and `missilelauncher_anims`.

### Animation — decoded

`tools/objmodel.py` poses any animated model (`pose`) and checks the whole
chain (`verify`). The frame code is `FUN_8012919c` (clip → frame → matrices
per bone; it blends two clips by lerping the twelve floats) and the key
decode inside it at `0x80129334`. Ghidra loses that decode: it is written in
paired-single instructions the stock language cannot read, behind a call it
wrongly marks no-return. It was read with `tools/gekko.py` (below).

**Clips and frames.**

```c
struct Anim {                  // chain at ObjModel +0x34
    Anim*   next;
    char*   name;              // 'idle1', 'attack1', 'deathbackward1Source'
    u32     _08;
    u32     frames;
    struct { u16* keys; u32 _[4]; } *table;   // frames entries, 0x14 bytes each
};
```

- A frame is one **u16 key index per bone**, in bone-chain order, padded to
  an even count. The stride 2 × even(bones) holds on 666 of 666 animations
  sampled.
- The index points into the model's **key pool**, `{Key* keys, u32 count,
  f32 sx, sy, sz}`. Its count is one past the largest index any frame uses,
  exactly, on every character: Atlantean 4,316, DevilStallion 16,816,
  Werebull 5,892.
- Keys are shared: bones that move together, or hold still, point at the
  same key.

**The key**, ten s16. `GQR5` (set at `0x8010a6a4` to `0x00070007`) makes
each `psq_l` a plain s16 → float, and the code scales explicitly:

```
c0 = k[0..2] / 4096      c1 = k[3..5] / 4096      (4.12 fixed point)
c2 = (c0 × c1) · k[6] / 2048                      (k[6] = 2048 on 92% of keys)
t  = (k[7]·sx, k[8]·sy, k[9]·sz)
M  = [c0 c1 c2 | t]       row-major 3×4, v' = M·v
```

The keys are **skinning matrices**: bind pose → posed, for each bone. Frame
0 of most clips is close to identity. So posing a vertex needs no hierarchy
and no bind skeleton. That is why the bone records carry only names.

**Skinning — two kinds, both in the data.**

- **GPU (type-0 lists).** Every display list sits in a record:

  ```c
  struct ListRecord {           // chain head at MeshNode +0x0c
      ListRecord* next;         // draw order; 0 on the last
      u32  hasPalette;          // 1 on type-0 records
      Desc* desc;               // = this + 0x1c
      u16 (*palette)[2];        // {bone, pnmtx} pairs
      u32  count;
      u32  _14, _18;
      Desc descriptor;          // +0x1c {ptr, paddedSize, usedSize, vertexType}
      ...                       // then the palette
  };
  ```

  - GX matrix memory **persists through the whole model**, across lists and
    across mesh nodes. A list reloads only the slots it changes.
    Firecracker_Grunt's second list loads 5 slots and uses others the first
    set. The sniper rifle's fourth mesh node draws with slots its three
    predecessors loaded.
  - So the slot → bone map is carried along the mesh-node chain, and
    within each node along its record chain.
  - Carried per mesh in address order instead, 22,741 type-0 vertices on
    the disc point at slots nothing loaded.
- **CPU (bone records).** A bone record holds a position group and a
  normal group:

  ```c
  struct Bone {                 // 0x60 bytes
      Bone* next; char* name; u32 index;
      u32 nPos;  f32 (*pos)[3]; u32* posIdx; f32* weight;   // +0x0c..+0x18
      u32 _1c, _20, _24;
      u32 nNrm;  f32 (*nrm)[3]; u32* nrmIdx;                // +0x28..+0x30
      ...                                                    // then its name
  };
  ```

  A vertex is `Σ weight · M_bone · pos` over the groups that name it. On
  every character in `Alevel9_1`, the stored points equal the model's
  positions at those indices exactly, and each covered vertex's weights sum
  to 1.

  The game's routine is **`CcMesh_SkinCPU`** (`0x8012f9b0`), called once a
  frame by `FUN_801299a0` with the animator's runtime buffers
  (`animator + 0x14`: position and normal `{ptr, size}`). Per frame it:

  1. zeroes the destination (`DCZeroRange`, a `dcbz` loop);
  2. for each bone, transforms the group's points (`PSMTXMultVec` / `…Array`),
     scales each by its weight (`PSVECScale`), and adds it into the
     destination at its vertex index (`PSVECAdd`);
  3. transforms each normal by the rotation only, **stores** it rather than
     weighting it, and normalises it (`PSVECNormalize`);
  4. flushes both buffers from the cache for the GPU (`DCFlushRange`).

  Because step 3 overwrites, the last bone to name a normal wins. That is
  why only 60–75% of the stored normals equal the model's.

Characters use one kind or both:
- Firecracker_Grunt and the mechs are GPU only;
- Atlantean, Werebull, demonA and KleerKnight are CPU only;
- BikerSquid and KamikazeMarines carry both.

**How the reading was checked.**

- **Edge stretch against the wrong alternative.** Each character is posed
  mid-clip and every edge compared with the bind pose. The code's reading
  (columns) keeps the 95th-percentile stretch at 0.01–0.56. Reading the six
  values as rows instead tears the skin at the joints: 0.56–7.3. The mechs
  tie at 0.05–0.08, because they are rigid robots and every rotation keeps
  a rigid part's edges.
- **Disc-wide** (`python tools/objmodel.py verify`, 0 problems):

  | check | result |
  |---|---|
  | key pool found (count = max index + 1, keys validated) | 580 of 580 animated models |
  | keys with orthogonal columns / unit columns | 2,145,719 / 2,120,306 of 2,145,746 (the rest are scaled bones, as on the mechs) |
  | CPU skin points equal to the bind position | 284,506 of 284,506 |
  | skinned vertices whose weights sum to 1 | 215,291 of 215,291 |
  | type-0 lists on a record chain / vertices whose slot resolves | 914 of 914 / 410,107 of 410,107 |
  | stretch, 122 distinct characters | columns tighter on 44, a tie on 78 rigid ones, rows tighter on 0 |

  Two traps on the way, both recorded in the code:
  - A static one-bone rig (the Sirian Dark Lord's `Pelvis`) has a
    one-entry pool with translation scales of exactly 0. A finder that took
    the first `{ptr, count}` match got it wrong.
  - Palettes carried per mesh in address order left 22,741 vertices on
    empty slots.
- **By rendering** (`python tools/objmodel.py pose`):
  - the grunt raises its arm in `attack1` and lies flat in both deaths;
  - DevilStallion spreads its wings;
  - KamikazeMarines runs with its bombs;
  - the three dragon forms of `Clevel8_4` bend their necks in `death1`.

### Still open for the models

1. **Unbound vertices.** 50 animated model instances on the disc draw
   vertices that no skin group names and no type-0 list carries: KleerKnight
   807 of 1,409, merman 49 of 513, TweedleDumDum 19 of 591. They stay in
   the bind pose, and posed renders tear.
   - On KleerKnight they are whole parts: the torso armour, most spikes,
     and all four hooves. Yet a `R_Rear_Hoof` bone exists and lists 18
     weighted vertices.
   - There are no duplicate positions and no other index arrays that cover
     them.
   - The game's own skinner does not explain them. `CcMesh_SkinCPU` zeroes
     its destination and writes only vertices that its groups name, so by
     that path these vertices would sit at the model origin, which is
     surely not what the game shows.
   - So they are drawn from somewhere this path does not write, or bound
     through data not yet found. The draw call that binds the skinned
     buffer is the place to look next.
2. **Locators.** Records like `Locator_Lhand` → bone 19 + a matrix are
   attachment points (weapons, effects). They are unread beyond that.
3. **Which SE1 form.** SE1 `.mdl` models are vertex-animated, and SKA is
   skeletal. The keys bake straight into per-frame vertex positions, which
   is the easy route to `.mdl`. SKA would need a skeleton this format does
   not store. That decision belongs with the Stage 2 writer.

## `CcTexture` — typed

Recovered from the GX setup path at `0x80131cb4`, which feeds the fields
straight into `GXInitTexObj` / `GXInitTexObjLOD`.

```c
struct CcTexture {
    CcTexture* next;      // +0x00  linked list
    u32        format;    // +0x04  engine format id (see below)
    u32        width;     // +0x08  power of two; loader warns
    u32        height;    // +0x0c    "This texture is %dx%d" and forces 1024
    s32        lodCount;  // +0x10  >0 -> trilinear + max LOD; 0 -> linear
    u32        _pad14;    // +0x14
    void*      pixels;    // +0x18  texel data
    u32        handle;    // +0x1c  runtime only - the loader zeroes it, so the
                          //        value on disc is stale exporter garbage
    u32        _pad20;    // +0x20
    SubRect*   subs;      // +0x24  sub-rects; loader scales fields [4] and [5]
                          //        by 1/width and 1/height, i.e. they are UVs
};
```

Engine format id → GX format, from the switch at `0x80131cf4`:

| id | GX | |
|---|---|---|
| 1 | `GX_TF_RGB5A3` (5) | |
| 2 | `GX_TF_RGBA8` (6) | |
| 3 | `GX_TF_CMPR` (14) | DXT1-like, dominant |
| 4 | `GX_TF_IA4` (2) | |
| *other* | `GX_TF_RGB565` (4) | the default arm |

Two details that bite: GC `CMPR` packs its 2-bit indices **MSB-first** (DXT1 is
LSB-first), and an 8×8 CMPR block is four 4×4 DXT1 sub-blocks in row-major
order. `RGBA8` stores each 4×4 tile as a 32-byte AR half followed by a 32-byte
GB half.

## Extraction status

`tools/gxtex.py` implements the above (`list`, `extract`). It writes PNGs with
no external dependencies.

Across all 63 containers: **8,118 texture nodes** — 6,134 CMPR, 823 RGB565,
797 RGB5A3, 324 IA4, 40 RGBA8. That is the game's whole texture set.

Verified rather than assumed:

- `Data.ssg` 82/82 and `Rlevel1_1.ssw` 184/184 and `Alevel12_1.ssw` 150/150
  written without error.
- Visually spot-checked across formats and sources: a 512×512 RGB5A3 HUD/decal
  atlas, a 256×256 CMPR weapon sheet, a 512×512 CMPR character skin atlas, and a
  512×512 CMPR carved-stone wall from a different level. All correct — colours,
  alpha and detiling.
- A 49-texture random sample across 16 containers decoded with 0 errors.

An earlier sweep reported only 157 nodes; that was the glob layout applied to
`.ssw` files and is superseded by the numbers above.

## Next

1. ~~Texcoords into the OBJ~~ — done; field 3 -> array 4 (TEX0), verified by
   range fit and by texture-scale consistency.
2. Map `SubRect` (`CcTexture.subs`); it is the atlas/sprite table and the UI
   depends on it.
3. The `+0x48` sector tail holds small 6-byte records
   (`[u8 kind][u8 1][u16 n][u16 0]`, kinds 4/5/6) -- an unread per-sector
   table. (It is no longer a candidate for "the per-level attribute set":
   there isn't one, the vertex field order is uniform.)
4. Vertex types 0, 2, 4 and 5 (strides 8, 8, 8 and 7) remain unparsed. They are
   a small share of lists and types 2/4 do not index positions, so they are
   probably not world geometry.
5. ~~The `.clm` mesh format~~ — done. `CcMesh` at `image+0x5c`, GX vertex
   type 2, damage stages; see "The mesh data — typed" above and
   `tools/model.py`.
6. Camera tails - spline data, the last big unread blob inside entity props.
   (Mover keyframes are done: `docs/world-conversion.md`, "Mover motion".)
7. ~~Object models~~ — geometry done: embedded nested images, vertex types
   0 and 5, `tools/objmodel.py`. Skinning, hierarchy and animation are open;
   see "Object models" above.
