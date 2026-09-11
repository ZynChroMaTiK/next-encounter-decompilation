# Sound — effects, music and speech, and how entities name a sound

**Status.** Everything on the disc is decoded and verified.

- `tools/sound.py extract` writes the effects bank: 540 samples, 14.5
  minutes, plus `build/sound/index.json`.
- `tools/sound.py streams` writes `StreamData.dat`: 11 music tracks as 33
  stereo layer WAVs, 375 speech lines (21.8 minutes) in three languages, and
  `build/sound/streams.json`.

## The bank: `Sound/sfxbank1.spt` + `sfxbank1.spd`

It is Nintendo DSP-ADPCM, laid out as the Dolphin SDK's sound-pool tool writes
it: a table (`.spt`) and the frames (`.spd`).

```c
// .spt, big-endian
u32 count;                         // 540
struct Header {                    // count of these, 28 bytes each
    u32 type;                      // 0 one-shot (473), 1 looped (67)
    u32 rate;                      // 32000 on all 540
    u32 loopStart, loopEnd;        // nibble addresses into .spd
    u32 end, start;                // nibble addresses; start = first sample nibble
    u32 _0;                        // 0 on all
} hdr[count];
struct Adpcm {                     // then count of these, 46 bytes each
    s16 coef[16];                  // 8 predictor pairs
    u16 gain;
    u16 ps;                        // initial predictor/scale
    s16 yn1, yn2;                  // initial history
    u16 loopPs;                    // predictor/scale at the loop point
    s16 loopYn1, loopYn2;          // history at the loop point
} adpcm[count];
```

`.spd` is a run of 8-byte frames: one header byte (predictor index << 4 | scale
exponent) and 14 four-bit samples, high nibble first. An address is a nibble
offset, so every 16 nibbles hold 2 header nibbles and 14 samples. Decoding is
the standard DSP-ADPCM step:
`s = ((n << scale) << 11) + 1024 + c1·h1 + c2·h2 >> 11`, clamped to 16 bits.

### Verification (`python tools/sound.py verify`)

| check | result |
|---|---|
| table size | 4 + 540 × (28 + 46) = 39,964 bytes, the file's exact size |
| addresses | all 539 successive ends increase; each start is the first sample nibble after the previous end; the last end, in bytes, is the `.spd`'s size |
| table ↔ data pairing | each entry's `ps` equals the header byte of its first frame: 540 / 540 |
| loops | each looped entry's `loopPs` equals the header byte of the frame holding its loop start: 67 / 67 |
| decoder, exact | decoding up to the loop start reproduces the stored `loopYn1`/`loopYn2` on 32 of 32 testable loops, and on 0 with the nibble order swapped |
| clipping | 0.0074% of samples |

Two weaker tests were tried first and dropped. "Smoother than a neighbour's
predictors" and "smoother than swapped nibbles" both failed, even on correct
output. The predictor is a smoothing filter, so it makes almost any input look
like audio. Only the exact loop-history match can tell a right decode from a
wrong one.

## How the game names a sound

No sample has a name anywhere on the disc. Entities name a sound by a 32-bit
hash:

- `Sound_FindSampleByHash` (`main.dol 0x8010ee70`) scans a **479-row table at
  `0x8023adf4`**. Each row is 24 bytes: `{u32 hash, u32 n, u32* samples, u32 1,
  u32 flags, u32}`.
- `samples` lists `n` bank indices. Rows with `n > 1` (42 of them) pick one
  variation per play.
