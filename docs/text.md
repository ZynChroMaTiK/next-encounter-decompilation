# Text — the `.tdb` databases and NETRICSA's messages

**Status.** Decoded and verified. `tools/text.py` does four jobs:

- `list` prints one database.
- `json` writes `build/text/<lang>.json`.
- `messages` writes an SE1 message file for each MessageHolder, in each
  language, to `build/text/messages/<lang>/<level>_<id>.txt`.
- `verify` checks everything below.

## Files and languages

There are four databases at the disc root: `EnglishUSA.tdb`, `German.tdb`,
`french.tdb` and `EnglishEUR.tdb`.

- `Language_GetName` (`main.dol 0x80159834`) names the current language
  from a three-entry table at `0x802207d4`: **0 EnglishUSA, 1 German,
  2 French**.
- `Language_Set` (`0x80159804`) clamps the language to 0–2.
- **EnglishEUR is never loaded by this build.** It is the PAL English, and
  differs from EnglishUSA on 33 strings, mostly spelling ("Armour",
  "Centre", "Rome DM 4" for a USA typo "Rone DM 4").

The same language index picks the speech block in `StreamData.dat`
(`docs/sound.md`). There, German is a copy of the English voice.

## Format

```c
// big-endian
u32 count;                          // 858
u32 nVars;                          // 2
struct {
    u32 text;                       // file offset of a NUL-terminated string
    u32 hasVars;                    // 1: the text contains [button] or [val]
    u32 hash;                       // what callers look up
} rec[count];
u32 varName[nVars];                 // offsets of "button" and "val"
char strings[];                     // rec[0..count) texts, packed, in order;
                                    // then the variable names, 4-byte
                                    // aligned, 16 bytes each, ending the file
```

The strings are Latin-1: "Français", "Sprache auswählen". All four files
hold the same 858 hashes in the same order. Only the texts, and so the
offsets, differ.

## Lookup — by a hash the entity already holds

`Text_FindHashedString` (`0x80159b2c`) scans `rec[].hash` for the hash it
is given and returns the record's index.

- If the hash is absent, it logs "Failed to find string in
  FindHashedString()" and returns **0x37**.
- Callers use **0x38** for hash 0.
- Record 0x37 is "HASH LOOKUP FAILED ON THIS STRING" and record 0x38 is
  "EMPTY STRING USED FOR HASH LOOKUP", in every language. That is an
  exact check that the index the code returns is the record index.

Nothing in this path hashes a string. Entities carry the hash itself, so
the lookup never needs the hash function. The designers' key strings, such
as `NETRISCA_ALEVEL_10_2_INTRO`, ride along in MessageHolder props and are
never read. Common string hashes (×31, djb2, sdbm, FNV, CRC-32, and a
brute-forced multiplier) do not reproduce the stored values from those keys.

## MessageHolder (class 4130) — NETRICSA's messages

`CcMessageHolder_Init` (`0x8007d3a4`) resolves two hashes when a level
loads.

```c
struct CcMessageHolderProps {  // class 4130, 56-68 bytes
    u32 active;                // +0x00  62 of 63
    u32 text;                  // +0x04  .tdb hash: the message body
    u32 _08;                   // +0x08  flag, 41 of 63
    u32 title;                 // +0x0c  .tdb hash: the title
    u16 _10;                   // +0x10  flag, 41 of 63; set -> FUN_801664c4 at init
    ...                        // then the NETRISCA_* key string
};
```

On a trigger it logs "TRIGGER received, message sent to NETRICSA". So the
game names it itself: this is SE1's NETRICSA computer message, and the
mapping is one to one:

- SE1's `MessageHolder` takes a message file.
- `GameMP/CompMessage.cpp` reads that file as `SUBJECT`, a line, `IMAGE`,
  `none`, `TEXT`, and then text to end of file.
- `text.py messages` writes exactly that shape.
- `tools/world.py` records the title, the text and the file per level
  (`docs/world-conversion.md`, "Sounds and messages").

## Verification (`python tools/text.py verify`)

| check | result |
|---|---|
| layout | in all four files, the strings start right after the tables; 859 of 859 strings are packed (858 texts, then the two 16-byte variable slots), and the last slot ends the file exactly |
| `hasVars` | equals "the text contains `[button]` or `[val]`" on 858 of 858 records, in every file |
| order | the same 858 unique hashes in the same order in all four |
| fallbacks | records 0x37 and 0x38 are the two strings the code's return values name |
| MessageHolders | 63 of 63 resolve their title and text in all three languages the game loads |
| pairing | 10 designer entity names contain their resolved title ("MessageHolder Temple Of Steam" → "Temple of Steam"); read one record off, 0 do |

The pairing row is what fixed the record layout. Read from offset 4,
`{hash, text, flag}` looks just as regular, but it pairs every hash with
the next record's text.

## Open

- **The remaining callers.** HUD prompts, menus and pickups ("Health
  +[val]", "Treasure! [val] points!") reach the same table. How `[button]`
  and `[val]` are substituted is unread.
- **Speech transcripts.** No SoundHolder sets a subtitle hash, and whether
  the `.tdb` holds transcripts of the speech lines at all is unchecked.
- **The `+0x08` / `+0x10` MessageHolder flags.** They are set together on
  41 holders. One of them is likely "add to NETRICSA's log" and the other
  "show at once", but that is unconfirmed.
