# Recon findings — Serious Sam: Next Encounter (GameCube)

Everything here is derived from `orig/sys/main.dol` and the extracted disc.
Dates are absolute; this file is the running record of what we actually know
versus what we assumed.

## Disc

| | |
|---|---|
| Game ID | `G3BE9G` |
| Region | NTSC-U (USA), Rev 1 |
| Internal name | `Serious Sam` |
| Source image | `orig/Serious Sam - Next Encounter (USA) (Rev 1).rvz` (Zstd-19, 128 KB blocks) |
| Extracted | `orig/sys/` + `orig/files/`, 134 entries, 939 MB |

## main.dol memory map

2,554,080 bytes. Entry `0x80003100`. Produced by `ghidra/scripts/import_dol.py --describe`.

| Section | File off | Address | Size | Perms |
|---|---|---|---|---|
| `.init` | `0x000100` | `0x80003100` | `0x0003a0` | RX |
| `.text` | `0x0004a0` | `0x800034a0` | `0x1dd8a0` | RX |
| `.data0` | `0x1ddd40` | `0x801e0d40` | `0x000260` | RW |
| `.data1` | `0x1ddfa0` | `0x801e0fc0` | `0x0573c0` | RW |
| `.data2` | `0x235360` | `0x80238380` | `0x037320` | RW |
| `.data3` | `0x26c680` | `0x802d9ba0` | `0x001f00` | RW (`.sdata`) |
| `.data4` | `0x26e580` | `0x802dc7e0` | `0x001360` | RW (`.sdata2`) |
| `.bss` | — | `0x8026f6a0` | `0x06d124` | RW (uninit) |

`.bss` as declared in the DOL header **overlaps `.data3`**, which is normal: the
header's bss range spans `.bss` + `.sdata` + `.sbss`. The importer carves it into
`.bss` (`0x8026f6a0`, `0x6a500`) and `.bss1` (`0x802dbaa0`, `0xd24`) around the
real `.data3` block. Do not "fix" this by creating one flat bss block.

The importer also maps two synthetic blocks so xrefs resolve instead of dangling:
`OSGlobals` (`0x80000000`, `0x3100`) and `HW_REGS` (`0xCC000000`, `0x8000`, volatile).

## The engine is NOT Croteam's Serious Engine

This is the single most important finding, and it contradicts the obvious
assumption behind pairing this repo with `ref/serious-engine`.

Evidence from strings in `main.dol`:

- **Source paths are Climax's tree**, not Croteam's:
  `C:/SeriousSam/src/game/system/Engine/engine.cpp`, `.../Engine/xforms.cpp`.
  Croteam's SE1 tree is `Sources/Engine/...`.
- **Class prefix is `Cc`, not `C`**: `CcEngine`, `CcLevel`, `CcSystem`,
  `CcStash`, `CcBaseVehicle`, `CcControlEntity_RollingStone`, plus lowercase
  `cAnimControl`, `cWickerManAI`, `cWereBullAI`, `cWarriorAI`. SE1 uses
  `CEntity`, `CWorld`, `CTString`, `CModelObject`.
- **Zero SE1 identifiers**: no `CEntity`, `CTString`, `CWorld`, `CModel`
  anywhere in the binary.
- **Nintendo Dolphin SDK is linked in**: `OSThread.c`, `GXMisc.c`, `dvdfs.c`,
  `dvd.c`, `OSAllocFromHeap`, `OSCreateHeap`, `CARD_RESULT_FATAL_ERROR`.
- Asset formats are Climax's too: `Data.ssg`, `levels/%s.ssg`, `.ssw`, `.tdb`,
  `Game.Gui` — none of SE1's `.wld` / `.mdl` / `.tex` / `.gro`.

**Consequence:** `ref/serious-engine` is a *reference for the game's design and
for how a Serious Sam engine is shaped*, not a codebase to drop decompiled
functions into. Next Encounter was written by Climax Solent on their own engine.
Any plan that assumed "recompile the SE1 source with NE's game logic" needs to be
re-scoped. See `docs/strategy.md`.

