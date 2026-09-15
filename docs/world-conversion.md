# Converting a level to a Serious Engine 1 world — sectors and lights

**Status.** Stage 1 is implemented and verified on all 49 levels
(`tools/world.py`). Stage 2 — the writer that produces a `.wld` — is specified
below against the `pc/engine` headers, with every call checked against a
file and line. It is **not written yet**. The engine now builds on this
machine (`pc/README.md`), so nothing blocks it.

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

## Axes — NE is the original, mirrored

Climax's converter mirrored everything on the way to the GameCube:

    M_NE = S_z · R_orig · S_x      S_z = diag(1, 1, −1), S_x = diag(−1, 1, 1)

- **The world is mirrored in Z.** The first level exports came out mirrored
  front to back against the game. A Z-up viewer such as Blender shows an
  OBJ's Z as its Y, so there it looked like an inverted Y.
- **Every model's local space is mirrored in X.** The level items match
  their SE1 modeller sources in `dev/` vertex for vertex once X is negated:
  - dynamite 475 of 476, and the shield 34 of 34, against 0 of each without
    the flip;
  - the Neptune statue 729 of 731, and the tollgate statue 518 of 519.
  Symmetric items match either way. The characters show the same: merman's
  asymmetric box.
- **The editor's own angles survive** in the camera markers. Every placement
  matrix equals `MakeRotationMatrix(180 − h, p, −b)` of them on 443 of 443,
  which is the relation above (see "Cameras").
- **A brush's local space is mirrored in Z instead**, the way the world is:
  the brush nodes at `image+0x48` (movers, destroyables, the extra
  `WorldBase`s) hold `M_NE = S_z · R_orig · S_z`. A mover's key matrices are
  entity placements, like the camera markers, and its first key is the
  brush's own placement. Converted that way, key 0's rotation equals the
  brush's on 953 of 958 movers; the other 5 are pads and doors whose first
  key sits 5–90° from where they rest. Converted like an entity placement
  instead, every brush came out turned half a turn about its own vertical
  axis from its first marker, and most brushes faced heading 180. Now 407
  of 958 movers, 82 of 116 destroyables and 71 of 135 extra `WorldBase`s
  face heading 0, the editor's default.

`tools/space.py` takes everything to the original space, and every exporter
goes through it:

| NE | original |
|---|---|
| world point or direction | `(x, y, −z)` |
| model-local point | `(−x, y, z)` |
| placement rotation `R` | `S_z · R · S_x` |
| brush node rotation `R` | `S_z · R · S_z` |
| a face | vertex order reversed, since each export crosses exactly one mirror |

It is applied in these places:
- the level OBJs (`tools/level.py mesh`);
- the prop and object-model OBJs (`tools/model.py`, `tools/objmodel.py`);
- the world descriptions (`tools/world.py describe`), converted as a last
  step keyed by field name;
- the top-down renders.

The raw decodes in `build/entities` stay in NE space and say so.

**Checks after conversion:**

| check | result |
|---|---|
| camera rotation equals SE1's `MakeRotationMatrix` of the stored editor angles | 171 of 171 |
| marker rotation equals it too, including the 54 that borrow another placement | 326 of 326 |
| asymmetric level items that match their `dev/` source as exported | 18 of 18; 13 more are symmetric, 3 are other versions |

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
| the zoning `WorldBase`, its sectors rooms joined by portals, content Air, plus a `WorldBase` entity per separate object | every `CcMaterial` group at `image+0x08`; the largest connected piece and the liquid surfaces are the world base, the other pieces the objects, and the rooms are generated (see "Rooms") | 49 levels | geometry verified by render |
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
| haze (`bsc_ulFlags` bits 8–11) | a slot 1–4 per distinct fog (TSE's `WorldBase` has `Haze 0`–`Haze 4`; no level uses more than 2); slot 0 stays empty for "no haze" |

Each distinct fog becomes a `HazeMarker` — Attenuation `FA_LINEAR`,
Near = start, Far = end, Base Color = rgb × 255 — bound as
`WorldBase.m_penHaze<slot>`. SE1's `FogMarker` is height-layered fog; NE's is
plain distance fog, which is SE1's haze.

**As written** (`tools/wldprep.py`, `region_lines`). The cells are one more
`WorldBase`, **zoning**: SE1 relates an entity only to the sectors of zoning
brushes (`CEntity::FindSectorsAroundEntity`), and a moving entity takes its
content from those, so a non-zoning brush would carry content nobody feels.
Each cell's polygons are invisible and passable (and full bright, so the
shadow bake skips them). `WldWriter --check` sums each cell's signed volume
over its triangles: **2,284 of 2,285 cells face inward**; the other is a
0.5-unit sliver on `Alevel10_3` that the region tree's planes leave
degenerate. All 643 cells with haze find their `HazeMarker`.

The cells' faces had come out of Stage 1 wound for NE's mirrored space:
`space.to_original` reversed faces given as index lists but not the region's
`{plane, verts, cap}` records. It now mirrors both, and the planes too.

**The haze does not show yet.** SE1 hazes the polygons of a sector that has
haze (`RenCache.cpp`), and a cell's own polygons are invisible; the visible
floors and walls under water belong to the rooms, which have none. Water and
lava content and the underwater environment work; drawing the fog needs the
rooms cut at the liquid surfaces.

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

## Polygons — rebuilt from the collision mesh's order

NE's levels were Serious Editor worlds, and Climax triangulated every polygon
on the way to the GameCube. A world written from the triangles opens in the
editor with every wall and floor cut into triangles, so the writer rebuilds
the polygons first (`tools/polygons.py`).

**The render mesh has lost the borders.** Its display lists were re-stripped
across polygons: on `Rlevel1_2`, 407 of 489 strips, 296 of 540 fans, 171 of
211 quad lists and 139 of 220 triangle lists are not even flat. No vertex
field marks a border either. Normals, all three UV sets and array 7 each run
continuously across most borders, and field 2 is constant per material.

**The collision mesh kept them.** Its triangles are listed polygon by polygon.
`Rlevel1_2`'s first object is a slab: its 6-corner top as 4 triangles, its
bottom the same, then each side as a pair. The triangle's `value` word stays
constant inside a polygon and changes between neighbours. Render and collision
use one vertex array (the same pointer on every level), so a render triangle
finds its collision triangle by its three vertex indices: 837,099 of 841,032
static triangles and 41,114 of 43,281 brush triangles do.

