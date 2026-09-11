#!/usr/bin/env python3
"""
The game's one sound bank, `Sound/sfxbank1.spt` (table) + `sfxbank1.spd`
(data): Nintendo DSP-ADPCM, laid out as the Dolphin SDK's sound-pool tool
writes it.

    .spt  u32 count                                  (540)
          count x 28-byte headers  {u32 type,        0 one-shot, 1 looped
                                    u32 rate,        32000 on all 540
                                    u32 loopStart, u32 loopEnd,
                                    u32 end, u32 start, u32 0}
                                   addresses are nibble offsets into .spd
          count x 46-byte ADPCM    {s16 coef[16], u16 gain, u16 ps,
                                    s16 yn1, s16 yn2,
                                    u16 loopPs, s16 loopYn1, s16 loopYn2}
    .spd  8-byte frames: a header byte (predictor << 4 | scale) + 14 nibbles

Evidence: 4 + 540 x (28 + 46) = 39,964 bytes, the table's exact size; all 539
successive end addresses increase; each start lies just past the previous
end, on the first sample nibble of a frame; the last end, in bytes, is the
.spd's size.

The game names no sample. An entity names a sound by a 32-bit hash, which
Sound_FindSampleByHash (main.dol 0x8010ee70) looks up in a 479-row table at
0x8023adf4 of 24-byte rows {hash, n, u32* sample indices, ...}; a row with
n > 1 picks among variations. `extract` writes one WAV per sample index and
an index JSON that maps every hash to the samples it plays.

Usage:
    python tools/sound.py list
    python tools/sound.py extract -o build/sound
    python tools/sound.py verify
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import wave
from array import array
from pathlib import Path

SPT = Path("orig/files/Sound/sfxbank1.spt")
SPD = Path("orig/files/Sound/sfxbank1.spd")
DOL = Path("orig/sys/main.dol")
HASH_TABLE, HASH_ROWS, HASH_ROW = 0x8023ADF4, 479, 24
HEADER, ADPCM = 28, 46

# Streams: StreamData.dat is 441 back-to-back mono DSP-ADPCM channels,
# described by a table in main.dol (docs/sound.md, "Streams").
STREAMS = Path("orig/files/StreamData.dat")
STREAM_TABLE, STREAM_ROWS, STREAM_ROW = 0x802411F8, 441, 28
MUSIC_VOICES = 6        # a music hash starts rows i..i+5; even voice left, odd right
# FUN_8010ff3c calls FUN_80110218(0x2380, 6): the chunk size and voice count.
# Voice k of a track reads chunk c at track + (c * 6 + k) * 0x2380 -- the six
# voices are interleaved chunk by chunk, so a non-leading row's own offset is
# nominal. Speech rows are single voices, stored contiguously.
STREAM_CHUNK = 0x2380
SPEECH_BLOCK = 125      # rows per language: FUN_80111600 adds language * 0x7d
STREAM_RATE = 32000     # not stored per row; the SFX bank's rate (see docs)


def load_bank(spt=SPT):
    t = spt.read_bytes()
    n = struct.unpack_from(">I", t, 0)[0]
    base = 4 + HEADER * n
    out = []
    for i in range(n):
        typ, rate, ls, le, end, start, _ = struct.unpack_from(">7I", t, 4 + HEADER * i)
        a = base + ADPCM * i
        coefs = struct.unpack_from(">16h", t, a)
        gain, ps, yn1, yn2, lps, lyn1, lyn2 = struct.unpack_from(">HHhhHhh", t, a + 32)
        out.append({"index": i, "looped": typ == 1, "rate": rate,
                    "loop_start": ls, "loop_end": le, "start": start, "end": end,
                    "coefs": coefs, "ps": ps, "yn1": yn1, "yn2": yn2,
                    "loop_ps": lps, "loop_yn1": lyn1, "loop_yn2": lyn2})
    return out


def nibble_to_sample(addr):
    """Samples before nibble `addr`, counting from the start of .spd: each
    16-nibble frame holds 2 header nibbles and 14 samples."""
    return addr // 16 * 14 + max(0, addr % 16 - 2)


def decode(e, spd, swap=False):
    """int16 samples of one entry; `swap` reverses the nibble order within a
    byte, which verify uses as the known-wrong control."""
    c = e["coefs"]
    h1, h2 = e["yn1"], e["yn2"]
    start, end = e["start"], e["end"]
    out = array("h")
    frame = start // 16
    while frame * 8 < len(spd):
        base = frame * 8
        ps = spd[base]
        c1, c2 = c[2 * (ps >> 4 & 7)], c[2 * (ps >> 4 & 7) + 1]
        scale = 1 << (ps & 0xF)
        for k in range(2, 16):
            a = frame * 16 + k
            if a < start:
                continue
            if a > end:
                return out
            b = spd[base + k // 2]
            n = b >> 4 if (k % 2 == 0) != swap else b & 0xF
            if n >= 8:
                n -= 16
            s = (((n * scale) << 11) + 1024 + c1 * h1 + c2 * h2) >> 11
            s = -32768 if s < -32768 else 32767 if s > 32767 else s
            out.append(s)
            h2, h1 = h1, s
        frame += 1
    return out


def _dol_reader(dol=DOL):
    d = dol.read_bytes()
    offs = struct.unpack_from(">18I", d, 0)
    addr = struct.unpack_from(">18I", d, 0x48)
    size = struct.unpack_from(">18I", d, 0x90)

    def at(va):
        for o, a, s in zip(offs, addr, size):
            if s and a <= va < a + s:
                return o + va - a
        raise ValueError("0x%08x not in main.dol" % va)
    return d, at


def hash_table(dol=DOL):
    """{hash: [sample index, ...]} from main.dol's sample table."""
    d, at = _dol_reader(dol)
    o = at(HASH_TABLE)
    out = {}
    for i in range(HASH_ROWS):
        h, n, lst = struct.unpack_from(">3I", d, o + HASH_ROW * i)
        out[h] = list(struct.unpack_from(">%dI" % n, d, at(lst))) if n else []
    return out


