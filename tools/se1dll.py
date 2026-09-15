#!/usr/bin/env python3
"""
Entity classes and property tables from the SE1 1.04 entity DLLs in dev/Bin
(docs/dev-material.md). These are the classes the NE levels were authored
with in Serious Editor before Climax converted them for the GameCube.

Each class exports `C<Name>_DLLClass`, a `CDLLEntityClass`:

    CEntityProperty* aep; INDEX ctp;      // properties
    ...handlers, components...
    char* name; char* icon; INDEX id;     // +0x18 +0x1c +0x20
    CDLLEntityClass* base;                // +0x24

`CEntityProperty` is 32 bytes {type, enum*, id, offset, name*, flags,
shortcut(char, padded), colour}. It has a constructor, so in 1.04 the arrays
are zero in the file and static initializers fill them at load. This tool
recovers them by emulating those initializers (capstone): constant registers,
stores to [abs] / [reg+disp], and thiscall constructor calls (8 arguments
pushed right to left, ecx = the record). Property ids are class id << 8 | n;
that holds for all but 3 of 4,293 properties recovered from the three
Entities.dll builds, which is the check. Nameless properties are real: SE1
hides properties that have no editor name.

    python tools/se1dll.py dump [DLL...]          # -> build/dev/se1_classes.json
    python tools/se1dll.py show CLASS [--dll D]   # one class's properties
    python tools/se1dll.py list [--dll D]         # id, class, base, #props
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEV_BIN = ROOT / "dev" / "Bin"
OUT = ROOT / "build" / "dev" / "se1_classes.json"
DLLS = ("Entities.dll", "Entities.dll.new", "Entities.dll.orig", "GCEntities.dll")

EPT = {1: "ENUM", 2: "BOOL", 3: "FLOAT", 4: "COLOR", 5: "STRING", 6: "RANGE",
       7: "ENTITYPTR", 8: "FILENAME", 9: "INDEX", 10: "ANIMATION", 11: "ILLUMINATIONTYPE",
       12: "FLOATAABBOX3D", 13: "ANGLE", 14: "FLOAT3D", 15: "ANGLE3D", 16: "FLOATplane3D",
       17: "MODELOBJECT", 18: "PLACEMENT3D", 19: "ANIMOBJECT", 20: "FILENAMENODEP",
       21: "SOUNDOBJECT", 22: "STRINGTRANS", 23: "FLOATQUAT3D", 24: "FLOATMATRIX3D",
       25: "FLAGS", 26: "MODELINSTANCE"}


class PE:
    def __init__(self, path):
        b = self.b = Path(path).read_bytes()
        pe = struct.unpack_from("<I", b, 0x3c)[0]
        nsec = struct.unpack_from("<H", b, pe + 6)[0]
        opt = pe + 24
        self.base = struct.unpack_from("<I", b, opt + 28)[0]
        exp_rva = struct.unpack_from("<I", b, opt + 96)[0]
        so = opt + struct.unpack_from("<H", b, pe + 20)[0]
        self.secs = [struct.unpack_from("<8sIIII", b, so + 40 * i)[1:] for i in range(nsec)]
        e = self.off(exp_rva)
        _nfun, nnames, af, an, ao = struct.unpack_from("<IIIII", b, e + 20)
        self.exports = {}
        for i in range(nnames):
            name = self.cstr(self.base + self.u32(self.base + an + 4 * i))    # name RVAs
            ordinal = struct.unpack_from("<H", b, self.off(ao) + 2 * i)[0]
            self.exports[name] = self.base + self.u32(self.base + af + 4 * ordinal)

    def off(self, rva):
        """File offset of an RVA, or None if it is outside the file's bytes."""
        for vs, va, rs, ra in self.secs:
            if va <= rva < va + max(vs, rs):
                return rva - va + ra if rva - va < rs else None
        return None

    def u32(self, va):
        o = self.off(va - self.base)
        return struct.unpack_from("<I", self.b, o)[0] if o is not None else None

    def cstr(self, va):
        if not va:
            return None
        o = self.off(va - self.base)
        if o is None:
            return None
        return self.b[o:self.b.index(b"\0", o)].decode("latin1")


