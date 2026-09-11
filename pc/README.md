# `pc/` — the PC port

## Engine baseline

`pc/engine/` is a clone of **[tx00100xt/SeriousSamClassic-VK](https://github.com/tx00100xt/SeriousSamClassic-VK)**,
a maintained fork of Serious Engine 1.10 — GPL-2.0, same licence as Croteam's
release.

Chosen over the alternatives on 2026-09-09:

| candidate | last push | verdict |
|---|---|---|
| **tx00100xt/SeriousSamClassic-VK** | **2026-01-14** | **chosen** — Vulkan + OpenGL fallback, x86-64 and ARM64, full engine + tools, CMake and MSVC |
| tx00100xt/SeriousSamClassic | 2026-01-13 | same author, OpenGL/SDL2 only — no modern renderer |
| DreamyCecil/SE1-ModSDK | 2026-03-12 | active, but a mod SDK against the retail games, not a full engine tree |
| sultim-t/Serious-Engine-RT | 2022-10-08 | unmaintained; Windows-only, RTX-hardware-gated, TFE only |
| SE1-Community/SE1-GameShell | 2021-06-22 | unmaintained |
| Croteam-official/Serious-Engine | 2020-10-31 | upstream, 32-bit, unmaintained — kept as `ref/` for reference |

What decided it: the route needs to *add entity classes*, so the tree has to
carry `Ecc` (the entity class compiler) and the editor alongside the engine,
not just a game binary. This fork has all of it — `Engine/`, `EntitiesMP/`,
`GameMP/`, `Ecc/`, `WorldEditor/`, `Modeler/`, `SeriousSkaStudio/`, `Shaders/`.

The Vulkan renderer is real and in-tree (`Engine/Graphics/Gfx_Vulkan.cpp`,
`Gfx_Vulkan_Textures.cpp`, `Gfx_wrapper_Vulkan.cpp`) with OpenGL as an
automatic fallback, so it does not hard-require RT-class hardware.

### Entity classes are unchanged from the reference tree

The fork carries the same **144** `EntitiesMP` classes as Croteam's release. It
drops `ParticleCloudsHolder` and `ParticleCloudsMarker` and adds
`PlayerWeaponsHD` and `PlayerWeapons_old`; nothing in the ruleset mapping
(`tools/sediff.py`) touches the dropped pair, so the diff results carry over
unchanged. `sediff.py` now reads this tree by preference and falls back to
`ref/serious-engine`.

## Layout

```
pc/
  engine/          the fork, as a git submodule pinned to upstream (not vendored)
    SamTSE/Sources/    The Second Encounter — the fuller game, the intended base
    SamTFE/Sources/    The First Encounter
```

`SamTSE` is the base: TSE is a superset of TFE's entity and weapon set, which
matters because NE's roster maps onto TSE-era classes.

## Building on Windows — not yet done

Nothing has been compiled. The prerequisites are **not installed on this
machine** and installing them is a decision for the user, not something to do
unprompted:

| requirement | state |
|---|---|
| Visual Studio 2015+ with "Desktop development with C++" | **missing** — `vswhere` reports no installation carrying the C++ toolset; only shared/installer remnants under `Program Files (x86)` |
| Windows 10/11 SDK | missing (comes with the above workload) |
| Vulkan SDK 1.3.204.1+ | **missing** — no `C:\VulkanSDK`, `VULKAN_SDK` unset |
| CMake / Ninja | not on `PATH` (optional on Windows; the `.sln` route does not need them) |

Then, per the fork's README:

```
1. Install the Vulkan SDK 1.3.204.1 or higher
2. Install Visual Studio 2015 Community or higher, with the C++ workload
3. Install the Windows 10 SDK
4. Open pc/engine/SamTSE/Sources/SamTSE.sln, select "Release x64", build
```

Running the stock game additionally needs retail game data (`.gro` archives)
from a legitimate copy — the fork ships the engine, not the assets. That is
only needed to test the engine as-is; the port itself supplies NE's own
content.