def stream_table(dol=DOL):
    """The 441 rows at 0x802411f8: {u32 adpcm, u32 offset, u32 size, u32 hash,
    u32 music, s16* coefs, u16 ps, u16 0}. Returned in the entry shape
    decode() takes, with nibble addresses into StreamData.dat."""
    d, at = _dol_reader(dol)
    out = []
    for i in range(STREAM_ROWS):
        adpcm, off, size, h, music, cp, ps, _ = struct.unpack_from(
            ">IIIIIIHH", d, at(STREAM_TABLE + STREAM_ROW * i))
        out.append({"index": i, "adpcm": adpcm, "offset": off, "size": size,
                    "hash": h, "music": bool(music),
                    "coefs": struct.unpack_from(">16h", d, at(cp)), "ps": ps,
                    "yn1": 0, "yn2": 0, "start": off * 2 + 2,
                    "end": (off + size) * 2 - 1})
    return out


def stream_layout(rows):
    """(music tracks, speech lines). A track is MUSIC_VOICES rows played
    together as stereo pairs; speech rows come in SPEECH_BLOCK-row language
    blocks."""
    music = [r for r in rows if r["music"]]
    tracks = [music[k:k + MUSIC_VOICES] for k in range(0, len(music), MUSIC_VOICES)]
    speech = [r for r in rows if not r["music"]]
    first = speech[0]["index"] if speech else 0
    lines = [(r, (r["index"] - first) // SPEECH_BLOCK, (r["index"] - first) % SPEECH_BLOCK)
             for r in speech]
    return tracks, lines


def voice_bytes(track, k, data, nchunks=None):
    """Voice k's own ADPCM bytes, de-interleaved from its track."""
    base, n = track[0]["offset"], track[k]["size"] // STREAM_CHUNK
    v = len(track)
    return b"".join(data[base + (c * v + k) * STREAM_CHUNK:
                         base + (c * v + k + 1) * STREAM_CHUNK]
                    for c in range(n if nchunks is None else min(n, nchunks)))


def decode_voice(track, k, data, nchunks=None):
    buf = voice_bytes(track, k, data, nchunks)
    return decode({"coefs": track[k]["coefs"], "yn1": 0, "yn2": 0,
                   "start": 2, "end": len(buf) * 2 - 1}, buf)


def _corr(a, b):
    n = min(len(a), len(b))
    if n < 2:
        return 0.0
    ma, mb = sum(a[:n]) / n, sum(b[:n]) / n
    sab = sum((x - ma) * (y - mb) for x, y in zip(a[:n], b[:n]))
    sa = sum((x - ma) ** 2 for x in a[:n]) ** 0.5
    sb = sum((y - mb) ** 2 for y in b[:n]) ** 0.5
    return sab / (sa * sb) if sa and sb else 0.0


def write_wav(path, samples, rate, right=None):
    """Mono, or stereo when `right` is given (interleaved L/R)."""
    if right is not None:
        n = min(len(samples), len(right))
        inter = array("h", [0]) * (2 * n)
        inter[0::2] = samples[:n]
        inter[1::2] = right[:n]
        samples = inter
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2 if right is not None else 1)
        w.setsampwidth(2)
        w.setframerate(rate)
        if sys.byteorder == "big":
            samples = array("h", samples)
            samples.byteswap()
        w.writeframes(samples.tobytes())


