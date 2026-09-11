#!/usr/bin/env python3
"""
The localisation databases -- EnglishUSA.tdb, German.tdb, french.tdb, and
EnglishEUR.tdb, which this NTSC-U build never loads. See docs/text.md.

    python tools/text.py list [--lang German] [--grep Temple]
    python tools/text.py json          # build/text/<lang>.json
    python tools/text.py messages      # SE1 computer messages, one per MessageHolder
    python tools/text.py verify

Format, big-endian:

    u32 count;                              // 858
    u32 nVars;                              // 2
    struct { u32 text, hasVars, hash; } rec[count];
    u32 varName[nVars];                     // offsets of "button", "val"
    char strings[];                         // NUL-terminated, in record order

The game never hashes a string at runtime. Entities store the hash, and
Text_FindHashedString (main.dol 0x80159b2c) scans rec[].hash for the record
index -- 0x37, "HASH LOOKUP FAILED ON THIS STRING", when it is absent. Callers
use 0x38, "EMPTY STRING USED FOR HASH LOOKUP", for hash 0.
"""

from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

FILES = {"EnglishUSA": "EnglishUSA.tdb", "German": "German.tdb",
         "French": "french.tdb", "EnglishEUR": "EnglishEUR.tdb"}
# Language_GetName (0x80159834) indexes {EnglishUSA, German, French} by the
# language Language_Set (0x80159804) clamps to 0..2 -- the same index
# Stream_PlayIndex multiplies by 125 to pick a speech block.
LANGS = ("EnglishUSA", "German", "French")
FAILED, EMPTY = 0x37, 0x38
VAR_SLOT = 16          # each variable name's field, after the strings
MESSAGE_CLASS = 4130
OUT = ROOT / "build" / "text"


def load(lang):
    d = (ROOT / "orig" / "files" / FILES[lang]).read_bytes()
    n, nv = struct.unpack_from(">II", d, 0)

    def s(o):
        return d[o:d.index(b"\0", o)].decode("latin-1")

    recs = []
    for i in range(n):
        off, flag, h = struct.unpack_from(">III", d, 8 + 12 * i)
        recs.append({"hash": h, "offset": off, "vars": flag, "text": s(off)})
    vo = list(struct.unpack_from(">%dI" % nv, d, 8 + 12 * n))
    return {"lang": lang, "raw": d, "count": n, "var_offsets": vo,
            "vars": [s(o) for o in vo], "records": recs}


_CACHE = {}


def _db(lang):
    if lang not in _CACHE:
        db = load(lang)
        db["index"] = {r["hash"]: i for i, r in enumerate(db["records"])}
        _CACHE[lang] = db
    return _CACHE[lang]


def find(h, lang="EnglishUSA"):
    """The record index the game resolves a hash to."""
    if not h:
        return EMPTY
    return _db(lang)["index"].get(h, FAILED)


def lookup(h, lang="EnglishUSA"):
    return _db(lang)["records"][find(h, lang)]["text"]


def resolves(h, lang="EnglishUSA"):
    return bool(h) and h in _db(lang)["index"]


def message_file(level, entity_id, lang="EnglishUSA"):
    """Where `messages` writes a MessageHolder's SE1 message. Named by level
    and entity id: one MessageHolder has no key string, and keys repeat."""
    return "build/text/messages/%s/%s_%d.txt" % (lang, level, entity_id)


def se1_message(title, body):
    """SE1's computer-message file, as GameMP/CompMessage.cpp reads it:
    SUBJECT, IMAGE ("none"), TEXT, then the text to end of file."""
    body = body.replace("\r\n", "\n").replace("\n", "\r\n")
    return ("SUBJECT\r\n%s\r\nIMAGE\r\nnone\r\nTEXT\r\n%s\r\n"
            % (title, body)).encode("latin-1")


def _levels(paths):
    roots = [Path(p) for p in paths] or [ROOT / "orig" / "files" / "Levels"]
    out = []
    for r in roots:
        out.extend(sorted(r.rglob("*.ssw")) if r.is_dir() else [r])
    return out


def message_holders(paths=()):
    """(level, entity) for every MessageHolder (class 4130) on the disc."""
    from entity import Container, entities
    for p in _levels(paths):
        for e in entities(Container(p)):
            if e["cls"] == MESSAGE_CLASS:
                yield p.stem, e


# --- subcommands ------------------------------------------------------------

def cmd_list(args):
    db = _db(args.lang)
    rx = re.compile(args.grep, re.I) if args.grep else None
    for i, r in enumerate(db["records"]):
        if rx and not rx.search(r["text"]):
            continue
        print("%4d  %08x  %s  %s" % (i, r["hash"], "v" if r["vars"] else " ",
                                     r["text"][:args.width].replace("\n", " | ")))
    return 0


def cmd_json(args):
    OUT.mkdir(parents=True, exist_ok=True)
    for lang in FILES:
        db = _db(lang)
        dest = OUT / ("%s.json" % lang)
        dest.write_text(json.dumps({
            "lang": lang, "vars": db["vars"],
            "strings": [{"index": i, "hash": "%08x" % r["hash"],
                         "vars": bool(r["vars"]), "text": r["text"]}
                        for i, r in enumerate(db["records"])]},
            indent=1, ensure_ascii=False), encoding="utf-8")
        print("%s: %d strings -> %s" % (lang, db["count"], dest))
    return 0