**The rule.** A polygon is a run of consecutive collision triangles that have
the same flags and value, lie within 0.02 units of the plane of the run's
largest triangle, and face the same way, split into the parts shared edges
hold together. On the render side it keeps to one material and one texture
mapping: two groups of triangles join only if every corner of the smaller
agrees with the UV map of the larger group's biggest triangle, **up to whole
tiles**, and, when the smaller group holds the bigger triangle, every corner of
the larger agrees with that one's map too. The disc restarts UVs at seams inside flat areas, which the repeating
texture hides, and a group that closes around (a ring floor) meets itself
across one. The first version compared only the two triangles on each edge;
the mapping check ("Textures") then found polygons whose corners fit no single
map, by up to 15.8 tiles. Triangles with no collision triangle (3,377
polygons' worth) join by the render conditions and the plane test alone. Comparing each triangle against
the *largest* triangle's plane matters: slivers carry unstable normals, and
testing against the run's first triangle split 207 originals in the replay
below.

**Measured on an original.** `dev/` has `waterfallArea.wld`, a Serious Editor
world of `Rlevel1_2`'s waterfall area from before the conversion
(docs/dev-material.md). It is an early version, matching no disc geometry
under any rotation, mirror or scale, but it is the designers' own polygons.
1,741 of its 1,934 polygons are triangles, 167 quads and 26 larger, up to 11
corners; 44 of the 193 with more than three corners are concave.
`WldWriter --dump` lists them with the triangulation stored in the file, and
`polygons.py replay` runs the rule over that list:

| of 1,918 original polygons with triangles | |
|---|---|
| rebuilt exactly | 1,550 |
| inside a rebuilt polygon made of whole originals | 367, in 122 unions |
| split | 1, into 2 pieces |
| mixed with part of another | 0 |

The unions are what the data cannot separate. In the original's own order, 179
consecutive pairs of polygons are coplanar and share an edge with the same
texture, mapping and flags, often flat areas built from separate triangles.
Joined, they draw the same. Two things that looked like markers are not: the
order in which SE1's triangulator emits a polygon's triangles (NE's pairs
follow a different pattern from the file's), and a reverse-fan link between
consecutive triangles (6,643 coplanar edge-sharing pairs on three levels have
none).

**Holes.** A Serious Engine polygon is a set of edges, not one loop, and its
triangulator allows for holes. 3,689 rebuilt polygons outline a hole, 4,679
holes in all, and keep them: `.wldsrc` has a `hole` line after the `poly`, and
the writer adds those edges with `CObjectSector::CreateEdgeInPolygon`. OBJ has
no holes, so `level.py mesh` writes those polygons as their triangles and
`wldprep.py` reads polygons from `polygons.py`, not from the OBJ.

**Polygons SE1 will not triangulate.** Long thin concave bands, strips of a
ring floor with 25 corners and many nearly collinear ones, make
`CTriangularizer` give up (`BPOF_INVALIDTRIANGLES`): 2 on `Rlevel0_1` in the
first run. The disc's triangles cover them, so they are most likely several
originals joined. Every concave or holed polygon (21,575) therefore also
carries `split` lines, convex pieces covering it. The writer builds the
polygon alone in a scratch entity's brush and triangulates it, and uses the
pieces only if SE1 rejects the whole: 239 polygons on the 49 levels, which
take 487 of the holes with them. After that, 16 of 424,643 brush polygons are
rejected (`WldWriter --tri`), where the triangle worlds had 15.

Brush import runs SE1's own object optimiser, which joins collinear edges and
removes edges a polygon uses twice. 355 rebuilt polygons are zero-area slivers
it collapses to nothing; the surface area does not change (see "Stage 2 as it
stands").

`python tools/polygons.py verify`, all 49 levels, 0 problems:

| check | result |
|---|---|
| triangles | 883,441, of which 872 degenerate are dropped |
| polygons | 426,092: 129,101 triangles, 258,266 quads, 15,894 with 5 corners, 9,173 with 6, 13,658 with more |
| from collision runs / without | 422,713 / 3,379 |
| joins refused because a texture layer's UVs disagree | 3,743 (see "Texture layers 1 and 2") |
| concave (single loop) | 17,886 |
| groups split into pieces | 857, where two triangles overlap using the same edge the same way |
| outline covers exactly its triangles | all: the loops' Newell vectors equal the triangles' summed area vectors |
| flatness | all within 0.02 of their best-fit plane; the worst is 15 mm |

## Textures — `.tex` files and one mapping per polygon

Every polygon gets its material's diffuse texture, converted to a Serious
Engine `.tex`, and a texture mapping that reproduces the disc's UVs.

**The files** (`tools/wldtex.py`). Levels carry their own copies of shared
textures, so identical images (same size and decoded pixels) become one file,
named after the first level and texture index that use it:
`Textures\NextEncounter\<level>_<index>.tex` under the game root. Each is
written as a TGA (32-bit only when some pixel is not opaque, rows top-down)
and turned into a `.tex` by the engine itself, `WldWriter --tex` calling
`CreateTexture_t` with 16 mip levels as the editor's texture dialog does.
On the 49 levels: 2,077 level textures in use, diffuse and in texture
layers, are 913 distinct images, 74 of them with transparent pixels;
`CreateTexture_t` made 913 of 913.

**Size in mexels.** A `.tex` also has a size in the world: 1024 mex is one
metre, and the mex width must be the pixel width times a power of two
(`CTextureData::Create_t`). The power chosen is the one nearest the world size
of one UV tile on the triangles that use the texture (their median), so the
mappings come out near stretch 1, as a designer would have set them.

**The mapping.** SE1 draws a point's texture coordinate as
`((s·UoS + t·UoT) − UOffset) · 1024` mex, where `s` and `t` are the point's
position along the default mapping axes of the polygon's plane, measured from
the plane's reference point (`CMappingVectors::FromPlane`). The texture's mex
size turns that into tiles.

**The offset is subtracted.** The renderer builds the layer's mapping vectors
with `CMappingDefinition::MakeMappingVectors` (`RenCache.cpp`), which moves the
origin by the offset. The texture coordinate is then `(p − O′)·U′`
(`DrawPort_RenderScene.cpp`), which works out to `s·UoS + t·UoT − UOffset`.
`FromMappingVectors` agrees. `CMappingDefinition::GetTextureCoordinates`, used
by editor tools, adds the offset instead. The first worlds followed it, and the
editor's mapping dialog showed every offset with the wrong sign (stretch 1.641,
offset 3.036 / 2.473 where −3.036 / −2.473 is right).

1. `wldprep.py` fits each polygon's affine UV map, `U = a·p + a0` and
   `V = b·p + b0` in mex with `a` and `b` in the plane, by least squares over
   its corners. It does this in the polygon's final coordinates: world space
   for the static brush, local space for a brush entity. The `.wldsrc`
   carries it as a `uv` line.
2. The writer solves the mapping against the plane `CreatePolygon` actually
   computed. A point on the plane is `O + s·U + t·V`, so `UoS = a·U / 1024`,
   `UoT = a·V / 1024` and `UOffset = −(a·O + a0) / 1024`, and the same for V.
   The offset is kept within one tile, since the texture repeats.

Every polygon joined its triangles only where their UVs and UV maps agree
("Polygons"), so one mapping per polygon loses nothing.

**Measured** (`python tools/wldtex.py --check <level>`, all 49 levels):
| check | result |
|---|---|
| polygons with a texture map | 423,837, 0 without one |
| the fit, against the disc's corner UVs | worst corner 0.0027 of a tile; 2,193 slivers under 0.01 square units not measured, since their plane is too thin to fit |
| the saved worlds: SE1's texture coordinate at every vertex of a brush polygon, against its source map, modulo whole tiles | 425,833 of 425,833 brush polygons within 1% of a tile; 1,773 slivers not measured |

The check reads SE1's own default mapping vectors from `WldWriter --dump`
rather than recomputing them: `FromPlane` picks its axes by whether
`|normal.y| > 0.5` in single precision, and every 30° slope sits on that
boundary. A first check that rederived them from a printed plane, and read the
mapping printed with six digits, blamed 477 correct polygons. It also found a
polygon's source maps by rounding vertices to three decimals, which splits a
corner lying on an exact half (4.0625 ± 1e-6) between two keys. That left 5
polygons "unexplained" and, once separate objects moved to local coordinates
full of such halves, 22. The check now finds source maps by distance (1e-3).

### Texture layers 1 and 2

SE1 polygons have three texture layers. NE keeps the designers' settings for
layers 1 and 2 in each material's label and draws them from records of its own
(docs/image-format.md, "Texture layers"). Every layer NE draws goes into the
world: its texture, its own mapping, and SE1's blend and flags.

- **Textures.** `tools/wldtex.py` converts every texture a layer uses, sized
  in mex by the texel density of the layer's own UV set. With the layers,
  2,077 level textures in use are 913 distinct images, 74 with alpha, all
  made by `CreateTexture_t`.
- **Which layer.** Detail maps (Shade) go on the third texture (layer 2) and
  blends and glows on the second, as SE1 levels have them; NE's labels do not
  keep that order, so the layers are placed by kind (docs/image-format.md,
  "Texture layers"). 353 materials have two detail maps, and their second is
  on layer 1.
- **Material.** A `.wldsrc` `mat` line names the layer textures after the
  diffuse one (`-` for none), which become `CObjectMaterial::omt_strName2`
  and `omt_strName3`.
- **Mapping.** Each layer's UVs are fitted per polygon like layer 0's and go
  in as a `layer` line; the writer turns them into `opo_amdMappings[1]` and
  `[2]` the same way (`MapToPlane`).
- **Settings.** Brush import copies a polygon's texture properties (scroll,
  blend, flags, colour per layer) from `CObjectPolygon::opo_ubUserData`, but
  only for a polygon marked `BPOF_WASBRUSHPOLYGON`; for any other it writes
  defaults. The writer marks polygons that have layers and fills the whole
  block as import would for a new polygon, then the layers.
- **Polygons follow the layers too.** Two coplanar polygons can share the
  diffuse mapping and place a grass edge or a detail map differently, so the
  rebuild (`tools/polygons.py`) now joins triangles only if every UV set their
  material draws agrees, the layers' within 2% of a tile: detail maps tile up
  to 4 times a unit, and a small triangle's map carried across a large polygon
  drifts by 0.002 to 0.01 of a tile, while the seams between the designers'
  polygons are 0.1 to 0.5. The layers refuse 3,743 joins, and 426,092
  polygons come back where the diffuse texture alone gave 422,854.

**Converted with judgement:**

- **Clamping is NE's, not the label's.** A layer (or a material's diffuse
  texture) clamps U or V where NE's record does (docs/image-format.md,
  "Texture layers"): grass fringes and patches drawn once instead of tiled.
  A clamped axis keeps its UVs where the disc has them: the polygon rebuild
  does not move them by whole tiles and joins triangles there only if they
  agree exactly, the writer does not wrap the mapping offset into one tile,
  and the check does not forgive whole tiles. The label's clamp V on setting
  3074 (373 layers) is left out: NE repeats those layers, and clamped they
  draw their edge row as stripes.
- **Opaque textures on Blend layers get an alpha.** 285 of 365 of those
  `3074` layers have no alpha: rock and earth stretched over large areas.
  NE draws them with a TEV operation not decoded (9). SE1 would cover the
  base texture with them. The designers' `waterfallArea.wld` blends its
  opaque "rockblend" texture the same way through the layer colour's alpha
  (0x82 to 0xBC over 109 polygons). A Blend layer whose texture has no alpha
  gets 0x45, half their median: 0x8A looked too opaque in the editor. This
  is an estimate.

**Not carried over:**

- **Caustics.** 158 Add layers carry two bits above SE1's flags (`0x20`,
  `0x40`) and NE animates their texture coordinates at run time. SE1 draws
  them static; which of its texture transformations (`WorldBase` scroll
  table) would match is not worked out.
- **Detail maps are not equalized.** SE1's Shade doubles the product, so a
  detail map should average grey 128 (`TEX_EQUALIZED`); NE's average about
  103 to 115, so they darken by 10 to 20% and SE1 does not fade them out
  with distance.

`python tools/wldtex.py --check <level>` measures every layer:

| all 49 levels | layer 0 | layer 1 | layer 2 |
|---|---|---|---|
| polygons whose map fits the disc's UVs (worst corner, tiles) | 423,837 (0.0027) | 93,261 (0.0092) | 317,539 (0.0095) |
| brush polygons where SE1's coordinates match the source map within 1% of a tile | 425,833 of 425,833 | 93,329 of 93,329 | 318,505 of 318,505 |

A top-down render of a converted world from `WldWriter --dump`, compositing
the layers as SE1 blends them, shows on `Rlevel1_1` grass edges growing over
the paving, one clamped fringe along each path edge, detail grain on marble
and plaster, and the stretched rock variation over the dirt. Repeating the
fringe drew grass stripes across the paving, clamping the setting-3074 layers
drew stripes of their edge rows, and without the alpha there was rock where
the dirt was.

## Separate objects and sectors

The designers built each level as a world base plus separate brushes (columns,
statues, trim) and cut it into sectors. The first worlds put all static
geometry into one `WorldBase` with one sector, and the editor showed it so.

**Separate objects survive on the disc; sectors do not.**