- Between them the rows reference all 540 samples, each exactly once.
- A hash that is not in the table is logged ("Failed to find sample in
  FindHash") and plays nothing.

Movers are the first consumer (`docs/world-conversion.md`, "Mover motion").
2,117 of the 2,173 START/LOOP/END hashes they set are in the table. The other
56 are missing in the game too.

## Streams: `StreamData.dat` — music and speech

The 112 MB file is 441 DSP-ADPCM channels. A table in `main.dol` describes
them, and `tools/sound.py streams` extracts them.

```c
struct StreamRow {             // 441 of them, 28 bytes each, at 0x802411f8
    u32  adpcm;                // 1 on all
    u32  offset, size;         // bytes into StreamData.dat
    u32  hash;                 // what entities name
    u32  music;                // 1 on the first 66 rows
    s16* coefs;                // 16 predictor coefficients, 32 bytes each
    u16  ps;                   // initial predictor/scale; 0 on non-leading voices
    u16  _0;
};
```

- **Music: 11 tracks of 6 voices, as 3 stereo layers.** A Music entity's hash
  names the first row of a track, and the game starts that row and the next
  five together (`FUN_80111250`). Even voices pan left and odd voices right,
  giving three stereo layers. The game mixes them by fight intensity against
  `base.cfg` `LevelMusic.ThreshMedium`/`ThreshHeavy` (100 / 1000).
- **The six voices are interleaved in 0x2380-byte chunks.** `FUN_8010ff3c`
  calls `FUN_80110218(0x2380, 6)`, which sets the chunk size, the voice
  count, and a channel stride equal to the chunk. Voice k reads chunk c at
  `track + (c·6 + k)·0x2380`, so a non-leading row's own offset is nominal.
  The file shows the same thing: at the end of each track come six zero
  runs 0x2380 apart, one padded final chunk per voice.
- **Speech: 375 rows, three languages of 125.** `Stream_PlayIndex`
  (`0x80111600`) adds `language × 0x7d` to the row index. The language is
  the value `Language_Set` (`0x80159804`) clamps to 0–2, and
  `Language_GetName` (`0x80159834`) names it from the table at
  `0x802207d4`: **0 EnglishUSA, 1 German, 2 French**. Block 1 is a
  byte-for-byte copy of block 0 on all 125 lines. So this disc carries
  English and French voice, and a German player hears English. A
  SoundHolder names a line by the hash of a block-0 row: the game finds the
  row, then adds the language offset.
- **Rate.** No row stores one; 32 kHz is assumed, the effects bank's rate. It
  makes a track part exactly 80.0 s and a chunk 0.5 s, and voice lines 2–7.5
  s long.

| check (`python tools/sound.py verify`) | result |
|---|---|
| rows back to back | 440 / 440, ending at the file's exact size |
| stored `ps` = the first frame header | 386 / 386 leading rows (11 track heads plus 375 speech lines) |
| each layer's left and right voices correlate once de-interleaved | best pair per track 0.72 to 1.00 on 10 of 11 tracks; about 0 if read contiguously |

Two tracks are not three distinct stereo layers:

- **Track 10** carries one stereo recording in all three layers (left/right
  correlate at 0.88; the layers are identical). It is a track with no
  intensity levels.
- **Track 9** is unexplained. Voices k and k+3 are exact copies, and voices
  0, 1 and 2 do not correlate with each other. So its layers do not pair the
  way the other tracks' do.

Read contiguously, every stereo pair correlates at about 0. This test is what
settles the interleaving, as a fact of the data and not only of the code.
The smoothness and predictor-choice tests could not tell the two layouts
apart.

## SoundHolder (class 4100)

`SoundHolder` is NE's own name for the class: its init
(`CcSoundHolder_Init`, `0x800833a8`) logs it, and the init's warnings name
the fields.

```c
struct CcSoundHolderProps {  // class 4100, 52 bytes
    u32 active;              // +0x00  1: play on the first update (20 of 213)
    u32 destroyable;         // +0x04  "Parent %d died and I am destroyable"; 0 on all
    u32 parent;              // +0x08  entity id; set on 1
    u32 sample;              // +0x0c  effect hash -> Sound_FindSampleByHash; 0 = none
    f32 falloff;             // +0x10  "hotspot radius larger than falloff"
    f32 hotspot;             // +0x14
    f32 volume;              // +0x18  "volume greater than 1.0"
    u32 speech;              // +0x1c  stream hash -> Stream_PlayIndex
    u32 subtitle;            // +0x20  .tdb hash; 0 on all 213
};
```

- `CcSoundHolder_Play` (`0x80083610`) does three things in order:
  - plays the sample at the entity's position, with volume, hot-spot and
    fall-off;
  - starts the speech stream, a call that takes no position;
  - shows the subtitle when one is set ("Displaying subtitle"), through
    `Text_FindHashedString` (`docs/text.md`).
- No SoundHolder on the disc sets a subtitle, so no speech in this build is
  subtitled.
- Init corrects bad values instead of refusing them:
  - a fall-off below the hot-spot becomes hot-spot + 1;
  - one within 0.1 of the hot-spot gains 0.1;
  - the volume is held to 0..1. 13 holders ask for 2.0.
- Across the disc:
  - 116 holders set a sample. 108 are in the table; the other 8 are
    missing in the game too.
  - 96 set speech. 93 name a row; 3 do not.
  - 2 set both, and 3 set neither.

## For SE1

SE1 plays WAV, so `build/sound/sfx_NNN.wav` (16-bit mono, 32 kHz) loads
as-is. So do the music layers, which are 16-bit stereo:
`build/sound/music/trackNN_layerK.wav` feed `MusicHolder`'s Music
Light/Medium/Heavy (`docs/world-conversion.md`, "Music"). Speech lines,
`build/sound/speech/langL_NNN.wav`, are mono. NE's SoundHolder is SE1's
`SoundHolder` field for field (Sound, Fall-off, Hot-spot, Volume, Auto
start, Destroyable). SE1's defaults, 100 / 50 / 1.0, are also NE's most
common values (96 of 213 holders). SE1 loops a sound when it is played with the loop flag, not from loop
points in the file. The sample-accurate loop points are kept in `index.json`
(`loop: [start, end]`), for a looped sound that must not restart from zero.

## Open

- **Names.** The disc has none, so files are named by bank index. Names will
  have to come from what uses each hash: the entity class and the designer's
  entity name.
- **Other consumers.** The weapon and enemy code presumably use the same
  hashes. They have not been read yet.
- **Speech transcripts.** No SoundHolder sets a subtitle. Whether the
  `.tdb` holds transcripts of the speech lines at all is unchecked.
- **Video.** `Video/*.bik` are Bink files (35 of them) and need no decoding
  of ours.
