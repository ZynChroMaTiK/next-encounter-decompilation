#!/usr/bin/env python3
"""
Query tool for the NextEncounter Ghidra database.

Starting the JVM and opening the project costs ~15s, so this is one tool with
subcommands rather than a pile of one-off scripts.

    python ghtool.py strings --grep CLIMAX
    python ghtool.py xrefs 0x801ab2c0
    python ghtool.py decompile 0x800a1234 --name-only
    python ghtool.py disasm 0x800a1234 --count 40
    python ghtool.py callers FUN_800a1234
    python ghtool.py rename 0x800a1234 CcStash_Load     # needs --write

Addresses may be given as 0x8..., 8..., or a symbol name.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

PROJECT_DIR = str(Path(__file__).resolve().parent.parent)
PROJECT_NAME = "NextEncounter"
PROGRAM = "main.dol"

_state = {}


def boot(write: bool = False):
    """Start the JVM, open the project, return (program, flatapi)."""
    if not os.environ.get("GHIDRA_INSTALL_DIR"):
        os.environ["GHIDRA_INSTALL_DIR"] = r"C:\Software\ghidra_12.1.2_PUBLIC"
    import pyghidra
    pyghidra.start(verbose=False)

    from ghidra.base.project import GhidraProject
    from ghidra.program.flatapi import FlatProgramAPI

    proj = GhidraProject.openProject(PROJECT_DIR, PROJECT_NAME, True)
    program = proj.openProgram("/", PROGRAM, not write)
    _state["proj"] = proj
    _state["program"] = program
    return program, FlatProgramAPI(program)


def shutdown(save: bool = False):
    proj, program = _state.get("proj"), _state.get("program")
    if program is not None and save:
        proj.save(program)
    if proj is not None:
        proj.close()


def resolve(program, text: str):
    """Address from '0x8...', '8...', or a symbol name."""
    af = program.getAddressFactory()
    t = text.strip()
    if re.fullmatch(r"(0x)?[0-9a-fA-F]{6,8}", t):
        return af.getAddress(t[2:] if t.lower().startswith("0x") else t)
    st = program.getSymbolTable()
    syms = list(st.getSymbols(t))
    if not syms:
        raise SystemExit(f"error: cannot resolve {text!r} to an address or symbol")
    return syms[0].getAddress()


def func_at(program, addr):
    return program.getFunctionManager().getFunctionContaining(addr)


def label_for(program, addr):
    """Best human label for an address: function name, or symbol, or raw."""
    f = func_at(program, addr)
    if f is not None:
        off = addr.subtract(f.getEntryPoint())
        return f"{f.getName()}+0x{off:x}" if off else f.getName()
    s = program.getSymbolTable().getPrimarySymbol(addr)
    return s.getName() if s is not None else str(addr)


# --- subcommands ------------------------------------------------------------

def cmd_strings(args):
    program, _ = boot()
    rx = re.compile(args.grep, re.I) if args.grep else None
    n = 0
    for d in program.getListing().getDefinedData(True):
        v = d.getValue()
        if v is None:
            continue
        s = str(v)
        if len(s) < args.min_len or not d.hasStringValue():
            continue
        if rx and not rx.search(s):
            continue
        refs = program.getReferenceManager().getReferenceCountTo(d.getAddress())
        print(f"{d.getAddress()}  refs={refs:<3}  {s[:args.width]!r}")
        n += 1
        if n >= args.limit:
            print(f"... stopped at {args.limit}")
            break
    if n == 0:
        print("no matching defined strings")
    shutdown()


def cmd_xrefs(args):
    program, _ = boot()
    addr = resolve(program, args.target)
    rm = program.getReferenceManager()
    refs = list(rm.getReferencesTo(addr))
    print(f"{len(refs)} reference(s) to {addr}  [{label_for(program, addr)}]")
    for r in refs[:args.limit]:
        src = r.getFromAddress()
        print(f"  {src}  {r.getReferenceType()}  in {label_for(program, src)}")
    shutdown()


def cmd_callers(args):
    program, _ = boot()
    addr = resolve(program, args.target)
    f = func_at(program, addr)
    if f is None:
        raise SystemExit(f"no function at {addr}")
    callers = sorted({c.getName() + " @ " + str(c.getEntryPoint())
                      for c in f.getCallingFunctions(None)})
    called = sorted({c.getName() + " @ " + str(c.getEntryPoint())
                     for c in f.getCalledFunctions(None)})
    print(f"function {f.getName()} @ {f.getEntryPoint()}  size={f.getBody().getNumAddresses()}")
    print(f"  called by ({len(callers)}):")
    for c in callers[:args.limit]:
        print("    ", c)
    print(f"  calls ({len(called)}):")
    for c in called[:args.limit]:
        print("    ", c)
    shutdown()


def _decompile(program, f, timeout=120):
    from ghidra.app.decompiler import DecompInterface
    from ghidra.util.task import ConsoleTaskMonitor
    ifc = DecompInterface()
    ifc.openProgram(program)
    try:
        res = ifc.decompileFunction(f, timeout, ConsoleTaskMonitor())
        if not res.decompileCompleted():
            return None, res.getErrorMessage()
        return res.getDecompiledFunction().getC(), None
    finally:
        ifc.dispose()


def cmd_decompile(args):
    program, _ = boot()
    for target in args.targets:
        addr = resolve(program, target)
        f = func_at(program, addr)
        if f is None:
            print(f"// no function at {addr}")
            continue
        print(f"// ==== {f.getName()} @ {f.getEntryPoint()} ====")
        if args.name_only:
            continue
        c, err = _decompile(program, f)
        print(c if c else f"// decompile failed: {err}")
    shutdown()


def _functions_in(program, start, end):
    fm = program.getFunctionManager()
    lo, hi = resolve(program, start), resolve(program, end)
    for f in fm.getFunctions(lo, True):
        if f.getEntryPoint().compareTo(hi) >= 0:
            break
        yield f


def _strings_used(program, f):
    """Defined strings a function's instructions reference, in order."""
    listing, out = program.getListing(), []
    for ins in listing.getInstructions(f.getBody(), True):
        for r in ins.getReferencesFrom():
            d = listing.getDataAt(r.getToAddress())
            if d is not None and d.hasStringValue():
                s = str(d.getValue())
                if s not in out:
                    out.append(s)
    return out