def _emulate(pe, records):
    """Replay the .text initializers. Returns (stores, ctor_args)."""
    import capstone
    from capstone import x86
    vs, va, rs, ra = pe.secs[0]
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    md.detail = True
    md.skipdata = True
    regs = {x86.X86_REG_EAX: "eax", x86.X86_REG_EBX: "ebx", x86.X86_REG_ECX: "ecx",
            x86.X86_REG_EDX: "edx", x86.X86_REG_ESI: "esi", x86.X86_REG_EDI: "edi",
            x86.X86_REG_EBP: "ebp"}
    mem, calls, reg, stack = {}, {}, {}, []

    def addr(op):
        m = op.mem
        if m.index:
            return None
        if not m.base:
            return m.disp & 0xffffffff
        r = regs.get(m.base)
        return (reg[r] + m.disp) & 0xffffffff if r in reg else None

    for ins in md.disasm(pe.b[ra:ra + rs], pe.base + va):
        mn = ins.mnemonic
        ops = ins.operands if ins.id else []
        if mn == "mov" and len(ops) == 2:
            d, s = ops
            if d.type == x86.X86_OP_REG:
                r = regs.get(d.reg)
                if r:
                    if s.type == x86.X86_OP_IMM:
                        reg[r] = s.imm & 0xffffffff
                    else:
                        reg.pop(r, None)
            elif d.type == x86.X86_OP_MEM:
                a, v = addr(d), None
                if s.type == x86.X86_OP_IMM:
                    v = s.imm & 0xffffffff
                elif s.type == x86.X86_OP_REG and regs.get(s.reg) in reg:
                    v = reg[regs[s.reg]]
                if a is not None and v is not None:
                    mem.setdefault(("b", a) if d.size == 1 else a, v & 0xff if d.size == 1 else v)
        elif (mn == "xor" and len(ops) == 2 and ops[0].type == ops[1].type == x86.X86_OP_REG
              and ops[0].reg == ops[1].reg):
            r = regs.get(ops[0].reg)
            if r:
                reg[r] = 0
        elif mn == "push":
            o = ops[0] if ops else None
            if o is not None and o.type == x86.X86_OP_IMM:
                stack.append(o.imm & 0xffffffff)
            elif o is not None and o.type == x86.X86_OP_REG:
                stack.append(reg.get(regs.get(o.reg)))
            else:
                stack.append(None)
        elif mn == "call":
            this = reg.get("ecx")
            if this is not None and stack and (records is None or this in records):
                calls.setdefault(this, stack[-8:][::-1])     # pushed right to left
            stack.clear()
            for r in ("eax", "ecx", "edx"):
                reg.pop(r, None)
        elif mn in ("ret", "jmp"):
            stack.clear()
            reg.clear()
        elif ops and ops[0].type == x86.X86_OP_REG and mn not in ("cmp", "test"):
            r = regs.get(ops[0].reg)
            if r:
                reg.pop(r, None)
    return mem, calls