## What's recoverable

- **No CodeWarrior-mangled symbols** — the build is stripped (0 hits for
  `name__<len>ClassF<args>` mangling).
- **399 printf-style format strings** and **~50 `CcClass::Method` debug strings**
  survive. These are the main naming lever: each sits next to (or is referenced
  by) the function it belongs to, so cross-referencing them recovers real
  function and class names without symbols.
- Verbose render-loop tracing exists (`CcEngine::Render Viewport BasePass`,
  `CcEngine::Flip() step %d`), which maps the frame structure fairly directly.

## Toolchain constraint: Gekko paired-singles

Stock Ghidra 12.1.2 ships no Gekko/Broadway PowerPC variant — the available
PowerPC language IDs are `default`, `4xx`, `e500`, `e500mc`, `MPC8270`, `QUICC`.
The GameCube CPU (Gekko) adds paired-single instructions (`psq_l`, `psq_st`,
`ps_add`, `ps_madd`, …) that `PowerPC:BE:32:default` decodes as invalid.

A 3D shooter uses these heavily in math and transform code, so a chunk of
`xforms.cpp`-adjacent functions will not decompile correctly until a Gekko
language is installed. `aldelaro5/ghidra-gekko-broadway-lang` is the known
extension but has no releases and was last updated 2022-02-09 (Ghidra ~10.x), so
it needs rebuilding against 12.1.2. Tracked as an open item.

Two consequences, and the workarounds in use (`docs/toolchain.md`):

- **Lost code after SDK matrix calls.** `PSMTXIdentity` (`0x801b9eac`) is
  pure paired-single code. Auto-analysis could not follow it and marked it
  no-return, so the instructions after its 98 call sites were never
  disassembled. The animation key decode was one of them.
  `ghtool.py fixflow` repairs that.
- **Paired-single code itself.** `tools/gekko.py` disassembles it straight
  from `main.dol`. `--gqr` shows how each quantization register is set, so
  that a `psq_l`'s scale can be known.

## Asset container: `CLIMAX` (.ssg / .ssw) — fully solved

Recovered from the loader at `CcImport_ReadOld` (`0x801206b8`, formerly
`FUN_801206b8`), identified by its `">> IMPORT Read Old %s"` debug string and its
`memcmp` against the magic. Verified against **all 63 containers**: every field
matches exactly, 4,943,495 pointer fields decode in range, 0 failures.

Header, big-endian:

| Offset | Field | Meaning |
|---|---|---|
| `0x00` | magic (8 bytes, NUL-terminated) | `CLIMAX-` new style, `CLIMAX.` old style |
| `0x08` | `reloc_count` | entries in the relocation table |
| `0x0c` | `alloc_size` | bytes allocated = inflated image size |
| `0x10` | `main_csize` | compressed size of the image stream (`0` = stored raw) |
| `0x14` | `reloc_csize` | compressed size of the reloc stream (`0` = stored raw) |
| `0x18` | `stage_off` | offset used to stage the reloc table inside the buffer |
| `0x1c` | `stage_lim` | size the loader compares against to pick that strategy |
| `0x20` | 32 bytes zero padding | |

**There are two concatenated zlib streams**, not one — this is the part that is
easy to get wrong, because inflating from `0x40` appears to "work" while
silently ignoring the second stream:

    0x40                  stream 1: the image,  main_csize  bytes -> alloc_size
    0x40 + main_csize     stream 2: reloc table, reloc_csize bytes -> reloc_count*4

`main_csize + reloc_csize == filesize - 0x40` exactly, on every file. The game
links **zlib 1.1.4** (the version string is handed to `inflateInit_`) and
inflates in 128 KB chunks.

New-style headers are `0x40` and the image starts at the buffer base. Old-style
headers are `0x10`, and the loader copies those 16 bytes to the front of the
buffer so the relocation base sits `0x10` *before* the image — which is why
`CcImport_FreeBlock` frees at `ptr - 0x10` after matching `"CLIMAX."`. Every
container shipped on the disc is new style.