def cmd_range(args):
    """Every function in [start, end): size, references to it, the strings it
    uses and the named functions it calls -- a quick map of a code unit."""
    program, _ = boot()
    rm = program.getReferenceManager()
    for f in _functions_in(program, args.start, args.end):
        named = sorted({c.getName() for c in f.getCalledFunctions(None)
                        if not c.getName().startswith("FUN_")})
        print(f"{f.getEntryPoint()}  {f.getName():<32} size={f.getBody().getNumAddresses():<5} "
              f"refs={rm.getReferenceCountTo(f.getEntryPoint())}"
              + (f"  calls {', '.join(named)}" if named else ""))
        for s in _strings_used(program, f):
            print(f"      str {s[:args.width]!r}")
    shutdown()


def cmd_dump(args):
    """Decompile every function in [start, end) into one file."""
    program, _ = boot()
    n = 0
    with open(args.output, "w", encoding="utf-8") as fh:
        for f in _functions_in(program, args.start, args.end):
            c, err = _decompile(program, f)
            fh.write(f"// ==== {f.getName()} @ {f.getEntryPoint()} ====\n")
            fh.write((c if c else f"// decompile failed: {err}") + "\n\n")
            n += 1
    print(f"{n} functions -> {args.output}")
    shutdown()


def cmd_vtable(args):
    """A CodeWarrior vtable of 8-byte entries {s16 this-adjust, 0, function}.
    A call reads the adjust at +8k and the function at +8k+4:
    (*(vt + 0x194))(this + *(short *)(vt + 0x190)). Stops after two entries
    in a row that are not function pointers."""
    program, _ = boot()
    mem = program.getMemory()
    fm = program.getFunctionManager()
    af = program.getAddressFactory()
    addr = resolve(program, args.target)
    misses = 0
    for i in range(args.count):
        a = addr.add(8 * i)
        adj = mem.getShort(a)
        fp = mem.getInt(a.add(4)) & 0xFFFFFFFF
        if not (0x80003100 <= fp < 0x80300000):
            print(f"  +0x{8 * i + 4:03x}  {fp:08x}")
            misses += 1
            if misses == 2:
                break
            continue
        misses = 0
        f = fm.getFunctionAt(af.getAddress(f"{fp:08x}"))
        name = f.getName() if f is not None else "(no function)"
        print(f"  +0x{8 * i + 4:03x}  {fp:08x} adj {adj:<4} {name}")
    shutdown()