- *Objects.* The static collision mesh falls into pieces that share no vertex:
  12,090 on the 49 levels, and 11,058 of them are one unbroken run of the
  collision order, which is the designers' polygon order ("Polygons"). On
  `Rlevel0_1` the six column bases (44 triangles, closed boxes) follow one
  another, then their capitals, shafts and tops. Each sits on a floor that is
  not cut around it, so none was CSG-added to the world base.
- *Sectors.* The designers' `waterfallArea.wld` (`docs/dev-material.md`) lists
  its 1,934 polygons sector by sector, and each of its 11 sectors starts with
  its portal polygons (`BPOF_PORTAL`, 303 of them). NE kept no portal: no
  collision triangle has a portal-like flag, no material draws one, `main.dol`
  has no sector or portal string, and the region tree's 1–4 distinct leaves
  per level are content types, not sectors. The order alone does not
  delimit them either:
  - On the fragment with its portals removed, a sector boundary always comes at
    a polygon sharing no corner with the run before it, but so do 40 other
    places. Joining those pieces back, by shared corners, merges neighbouring
    sectors.
  - On the disc the same rule cuts `Rlevel0_1` into 926 runs, most of them one
    or two triangles.

**The rule for objects** (`tools/wldprep.py`). The static geometry's pieces
are the groups of polygons that share a corner. The largest piece is the world
base. Every other piece becomes a `WorldBase` entity, not zoning, placed at
the centre of its box with its geometry in local space, and written after the
brush entities so entity links keep their indices. An object's brush is cut
into sectors of at most 256 whole polygons, halving at the median polygon
centre along the longest side of its box.

| | all 49 levels |
|---|---|
| separate objects | 11,456 `WorldBase` entities, from 2 on `Alevel12_4` to 679 on `Alevel12_3`; 6,648 with 12 polygons or fewer, the largest 1,083 polygons; 61 in more than one sector |
| objects left empty by the import | 2, both single sliver triangles; the writer removes them |

Each object's saved brush box equals its vertices within 0.005 units. Cutting
brushes into sectors moves slivers: the import's sector-wide edge passes
(`SplitCollinearEdges` and the removals after it, `CObjectSector::Optimize`)
treat one differently once its neighbours are in another sector, and one
sliver on `Clevel5_3` (0.000003 square units) is kept on some runs and not
others.

**Not decided by the data.** Whether an object was a `WorldBase` of its own or
a sector of the world base's brush joined in from another layer: the disc
cannot tell. Objects that touch the world base at a single vertex stay part
of it. On `Rlevel0_1`, two of the capitals do.

## Rooms — the zoning world base

The world base is zoning and anchored, as the editor makes the first
`WorldBase` of a new world (`EFirstWorldBase` in `WorldBase.es`), and its
sectors are rooms: closed volumes joined by portal polygons. SE1 needs both.
`CBrushArchive::LinkPortalsAndSectors` links a portal (`BPOF_PORTAL` or
`BPOF_PASSABLE`) of a zoning brush to every sector whose BSP it touches, and
the renderer walks those links from the viewer's sector.

**Why the rooms are generated.** The sectors are gone ("Separate objects and
sectors"), and the geometry around them cannot be closed back up: the static
mesh is open. `Rlevel0_1` has 1,972 edges used by one triangle only, 1,814
after splitting edges at T-junctions, and 274 of its 303 boundary chains never
close. They run along wall tops and floor borders, not around the flat loops
deleted portals would leave. Climax removed every invisible polygon (portals,
sky, never-seen wall tops), and `Rlevel0_1` has no collision-only triangles
that could stand in for any of them. So SE1's own CSG, which splits closed
sectors and makes portals where they meet (`CSGSplitSectors`), has nothing to
work on.

**Rooms are volumes made of the polygons** (`tools/rooms.py`). No polygon is
cut, and no room is sliced by a plane extended past its own polygon: a roof's
plane does not divide the courtyard beside it.

1. *Cells, as scaffolding.* A BSP of the level's box along the planes of the
   world base's polygons (the most coplanar area first, less for each polygon a
   plane passes through), until every polygon lies on the faces of the cells in
   front of it. The cells are never written.
2. *One polygon, one room.* The cells in front of a polygon are one room, so
   every polygon lies wholly inside its room and is written whole.
3. *Rooms grow across large openings.* Groups of cells join where the part of
   their shared faces that no polygon covers is at least 15% of the smaller
   group's whole boundary. A courtyard's top is open, so the cells above it
   join it and the roof planes beside it do not cut it. A house is joined to
   its yard only by a door, small beside its walls, floor and ceiling, so it
   stays a room. Openness is judged between whole groups, never one cell face:
   the cell inside a doorway is all opening.
4. *Pockets.* A room of 12 polygons or fewer joins the neighbour it is most
   open to, or failing that the one it shares most face with.
5. *Void.* A cell holding nothing (no polygon, and no separate object, brush
   entity, prop, effect, light or other placed entity) belongs to no room:
   wall interiors, the ground's underside, space outside the level.

**Closing a room.** Its boundary is the outside of its cells. Toward another
room, the part of that boundary the room's own polygons on it (facing in) do
not cover is a portal facing into the room: SE1 renders by span occlusion, so
a portal over one of the room's walls would show the next room through it.
Toward void and on the level's box, that part is an invisible polygon
(`BPOF_INVISIBLE`). Portal and closing polygons under 0.1 square units are left
out: they are slivers of gaps in NE's mesh, and a sliver portal is what fails
to link or to triangulate. Every sector's vertices closer than 0.001 are
welded. NE's neighbouring polygons disagree by about 1e-6 and portals are cut
along them, and left apart the triangulator failed on 205 polygons of
`Rlevel1_1`.

The writer also triangulates the world base in a scratch brush first, with
every polygon in its real sector (each polygon's colour carries its index
through the import), and puts the ones that fail there in as their convex
pieces. A polygon that triangulates alone can fail beside its neighbours.

**Tried and dropped.**

- A BSP whose leaves were the rooms, split along walls that covered most of
  their outline: rooms followed walls, but each split plane ran on past its
  wall, so a roof's plane sliced the courtyard next to the house, and polygons
  were cut wherever a plane crossed them.
- Merging cells across faces that were mostly open, judged face by face: the
  cell inside a doorway is all opening, so every house merged with its yard.
- Cells that stopped splitting while polygons still passed through them: a
  cell straddling a wall holds space on both sides of it, and keeping a
  polygon whole then fused the rooms on either side.

| | all 49 levels |
|---|---|
| rooms | 470. 11 levels are one room: open terrain and arenas (`Rlevel1_4a`, `Rlevel0_1`, `Alevel10_2`, ...), where nothing walls one area off from another. Most on `Rlevel4_3` (54), `Rlevel2_2` (39), `Rlevel3_4` (34), `Rlevel1_1` (29) |
| polygons cut | none |
| portal polygons | 6,885 in the worlds; 6,783 link to another sector after loading |
| rooms reached through a portal | 458 of the 459 on levels with more than one room |
| invisible closing polygons | 32,787: the level box above and around the rooms, and the gaps in NE's mesh toward wall interiors and the ground's underside |
| polygons the editor flags as badly triangulated | 49 of 467,322: 5 portals, 18 closing polygons, 26 of the level's |
| downward rays | 452, 402, 246, 308 and 173 hits, unchanged |
| texture mapping | every checked brush polygon matches the disc's UVs, in each layer: 425,846, 93,341 and 318,518 |
| entity links | 2,974 movers' links, all set after loading |

`python tools/rooms.py render <level>` draws a level's rooms in an oblique view,
every polygon in its room's colour and portal edges in white; `--below <height>`
leaves out what lies above, to look inside, and `--closing` draws the closing
polygons instead. On `Rlevel1_1` the central courtyard is one room with the
sloping roofs around it, and the corner houses, the colonnade and the small
buildings are rooms of their own. On `Rlevel2_3` the roofless nave and the
entrance courtyard are two rooms either side of the central building, whose
inner chambers, corridors and towers are rooms too.

**Not settled.** Rooms follow the polygons, not the designers' intent: what
the designers split for visibility across open ground stays one room. The 118
unlinked portals and 4 unreached rooms are not looked into; the editor turns
unlinked portals into walls when it next runs CSG (`RemoveDummyPortals`).

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
| 4170 TouchField | 444 | `TouchField` | the collision mesh's trigger triangles | properties and **volume** ready (428 of 444) |
| 4220 Bouncer | 64 | `Bouncer` | the collision mesh's bouncer triangles | properties and **volume** ready (64 of 64) |

Every `image+0x48` brush is tied to its owner the same way — the node's
`+0x08` points at the owning entity's own matrix — and the tie is total:
**958 of 958 Movers and 116 of 116 DestroyableArch entities have a brush, and
no brush points at any other class.** An earlier version of `world.py` only
matched Movers and filed the 108 visible DestroyableArch brushes as unlinked.

Every node points at its collision mesh record at **`+0x28`**, 1,209 of
1,209. The record's vertices are in the brush's local space. Put through the
node's transform, the collision box matches the render box within 10% of the
brush's size on all 1,059 brushes that have collision triangles, and within
1 unit on 1,048 (`python tools/collision.py verify`).

**The other 135 visible brushes have no collision triangles** (their records
hold vertices only), so nothing ever hits them. The RAM dump shows such a
brush becoming a runtime collision object with 0 triangles. They are:

- 134 of the 135 extra `WorldBase` brushes. Every one has an alpha texture
  (111 RGBA8, 24 RGB5A3 across the set): cutout decoration, mostly on
  `Rlevel2_2`, `Rlevel2_3`, `Rlevel3_1` and `Rlevel4_3`. The one
  `WorldBase` brush that collides (`Clevel7_2` sector 20) is opaque (CMPR),
  with 92 collision triangles for 92 render triangles.
- `Rlevel2_4b`'s "WaterFlow" mover.

