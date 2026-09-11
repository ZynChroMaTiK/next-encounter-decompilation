# Serious Sam: Next Encounter — decompilation & PC port

Reverse engineering of the GameCube release of *Serious Sam: Next Encounter*
(`G3BE9G`, NTSC-U Rev 1), aiming for a PC-playable rebuild of its levels and
game logic on Serious Engine 1.

> **No game data is included.** This repository has documentation, analysis
> scripts and converters only. You need your own copy of the game; see
> [`orig/README.md`](orig/README.md).

## What this is (and isn't)

Next Encounter was not built on Croteam's Serious Engine. It runs on Climax
Solent's own engine (`CcEngine`, `CcLevel`, `CcSystem`) on the Nintendo Dolphin
SDK, and `main.dol` contains no SE1 code at all. So this is not a
matching decompilation. The GameCube build is used as a **specification**:
its formats, rules and level data are recovered, and the game is rebuilt on SE1.
The evidence is in [`docs/findings.md`](docs/findings.md) and the chosen route in
[`docs/strategy.md`](docs/strategy.md).

## Engine

The port targets **[tx00100xt/SeriousSamClassic-VK](https://github.com/tx00100xt/SeriousSamClassic-VK)**,
a maintained fork of Croteam's Serious Engine 1.10 with a Vulkan renderer
(OpenGL fallback), 64-bit builds and the full tool tree (`Ecc`, `WorldEditor`,
`Modeler`). The reasons for choosing it and the build prerequisites are in [`pc/README.md`](pc/README.md).

Both engine trees are **git submodules** pinned to their upstream repositories:

| path | repository | role |
|---|---|---|
| `pc/engine` | [tx00100xt/SeriousSamClassic-VK](https://github.com/tx00100xt/SeriousSamClassic-VK) | the engine the port is built on |
| `ref/serious-engine` | [Croteam-official/Serious-Engine](https://github.com/Croteam-official/Serious-Engine) | original SE1 1.10 release, reference only |

```bash
git clone --recurse-submodules https://github.com/ZynChroMaTiK/next-encounter-decompilation.git
# or, in an existing clone:
git submodule update --init
```

## Layout

```
orig/        your disc image + extracted files (git-ignored; see orig/README.md)
build/       everything the tools produce from orig/ (git-ignored)
docs/        the reverse-engineered formats and findings — start here
tools/       Python extractors/converters (containers, meshes, sound, text, worlds)
ghidra/      scripts/ for importing and driving main.dol headlessly
pc/          the PC port; pc/engine is the SE1 fork (submodule)
ref/         Croteam's original SE1 source (submodule)
```

## Documentation

| doc | covers |
|---|---|
| [`docs/findings.md`](docs/findings.md) | what the binary is; the `CLIMAX` container format |
| [`docs/image-format.md`](docs/image-format.md) | resource types, textures, entities, meshes, object models |
| [`docs/world-conversion.md`](docs/world-conversion.md) | how an NE level becomes an SE1 world |
| [`docs/sound.md`](docs/sound.md) | DSP-ADPCM effects bank, music and speech streams |
| [`docs/text.md`](docs/text.md) | `.tdb` text databases and NETRICSA messages |
| [`docs/strategy.md`](docs/strategy.md) | the chosen route and ordered next steps |
| [`docs/toolchain.md`](docs/toolchain.md) | Ghidra (headless + MCP), DolphinTool setup |

## Tooling

Not in the repo; set up locally per [`docs/toolchain.md`](docs/toolchain.md):

- [Ghidra](https://github.com/NationalSecurityAgency/ghidra) 12.1.2 with [PyGhidra](https://pypi.org/project/pyghidra/)
- [GhidraMCP](https://github.com/LaurieWired/GhidraMCP) — `bridge_mcp_ghidra.py` goes in `tools/ghidra-mcp/`
- [Dolphin](https://dolphin-emu.org/) (`DolphinTool`) — goes in `tools/Dolphin-x64/`

## Licence

Licensed under the **GNU General Public License, version 2** — see
[`LICENSE`](LICENSE). That matches Serious Engine 1, which the port is built on
and which is GPL v2 only.

## Legal

*Serious Sam* is a trademark of Croteam. *Serious Sam: Next Encounter* was
developed by Climax Solent and published by Global Star Software / Take-Two
Interactive. This project is not affiliated with or endorsed by any of them.
It contains no copyrighted game assets or code from the game. Anything the
tools produce from your disc stays on your machine under `build/`.

Serious Engine 1 is © Croteam, released under the GNU GPL v2. The submodules
keep their own licences.