def cmd_mkfunc(args):
    """Create functions at every function pointer of a CodeWarrior vtable (and
    at any extra addresses). Small virtual methods reached only through a
    vtable are never made functions by auto-analysis, so they do not decompile."""
    if not args.write:
        raise SystemExit("error: mkfunc requires --write")
    program, api = boot(write=True)
    mem = program.getMemory()
    af = program.getAddressFactory()
    targets = []
    if args.vtable:
        base = resolve(program, args.vtable)
        misses = 0
        for i in range(args.count):
            fp = mem.getInt(base.add(8 * i + 4)) & 0xFFFFFFFF
            if not (0x80003100 <= fp < 0x80300000):
                misses += 1
                if misses == 2:
                    break
                continue
            misses = 0
            targets.append(af.getAddress(f"{fp:08x}"))
    targets += [resolve(program, t) for t in args.addresses]
    tx = program.startTransaction("mkfunc")
    made = 0
    try:
        for addr in targets:
            f = func_at(program, addr)
            if f is not None and f.getEntryPoint() == addr:
                continue
            api.disassemble(addr)
            if api.createFunction(addr, None) is not None:
                made += 1
    finally:
        program.endTransaction(tx, True)
    print(f"{len(targets)} targets, {made} functions created")
    shutdown(save=True)


def cmd_disasm(args):
    program, _ = boot()
    addr = resolve(program, args.target)
    listing = program.getListing()
    ins = listing.getInstructionAt(addr)
    if ins is None:
        raise SystemExit(f"no instruction at {addr} (undefined bytes?)")
    for _ in range(args.count):
        if ins is None:
            break
        cmt = listing.getComment(0, ins.getAddress())  # EOL comment
        print(f"  {ins.getAddress()}  {ins}" + (f"   ; {cmt}" if cmt else ""))
        ins = ins.getNext()
    shutdown()


def cmd_rename(args):
    if not args.write:
        raise SystemExit("error: rename requires --write")
    program, _ = boot(write=True)
    from ghidra.program.model.symbol import SourceType
    addr = resolve(program, args.target)
    f = func_at(program, addr)
    if f is None:
        raise SystemExit(f"no function at {addr}")
    tx = program.startTransaction("rename")
    try:
        old = f.getName()
        f.setName(args.newname, SourceType.USER_DEFINED)
        print(f"{old} -> {args.newname}  @ {f.getEntryPoint()}")
    finally:
        program.endTransaction(tx, True)
    shutdown(save=True)


def cmd_renames(args):
    """Several ADDR=NAME pairs in one JVM start; creates a function where the
    address is not yet one (vtable targets often are not)."""
    if not args.write:
        raise SystemExit("error: renames requires --write")
    program, api = boot(write=True)
    from ghidra.program.model.symbol import SourceType
    tx = program.startTransaction("renames")
    try:
        for pair in args.pairs:
            target, _, name = pair.partition("=")
            addr = resolve(program, target)
            f = func_at(program, addr)
            if f is None or f.getEntryPoint() != addr:
                api.disassemble(addr)
                f = api.createFunction(addr, None) or func_at(program, addr)
            if f is None:
                print(f"skip {target}: no function")
                continue
            old = f.getName()
            f.setName(name, SourceType.USER_DEFINED)
            print(f"{old} -> {name}  @ {f.getEntryPoint()}")
    finally:
        program.endTransaction(tx, True)
    shutdown(save=True)