`world.py` marks them `"passable": true`, and the writer sets SE1's
`BPOF_PASSABLE` ("not a physical barrier") on their polygons.

15 of the 1,209 brushes (7 Mover, 8 DestroyableArch) are **empty**. They have
no render triangles, and their collision record has no vertices and bounds
still at the ±1e6 sentinel. Their owners work as logic ("PillStealer",
"Move Key", "Neck Explosion", crates), so they are recorded with
`invisible: true` and no geometry. An earlier note called them
collision-only; their records show otherwise.

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
  the other 13. Its rotation is the brush's on 953 (see "Axes").
- All 14 damage targets resolve to Triggers.
- On `Rlevel1_1`, rendered at key 1, every door sits in its own opening and the
  gates rise by their own height.
- Disc-wide, a two-key sliding door travels a median **0.86× its own extent**
  along its direction of travel, with 65% between 0.8× and 1.2×. Doors open by
  about their own size, as doors do.
- 52 movers never stop (cogs, pistons).

### TouchField and Bouncer: both volumes are collision triangles

**Found: both volumes are groups of triangles in the level collision mesh**
(`tools/collision.py`). A TouchField's are non-solid trigger triangles; a
Bouncer's are the solid triangles of its pad. A RAM dump of `Rlevel1_2`
(`tools/ramdump.py`) led there, and the static check holds on every level.

- **The mesh.** The level root's slot `+0x50` points to the static collision
  mesh record: `+0x08` vertices (`Vec3`), `+0x0c` triangles, `+0x10`/`+0x14`
  index lists, `+0x40` the vertex count, `+0x48` the triangle count. A
  triangle is 10 bytes, `{u16 flags; u16 v[3]; u16 value}`. Flags are `0x1f`
  (solid) on 835,828 of 848,856 triangles.
- **A trigger triangle** has flag bit **0x40**.
  - The low 7 bits of its `value` are the TouchField's volume index,
    `props + 0x10`.
  - The high byte carries the field's props words 5 (bit 4) and 6 (bit 8).
    Those are SE1 GC's two collision properties, *Block Walking Enemy* and
    *Block Flying Enemy*: they ride on collision triangles, and they come in
    that order (likely).
- **A bouncer triangle** is solid (flags `0x1f`), and its `value` is
  `0x200 | index`, the index being the Bouncer's `props + 0x10`. All 646 are
  in root meshes.
- **The game's side.**
  - `Player_TouchFields` (`0x800ab4ac`) calls the scene's
    `Scene_SweepQuery` (`0x80140668`) with category mask 0x49.
  - `Collision_TestPair` (`0x8015d5d4`) tests a pair by
    `(flags_a & flags_b & 0xfc)`, which reaches the 0x40 triangles.
  - The pair callback, `Scene_CollisionHitCallback` (`0x801401e8`), records
    the collision object's `+0xb4`, which is `(type << 24) | index`. It also
    records the triangle's flags, and its value when `flags & 2`.
  - When a query copies its hits out, the type starts as the object's type
    byte. Flag `0x40` then makes it **3**, index `value & 0x7f`. Value bit
    `0x200` makes it **1**, index `value & 0x3f` (for example at
    `0x80140e28`).
  - `Collision_HandleHits` (`0x80090b74`) hands type 3 to
    `TouchField_GetByIndex` (`0x80084618`). It hands type 1 to
    `Bouncer_GetPush` (`0x80077af4`), which returns the speed and direction
    the Bouncer's init stored.
- **In the export,** `world.py` puts each field's and each bouncer's
  triangles, in world space, under `"volume"` (`vertices`, `faces`,
  `closed`). They go through the original-space pass like every other vertex
  list, and become the `TouchField` or `Bouncer` brush.

`python tools/collision.py verify`, 0 problems:

| check | result |
|---|---|
| touch fields with a trigger group | 428 of 444 |
| trigger groups that name no TouchField | 0 |
| closed volumes, every edge shared by two triangles | 426 of 428; 321 are boxes of 12 triangles |
| value high byte equals props words 5 and 6 | 428 of 428 |
| the field's position inside its volume's box | 399 of 428; an SE1 brush entity's origin need not lie inside its brush |
| bouncers with a volume | 64 of 64; 646 triangles, all solid |
| bouncer volumes closed | 29 of 64; the rest are open pads, a quad or a box without its bottom |
| the bouncer's position inside its volume's box | 64 of 64 |

**16 fields have no volume** in the mesh: `Alevel10_3` #0–7, `Alevel11_3` #9,
`Clevel7_1` #13 and #17, `Clevel8_3` #0, `Rlevel1_2` #7, `Rlevel1_3` #3,
`Rlevel1_4a` #0 and `Rlevel4_3` #61. They are not in the moving brushes'
meshes either, so these 16 have no volume on the disc.

**The mover meshes.**
- **Where they are.** A brush's collision mesh is a record laid out
  like the root's, whose `+0x08` is the same vertex array.
- **The shared array.** The root uses the first `+0x40` of its vertices.
  Each mover's triangles index its own range further on, in the mover's local
  space; `Rlevel1_2`'s first is a 3.75 × 10 × 1.25 slab.
- **The array's size is not stored.** `+0x44` is some other count:
  - on `Rlevel1_2` it is 11,470, but the highest index any mesh uses is 7,878;
  - on `Clevel8_4` and `Rlevel3_3a` it is below the root's own count, and
    their movers index past it (5,812 against 4,770; 7,652 against 7,212).

  So the reader sizes the array from the highest index the triangles use.
- **What they hold.** Every `image+0x48` brush owns one such record.
  - 1,209 are referenced, which on all 49 levels is exactly the level's
    brush count: Movers, DestroyableArch and the extra `WorldBase` brushes.
  - 1,059 of them have triangles, 41,199 in all.
  - All are solid-type (flags 0x1f, a few 0x0b). None is a trigger or
    bouncer triangle.
  - At run time each becomes a type-2 collision object indexed by its
    brush. The `Rlevel1_2` dump has 12 of them, index 0–11.
- **An earlier scan misled.** It checked indices against the root's own
  count, so it rejected every mover mesh. Instead it passed pointer lists
  that happened to parse, and the three "volumes" it produced were junk.

**Bouncer volumes were missed at first** because the search looked only at
non-solid groups. The hit-copy code (above) showed the value bit `0x200`.

**How the search got here** (kept for the record):


**Properties map cleanly.**

- TouchField `props + 0x0c` is its **Enter Target**: all 156 fields that set
  one point at a real entity id. `+0x00`/`+0x04` look like *Active* and
  *Players only* — (1,1) on 423 of 444 — but that is unconfirmed.
- Bouncer props read `(speed, heading, pitch)` — `(2000, 0, 90)` is straight
  up, the same convention as SE1's `Direction` default `ANGLE3D(0,90,0)` — so
  they become *Speed* and *Direction*. Likely, not proven.

**The volume was the gap.** Ruled out, in turn:

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

(Superseded: both volumes are collision mesh triangles, above.)

**The designers' field lists.** The GameCube build of SE1's `Entities.dll`
in `dev/` names every field these classes had in Serious Editor
(`docs/dev-material.md`, `python tools/se1dll.py show CTouchField`).
- **TouchField**: Name, Enter Target, Enter Event, Exit Target, Exit Event,
  Active, Players only, Exit check time, *(hidden)*, Block Walking Enemy,
  Block Flying Enemy. NE's 36 bytes are nine words, with the Enter Target
  at `+0x0c`.
- **Bouncer**: Speed, Direction (h, p, b), Control time, Max exit speed,
  and the normal and parallel component multipliers. NE's
  `(65, 90, 10, 0, 1)` reads as speed, then h, p, b, then the volume index.
Neither list has a size field, which fits the volume being brush geometry
that Climax's converter moved into collision.

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

  | type | comes from | meaning | index |
  |---|---|---|---|
  | 1 | triangle value bit `0x200` | bouncer | `value & 0x3f` |
  | 2 | the collision object | brush instance | its brush |
  | 3 | triangle flag `0x40` | touch field | `value & 0x7f` |
  | 4 | the collision object | an object with its own transform | object index |

  (Corrected after the hit-copy code was read: types 1 and 3 come from
  triangles, 2 and 4 from the object's own `+0xb4`.)
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

- **Render objects, not collision objects** (corrected by a RAM dump; see
  below). Four small virtual methods sit side by side in the render-object
  vtable at `0x8021e878`, the class whose strings are `RO::ActualRender` and
  "Object Being Completely hidden by mask". Each stores a small kind at
  `+0x24`. They were first read as the collision hit types; the dump shows
  they are render kinds:

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
- **The collision leaves index those runtime objects.** A leaf holds two
  lists. Both pointers are valid on all 1,114 collision leaves on the disc.
  - **Entries**, at `+0x08` (count at `+0x0c`): `(u16 object, u16 flags)`
    with one flag bit each, e.g. `(0x0b, 0x8000)`, `(0x15, 2)`. The object
    index stays inside the second runtime array (129 on `Rlevel1_1`).
  - **Records**, at `+0x10` (count at `+0x14`). Each carries a bounding
    sphere and points back to a leaf at `+4`. It points to this leaf on 1,030
    of 2,140, so records are shared between leaves.
  - **Not the touch volumes.** The record count matches the touch-field
    count on `Rlevel1_1` (6) by coincidence and on no other level, and none of
    `Rlevel1_1`'s six records sits at a touch field.
- **Ruled out:**
  - The runtime counts match no level list, or sum of up to four lists, on
    all 49 levels (materials, sectors, meshes, placed models, touch fields,
    bouncers, movers, destructibles, props, damagers, lights).
  - The first count equals the prop-mesh count on only 2 levels.
  - The `cmpwi …, 1000` sites are GX render-state setup, not the type-1000
    collision factory.
  - No level string names a trigger volume; every "Touch Field" string is
    an entity name.
- **The first RAM dump** (`tools/ramdump.py`) is of `Rlevel0_1`. Every one
  of its image's 29,244 pointers equals the RAM base 0x80a5a8e0 plus its
  on-disc value. That level has no touch fields or bouncers, and both index
  tables are empty. What it showed:
  - **The render objects are render kinds, not collision.** The scene object
    (`DAT_802ab3a4`, vtable 0x8021ebf0) lists 57 render objects: 45 of kind 9,
    7 of kind 2 and 5 of kind 3.
    - Each object's `+0x04` points to a placement `Mtx`, and its `+0x1c` to
      a shape object.
    - Kind 3's shape holds a GX display list of points (`b8 00 40`: 64
      vertices of position and colour): a particle effect, on a level with
      no touch fields.
    - Kind 2's shape holds an axis-aligned box and a radius.
    So "a touch field's volume is a render object" was wrong.
  - **The collision tree's leaf records are convex hulls:**

    ```
    struct CcHull {
        void* p0; void* p1;        // p1 -> a list of F entries (faces)
        Vec3* verts;               // +0x08, always this + 0x48
        u8  (*edges)[2];           // +0x0c, right after the vertices
        u16 nverts, nedges;        // +0x10
        f32 centre[3], radius;     // +0x14: radius = farthest vertex, exactly
        ...                        // vertices and edges follow at +0x48
    };
    ```

    Checked on all 1,194 leaf records: every edge index is in range, every
    bounding sphere is exact, and 1,193 have Euler's F ≥ 4.
  - **Trigger volumes are not hulls.** Scanning every image by that layout
    finds 1,626 hulls, and all 1,626 are referenced by the collision or
    header trees. None of the 444 touch fields or 64 bouncers lies inside an
    unreferenced hull, because there are none.