def cmd_messages(args):
    n = 0
    for level, e in message_holders(args.paths):
        for lang in LANGS:
            dest = ROOT / message_file(level, e["id"], lang)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(se1_message(lookup(e["title_hash"], lang),
                                         lookup(e["text_hash"], lang)))
        n += 1
    print("%d MessageHolders x %d languages -> %s" % (n, len(LANGS), OUT / "messages"))
    return 0


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def cmd_verify(args):
    problems = []
    base = _db("EnglishUSA")
    print("  %-12s %6s %5s %11s %11s %9s %9s" % ("file", "count", "vars", "packed",
                                                 "flag=[var]", "order=USA", "fallbacks"))
    for lang in FILES:
        db = _db(lang)
        d, n, recs, vo = db["raw"], db["count"], db["records"], db["var_offsets"]
        if recs[0]["offset"] != 8 + 12 * n + 4 * len(vo):
            problems.append("%s: strings do not start right after the tables" % lang)
        # every string follows the previous one's NUL; then the variable
        # names, 4-byte aligned, in 16-byte slots that end the file exactly
        seq = [r["offset"] for r in recs]
        texts = [r["text"] for r in recs]
        packed = sum(seq[i + 1] == seq[i] + len(texts[i].encode("latin-1")) + 1
                     for i in range(len(seq) - 1))
        if packed != len(seq) - 1:
            problems.append("%s: %d strings not packed" % (lang, len(seq) - 1 - packed))
        end = seq[-1] + len(texts[-1].encode("latin-1")) + 1
        slots = [((end + 3) & ~3) + VAR_SLOT * k for k in range(len(vo))]
        if vo != slots or len(d) != slots[-1] + VAR_SLOT or any(
                any(d[o + len(v) + 1:o + VAR_SLOT]) for o, v in zip(vo, db["vars"])):
            problems.append("%s: variable names not in aligned 16-byte slots at the end" % lang)
        else:
            packed += len(vo)
        seq += vo
        flag_ok = sum(bool(r["vars"]) == any("[%s]" % v in r["text"] for v in db["vars"])
                      for r in recs)
        if flag_ok != n:
            problems.append("%s: hasVars disagrees with the text on %d" % (lang, n - flag_ok))
        order = [r["hash"] for r in recs] == [r["hash"] for r in base["records"]]
        if not order or len(db["index"]) != n:
            problems.append("%s: hashes not the USA order, or not unique" % lang)
        fb = (recs[FAILED]["text"] == "HASH LOOKUP FAILED ON THIS STRING"
              and recs[EMPTY]["text"] == "EMPTY STRING USED FOR HASH LOOKUP")
        if not fb:
            problems.append("%s: fallback records are not at 0x37/0x38" % lang)
        print("  %-12s %6d %5d %5d / %-4d %5d / %-4d %9s %9s"
              % (FILES[lang], n, len(vo), packed, len(seq) - 1, flag_ok, n,
                 "yes" if order else "NO", "yes" if fb else "NO"))
    eur = sum(a["text"] != b["text"] for a, b in
              zip(_db("EnglishEUR")["records"], base["records"]))
    print("  EnglishEUR differs from EnglishUSA on %d strings (spelling: Armour, Centre)" % eur)

    # MessageHolders: both hashes must resolve in every language the game
    # loads. Pairing check: a designer's entity name often repeats the
    # title ("MessageHolder Temple Of Steam" -> "Temple of Steam"); reading
    # the hash one record off pairs it with the neighbouring title instead.
    mh = resolved = named = named_shifted = 0
    for level, e in message_holders(args.paths):
        mh += 1
        ok = all(resolves(e[k], lang) for k in ("title_hash", "text_hash") for lang in LANGS)
        resolved += ok
        if not ok:
            problems.append("%s: MessageHolder %d does not resolve" % (level, e["id"]))
            continue
        i = find(e["title_hash"])
        name = _norm(e["name"])
        named += bool(name) and _norm(base["records"][i]["text"]) in name
        named_shifted += bool(name) and _norm(base["records"][i + 1]["text"]) in name
    print("  MessageHolders           %d" % mh)
    print("  title + text resolve     %d / %d  (all three languages)" % (resolved, mh))
    print("  entity name has title    %d  (read one record off: %d)" % (named, named_shifted))
    if named <= named_shifted:
        problems.append("title pairing no better than the shifted reading")
    for p in problems[:20]:
        print("  PROBLEM " + p)
    print("%d problems" % len(problems))
    return 1 if problems else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list", help="print one database")
    p.add_argument("--lang", default="EnglishUSA", choices=list(FILES))
    p.add_argument("--grep")
    p.add_argument("--width", type=int, default=100)
    p.set_defaults(func=cmd_list)
    p = sub.add_parser("json", help="every database as build/text/<lang>.json")
    p.set_defaults(func=cmd_json)
    for name, fn, hlp in (("messages", cmd_messages, "SE1 message files for MessageHolders"),
                          ("verify", cmd_verify, "structure, pairing and entity checks")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("paths", nargs="*", help="levels (default: orig/files/Levels)")
        p.set_defaults(func=fn)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