def cmd_fixflow(args):
    """Clear a function's no-return flag and disassemble the fall-through
    after every call to it. Auto-analysis marks some helpers no-return
    (FUN_801b9eac, a matrix setup) and then never disassembles the code after
    their call sites, so whole loops vanish from the decompiler's output."""
    if not args.write:
        raise SystemExit("error: fixflow requires --write")
    program, api = boot(write=True)
    from ghidra.app.cmd.function import CreateFunctionCmd
    from ghidra.util.task import ConsoleTaskMonitor
    rm = program.getReferenceManager()
    listing = program.getListing()
    callers = {}
    tx = program.startTransaction("fixflow")
    try:
        for target in args.targets:
            addr = resolve(program, target)
            f = func_at(program, addr)
            if f is None:
                print(f"skip {target}: no function")
                continue
            was = f.hasNoReturn()
            f.setNoReturn(False)
            fixed = 0
            for r in rm.getReferencesTo(f.getEntryPoint()):
                if not r.getReferenceType().isCall():
                    continue
                site = r.getFromAddress()
                ins = listing.getInstructionAt(site)
                if ins is not None:
                    ins.setFlowOverride(ins.getFlowOverride())   # keep; clear fallthrough below
                    ins.clearFallThroughOverride()
                nxt = site.add(4)
                if listing.getInstructionAt(nxt) is None:
                    api.disassemble(nxt)
                    fixed += 1
                c = func_at(program, site)
                if c is not None:
                    callers[c.getEntryPoint()] = c
            print(f"{f.getName()} @ {f.getEntryPoint()}: no-return was {was}; "
                  f"{fixed} fall-throughs disassembled")
        # a function body does not grow to take in newly disassembled code
        # by itself; rebuild each caller's body so the decompiler sees it
        grown = 0
        for c in callers.values():
            before = c.getBody().getNumAddresses()
            CreateFunctionCmd.fixupFunctionBody(program, c, ConsoleTaskMonitor())
            grown += c.getBody().getNumAddresses() > before
        print(f"{len(callers)} calling functions re-bodied, {grown} grew")
    finally:
        program.endTransaction(tx, True)
    shutdown(save=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true",
                    help="open the program writable (required for rename)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("strings", help="defined strings, with reference counts")
    p.add_argument("--grep", help="regex filter")
    p.add_argument("--min-len", type=int, default=4)
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--width", type=int, default=100)
    p.set_defaults(func=cmd_strings)

    p = sub.add_parser("xrefs", help="references to an address or symbol")
    p.add_argument("target")
    p.add_argument("--limit", type=int, default=60)
    p.set_defaults(func=cmd_xrefs)

    p = sub.add_parser("callers", help="call graph neighbours of a function")
    p.add_argument("target")
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(func=cmd_callers)

    p = sub.add_parser("decompile", help="decompile to C")
    p.add_argument("targets", nargs="+")
    p.add_argument("--name-only", action="store_true")
    p.set_defaults(func=cmd_decompile)

    p = sub.add_parser("range", help="functions in [start, end) with strings and named calls")
    p.add_argument("start")
    p.add_argument("end")
    p.add_argument("--width", type=int, default=90)
    p.set_defaults(func=cmd_range)

    p = sub.add_parser("dump", help="decompile every function in [start, end) to a file")
    p.add_argument("start")
    p.add_argument("end")
    p.add_argument("-o", "--output", required=True)
    p.set_defaults(func=cmd_dump)

    p = sub.add_parser("vtable", help="a CodeWarrior vtable's {function, adjust} pairs")
    p.add_argument("target")
    p.add_argument("--count", type=int, default=120)
    p.set_defaults(func=cmd_vtable)

    p = sub.add_parser("mkfunc", help="create functions at a vtable's entries and/or addresses (writes the DB)")
    p.add_argument("addresses", nargs="*")
    p.add_argument("--vtable")
    p.add_argument("--count", type=int, default=160)
    p.set_defaults(func=cmd_mkfunc)

    p = sub.add_parser("disasm", help="linear disassembly")
    p.add_argument("target")
    p.add_argument("--count", type=int, default=30)
    p.set_defaults(func=cmd_disasm)

    p = sub.add_parser("rename", help="rename a function (writes the DB)")
    p.add_argument("target")
    p.add_argument("newname")
    p.set_defaults(func=cmd_rename)

    p = sub.add_parser("renames", help="rename several functions: ADDR=NAME ... (writes the DB)")
    p.add_argument("pairs", nargs="+")
    p.set_defaults(func=cmd_renames)

    p = sub.add_parser("fixflow", help="clear no-return on functions and disassemble "
                                       "after their call sites (writes the DB)")
    p.add_argument("targets", nargs="+")
    p.set_defaults(func=cmd_fixflow)

    args = ap.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