- **The second dump**, of `Rlevel1_2`, had the TouchField table filled
  (12 of 12) and led to `Player_TouchFields` and the collision mesh; see the
  top of this section.

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

**Decoded from their inits** (`tools/entity.py`). Field names come from the
GameCube `Entities.dll` (`docs/dev-material.md`).
- **LevelPar** (`CcParWatcher_Init`): ParTime, ParKills, Bronze, Silver, Gold.
- **WorldLink**: its init logs "WorldName %s" at `+0x04` and "EndGameFlag %d",
  a u16 at `+0x08`.
- **FMVPlayer**: the movie name.
- **WarpPlayers**: Active, then a u16 target at `+0x04`. All 139 set targets
  are PlayerStarts.
- **LockDown**: Active, Scale.
- **Arrow**: Active, Inherit Pos, points-at, Do Not Scale, Type (1 yellow,
  2 green).
- **ParticlesHolder**: s16 Active and type, Count, StretchAll, Stretch XYZ,
  Size, Param1–3. NE numbers the types its own way; the designers' names give
  0 Lock Twinkle, 1 Waterfall Spray1, 2 Teleport, 4 Smoke, 5 Steam,
  6 Lightning.
- **ModelDestruction**: debris type (SE1's `ModelDebrisType` unchanged, by the
  names: urns, statues, barrels, trees…), count, size, health, and an s16
  trigger. All 6 set triggers resolve.
- **Vehicle**: NE's own type (Jeeps 2, submarines 3, the Combine 0), a u16
  Active, and two links. 11 of 11 resolve.
- **4230** is `RollingStone`, by `EntityFactory_Create`. It is the one
  boulder in `Rlevel4_3`: `(14, 90, 0, 0, 0.2, 1000, 400, 1.4, 0.2, …)`,
  which fits SE1's Start Speed, Start Direction, Bounce, Health, Damage,
  Stretch and Deceleration. That fit is likely; the order is unconfirmed.

**Still raw:**
- **Class 5020**: the force fields; see "Force fields".
- **Two flags** that are not links: EnemyTemplate `+0x0c`, and pickup
  `+0x30`/`+0x34`.

### Scripting as written

`tools/world.py` (`script_description`) describes each entity with the SE1
class that does the same; `tools/wldprep.py` (`script_lines`) writes it.
Links keep NE's entity ids until every entity in the world has its index,
then resolve against all of them: brush entities, cameras, music, sounds,
messages, touch fields, bouncers and these.

| NE | n | SE1 | as written |
|---|---|---|---|
| Trigger 4080 | 3,632 | `Trigger` | Name, Active, Count, Count use, Max trigs, Wait; each target as Target 01–10 with its event (below) |
| Wave 4040 | 6,665 | `EnemySpawner` | Count total = count, Delay initial = delay, Delay single = interval; Patrol target; Template Target |
| EnemyTemplate 5000 | see `docs/enemies.md` | a TSE stand-in, or NE's own class once ported | Template, Death target (its event chosen as for triggers), the stand-in's variant |
| Marker 4150 | 2,079 | `EnemyMarker` | Target = the next marker of the path |
| TeleportTarget 4060 | 76 | `Marker` | |
| Watcher 4030 | 1,148 | `WatchPlayers` | Owner/Target, Watch distance, Wait time, Active; Far Event type cleared, since NE's watcher sends one event |
| Copier 4010 | 874 | `Copier` | Target |
| Damager 4110 | 134 | `Damager` | Ammount, Entity to Damage |
| Teleport 4020 | 65 | `Teleport` | Target, Width, Height, Active |
| DoorController 4050 | 26 | `DoorController` | Target1/2, Width, Height, Type (all 26 are 1, `DT_TRIGGERED`), Active |
| PlayerStart 4140 | 456 | `PlayerMarker` | placement |
| WorldLink 4160 | 31 of 42 | `WorldLink` | World = `Levels\NextEncounter\<level>.wld`, Type (all 2, `WLT_RELATIVE`) |
| Health, Armour, Ammo, AmmoPack, Weapon, Key, Powerup | 4,211 of 4,861 | `HealthItem`, `ArmorItem`, `AmmoItem`, `AmmoPack`, `WeaponItem`, `KeyItem`, `PowerUpItem` | Type from `tools/sediff.py`'s item table (1,529 health, 1,009 armour, 1,445 ammo, 68 packs, 81 weapons, 26 keys, 53 power-ups); Target, fired on pickup |

Orientation is the entity placement, `S_z · R · S_x`, as for cameras
("Axes").

**A trigger's event is chosen by what its target listens for.** NE sends
trigger, on or off (see "Trigger" above); an SE1 class handles only some of
`ETrigger`, `EStart`, `EStop`, `EActivate` and `EDeactivate` (its `on (E…)`
handlers in `EntitiesMP`). So trigger becomes `EET_TRIGGER` where the target
handles it, else `EET_START`, else `EET_ACTIVATE`; on becomes `EET_ACTIVATE`,
else `EET_START`, else `EET_TRIGGER`; off becomes `EET_DEACTIVATE`, else
`EET_STOP`. Across the disc: trigger → trigger 10,817, → start 87 (sound
holders), → activate 16 (watchers); on → activate 288; off → deactivate 792,
→ stop 3; 7 slots reach a class that handles none of them and send nothing.

**Not written, and why:**

| NE | n | |
|---|---|---|
| EnemyTemplate 5000 | 594 of 1,866 | creatures still to port (`docs/enemies.md`); 1,128 are written as TSE stand-ins and 144 as NE's own DumDumLarge |
| Treasure 3215 | 454 | SE1 has no scoring pickups |
| pickups SE1 has no type for | 285 | ricochet and homing bullets, liquid nitrogen, laughing gas, limpet grenades, spider mines, heat-seeker and sonic rockets |
| game-mode item slots | 362 | the deathmatch weapon and ammo slots, which resolve to a type only in a game mode; and 3 pickups with no item type |
| LockDown, Arrow, WarpPlayers, LevelPar, FMVPlayer | 116, 156, 148, 42, 14 | GameCube-only classes |
| Switch 4180 | 47 | SE1's `Switch` is a model holder; NE's switch model is not converted |
| ParticleHolder 4310, LightFlare 5010 | 210, 28 | NE's particle types and flares are not mapped yet |
| Prop 4200, ModelDestruction 4210, RollingStone 4230 | 1,443, 101, 1 | props are not written yet |
| Vehicle 4190, Flag 4320 | 8, 6 | SE1 has neither |
| WorldLink | 11 | 10 end the game (`endofgame`), 1 names no level |

**Links.** Of 25,277 links the written entities carry, 22,047 point at an
entity that is written. 2,135 of the rest are spawners' templates of
creatures still to port; the other 1,095 point at the classes above (237
arrows, 175 particle holders, 152 lockdowns, 147 warps, 119 ammo templates of
types SE1 lacks, 101 speech-only sound holders, 80 props, …) and 5 at ids no
entity has. Every link written survives loading: `WldWriter --check` counts
25,440 entity pointers set, the number written, across all 49 worlds. Camera
markers' Trigger and touch fields' Enter Target, left unset before, resolve
the same way.

## Cameras — SE1's `Camera` and `CameraMarker` chain, packed

NE's class 4120 is SE1's cutscene camera. The editor's separate
`CameraMarker` entities were folded into the camera's own props.
`tools/entity.py` decodes them and `tools/world.py` emits them under
`"cameras"`.

**The header** is `{f32 fov, f32 time, marker* first, 0, 0, f32 h, p, b,
0 ×4}`. A camera with no markers is 48 bytes (88 of 171). It holds its own
view for `time` seconds; the update logs "Static cam WAIT finished". FOV 90
and time 5 are SE1's `Camera` defaults.

**Each marker** is read by the camera update (`0x80078a38`) and its loader
(`0x8007992c`):

