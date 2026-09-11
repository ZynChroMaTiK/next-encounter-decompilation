#!/usr/bin/env python3
"""
Parser for Next Encounter's plain-text config files.

Two dialects ship on the disc.

**Stash** (`base.cfg`, `MinionStats.cfg`, `LevelModels.cfg`) - the `CcStash`
system, cf. `CcStash::LoadFromMemory()` in main.dol. A tree of named scopes:

    {
        stash MinionStats
        {
            float    DeathAliveTime  4.0  0.0  100.0   # how long to stay dead
            sqrfloat SeeDist         500.0 1.0 1000.0  # stored squared
            flag     BloodyGibs      true
            string   Level           "RLevel0_1"
            stash    DiffScaleTable  { ... }
        }
    }

Declarations are `<type> <name> <value> [min] [max]`. `sqrfloat` is a float the
engine squares on load, so distance tests can skip the square root - the value
in the file is the real distance.

**DYNE** (`Combine.cfg`, `Jeep.cfg`) - flat vehicle physics parameters, headed
"DYNE Parameters File". `KEYWORD value...` with two extras: a count keyword
followed by a bare-number list (`NENGINEBINS 11`, then `ENGINETORQUECURVE` and
11 loose floats), and repeated `WHEEL <n>` sections that scope the `WHEEL*`
keys following them.

Comments are `#` or `//` to end of line, and are kept: they are the developers'
own notes and are often the only documentation of what a field means.

Usage:
    python tools/cfg.py parse orig/files/MinionStats.cfg
    python tools/cfg.py parse orig/files/*.cfg -o build/cfg/
    python tools/cfg.py keys  orig/files/MinionStats.cfg
    python tools/cfg.py enemies orig/files/LevelModels.cfg
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SCALAR_TYPES = {"float", "int", "sqrfloat", "flag", "string"}
COMMENT = re.compile(r"(#|//).*$")
# "type name value [min] [max]", value may be a quoted string
DECL = re.compile(
    r"""^\s*(?P<type>float|int|sqrfloat|flag|string)\s+
         (?P<name>[A-Za-z_][\w.]*)\s+
         (?P<rest>.*)$""", re.X)


class ParseError(Exception):
    pass


def strip_comment(line: str) -> tuple[str, str | None]:
    m = COMMENT.search(line)
    if not m:
        return line.rstrip(), None
    # don't cut inside a quoted string
    q = line.find('"')
    if q != -1 and q < m.start():
        end = line.find('"', q + 1)
        if end > m.start():
            m = COMMENT.search(line, end)
            if not m:
                return line.rstrip(), None
    return line[:m.start()].rstrip(), line[m.start():].lstrip("#/ \t").rstrip()


def coerce(kind: str, tok: str):
    if kind == "flag":
        return tok.lower() in ("true", "1", "yes")
    if kind == "int":
        try:
            return int(tok, 0)
        except ValueError:
            return float(tok)
    if kind in ("float", "sqrfloat"):
        return float(tok)
    return tok.strip('"')


TOKEN = re.compile(r'"[^"]*"|[{}]|[^\s{}]+')


def split_tokens(rest: str) -> list[str]:
    return TOKEN.findall(rest)


# --- stash dialect ----------------------------------------------------------

def parse_stash(text: str) -> dict:
    """Token-driven, because declarations, braces and nested stashes all share
    lines: `stash Rlevel0_1 {int Score 10000 -2147483648 2147483647 string Name
    "Serious Sam"}` is real content in base.cfg."""
    root: dict = {}
    stack: list[dict] = [root]
    pending_name: str | None = None

    for lineno, raw in enumerate(text.splitlines(), 1):
        line, comment = strip_comment(raw)
        toks = TOKEN.findall(line)
        i = 0
        while i < len(toks):
            t = toks[i]

            if t == "{":
                parent = stack[-1]
                if pending_name is not None:
                    new: dict = {}
                    parent[pending_name] = new
                    pending_name = None
                    stack.append(new)
                else:
                    # bare '{' - the file's outer wrapper; reuse the scope so
                    # its contents land in the parent, not an orphan dict.
                    stack.append(parent)
                i += 1
                continue

            if t == "}":
                if len(stack) == 1:
                    raise ParseError(f"line {lineno}: unbalanced '}}'")
                stack.pop()
                i += 1
                continue

            if t == "stash":
                if i + 1 >= len(toks):
                    raise ParseError(f"line {lineno}: 'stash' with no name")
                pending_name = toks[i + 1].strip('"')
                i += 2
                continue

            if t in SCALAR_TYPES:
                if i + 1 >= len(toks):
                    raise ParseError(f"line {lineno}: '{t}' with no name")
                # names are normally bare, but base.cfg quotes any that would
                # be an invalid identifier, e.g. float "9MMBullets" 5 1 600
                kind, name = t, toks[i + 1].strip('"')
                i += 2
                # flag/string take one value; numerics take value[,min,max].
                # Stop early at anything that starts a new construct.
                want = 1 if kind in ("flag", "string") else 3
                vals: list[str] = []
                while (i < len(toks) and len(vals) < want
                       and toks[i] not in SCALAR_TYPES
                       and toks[i] not in ("{", "}", "stash")):
                    vals.append(toks[i])
                    i += 1
                if not vals:
                    raise ParseError(f"line {lineno}: {name} has no value")
                entry: dict = {"type": kind, "value": coerce(kind, vals[0])}
                if len(vals) >= 3:
                    entry["min"] = coerce(kind, vals[1])
                    entry["max"] = coerce(kind, vals[2])
                if comment:
                    entry["note"] = comment
                    comment = None
                scope = stack[-1]
                if name in scope:                 # keep duplicates, don't drop
                    scope.setdefault("__dups__", {}).setdefault(name, []).append(
                        scope[name])
                scope[name] = entry
                continue

            stack[-1].setdefault("__unparsed__", []).append(
                {"line": lineno, "text": t})
            i += 1

    if len(stack) != 1:
        raise ParseError(f"unbalanced braces: {len(stack) - 1} scope(s) left open")
    return root


# --- DYNE dialect -----------------------------------------------------------

def parse_dyne(text: str) -> dict:
    out: dict = {}
    notes: dict = {}
    wheels: list[dict] = []
    scope = out
    pending_array: str | None = None

    for raw in text.splitlines():
        line, comment = strip_comment(raw)
        toks = split_tokens(line)
        if not toks:
            continue

        key = toks[0]

        if key.upper() == "WHEEL" and len(toks) == 2 and toks[1].isdigit():
            scope = {"index": int(toks[1])}
            wheels.append(scope)
            pending_array = None
            continue

        # a bare number line continues the array most recently opened
        if pending_array is not None and re.fullmatch(r"[-+0-9.eE]+", key):
            scope[pending_array].extend(float(t) for t in toks)
            continue
        pending_array = None

        if len(toks) == 1:              # keyword that opens a float list
            scope[key] = []
            pending_array = key
            if comment:
                notes[key] = comment
            continue

        vals = []
        for t in toks[1:]:
            try:
                vals.append(float(t) if re.search(r"[.eE]", t) else int(t))
            except ValueError:
                vals.append(t.strip('"'))
        scope[key] = vals[0] if len(vals) == 1 else vals
        if comment:
            notes[key] = comment

    if wheels:
        out["WHEELS"] = wheels
    if notes:
        out["__notes__"] = notes
    return out


def detect_and_parse(path: Path) -> tuple[str, dict]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if re.search(r"^\s*stash\s+\w", text, re.M):
        return "stash", parse_stash(text)
    return "dyne", parse_dyne(text)


# --- subcommands ------------------------------------------------------------

def _count_leaves(node) -> int:
    if isinstance(node, dict):
        if "type" in node and "value" in node:
            return 1
        return sum(_count_leaves(v) for k, v in node.items()
                   if not k.startswith("__"))
    return 0


def cmd_parse(args) -> int:
    rc = 0
    for path in args.files:
        try:
            dialect, data = detect_and_parse(path)
        except ParseError as e:
            print(f"{path}: PARSE ERROR: {e}", file=sys.stderr)
            rc = 1
            continue
        leaves = _count_leaves(data) if dialect == "stash" else len(data)
        print(f"{path}: {dialect} dialect, {leaves} settings")
        if args.output:
            args.output.mkdir(parents=True, exist_ok=True)
            dest = args.output / (path.stem + ".json")
            dest.write_text(json.dumps(data, indent=2), encoding="utf-8")
            print(f"    -> {dest}")
        elif args.print:
            print(json.dumps(data, indent=2)[:args.limit])
    return rc


def _walk(node, prefix=""):
    for k, v in node.items():
        if k.startswith("__"):
            continue
        if isinstance(v, dict) and "type" in v and "value" in v:
            yield prefix + k, v
        elif isinstance(v, dict):
            yield from _walk(v, prefix + k + ".")


def cmd_keys(args) -> int:
    for path in args.files:
        dialect, data = detect_and_parse(path)
        if dialect != "stash":
            print(f"{path}: dyne dialect - keys: {', '.join(k for k in data if not k.startswith('__'))}")
            continue
        print(f"=== {path} ===")
        for name, e in _walk(data):
            rng = (f"  [{e['min']}..{e['max']}]" if "min" in e else "")
            note = f"   # {e['note']}" if "note" in e else ""
            print(f"  {e['type']:<8} {name:<52} = {e['value']!r}{rng}{note}")
    return 0


def cmd_enemies(args) -> int:
    """Pull the ObjectModel<N> table - the enemy/entity enum in model form."""
    for path in args.files:
        text = path.read_text(encoding="utf-8", errors="replace")
        rows = re.findall(
            r'string\s+ObjectModel(\d+)\s+"([^"]*)"\s*(?:[#/]+\s*(.*))?', text)
        print(f"=== {path}: {len(rows)} object models ===")
        for idx, model, note in sorted(rows, key=lambda r: int(r[0])):
            enum = (note or "").strip().rstrip(",")
            print(f"  {int(idx):>3}  {model:<28} {enum}")
    return 0


def cmd_roster(args) -> int:
    """Join MinionStats.Object<N> with LevelModels.ObjectModel<N>.

    Both tables are indexed by the same entity id, and every minion inherits
    from the `Generic` stash unless it overrides a field - the file says so
    itself: "All minions will contain this data, unless explicitly set".
    """
    files = {p.stem: p for p in args.files}
    for need in ("MinionStats", "LevelModels"):
        if need not in files:
            print(f"error: need {need}.cfg among the inputs", file=sys.stderr)
            return 2

    _, mdata = detect_and_parse(files["MinionStats"])
    _, ldata = detect_and_parse(files["LevelModels"])
    minions = mdata["MinionStats"]
    models = ldata["ObjectModels"]
    generic = {k: v for k, v in minions.get("Generic", {}).items()
               if isinstance(v, dict) and "type" in v}

    # the enum name lives in each line's trailing comment
    enum = {}
    for idx, _model, note in re.findall(
            r'string\s+ObjectModel(\d+)\s+"([^"]*)"\s*(?:[#/]+\s*(.*))?',
            files["LevelModels"].read_text(encoding="utf-8", errors="replace")):
        enum[int(idx)] = (note or "").strip().rstrip(",")

    def val(scope, key):
        e = scope.get(key) or generic.get(key)
        return e.get("value") if isinstance(e, dict) else None

    rows = []
    for i in range(args.max_index + 1):
        obj = minions.get(f"Object{i}")
        if not isinstance(obj, dict) or "type" in obj:
            continue
        model = models.get(f"ObjectModel{i}")
        row = {
            "index": i,
            "model": model.get("value") if isinstance(model, dict) else None,
            "enum": enum.get(i) or None,
        }
        for f in args.fields:
            row[f] = val(obj, f)
        row["overrides"] = sorted(k for k, v in obj.items()
                                  if isinstance(v, dict) and "type" in v)
        rows.append(row)

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    hdr = ["#", "model", "enum"] + args.fields + ["ovr"]
    widths = [4, 24, 24] + [max(8, len(f)) for f in args.fields] + [4]
    print("  ".join(h.ljust(w) for h, w in zip(hdr, widths)))
    print("-" * (sum(widths) + 2 * len(widths)))
    for r in rows:
        cells = ([str(r["index"]), str(r["model"] or "?"), str(r["enum"] or "")]
                 + [("" if r[f] is None else str(r[f])) for f in args.fields]
                 + [str(len(r["overrides"]))])
        print("  ".join(c.ljust(w)[:w] for c, w in zip(cells, widths)))
    print()
    print(f"{len(rows)} minions; Generic supplies {len(generic)} inherited fields")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("parse", help="parse to JSON")
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument("-o", "--output", type=Path, help="write <stem>.json here")
    p.add_argument("--print", action="store_true", help="dump JSON to stdout")
    p.add_argument("--limit", type=int, default=4000)
    p.set_defaults(func=cmd_parse)

    p = sub.add_parser("keys", help="flat listing of every setting")
    p.add_argument("files", nargs="+", type=Path)
    p.set_defaults(func=cmd_keys)

    p = sub.add_parser("enemies", help="the ObjectModel<N> entity table")
    p.add_argument("files", nargs="+", type=Path)
    p.set_defaults(func=cmd_enemies)

    p = sub.add_parser("roster", help="minion stats joined to models, with Generic defaults")
    p.add_argument("files", nargs="+", type=Path,
                   help="must include MinionStats.cfg and LevelModels.cfg")
    p.add_argument("--fields", nargs="*",
                   default=["Health", "NormalSpeed", "FastSpeed", "SeeDist", "Score"])
    p.add_argument("--max-index", type=int, default=63)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_roster)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
