# `orig/` — your own disc goes here

This directory is intentionally empty in the repository. Nothing from the game
is distributed here: the tools read game data from a copy you dump from a disc
you own.

Expected contents, all git-ignored:

```
orig/
  Serious Sam - Next Encounter (USA) (Rev 1).rvz    G3BE9G, NTSC-U Rev 1
  sys/     main.dol, apploader.img, boot.bin, bi2.bin, fst.bin
  files/   Levels/, Textures/, Sound/, Video/, ...
```

Dump the disc with a Wii or GameCube homebrew dumper (e.g. CleanRip), convert
it to `.rvz` with Dolphin if you like, then extract it:

```bash
DolphinTool extract -i "orig/Serious Sam - Next Encounter (USA) (Rev 1).rvz" -o orig
```

Other revisions or regions have not been checked. Addresses in `docs/` are for
`G3BE9G` Rev 1's `main.dol` (2,554,080 bytes).

Treat this directory as read-only once it is populated.