| offset | field | SE1 `CameraMarker` | evidence |
|---|---|---|---|
| `+0x00` | f32 FOV (90, 120, 100, 110) | FOV | loader slot 6 |
| `+0x04` | skip to next | Skip to next | logs "Maker says SKIP_TO_NEXT" |
| `+0x08` | stop moving | Stop moving | logs "Maker says STOP_MOVING" |
| `+0x0c` | trigger entity id, -1 for none | Trigger | logs "Send TRIGGER to %d"; 53 of 53 resolve |
| `+0x10` | f32 delta time | Delta time | loader stores 1/value |
| `+0x14` | `Mtx*` placement | the marker's position | loader reads its translation only |
| `+0x18` | next marker | Target | the chain closes on its first marker, 83 of 83 |
| `+0x1c` | f32, 0 or 0.2–0.7 | unconfirmed; SE1 GC's marker has a *Motion blur* | loader slot 8 |
| `+0x20` | f32 tension | Tension | the update's `(1 − t)` factor |
| `+0x24`, `+0x28` | f32, 0 on the whole disc | Bias and Continuity | the update's `(1 ± b)(1 ± c)` factors |
| `+0x2c` | f32, -1 on the whole disc | unconfirmed; SE1 GC's marker has a *GameTime* | loader slot 9 |
| `+0x30` | f32 h, p, b | the placement's angles | loader: radians, h and p negated |
| `+0x48` | `Mtx` | own placement | only on 0x78-byte records |

The spline is Kochanek–Bartels over a window of four markers. The update
shifts the window on each "Reached marker". Of the 83 paths, 69 stop at
their last marker; the other 14 go round.

A 0x48-byte record (54 of 326) has no `Mtx` of its own. Its `+0x14` borrows
a placement from elsewhere: another marker's `Mtx` in the same camera
(29), or a Camera entity's own transform (25).

**Angles and matrices.** The game takes a marker's position from the
placement's translation and its orientation from the stored angles. It never
reads the `Mtx` rotation. The stored angles are the SE1 editor's. Checked
exactly on all 171 camera transforms and all 272 marker matrices, every
`Mtx` rotation equals SE1's `MakeRotationMatrix` at **(180 − h, p, −b)**.
That is:

    M_NE = S_z · R_SE1(h, p, b) · S_x      S_z = diag(1, 1, −1), S_x = diag(−1, 1, 1)

Plain angles fit only 35 / 171 and 59 / 272, where the heading happens to
make both readings agree. Mirroring only one side fits none.

A marker's rotation is built in NE space as `ne_rotation(h, p, b)`. That
reproduces the marker's own `Mtx` 272 of 272 times and covers the
borrowed-placement records too. The exporter then takes it to the original
space, where it is SE1's plain `MakeRotationMatrix(h, p, b)` (see "Axes").

**Settled: NE's world is the original, mirrored in Z.** The exported level
meshes were compared by eye with the game and came out mirrored front to
back. Together with the models' X mirror, that is the `S_z … S_x` above.
Every exporter now converts; see "Axes".

The one SE1-space piece of a shipped level in `dev/`, `waterfallArea.wld`,
played no part. It doesn't register against `Rlevel1_2` under any mirror or
yaw: no sector matches more than 12 of its vertices.

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
layout is in `docs/image-format.md`. 13 levels list every light twice, the
second copy marked 2 at `+0x3c`; the counts here are without those 5,439.

| type | count | SE1 type | runtime behaviour |
|---|---|---|---|
| 1 | 6,610 | ambient | **never instantiated** by the level light setup (`0x801386cc`) |
| 2 | 8,119 | point | instantiated as runtime lights, **at most 270 per level**; colour × 255.0 (`0x8021e65c`) and clamped; the object also receives a constant 5000.0 (`0x8021e660`) |
| 3 | 42 | directional | separate directional path (`0x8013f0ec`); colour × 256.0 and clamped; uses `dir` |

The runtime lights light models. The world is lit by a **baked lightmap**: every
world material binds a 1024x1024 atlas at `+0x20`, and vertex array 7 holds
each vertex's coordinates in it (`docs/image-format.md`, "The lightmap
atlas"). Its values are SE1 shadow map values, and it is what the conversion
is measured against (below).

What shapes the mapping:

- **`as`/`ae` are authored, not consumed.** The runtime light object never
  receives them, and the per-material light binder the setup calls,
  `0x8013f6d8`, is an **empty stub** in the shipping build. They go to SE1's
  hot-spot and fall-off, and the lightmap agrees with SE1's re-bake from them
  (below).
- **NE's colour is SE1's colour byte / 127.5.** 7,437 lights have a component
  above 1.0, up to 2.30, and the lightmap says why: 1.0 is SE1's neutral 127.
  On `Rlevel1_1` the ground the sun cannot reach holds (36,36,32), the sun
  fill × 127.5 = (39,38,31); sunlit ground holds that plus the sun × 127.5 ×
  N·L, (205,203,142) baked against (212,209,141) predicted at N·L 0.8. The
  runtime's × 255 is for models. At × 127.5, 426 lights clamp.
- **Type 1 is an ambient light, type 2 a point light.** The designers placed
  SE1's own `Light` class (`dev/`, `Entities.dll`), whose types are point,
  ambient, strong ambient, directional and strong point; NE numbers the three
  it uses its own way. SE1's ambient light casts no shadows and has no angle
  term (`Light.es`, `SetupLightSource`: no `LSF_CASTSHADOWS`, no diffusion).
  Baked as point lights, NE's type-1 lights left the broad glows of the
  Atlantis levels as small dark pools; baked as ambient lights, `AlevelDM_2`
  matches NE's lightmap on 95% of pixels instead of 20%, `Alevel12_2` on 95%
  instead of 26%, and no level loses more than 0.2 points. Type 2 casts shadows: drawn
  shadowless, `Rlevel1_1`'s flag-1 lights, nearly all type 2, lit through
  walls NE keeps dark (below).
- **Every level's sun is a pair.** All 42 non-directional lights with
  infinite reach (`as = ae = 999999`) are type 1, ambient, and **all 42 sit at
  exactly the position of a directional light**, across 38 levels. That is
  one authored object: a directional light plus its ambient fill. As an
  ambient light of its own it would light every surface, indoors too, where
  NE's lightmap is black; it goes in as the directional light's ambient.