def roughness(s):
    """Mean |step| over mean |level|: real audio at 32 kHz is smooth (well
    under 1); decoding with the wrong predictors gives noise (about 1.4)."""
    if len(s) < 2:
        return 0.0
    lvl = sum(abs(x) for x in s) / len(s)
    step = sum(abs(b - a) for a, b in zip(s, s[1:])) / (len(s) - 1)
    return step / lvl if lvl else 0.0


# --- subcommands ------------------------------------------------------------

def cmd_list(args):
    bank = load_bank()
    users = {}
    for h, idx in hash_table().items():
        for i in idx:
            users.setdefault(i, []).append(h)
    for e in bank:
        n = nibble_to_sample(e["end"] + 1) - nibble_to_sample(e["start"])
        print("%3d %s %5d Hz %7d samples %6.2fs  hashes %s"
              % (e["index"], "loop" if e["looped"] else "    ", e["rate"], n,
                 n / e["rate"], " ".join("%08x" % h for h in users.get(e["index"], []))))
    return 0


def cmd_extract(args):
    bank, spd = load_bank(), SPD.read_bytes()
    args.output.mkdir(parents=True, exist_ok=True)
    samples = []
    for e in bank:
        s = decode(e, spd)
        name = "sfx_%03d.wav" % e["index"]
        write_wav(args.output / name, s, e["rate"])
        rec = {"index": e["index"], "file": name, "rate": e["rate"],
               "samples": len(s), "looped": e["looped"]}
        if e["looped"]:
            first = nibble_to_sample(e["start"])
            rec["loop"] = [nibble_to_sample(e["loop_start"]) - first,
                           nibble_to_sample(e["loop_end"] + 1) - first]
        samples.append(rec)
    index = {"samples": samples,
             "hashes": {"%08x" % h: idx for h, idx in sorted(hash_table().items())}}
    (args.output / "index.json").write_text(json.dumps(index, indent=1))
    print("%d samples -> %s, %d hashes in index.json"
          % (len(samples), args.output, len(index["hashes"])))
    return 0