def classes(path):
    pe = PE(path)
    byaddr = {va: n[:-9] for n, va in pe.exports.items() if n.endswith("_DLLClass")}
    enums = {va: n[1:n.index("_enum")] for n, va in pe.exports.items()
             if n.endswith("_enum@@3VCEntityPropertyEnumType@@A")}
    hdr, records = {}, set()
    for va, n in byaddr.items():
        aep, ctp, _aeh, _cth, _aec, _ctc, nm, _icon, iid, pbase = struct.unpack_from(
            "<10I", pe.b, pe.off(va - pe.base))
        hdr[n] = (aep, ctp, nm, iid, pbase)
        records.update(aep + 32 * i for i in range(ctp))
    mem, calls = _emulate(pe, None)

    def rd(a):
        return mem[a] if a in mem else pe.u32(a)

    def field(a, k, n):
        """k-th of n constructor arguments at `a`, else the k-th stored word."""
        c = calls.get(a)
        if c is not None and len(c) >= n:
            return c[k]
        return rd(a + 4 * k)

    # enums: CEntityPropertyEnumType {values*, count}, values {INDEX, char*}
    enum_tables = {}
    for n, va in pe.exports.items():
        if not n.endswith("_enum@@3VCEntityPropertyEnumType@@A"):
            continue
        vals, cnt = field(va, 0, 2), field(va, 1, 2)
        if not vals or cnt is None or not 0 < cnt < 1000:
            continue
        enum_tables[n[1:n.index("_enum")]] = {
            field(vals + 8 * i, 0, 2): pe.cstr(field(vals + 8 * i, 1, 2)) for i in range(cnt)}

    out = {"__enums__": enum_tables}
    for n, (aep, ctp, nm, iid, pbase) in sorted(hdr.items(), key=lambda kv: kv[1][3]):
        props = []
        for i in range(ctp):
            q = aep + 32 * i
            if q in calls:
                t, en, pid, off, name, _sc, _col, flags = calls[q]
            else:
                t, en, pid, off, name, flags = (rd(q + 4 * k) for k in range(6))
            if off is not None and off >= 2 ** 31:
                off -= 2 ** 32
            props.append({"id": pid, "n": pid & 0xff if pid is not None else None,
                          "type": EPT.get(t, t), "enum": enums.get(en), "offset": off,
                          "name": pe.cstr(name) or None, "flags": flags})
        out[n] = {"id": iid, "name": pe.cstr(nm), "base": byaddr.get(pbase), "props": props}
    return out


def cmd_dump(args):
    res = {}
    for d in args.dlls or DLLS:
        path = DEV_BIN / d
        res[d] = classes(path)
        c = {n: k for n, k in res[d].items() if n != "__enums__"}
        allp = [q for k in c.values() for q in k["props"]]
        good = sum(q["id"] is not None and q["id"] >> 8 == k["id"] for k in c.values() for q in k["props"])
        en = res[d]["__enums__"]
        print("  %-18s %3d classes  %5d properties  id>>8 == class id: %5d  untyped: %d  "
              "enums: %d (%d values, %d unnamed)"
              % (d, len(c), len(allp), good, sum(not isinstance(q["type"], str) for q in allp),
                 len(en), sum(len(v) for v in en.values()),
                 sum(1 for v in en.values() for s in v.values() if s is None)))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, indent=1))
    print("wrote", OUT.relative_to(ROOT))
    return 0


def _load(dll):
    if OUT.exists():
        data = json.loads(OUT.read_text())
        if dll in data:
            return data[dll]
    return classes(DEV_BIN / dll)


def cmd_show(args):
    c = _load(args.dll).get(args.cls)
    if not c:
        print("no class", args.cls)
        return 1
    print("%s  id %d  base %s" % (args.cls, c["id"], c["base"]))
    for q in c["props"]:
        print("  %3s  %-14s +%-4s %-32s %s" % (q["n"], q["type"], q["offset"], q["name"] or "-", q["enum"] or ""))
    return 0


def cmd_list(args):
    for n, c in _load(args.dll).items():
        if n != "__enums__":
            print("%5d  %-40s %-20s %d" % (c["id"], n, c["base"] or "", len(c["props"])))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("dump"); p.add_argument("dlls", nargs="*"); p.set_defaults(fn=cmd_dump)
    p = sub.add_parser("show"); p.add_argument("cls"); p.add_argument("--dll", default="Entities.dll")
    p.set_defaults(fn=cmd_show)
    p = sub.add_parser("list"); p.add_argument("--dll", default="Entities.dll"); p.set_defaults(fn=cmd_list)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
