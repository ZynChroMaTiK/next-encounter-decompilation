# Toolchain

Absolute paths, because several of these are outside the repo.

| Thing | Path |
|---|---|
| Ghidra 12.1.2 | `C:\Software\ghidra_12.1.2_PUBLIC` |
| Ghidra user dir | `C:\Users\brian\AppData\Roaming\ghidra\ghidra_12.1.2_PUBLIC` |
| JDK | Temurin 25.0.3 (`JAVA_HOME` already set; works with Ghidra 12.1.2) |
| DolphinTool | `tools/Dolphin-x64/DolphinTool.exe` (Dolphin 2606a) |
| PyGhidra venv | `tools/ghidra-headless/.venv` |
| GhidraMCP bridge venv | `tools/ghidra-mcp/.venv` |

## Unpacking the disc

Already done, but for reference:

```bash
tools/Dolphin-x64/DolphinTool.exe extract \
  -i "orig/Serious Sam - Next Encounter (USA) (Rev 1).rvz" -o orig
tools/Dolphin-x64/DolphinTool.exe header \
  -i "orig/Serious Sam - Next Encounter (USA) (Rev 1).rvz"
```

`convert -f iso` regenerates a plain ISO if an emulator needs one; we don't keep
it checked out because it's 1.4 GB of redundancy against the RVZ.

## Importing main.dol into Ghidra

Ghidra has no DOL loader. `ghidra/scripts/import_dol.py` parses the DOL header
and builds the memory map itself. It runs as a plain CPython script driving
Ghidra through PyGhidra — no GUI, no `analyzeHeadless` wrapper.

Inspect the map without touching Ghidra at all:

```bash
python ghidra/scripts/import_dol.py --dol orig/sys/main.dol --describe
```

Do the real import (Ghidra must not have the project open — it takes an
exclusive lock):

```bash
GHIDRA_INSTALL_DIR="C:/Software/ghidra_12.1.2_PUBLIC" \
  tools/ghidra-headless/.venv/Scripts/python.exe ghidra/scripts/import_dol.py \
  --dol orig/sys/main.dol \
  --project-dir ghidra --project-name NextEncounter \
  --analyze
```

`--analyze` runs Ghidra's auto-analysis in-process; on a 1.9 MB `.text` expect
this to take a while. Drop it to import in seconds and analyse later in the GUI.
Use `--language` to switch to a Gekko language id once one is installed.

Until then, two workarounds recover paired-single code:

- **`ghtool.py --write fixflow ADDR`** clears a no-return flag that
  auto-analysis put on an SDK matrix helper (`PSMTXIdentity` at
  `0x801b9eac` was one). It then disassembles the fall-through after every
  call and rebuilds the callers' bodies. Without it, the code after 98
  calls was never disassembled.
- **`tools/gekko.py START END`** disassembles `main.dol` directly,
  `psq_l`/`psq_st`/`ps_*` included. `--gqr` lists every write to the
  quantization registers, which set the scale of each `psq_l`. The
  animation key decode (`0x80129334`) was read this way.

## Reading the developer material (`dev/`)

Both tools read `dev/` in place and write only under `build/dev/`
(`docs/dev-material.md`).

- **`python tools/clm.py verify | tree FILE | meshes FILE...`** parses
  Climax's `.clm` Maya exports. There are no dependencies.
- **`python tools/se1dll.py dump | list | show CLASS`** recovers the SE1
  1.04 entity classes, their property tables and their enums from the
  entity DLLs. The tables are built by static initializers, so it emulates
  the x86 code. It needs **capstone**: `python -m pip install capstone`.

## RAM dumps

To see runtime state the disc doesn't hold:
1. Start the game in Dolphin (`tools/Dolphin-x64`) with the debugging UI on
   (Options → Configuration → Interface → "Enable Debugging UI").
2. Go into a level.
3. Use Debug → Memory → "Dump MEM1". Dolphin writes
   `%APPDATA%/Dolphin Emulator/Dump/mem1.raw`: 24 MiB, the RAM from
   0x80000000.
4. Copy it to `build/ramdump/`, which is git-ignored.

**`python tools/ramdump.py info`** first identifies the loaded level and
where its image sits in RAM. It then checks that every relocated pointer
equals the base plus its on-disc value, and prints the scene's render
objects, the TouchField and Bouncer tables, and each TouchField entity.

## Driving Ghidra from Python (headless)

This is the general lever — anything the Ghidra API can do:

```bash
GHIDRA_INSTALL_DIR="C:/Software/ghidra_12.1.2_PUBLIC" \
  tools/ghidra-headless/.venv/Scripts/python.exe - <<'PY'
import pyghidra; pyghidra.start(verbose=False)
from ghidra.base.project import GhidraProject
# Ghidra requires an ABSOLUTE project path, and openProgram registers the
# project itself as the consumer - so just close(), never release() first.
proj = GhidraProject.openProject(
    "C:/SeriousEngine/Coding/next-encounter-decompilation/ghidra",
    "NextEncounter", True)
try:
    p = proj.openProgram("/", "main.dol", True)
    print(p.getFunctionManager().getFunctionCount(), "functions")
finally:
    proj.close()
PY
```

Ghidra prints two harmless `Module manifest file error` lines about GhidraMCP on
every startup; filter them with `grep -v "Module manifest file error"`.

## GhidraMCP (interactive, alongside the GUI)

`.mcp.json` registers a `ghidra` MCP server. The chain is:

    Claude  <-stdio->  tools/ghidra-mcp/bridge_mcp_ghidra.py  <-HTTP:8080->  GhidraMCP plugin in Ghidra

For it to work, **all** of these must hold:

1. Ghidra is running with a program open in the CodeBrowser (the plugin's HTTP
   server only serves an open program).
2. The GhidraMCP plugin is enabled: `File > Configure > Miscellaneous > GhidraMCP`.
3. `curl http://127.0.0.1:8080/methods?limit=3` returns data.
4. The Claude Code session has been restarted since `.mcp.json` was written —
   MCP servers are loaded at session start.

Caveat: the released GhidraMCP 1.4 is built for **Ghidra 11.3.2**. Its
`extension.properties` has been patched to `ghidraVersion=12.1.2` (originals kept
as `*.bak` next to it) so Ghidra will load it, and its malformed `Module.manifest`
was emptied. The jar is still compiled against 11.3 APIs, so it may still throw
at runtime. If it does, rebuild GhidraMCP from source against 12.1.2 — or just
use the PyGhidra path above, which is strictly more capable and has no version
coupling.

## Building the engine (`pc/engine`)

`pc\build-windows.ps1` builds the fork's `SamTSE.sln`, Release x64, with the
installed VS 2022 Build Tools (MSVC v143, ATL/MFC) and the Vulkan SDK.
`-Target Engine` builds one project. Setup, overrides and outputs are in
`pc/README.md`.

## NE's creatures as SE1 models and classes

`python tools/creature.py export <name>` bakes a creature's animations from
the disc into OBJ frames, its texture and a modeler script under
`SamTSE/Models/NextEncounter/Enemies/<name>/`. `WldWriter --mdl <script>
<model>` builds the `.mdl` and its animation header with the engine's own
modeler code; `WldWriter --mdlinfo <model> <txt>` lists a model's
animations, frame boxes, first-frame vertices and polygons for checking;
`python tools/creature.py check <name>` runs it and compares every polygon
corner's texture coordinates with the bind-pose OBJ.
`pc\build-windows.ps1 -Entities` builds the classes in `pc/entities`
(`docs/enemies.md`).

## Converting a level to a world (Stage 2)

`python tools/wldtex.py` writes every level texture a polygon uses as a TGA
under `pc/engine/SamTSE/Textures/NextEncounter`, and `WldWriter.exe --tex
build/wld/textures.lst` turns them into `.tex`; worlds need those first.
`python tools/wldprep.py --all` flattens each description into
`build/wld/<level>.wldsrc` (copying the sounds, music and messages its
entities name under `SamTSE/Sounds`, `Music` and `Data/Messages`), with the polygons rebuilt from the level's
triangles (`tools/polygons.py`) and the world base cut into rooms joined by
portals, built from its whole polygons (`tools/rooms.py`;
`python tools/rooms.py render <level>` draws them in an oblique view into
`build/render/`). `pc\build-windows.ps1 -Writer` builds
`pc/wldwriter` into `pc/engine/SamTSE/Bin`, and `WldWriter.exe <file>.wldsrc`
writes `SamTSE/Levels/NextEncounter/<level>.wld`. `--check` loads a world back
and links its portals as the renderer does,
`--rays` casts test rays through it, `--tri` re-triangulates every polygon as
the editor does and names the ones it rejects, and `--dump <wld> <txt>` lists
a world's polygons with every texture layer's settings and mapping
(`tools/devworld.py` runs it on a `dev/` world). The writer bakes each world's
shadow maps from its lights before saving; `--lightmap <wld> <txt>` dumps
every polygon's mixed shadow map, and `python tools/lightmap.py render <level>`
draws it beside the level's own baked lightmap, with their difference, into
`build/render/<level>_lightmap.png` (the dump runs to hundreds of MB on large
levels). The writer needs the retail data and a
game ID; see `docs/world-conversion.md`, "What the writer needs at run time".