def cmd_streams(args):
    rows, data = stream_table(), STREAMS.read_bytes()
    tracks, lines = stream_layout(rows)
    out = args.output
    (out / "music").mkdir(parents=True, exist_ok=True)
    (out / "speech").mkdir(parents=True, exist_ok=True)
    index = {"rate": STREAM_RATE, "music": [], "speech": []}
    for t, tr in enumerate(tracks):
        layers = []
        for k in range(0, len(tr) - 1, 2):
            name = "music/track%02d_layer%d.wav" % (t, k // 2)
            write_wav(out / name, decode_voice(tr, k, data), STREAM_RATE,
                      right=decode_voice(tr, k + 1, data))
            layers.append(name)
        index["music"].append({"track": t, "hash": "%08x" % tr[0]["hash"],
                               "rows": [r["index"] for r in tr], "layers": layers,
                               "seconds": round(tr[0]["size"] // 8 * 14 / STREAM_RATE, 2)})
        print("track %2d: %d layers, %.1f s" % (t, len(layers), index["music"][-1]["seconds"]))
    for r, lang, j in lines:
        name = "speech/lang%d_%03d.wav" % (lang, j)
        s = decode(r, data)
        write_wav(out / name, s, STREAM_RATE)
        index["speech"].append({"row": r["index"], "lang": lang, "line": j,
                                "hash": "%08x" % r["hash"], "file": name,
                                "seconds": round(len(s) / STREAM_RATE, 2)})
    (out / "streams.json").write_text(json.dumps(index, indent=1))
    print("%d music tracks, %d speech lines -> %s" % (len(tracks), len(lines), out))
    return 0


def verify_streams():
    """Structural checks on StreamData.dat; exact where the data allows."""
    rows, data = stream_table(), STREAMS.read_bytes()
    tracks, lines = stream_layout(rows)
    problems = []
    cont = sum(1 for a, b in zip(rows, rows[1:]) if b["offset"] == a["offset"] + a["size"])
    end = rows[-1]["offset"] + rows[-1]["size"]
    # the stored initial ps must be the first frame's header; the game stores
    # 0 for the non-leading voices of a track (the DSP reloads ps per frame)
    leads = [tr[0] for tr in tracks] + [r for r, _, _ in lines]
    ps_ok = sum(1 for r in leads if data[r["offset"]] == r["ps"])
    others_zero = sum(1 for tr in tracks for r in tr[1:] if r["ps"] == 0)
    print("  stream rows          %d (%d music in %d tracks, %d speech in %d language blocks)"
          % (len(rows), sum(len(t) for t in tracks), len(tracks), len(lines),
             len({lang for _, lang, _ in lines})))
    print("  contiguous           %d / %d, end 0x%x, file 0x%x"
          % (cont, len(rows) - 1, end, len(data)))
    print("  ps == first header   %d / %d leading rows; %d / %d other voices store 0"
          % (ps_ok, len(leads), others_zero, sum(len(t) - 1 for t in tracks)))
    if cont != len(rows) - 1 or end != len(data):
        problems.append("stream rows are not back to back over the whole file")
    if ps_ok != len(leads):
        problems.append("%d leading rows whose ps is not their first header" % (len(leads) - ps_ok))
    if any(len(t) != MUSIC_VOICES for t in tracks):
        problems.append("a music track without %d voices" % MUSIC_VOICES)
    if any(r["size"] % STREAM_CHUNK for r in rows):
        problems.append("a row not a whole number of 0x2380-byte chunks")
    # Layout: a layer's left and right voices correlate strongly only when
    # de-interleaved (track 0: 0.94; read contiguously every pair is ~0).
    best = []
    for tr in tracks:
        head = [decode_voice(tr, k, data, 8) for k in range(len(tr))]
        best.append(max(_corr(head[k], head[k + 1]) for k in range(0, len(tr) - 1, 2)))
    good = sum(1 for c in best if c > 0.5)
    print("  stereo layer pairs   best correlation per track %s; %d / %d above 0.5"
          % (" ".join("%.2f" % c for c in best), good, len(best)))
    if good < len(best) - 1:
        problems.append("de-interleaved stereo pairs do not correlate")
    return problems


def cmd_verify(args):
    bank, spd = load_bank(), SPD.read_bytes()
    table = hash_table()
    problems = []
    rough, ps_ok, clipped = [], 0, 0
    loop_tested = loop_ok = swap_ok = loop_ps_ok = 0
    total = 0
    for e in bank:
        s = decode(e, spd)
        total += len(s)
        if not s:
            problems.append("sample %d decodes empty" % e["index"])
            continue
        # the table's initial predictor/scale must be the first frame's header
        ps_ok += spd[e["start"] // 16 * 8] == e["ps"]
        clipped += sum(1 for x in s if x in (-32768, 32767))
        rough.append(roughness(s[:20000]))
        if e["looped"]:
            if not (e["start"] <= e["loop_start"] < e["loop_end"] <= e["end"]):
                problems.append("sample %d loop outside its data" % e["index"])
                continue
            # Exact test: the encoder stored the decoder's history at the loop
            # point. Decoding up to it must reproduce both values -- and the
            # swapped nibble order must not.
            L = nibble_to_sample(e["loop_start"]) - nibble_to_sample(e["start"])
            want = (e["loop_yn1"], e["loop_yn2"])
            if L >= 2:
                loop_tested += 1
                loop_ok += (s[L - 1], s[L - 2]) == want
                w = decode(e, spd, swap=True)
                swap_ok += (w[L - 1], w[L - 2]) == want
            loop_ps_ok += spd[e["loop_start"] // 16 * 8] == e["loop_ps"]
    if ps_ok != len(bank):
        problems.append("%d entries whose ps is not their first frame's header"
                        % (len(bank) - ps_ok))
    refs = [i for idx in table.values() for i in idx]
    bad = [i for i in refs if i >= len(bank)]
    if bad:
        problems.append("%d table references past the bank" % len(bad))
    rough.sort()
    print("  samples              %d (%d looped)" % (len(bank), sum(e["looped"] for e in bank)))
    print("  decoded              %s samples, %.1f minutes"
          % (format(total, ","), total / 32000 / 60))
    print("  roughness median     %.2f  (p90 %.2f; noise is ~1.4)"
          % (rough[len(rough) // 2], rough[int(len(rough) * 0.9)]))
    print("  ps == first header   %d / %d" % (ps_ok, len(bank)))
    print("  clipped samples      %s (%.4f%%)" % (format(clipped, ","), 100 * clipped / max(total, 1)))
    print("  loop ps == its frame header    %d / %d" % (loop_ps_ok, sum(e["looped"] for e in bank)))
    print("  loop history reproduced        %d / %d  (swapped nibbles: %d)"
          % (loop_ok, loop_tested, swap_ok))
    print("  hash rows            %d, %d sample refs, %d distinct samples used"
          % (len(table), len(refs), len(set(refs))))
    problems += verify_streams()
    for p in problems[:20]:
        print("  PROBLEM " + p)
    if loop_ok != loop_tested or swap_ok:
        problems.append("loop history: %d/%d reproduced, swapped control %d"
                        % (loop_ok, loop_tested, swap_ok))
    print("%d problems" % len(problems))
    return 1 if problems else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list", help="every sample with its length and hashes")
    p.set_defaults(func=cmd_list)
    p = sub.add_parser("extract", help="one WAV per sample plus index.json")
    p.add_argument("-o", "--output", type=Path, default=Path("build/sound"))
    p.set_defaults(func=cmd_extract)
    p = sub.add_parser("streams", help="StreamData.dat: music as stereo WAVs, speech as mono")
    p.add_argument("-o", "--output", type=Path, default=Path("build/sound"))
    p.set_defaults(func=cmd_streams)
    p = sub.add_parser("verify", help="decode everything and check it is audio")
    p.set_defaults(func=cmd_verify)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