### Relocation table (the useful part)

The payload is a serialised memory image with no type tags, so on its own it is
opaque. The relocation table names **every pointer field in it**, which is what
makes the object graph walkable. Entries are RLE-coded (loop at `0x801208f8`):

```c
v = table[i];
if (v & 0x80000000) {                 // run of evenly spaced pointers
    count  = (v >> 10) & 0x1fffff;
    stride = v & 0x3ff;
    off    = table[++i];              // run start, consumes the next word
    while (count--) { *(u32*)(base + off) += base; off += stride; }
} else {                              // single pointer at offset v
    *(u32*)(base + v) += base;
}
```

Stored values are image-relative offsets; the console added the load address.
For parsing, leave `base = 0` and the fields are already usable as offsets.

`tools/ssg.py` implements all of this — `info`, `unpack`, `strings`, `relocs`,
and `verify` (which re-checks all 63 containers).

## Ghidra database

`ghidra/NextEncounter.gpr`, built by `ghidra/scripts/import_dol.py` with
`--analyze`. Auto-analysis found **6,539 functions** in `.text`. Note this number
was produced with `PowerPC:BE:32:default`, so functions using Gekko
paired-singles are mis-decoded — expect the count and the decompiler output in
math-heavy code to change once a Gekko language is installed.

## Config files (`CcStash`) — extracted

Two dialects, both plain text, both fully parsed by `tools/cfg.py`
(`parse`, `keys`, `enemies`, `roster`). Output JSON in `build/cfg/`.

| File | Dialect | Settings |
|---|---|---|
| `base.cfg` | stash | 1,355 |
| `MinionStats.cfg` | stash | 1,081 |
| `LevelModels.cfg` | stash | 165 |
| `Combine.cfg` | DYNE | 98 |
| `Jeep.cfg` | DYNE | 97 |

**Stash** is the `CcStash` system: `stash NAME { <type> <name> <value> [min] [max] }`,
nested. Types are `float`, `int`, `sqrfloat`, `flag`, `string`. `sqrfloat` is a
float the engine squares on load so distance tests skip the square root — the
value in the file is the true distance. Names are normally bare identifiers but
are quoted when they would be invalid, e.g. `float "9MMBullets" 5 1 600`.

**DYNE** (`Combine.cfg`, `Jeep.cfg`, headed "DYNE Parameters File") is flat
vehicle physics: `KEYWORD value...`, plus count-then-list arrays
(`NENGINEBINS 11`, then `ENGINETORQUECURVE` and 11 bare floats) and repeated
`WHEEL <n>` sections scoping the `WHEEL*` keys after them.

### The entity table

`MinionStats.Object<N>` and `LevelModels.ObjectModel<N>` are indexed by the same
entity id, and each line's trailing comment carries the original enum name. That
gives a **51-entry enemy roster** joining stats to models to enum constants —
`e_KamikazeMarine`, `e_WereBull`, `e_KleerKnight`, `e_Minotaur` (10,000 HP),
`e_DragonDrill`/`e_DragonFire`/`e_DragonCannon` (7,250 HP each), through to the
`e_FMA0_1Intro*` cutscene actors. `tools/cfg.py roster` prints it.

Every minion inherits the `Generic` stash's 29 fields unless it overrides them —
the file states this itself. Generic supplies `FOV`, `SeeDist`, `HearDist`,
`PatrolSeeDist`, `ActiveTime`, `NormalSpeed`, `FastSpeed`, `AttackTime`,
`AttackFrequency`, `TurnSpeed`, `Mass`, `CollisionRadius`, `SplatterType`,
separate `UpdateRate60hz`/`UpdateRate50hz`, and more. Any port must implement
that fallback or the stats read wrong.

`base.cfg` additionally holds `Weapons` (18), `Player` (70), `Scoring`,
`UnlockTable`, `PuzzleConfig`, `AmmoValues`, `WeaponReticules`, `HiScore`,
`Cheats`, `Credits` and `LevelMusic` — i.e. most of the game's tuning, readable
without touching the binary.
