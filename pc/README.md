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
  entities/        EntitiesNE: NE's own creature classes, built against the fork
  patches/         fixes to the fork, applied before an engine build
  wldwriter/       the Stage 2 world writer (docs/world-conversion.md)
```

`SamTSE` is the base: TSE is a superset of TFE's entity and weapon set, which
matters because NE's roster maps onto TSE-era classes.

## Building on Windows

**The whole solution builds.** On 2026-09-11, `SamTSE.sln` Release x64 built
22 of 22 projects with 0 errors. The 173 warnings are mostly format strings
and 64-bit conversions (`C4477`, `C4267`, `C4091`, `C4244`).

| requirement | installed |
|---|---|
| Visual Studio 2022 Build Tools, "Desktop development with C++" and ATL/MFC | MSVC 14.44 (toolset v143). Every engine project uses MFC. |
| Windows SDK | 10.0.22621 and 10.0.26100 |
| Vulkan SDK 1.3.204.1 or newer | 1.4.357.0 in `C:\VulkanSDK\1.4.357.0`; `VULKAN_SDK` is set machine-wide |
| CMake | comes with the Build Tools, but is not needed: the build uses the `.sln` |

```
pc\build-windows.ps1                     # SamTSE, Release x64, whole solution
pc\build-windows.ps1 -Target Engine      # one project and what it depends on
pc\build-windows.ps1 -Writer             # pc/wldwriter into SamTSE/Bin
pc\build-windows.ps1 -Entities           # pc/entities into SamTSE/Bin as EntitiesNEMP.dll
```

- **Toolset.** The projects ask for toolset v140 and Windows SDK 10.0.14393;
  the fork's CI builds them on a VS 2019 image. The script passes
  `PlatformToolset=v143` and `WindowsTargetPlatformVersion=10.0` as global
  msbuild properties instead, so the pinned submodule is not edited.
- **Finding the tools.** The script finds MSBuild through `vswhere`. If the
  shell was opened before the SDK was installed, it reads `VULKAN_SDK` from
  the machine environment.
- **Outputs** land inside the submodule, as the fork's projects are set up to:
  each project's `Release\` folder, copied into `SamTSE/Bin/`. That includes
  `SeriousSam.exe`, `SeriousEditor.exe`, `SeriousModeler.exe`, `Ecc.exe`,
  `Engine.dll` and `EntitiesMP.dll`.
- **Patches.** Fixes to the fork are kept in `pc/patches/*.patch`, not
  committed into the submodule. The script applies each one to the checkout
  before an engine build, skipping those already applied, and stops if one
  no longer applies.
- **Untracked files.** The build also generates parser and Ecc sources that
  the fork's `.gitignore` misses, because its patterns lack the `SamTSE/`
  prefix. So `.gitmodules` sets `ignore = untracked` for `pc/engine`.

### Patches to the fork

| patch | fixes |
|---|---|
| `shadow-mask-spans.patch` | `CRenderer::AddSpansToScene` (`Rendering/RenCache.cpp`) wrote a scan line's shadow spans one after another from the row's start, trusting them to tile it. When their edges land outside a small mask they do not, and the last row ran past the mask: 83 bytes past an 8×8 mask's safety wall while baking `Rlevel4_3`'s shadow maps, which corrupted the heap and crashed the writer (and would crash the editor's shadow recalculation). Each span is now clamped to the mask and written where it starts. `Rlevel1_1`'s baked shadow maps compare with the level's lightmap exactly as before. |
| `obj-model-import.patch` | The fork is built without the proprietary Exploration 3D library, so `CObject3D::LoadAny3DFormat_t` (`Math/Object3D_IO.cpp`) loaded nothing and `BatchLoading_t` threw: no model could be built from a modeler script. It now reads Wavefront OBJ into the same conversion arrays Exploration 3D filled, with its conventions (x, z and v negated; faces flipped under a mirroring transform), and batch loading is a no-op. **It also fixes UV seams**, which the original importer got wrong too: the modeler kept one texture coordinate per vertex per surface (`RemapVertices` built the opened and unwrapped copies per vertex, and `CEditModel::CalculateUnwrappedMapping` matched texture vertices by position), so on a model with one material the polygons along every seam took the other side's coordinates. The designers' own `MonkeyMan.mdl` has 324 of 706 polygons mapped wrong that way, and DumDumlarge had 187 of 1,070. Now the copies are keyed on vertex and texture vertex, and a model mapped from one object (`MIP_MODELS` / `IMPORT_MAPPING`) takes each polygon corner's coordinates from the same corner, with a texture vertex per distinct UV: 0 wrong on both, and on a two-material test. Three-file `DEFINE_MAPPING` keeps the position match. Positions are unchanged: the designers' MonkeyMan rebuilds vertex for vertex (`docs/enemies.md`). |
| `stereo-mixer-channels.patch` | The portable C `MixStereo` (`Sound/SoundMixer.cpp`), which x64 builds use since MSVC has no inline assembly there, found a stereo frame at `fixOfs>>15`: twice the sample position plus the top bit of its fraction. Whenever the fraction was 0.5 or more, the left channel read the right sample and the right channel the next frame's left. A sound at the mixer's own rate, or at half of it, never hits such fractions, so retail data rarely showed it. NE's 32 kHz stereo music does all the time: simulated at a 44.1 kHz mixer, the bug adds a whistle 34–41 dB above the music between 8 and 16 kHz. The frame is now `(fixOfs>>16)*2`. Mono sounds go through `MixMono`, which was right. |

Running the stock game additionally needs retail game data (`.gro` archives)
from a legitimate copy — the fork ships the engine, not the assets. That is
only needed to test the engine as-is; the port itself supplies NE's own
content.
