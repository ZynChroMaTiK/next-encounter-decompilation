# `Game.Gui` — the menu layout

`orig/files/Game.Gui` (37,292 bytes) lays out the front end and the in-game
menus as one flat table of 328 components. `CcSystem_Init` (`0x80161cc8`)
loads it at boot through `FUN_800f52e0`. That routine fixes it up with
`FUN_800f53c0` and logs "(GUI) %i Components". `tools/gui.py` reads it.

## The container: `DXFF`, a relocatable blob

```
char magic[4]  "DXFF"
u32  0
u32  reloc_offset        relocation table, relative to the data base
u32  reloc_count
---- data base: file offset 0x10 ----
u32  count               components
u32  component[count]    pointers
...                      components, their sub-records, then strings
u32  reloc[reloc_count]  data offsets of every pointer field
```

On disc, a pointer field holds an offset from the data base. The loader walks
the relocation table and adds the base to each field it names:
`*(base + r) = base + *(base + r)`.

## Components

Every component opens with the same header:

| offset | field | notes |
|---|---|---|
| `+0x00` | u16 kind | 0–7. Each kind has one record size: 0 = 76, 1 = 88, 2 = 104, 3 = 96, 5 = 84, 6 = 84, 7 = 92 bytes |
| `+0x02` | u16 index | its own position in the table |
| `+0x04` | u16 screen | the kind-0 component it belongs to. A screen holds 0xFFFF, or its outer screen when nested |
| `+0x08` | u16 | small values (0, 1, 2, 4 …), meaning unread |
| `+0x0c` | u32 flags | 0x10, 0x8010, 0x500, … |
| `+0x10` | u16 ×2 | component indices, likely navigation links |
| `+0x28` | u32 | 4 on every component |
| `+0x2c` | f32 x, y | position on a 640×480 screen centred on 0; #3–#11 step 32 apart in a row |
| `+0x34` | f32 w, h | 640, 480 on most components |
| `+0x3c` | u32 RGBA | `a0a0a0ff`, `ffffffff`, `a0be10dc` |
| `+0x40` | pointer | on kinds 2 and 3: the **config key** the widget edits (5 and 26 components) |
| `+0x4c` | pointer | on kind 6: the **sprite** it draws, `Atlas:Sprite` (13 of 22; the others get theirs from code) |
| `+0x44`–`+0x48`, `+0x5c` | pointers | to sub-records, on the kinds that have them |

What the kinds do, where the data says:

| kind | size | count | reading |
|---|---|---|---|
| 0 | 76 | 29 | a **screen**: its members follow it, and it can nest in another (#164 in 160, #239 in 227) |
| 1 | 88 | 226 | the common element: rows, cells, labels |
| 2 | 104 | 6 | bound to a config key |
| 3 | 96 | 35 | bound to a config key: `GameSetup…`, `SystemSetup…`, `cheats…` |
| 5, 7 | 84, 92 | 5, 5 | unread |
| 6 | 84 | 22 | an **image**: draws a sprite from `FrontEnd.ssg` |

**No text lives in the file.** No word in it resolves as a `.tdb` hash, so the
labels come from the menu code. Its strings are of two sorts:
- **config keys** the widgets bind to: `GameSetup.RulesCoop.Difficulty`,
  `SystemSetup.SFXVolume`, `cheats.GiveAllWeapons`, `PuzzleConfig.HideWeapons`…;
- **sprite names**, written `Atlas:Sprite`: `GuiA:ArrowL`,
  `Netricsa:title_etricsa`, `FrontEndA:GTCooperative`. The atlases are in
  `FrontEnd.ssg`.

## Checks

`python tools/gui.py verify`, 0 problems:

| check | result |
|---|---|
| relocations whose field and value both lie inside the data | 506 of 506 |
| the relocation table ends exactly at end of file | yes |
| component-table entries that are relocated pointers | 328 of 328 |
| components whose index equals their position | 328 of 328 |
| screens (kind 0) | 29, of which 2 are nested (#164 in 160, #239 in 227) |
| components that belong to the latest screen, or are screens | 327 of 328; the other (#166) belongs to the outer screen 160 |
| words that resolve as `.tdb` hashes | 0 |
| kinds with a single record size | 7 of 7 |

## Open

- **Per-kind fields.** The fields past the header differ per kind, and so do
  the sub-records behind the pointers. Kind 1 is the common one: 225 of the
  88-byte records.
- **The last component.** It runs on into a long table of small integers
  (navigation or ordering) before the strings.
- **Which code draws each kind.** The `CcGUI` code (`0x800f4b3c`–`0x800fa270`)
  keeps the open GUIs on a list and logs "AddToList" / "RemoveFromList". The
  widget classes that interpret each kind are unread.

For the port, SE1 has its own menu system, so this file is a reference for
layout and bindings, not something to load.