- **Only kind 0 is baked.** `+0x3c` is 0 on 14,742 lights. 2 marks the
  5,439 second copies and three lights of their own; re-baked without those
  three, `ClevelDM_1` matches NE's lightmap on 84% of pixels instead of 0.1%
  (its second sun) and `RlevelDM_3` on 79% instead of 35% (an orange light
  with 300 units' reach). 3 marks one light on each of 25 levels; without it
  `Rlevel0_1` and `Rlevel3_2` match a point or two better, `Clevel8_2` the
  same. 1 marks one sun on `Clevel5_3`.
- **Two more suns light nothing.** White, with no fill and pointing exactly
  level, on `Alevel12_3` and `Alevel12_4` (`Clevel5_3`'s kind-1 sun is the
  third): on `Alevel12_3` a wall facing one would bake 127, and bakes 0.
- **No sector ambient.** Wherever no light reaches, the lightmap is black, on
  every level with or without a sun.

What NE's `flags` (`+0x38`: 0 on 11,703 lights, 1 on 2,794, 2 on 245) mean is
not known. 2,767 of the flag-1 lights are type 2. Flag 1 is not a light
without shadows: drawn as SE1's shadowless ambient lights, `Rlevel1_1`'s 179
flag-1 lights lit through walls NE keeps dark, and the pixels within 16
levels of NE's fell from 76% to 70%. Nor is it SE1's strong point light
(hot-spot at 90% of fall-off): drawn that way, `Rlevel3_4` fell from 93% to
12% and `Rlevel1_1` from 85% to 78%.

### Mapping onto `EntitiesMP/Light.es`

| SE1 property | from NE | rule | why |
|---|---|---|---|
| Type | `type` | 1 → `LT_AMBIENT`, 2 → `LT_POINT`, 3 → `LT_DIRECTIONAL` | the lightmap: type 1 has neither shadows nor an angle term, above |
| Color | `col` | `round(abs(c) × 127.5)`, clamped to 0–255 | the lightmap's scale, above |
| Dark light | sign of `col` | TRUE when negative | all 28 are negative in every component; SE1 refuses dark *directional* lights and none are |
| Hot-spot | `as` | copied | SE1 copies it straight to `ls_rHotSpot` |
| Fall-off | `ae` | copied | SE1 copies it straight to `ls_rFallOff` |
| Directional ambient | the co-located sun fill | fill colour, on the directional light | `SetupLightSource`: *"only directional lights are allowed to have ambient component"* |
| orientation | `dir` | a rotation whose front (SE1's −z) is `dir` (`tools/wldprep.py`, `facing`) | SE1 takes a directional light's direction from its angles (`AnglesToDirectionVector`) and lights a polygon by −N·dir (`LayerMixer.cpp`): `dir` is the direction light travels in both engines |
| Dynamic | — | FALSE | NE lights never move; SE1 bakes shadow maps for static lights |
| — | `ae ≤ 0` | not created | 4 lights, all in `Rlevel2_2`: authored with no reach |
| — | `+0x3c` not 0 | not created | 5,439 second copies and 29 more lights, above |
| — | a directional light's fill, when that light is not created | not created | 1, on `Clevel5_3` |
| — | level, white, no fill | not created | 2 directional lights, above |

**14,771 NE lights become 14,695 SE1 `Light` entities**, 8,089 point, 6,568
ambient and 38 directional: 40 sun fills fold into their directional lights, and 29 lights not in the lightmap, the fill of
one of them, the 4 no-reach lights and the 2 suns that light nothing are
dropped.

### The world around them

- **Sector ambient** is 0 in every sector (`tools/wldprep.py`, `AMBIENT`).
- **Polygon flags.** Every polygon of the designers' `waterfallArea.wld` has
  `BPOF_HASDIRECTIONALLIGHT` and `BPOF_HASDIRECTIONALAMBIENT`. NE's polygons
  did not all: indoors, where no lamp reaches, the lightmap is black, where a
  polygon taking the sun's fill could never be darker than the fill. Each
  rebuilt polygon samples the lightmap over the triangles it came from
  (`tools/lightmap.py`, `Baked`): darker than half the fill anywhere, no
  directional flags; a flat 127 everywhere, `BPOF_FULLBRIGHT`; otherwise
  both flags. Doors and moving brushes have their own lightmap and are
  judged the same way. Of 426,033 polygons, 394,936 take the directional
  light, 30,075 do not, and 1,022 are full bright.
- **Shadow maps are baked by the writer** before it saves, as the editor's
  batch conversion does: `DiscardAllShadows`, `CalculateDirectionalShadows`,
  `CalculateNonDirectionalShadows`. The lights exist before the brushes they
  fall on, so every layer is found again first. SE1's default shadow texel,
  0.5 units, is kept. Baking all 49 levels takes under 5 minutes, since the
  ambient lights cast no shadows. It first crashed on `Rlevel4_3`: SE1's shadow renderer wrote
  past the end of a small shadow mask and corrupted the heap. The fix is a
  patch to the engine (`pc/README.md`, "Patches to the fork").

### Checked against NE's lightmap

`WldWriter --lightmap` dumps every polygon's shadow map as SE1 mixes it, and
`python tools/lightmap.py render <level>` draws it and NE's lightmap
top-down from the same view, the highest surface under a ceiling clip, and
compares them pixel by pixel where both show the same surface.

All 49 levels, 17.8 million compared pixels:

| | |
|---|---|
| pixels within 16 levels of NE's, every channel | 88.5% (82.2% with type 1 as point lights) |
| mean difference per channel | 8.1 levels (10.2) |
| levels at 75% or better | 46 of 49 (34); median level 88.3%, best `Clevel8_4` 99.6% |
| levels under 50% | `Alevel10_3` 33%, `Alevel11_3` 46% |

Each rule above was kept for what it did to this comparison. Type 1 as
ambient lights took `Alevel12_2` from 26% to 95%, `AlevelDM_2` from 20% to
95%, `RlevelDM_2` from 41% to 86%, `Alevel11_2` from 17% to 75% and
`Rlevel1_1` from 76% to 85%. The per-polygon flags took `Rlevel1_1` from 56%
to 76%. Dropping the second copies and the
unlit sun took `Alevel12_3` from 0.4% to 73%, and the second copies alone
`Alevel11_1` from 54% to 93%. The other lights not in the lightmap took
`ClevelDM_1` from 0.1% to 85% and `RlevelDM_3` from 35% to 79%. (Counting full
bright polygons as SE1 draws them, 127, corrected the comparison itself:
`Rlevel0_1` read 75% before, 89% after.)

Where they still differ:

- **Prop shadows.** NE's orchard trees on `Rlevel1_1` cast shadows; props are
  not in the world yet, and SE1 does not shadow brushes with models.
- **Small occluders.** SE1 casts sharp shadows of roof ridges and beams from
  lamps close above them, where NE's lightmap, at 0.3 to 1.2 units a texel
  and blurred, shows them faintly.
- **`Alevel10_3`'s sun.** NE lights the level's large open floors with its
  teal sun; SE1 leaves them dark, 57 levels darker in green on average, as
  if something above shadowed them. What does is not found.
- **`Alevel11_3`**, a small level (6,401 compared pixels), comes out
  brighter than NE's, by 36 levels in green on average.
- **A few areas NE bakes brighter**, such as `Rlevel1_1`'s pool and an
  alcove beside it. Why is not investigated.

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

Every name below was checked against `pc/engine/SamTSE/Sources`. The writer
itself is `pc/wldwriter` (`pc\build-windows.ps1 -Writer`); it **builds and
runs**, and its smoke test writes a one-sector world and reads it back with
the same counts (see "What the writer needs at run time" below).

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

### Stage 2 as it stands: geometry and brush entities, all 49 levels

The pipeline is two pieces, so the conversion logic stays in tested Python:

    python tools/wldtex.py                   # level textures -> SamTSE/Textures/NextEncounter/*.tga
    Bin\WldWriter.exe --tex textures.lst     # ... -> *.tex, before any world that uses them
    python tools/wldprep.py --all            # build/world/*.json + the levels' polygons -> build/wld/*.wldsrc
    pc\build-windows.ps1 -Writer             # build the writer
    Bin\WldWriter.exe <level>.wldsrc         # -> SamTSE/Levels/NextEncounter/<level>.wld
    Bin\WldWriter.exe --check <level>.wld    # load it back, print counts and box
    Bin\WldWriter.exe --rays  <level>.wld    # cast a grid of rays down through it
    Bin\WldWriter.exe --tri   <level>.wld    # triangulate every polygon as the editor does
    python tools/wldtex.py --check <level>   # SE1's texture coordinates against the disc's UVs
    Bin\WldWriter.exe --lightmap <level>.wld <txt>   # every polygon's shadow map as SE1 mixes it
    python tools/lightmap.py render <level>  # ... beside NE's baked lightmap

`.wldsrc` is a flat token stream (`world`, `polyflags`, `ambient`, `sector`,
`sectorflags`, `verts`, `mat`, `poly`, `hole`, `split`, `flags`, `uv`, `layer`,
`entity`, `fprop`/`bprop`/`iprop`/`cprop`/`sprop`/`aprop`/`eprop`, ...), so the
C++ parses no JSON and no OBJ. The
polygons come from `tools/polygons.py` (see "Polygons").

| check | result |
|---|---|
| levels converted | 49 of 49, the largest `Alevel12_3` with 19,243 polygons |
| polygons into the brushes | 426,092 rebuilt from 884,313 triangles (see "Polygons"), and 488,830 brush polygons with the rooms' portal and closing polygons, the region cells and the touch field and bouncer volumes. Brush import drops the slivers its optimiser collapses. Before the world base was cut into sectors it kept 424,643, and on `Alevel9_1` the saved polygon area differed from the source's by 0.016 of 3,832,164 square units |
| world base and separate objects | the world base in 470 rooms with 6,885 portals, no polygon cut ("Rooms"); 11,456 separate objects as `WorldBase` entities; 13,227 brush sectors in all |
| holes | 46,018 extra loops, added as extra edges of their polygon: the level's holes and the pieces of portal and closing polygons |
| textures | 913 `.tex` from 2,077 level textures, texture layers included; every mapped polygon's texture coordinates match the disc's in all three layers (see "Textures") |
| worlds that load back | 49 of 49, same polygon count and world name |
| brush bounding box equals the vertices sent | every `WorldBase` brush with polygons, world base and separate objects, within 0.005 units |
| brush entities placed | 1,209: 958 `MovingBrush`, 116 `DestroyableArchitecture`, 135 `WorldBase`, plus 2,016 `MovingBrushMarker`s |
| brush geometry in the right place | 1,194 of 1,194 with geometry: the entity brush box equals its OBJ group box in world space, worst error 0.005 units. The other 15 are the empty brushes. |
| properties set by editor name | 9,880, with **no unknown property name** on any SE1 class |
| entity links | 25,440 written, and 25,440 entity pointers set after loading: movers' and camera markers' paths, haze markers, and the scripting's 22,047 (see "Scripting as written") |
| region cells | 2,285 as the sectors of a zoning `WorldBase` per level, 2,284 facing inward; 10 `HazeMarker`s, found by all 643 cells with haze (see "NE's region system") |
| point entities | 171 `Camera` and 326 `CameraMarker`, 43 `MusicHolder` and 12 `MusicChanger`, 108 `SoundHolder`, 63 `MessageHolder`, 428 `TouchField` (427 volumes facing inward) and 64 `Bouncer` (see "Point entities as written") |
| scripting and pickups | 19,397: 3,632 `Trigger`, 6,665 `EnemySpawner`, 2,079 `EnemyMarker`, 1,148 `WatchPlayers`, 874 `Copier`, 456 `PlayerMarker` and the rest, 4,211 items; and 1,272 enemy templates (`docs/enemies.md`). 51,341 entities and 200,321 properties in the worlds, no unknown property name (see "Scripting as written") |
| lights | 14,695 `Light` entities: 8,089 point, 6,568 ambient, 38 directional; shadow maps baked from them in every world, under 5 minutes for all 49; 88.5% of pixels within 16 levels of NE's own lightmap (see "Lights") |
| polygons that triangulate | 488,780 of 488,830; 50 get `BPOF_INVALIDTRIANGLES`, the flag the editor reports as bad triangulation (`WldWriter --tri`), 19 of them portal, closing or other invisible polygons. 696 polygons SE1 would not triangulate whole went in as their convex pieces. **These counts move slightly between runs:** the writer does not give the same polygon count twice from the same source (`Alevel11_2`: 9,891, then 9,892 twice), and a later run of all 49 wrote 488,817 polygons, 53 with invalid triangles. Why is not found |
| **open-sector BSP and collision** | rays cast down through the world hit the geometry: 452 of 576 on `Rlevel0_1`, 402 of 576 on `Rlevel1_1`, 246 of 576 on `Alevel12_3`. The misses are rays outside the level's footprint, since levels are not rectangular. |

So SE1 accepts NE's open meshes as brush sectors, builds their BSP, and
collides against them. That was the risk this step existed to test.

Geometry goes in as world space for the static `WorldBase` and as local space
for each brush entity (`tools/wldprep.py` inverts the brush matrix); the
writer hands the engine the rotation matrix and lets
`DecomposeRotationMatrixNoSnap` produce SE1 angles, so no angle convention is
guessed. Entity links are resolved in a second pass, once every entity exists.

**Not in the world yet**: the templates of creatures still to port, props,
and translucency; the scripting, pickups and 1,272 enemy templates are in,
less the classes SE1 has no form for ("Scripting as written"). All three texture layers are in (see "Textures"),
the lights, with shadow maps baked and checked against NE's own lightmap
(see "Lights"), the region cells, and the point entities Stage 1 gives an
SE1 form (below).

### Point entities as written

`tools/wldprep.py`, `point_lines`, from the description's own SE1 records:

| entity | from | as written |
|---|---|---|
| `Camera`, `CameraMarker` | "Cameras" | the camera's Name, FOV and Time and its first marker as Target; each marker's placement, Delta time, Tension, Bias, Continuity, FOV, Stop moving, Skip to next, and the next marker as Target |
| `MusicHolder` | a level's first Music entity | the three layers as Music Light, Medium and Heavy; Score Medium 100, Score Heavy 1000 |
| `MusicChanger` × 3 | each later Music entity | one per layer, Type light, medium and heavy: SE1 changes one channel a changer, NE swaps the whole track |
| `SoundHolder` | the 108 with a sample | Sound, Fall-off, Hot-spot, Volume, Looping, Auto start, Destroyable |
| `MessageHolder` | all 63 | Name, Active, and the English message file |
| `TouchField` | the 428 with a volume | Name and Active; its brush is the field's collision triangles, local to its position |
| `Bouncer` | all 64 | Speed [m/s] as NE stores it (10–160 on 63 of them, 2,000 on one), Direction from the direction vector (`aprop`: the engine's own `DirectionVectorToAnglesNoSnap`); its brush is the pad's triangles, invisible since the level draws the pad |

The files they name are copied under the game root: `Sounds\NextEncounter`
(32 samples), `Music\NextEncounter` (21 layers, 283 MB at 44.1 kHz) and
`Data\Messages\NextEncounter` (63 messages). SE1 plays any 16-bit PCM WAV,
so the 32 kHz effects go in as they are; the music is resampled to SE1's 44.1 kHz
(`docs/sound.md`, "For SE1").

Left unset for now:
- **Block Walking Enemy and Block Flying Enemy.** They are the designers'
  GameCube `TouchField` fields (`docs/dev-material.md`); TSE's class has
  *Players only* and *Block non-players* instead, which mean something else.
- **Speech**, which NE plays with no position (see "Sounds and messages").

**Bad triangulation, found in the editor and fixed.** The first worlds opened
with every polygon invisible or broken and flagged as badly triangulated. The
writer had used the index form of `CObjectSector::CreatePolygon`, which with
`bReverse = FALSE` builds each edge from the *next* vertex to the current one
(`ObjectSector.cpp`), so a polygon's edges do not chain head to tail. Its plane
is still computed as if they did, and `CTriangularizer` finds no valid triangle
in any polygon. The vertex-position form chains its edges correctly; the writer
now uses it, and `FromObject3D_t` merges the duplicate vertices it creates.
Before the fix, `--tri` counted 5,974 invalid of 5,974 polygons on `Rlevel0_1`;
after it, 0.

**Polygons, not triangles, found in the editor and fixed.** With triangulation
fixed, every wall and floor still opened as triangles, since the writer took
the disc's triangles as polygons. It now takes the polygons `tools/polygons.py`
rebuilds from the collision mesh's order (see "Polygons"): 882,472 brush
polygons became 424,643. Brush boxes (1,258 of 1,258), entity links (2,974)
and the downward rays (452, 402, 246, 308 and 173 hits on `Rlevel0_1`,
`Rlevel1_1`, `Alevel12_3`, `Clevel8_2`, `Rlevel3_2`) are unchanged.

**Facing.** SE1 draws a polygon only when the viewer is in front of its plane
(`IsViewerPlaneVisible`), and a ray cast only hits a polygon from its front
(`TestBrushSector`). Rays cast straight down from above each level hit its
floors across the footprint (452 of 576 on `Rlevel0_1`), so floors face up,
toward the player: the OBJ's winding is SE1's front.

### What the writer needs at run time

Found by running it (`pc/wldwriter/WldWriter.cpp`):

- **A game ID.** `SE_InitEngine("")`, as the fork's own console tools call it,
  **deletes the network library** (`Engine.cpp`: `_pNetwork = NULL` on an empty
  ID). `CWorld::CreateEntity` ends with `_pNetwork->IsPredicting()`, so every
  entity creation then crashes. `SE_InitEngine("SeriousSam")` keeps it, and the
  engine also preloads `Classes\Player.ecl` at start-up.
- **A game root.** The engine takes the parent of the exe's directory, so the
  writer is built into `pc/engine/SamTSE/Bin`.
- **Entity classes, from retail data.** The fork ships no `.ecl` files. A
  `.ecl` is two lines (`Package:`, `Class:`), and the package name gets
  `_strModExt` inserted, which `SamTSE/ModEXT.txt` sets to `MP` — so retail
  TSE's `Classes\*.ecl`, which name `Bin\Entities.dll`, resolve to our own
  `Bin\EntitiesMP.dll`. Archive lookups are case-insensitive (`stricmp`), so
  the archive's lowercase `classes/` is found as `Classes\`. Linking the
  retail `SE1_00.gro` into `SamTSE/` therefore supplies all 151 classes and the
  94 `Models/Editor` files the marker entities want. That data is the user's
  own copy, read-only and never committed.
- **Class components are forgiving.** `ObtainComponents_t` only loads
  sub-*classes* unless the precache policy is raised, and it swallows load
  errors unless it is set to paranoia.

The description is already in the original space ("Axes"): y-up, z negated
from NE, rotations `S_z · R · S_x`, and it is unscaled. Whether gameplay
needs a unit scale (player height against NE's door sizes) is open.

## Verification

`python tools/world.py verify` across all 49 levels:

| check | result |
|---|---|
| liquid surfaces, render only | 40 — 28 water, 12 lava |
| region cells | 2,285 on 22 levels — 2,062 water, all environment Underwater; 223 lava |
| haze markers | 10; 643 cells use one |
| brush entities | 1,209 — 958 `MovingBrush`, 116 `DestroyableArchitecture`, 135 `WorldBase`; 1,059 with collision triangles; 135 passable (134 `WorldBase`); 15 empty |
| mover motion | 958 of 958 movers, 2,016 markers; 68 auto-start, 52 never stop, 0 zero-time keys; key 0's rotation equals its brush's on 953 |
| touch fields / bouncers | 444 / 64; all 156 set TouchField targets resolve to an entity; 428 fields have a volume, 426 of them closed; 64 of 64 bouncers have a volume, 29 closed |
| cameras | 171: 88 static, 83 with 326 markers. Angle-derived rotation equals each marker's own `Mtx` on 272 of 272; 53 of 53 marker triggers resolve |
| lights | 20,210 records — 48 directional, 52 dark, 690 clamped at × 127.5 |
| second copies dropped (`+0x3c` = 2) | 5,439, on 13 levels |
| other lights not in the lightmap dropped (`+0x3c` not 0) | 29, and the fill of one |
| sun fills folded into a directional light | 40, 0 left unpaired |
| no-reach lights / unlit suns dropped | 4 / 2 |
| SE1 `Light` entities to create | 14,695: 8,089 `LT_POINT`, 6,568 `LT_AMBIENT`, 38 `LT_DIRECTIONAL` |
| force fields | 23 recorded, 0 emitted |
| OBJ groups referenced but missing from the mesh | 0 |
| props | 7,171 — 6,694 `ModelHolder2`, 477 `ModelHolder3`; 1,105 with damage stages |
| prop model or damage-stage files missing | 0 |
| effects (instances with no mesh) | 2,865 — 1,502 `smallflame`, 1,358 `bigflame`, 5 editor placeholders, 0 unknown |
| bouncers with a code-derived direction | 64 of 64 |
| sound holders | 213. 108 become `SoundHolder` (27 looping, 58 corrected like the game); 93 of 96 speech hashes resolve; 8 samples and 3 speech hashes are missing in the game too |
| messages | 63 of 63 resolve title and text in all three languages |

`python tools/world.py render` draws each description over its level —
liquid sectors tinted by content, brush entities in purple, point and ambient
lights as fall-off rings in their own colour, dark lights in magenta, directional light
as an arrow. On `Rlevel1_1` the pool is its water sector, every door and gate
sits in its fence gap or doorway, and light rings cluster inside rooms. On
`Alevel10_2` the lava field and the octagonal pool are separate content
sectors and the pistons are movers.

## Open, in order

1. **Stage 2, the rest of the world.** Static geometry and brush entities
   (with motion markers and links) convert for all 49 levels and collide, as
   the designers' polygons with all three texture layers, the separate objects
   as `WorldBase` entities of their own, and the world base zoning, in
   rooms built from its whole polygons and joined by portals (above). The lights are in, baked and checked against
   NE's lightmap ("Lights"), 88.5% of pixels within 16 levels; on
   `Alevel10_3` the sun still does not reach the open floors. The region
   cells and the point entities with an SE1 form are in too, though the haze
   does not render until the rooms are cut at liquid surfaces. The scripting
   and pickups are in ("Scripting as written"). Still to write: enemy
   templates, props, and translucency for the textures with alpha. NE's material fields at
   `+0x24`–`+0x33` are the same on alpha and opaque materials, so what marks
   a see-through surface is still to find. The caustic layers' animation and
   NE's own blend for opaque Blend layers (TEV operation 9) are open too
   ("Texture layers 1 and 2").
2. **Force (gravity)** — absent from the region leaves; read the code that
   consumes class 5020 before emitting `GravityMarker`s.
3. **Region cells** — they are in (invisible, passable, zoning); still open
   are trimming them to the level's geometry and making their haze render.
4. **Units** — whether NE's scale needs converting for gameplay.
5. **Props, the rest** — OBJ → `.mdl` / `.smc` conversion needs the same
   toolchain as Stage 2. Still unread: prop collision, NE's ModelDestruction
   props (health, debris), and the two pointers in each damage-stage
   record. The 2,860 flames are identified (small or big, at a known
   position) but have no stock SE1 class: `ParticlesHolder`'s 19 types
   include none. Either give a `ModelHolder2` an animated flame model, or add
   a flame class with Ecc. NE's effects 2 and 4 are themselves unread.
6. **Sounds** — mover START/LOOP/END sounds, and likely every other entity
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
