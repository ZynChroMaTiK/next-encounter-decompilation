# Serious Sam: Next Encounter — GameCube → PC

Reverse-engineering the GameCube build of *Serious Sam: Next Encounter*
(`G3BE9G`, NTSC-U Rev 1) toward a PC-playable reimplementation.

## Layout

```
orig/                  pristine disc contents — READ ONLY, never edit
  sys/                 main.dol, apploader.img, boot.bin, bi2.bin, fst.bin
  files/               game assets (Levels/, Textures/, Sound/, Video/, ...)
  *.rvz                the source disc image
ghidra/                Ghidra project + scripts
  scripts/             import_dol.py and friends
ref/serious-engine/    Croteam SE1 1.10 (GPL) — reference only, see caveat below
dev/                   NE developer material (SE1 1.04 tools, entity DLLs, Maya
                       .clm model sources) — READ ONLY, git-ignored, never commit
pc/                    the PC port being built
  engine/              SeriousSamClassic-VK — the chosen SE1 fork, see pc/README.md
  entities/            NE's own entity classes (EntitiesNE), see docs/enemies.md
tools/                 DolphinTool, PyGhidra venv, GhidraMCP bridge venv
docs/                  findings.md, toolchain.md, strategy.md
```

`orig/` is ground truth. Never modify anything under it; regenerate from the
`.rvz` if it's ever disturbed.

## Read first

- `docs/findings.md` — what the binary actually is. **Read this before assuming
  anything about the engine.** Also the container format.
- `docs/image-format.md` — inside the container: resource types, image header,
  the typed `CcTexture`, the entity/placement list, the prop meshes, the
  object models (enemies, pickups, weapons) embedded as nested images, and
  what is still untyped.
- `docs/strategy.md` — the chosen route and the ordered next steps.
- `docs/world-conversion.md` — how a level becomes an SE1 world: sectors,
  lights, brush entities, and exactly which NE field feeds which SE1 property.
- `docs/enemies.md` — NE's creatures: the TSE stand-ins, and the port of the
  rest as SE1 classes (models baked from the disc, `pc/entities`, behaviour).
- `docs/sound.md` — the DSP-ADPCM effects bank, the music and speech streams
  in `StreamData.dat`, and the hash tables entities use to name a sound.
- `docs/text.md` — the `.tdb` text databases, the hashed lookup, and the
  NETRICSA messages MessageHolders carry.
- `docs/gui.md` — `Game.Gui`, the menu layout: a `DXFF` relocatable blob of
  328 components (`tools/gui.py`).
- `docs/dev-material.md` — what is in `dev/`: the `.clm` model sources
  (`tools/clm.py`) and the SE1 entity classes with every property named
  (`tools/se1dll.py`), and how NE's classes map onto them.
- `docs/toolchain.md` — how to run Ghidra headless, the MCP wiring, DolphinTool.

## The one thing that trips people up

**This is not Croteam's Serious Engine.** The GameCube build is Climax Solent's
own engine (`CcEngine`, `CcLevel`, `CcSystem`; source paths
`C:/SeriousSam/src/game/system/Engine/*.cpp`) on the Nintendo Dolphin SDK. There
are zero SE1 identifiers in `main.dol`. `ref/serious-engine` is useful for design
reference and for how a Serious Sam game is shaped — it is *not* a codebase to
paste decompiled functions into. Details and evidence in `docs/findings.md`.

## Conventions

- Ghidra work is scripted, not hand-clicked, so it's reproducible: put anything
  reusable in `ghidra/scripts/` rather than doing it once in the GUI.
- Ghidra takes an exclusive lock on the project. Headless scripts fail while the
  GUI has it open — close Ghidra first.
- Name functions from the surviving debug strings (399 printf formats, ~50
  `CcClass::Method` strings). That's the main source of ground-truth naming;
  the build has no symbols.
- Record anything learned about a file format in `docs/`, not just in Ghidra
  comments — the asset formats (`.ssg`, `.ssw`, `.tdb`, `Game.Gui`) are
  undocumented and will need their own loaders for the port.
