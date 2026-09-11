#!/usr/bin/env python3
"""
A minimal Gekko (PowerPC 750CL) disassembler for main.dol, for the code
stock Ghidra cannot read: the paired-single instructions (psq_l, psq_st,
ps_*). See docs/findings.md, "Toolchain constraint: Gekko paired-singles".

It covers the D-form loads and stores, the common integer and float ops,
branches, mtspr/mfspr, psq_l/psq_st(u)(x) and the ps_* arithmetic. Anything
else prints as ???.

    python tools/gekko.py 80129330 80129474   # disassemble a range
    python tools/gekko.py --gqr               # every write to GQR0-7

Used to read the animation key decode at 0x80129334: Ghidra had lost it
behind psq_l and a call it wrongly marked no-return (ghtool.py fixflow).
"""

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sound                                                    # noqa: E402

_d, _at = sound._dol_reader()


def word(a):
    return struct.unpack_from(">I", _d, _at(a))[0]


def s16(x):
    return x - 0x10000 if x & 0x8000 else x


def s12(x):
    return x - 0x1000 if x & 0x800 else x


DFORM = {32: "lwz", 33: "lwzu", 34: "lbz", 36: "stw", 37: "stwu", 38: "stb", 40: "lhz",
         42: "lha", 44: "sth", 48: "lfs", 49: "lfsu", 50: "lfd", 52: "stfs", 53: "stfsu",
         54: "stfd", 14: "addi", 15: "addis", 7: "mulli", 24: "ori", 25: "oris", 26: "xori",
         27: "xoris", 28: "andi.", 10: "cmplwi", 11: "cmpwi", 12: "addic", 8: "subfic"}
A59 = {18: "fdivs", 20: "fsubs", 21: "fadds", 25: "fmuls", 28: "fmsubs", 29: "fmadds",
       30: "fnmsubs", 31: "fnmadds"}
A63 = {18: "fdiv", 20: "fsub", 21: "fadd", 25: "fmul", 28: "fmsub", 29: "fmadd"}
X63 = {72: "fmr", 40: "fneg", 264: "fabs", 12: "frsp", 15: "fctiwz", 0: "fcmpu", 32: "fcmpo"}
X31 = {444: "or", 266: "add", 40: "subf", 235: "mullw", 459: "divwu", 491: "divw",
       24: "slw", 536: "srw", 792: "sraw", 28: "and", 316: "xor", 0: "cmpw", 32: "cmplw",
       23: "lwzx", 279: "lhzx", 343: "lhax", 151: "stwx", 407: "sthx", 535: "lfsx",
       663: "stfsx", 824: "srawi", 922: "extsh", 954: "extsb", 104: "neg"}
PS4_A = {18: "ps_div", 20: "ps_sub", 21: "ps_add", 25: "ps_mul", 28: "ps_msub",
         29: "ps_madd", 30: "ps_nmsub", 31: "ps_nmadd", 10: "ps_sum0", 11: "ps_sum1",
         12: "ps_muls0", 13: "ps_muls1", 14: "ps_madds0", 15: "ps_madds1", 23: "ps_sel"}
PS4_X = {528: "ps_merge00", 560: "ps_merge01", 592: "ps_merge10", 624: "ps_merge11",
         72: "ps_mr", 40: "ps_neg", 264: "ps_abs", 0: "ps_cmpu0"}


def decode(a, w):
    """One instruction at address a, or None when not covered."""
    op = w >> 26
    rD, rA, rB, rC = (w >> 21) & 31, (w >> 16) & 31, (w >> 11) & 31, (w >> 6) & 31
    if op in DFORM:
        imm, nm = w & 0xFFFF, DFORM[op]
        if nm == "cmplwi":
            return "cmplwi r%d,0x%x" % (rA, imm)
        if nm == "cmpwi":
            return "cmpwi r%d,%d" % (rA, s16(imm))
        if nm in ("ori", "oris", "xori", "xoris", "andi."):
            return "%s r%d,r%d,0x%x" % (nm, rA, rD, imm)
        if nm in ("addi", "addis", "mulli", "addic", "subfic"):
            return "%s r%d,r%d,%d" % (nm, rD, rA, s16(imm))
        reg = "f" if nm.startswith(("lf", "stf")) else "r"
        return "%s %s%d,%d(r%d)" % (nm, reg, rD, s16(imm), rA)
    if op in (56, 57, 60, 61):
        nm = {56: "psq_l", 57: "psq_lu", 60: "psq_st", 61: "psq_stu"}[op]
        return "%s f%d,%d(r%d),%d,qr%d" % (nm, rD, s12(w & 0xFFF), rA, (w >> 15) & 1, (w >> 12) & 7)
    if op == 59 and ((w >> 1) & 31) in A59:
        return "%s f%d,f%d,f%d,f%d" % (A59[(w >> 1) & 31], rD, rA, rC, rB)
    if op == 63:
        xo = (w >> 1) & 1023
        if xo in X63:
            if xo in (0, 32):
                return "%s cr%d,f%d,f%d" % (X63[xo], rD >> 2, rA, rB)
            return "%s f%d,f%d" % (X63[xo], rD, rB)
        if ((w >> 1) & 31) in A63:
            return "%s f%d,f%d,f%d,f%d" % (A63[(w >> 1) & 31], rD, rA, rC, rB)
    if op == 4:
        if ((w >> 1) & 31) in PS4_A:
            return "%s f%d,f%d,f%d,f%d" % (PS4_A[(w >> 1) & 31], rD, rA, rC, rB)
        xo = (w >> 1) & 1023
        if xo in PS4_X:
            return "%s f%d,f%d,f%d" % (PS4_X[xo], rD, rA, rB)
        if ((w >> 1) & 63) in (6, 7, 38, 39):
            nm = {6: "psq_lx", 7: "psq_stx", 38: "psq_lux", 39: "psq_stux"}[(w >> 1) & 63]
            return "%s f%d,r%d,r%d,%d,qr%d" % (nm, rD, rA, rB, (w >> 10) & 1, (w >> 7) & 7)
    if op == 31:
        xo = (w >> 1) & 1023
        if xo in (467, 339):
            spr = ((w >> 16) & 31) | (((w >> 11) & 31) << 5)
            return "mtspr %d,r%d" % (spr, rD) if xo == 467 else "mfspr r%d,%d" % (rD, spr)
        if xo in X31:
            if xo == 444:
                return "or r%d,r%d,r%d" % (rA, rD, rB)
            if xo == 824:
                return "srawi r%d,r%d,%d" % (rA, rD, rB)
            if xo in (922, 954, 104):
                return "%s r%d,r%d" % (X31[xo], rA if xo != 104 else rD, rD if xo != 104 else rA)
            return "%s r%d,r%d,r%d" % (X31[xo], rD, rA, rB)
    if op == 21:
        return "rlwinm r%d,r%d,%d,%d,%d" % (rA, rD, rB, rC, (w >> 1) & 31)
    if op == 18:
        li = w & 0x3FFFFFC
        li = li - 0x4000000 if li & 0x2000000 else li
        return "%s 0x%08x" % ("bl" if w & 1 else "b", a + li)
    if op == 16:
        return "bc %d,%d,0x%08x" % (rD, rA, a + s16(w & 0xFFFC))
    if w == 0x4E800020:
        return "blr"
    return None


def text_sections():
    dol = open(sound.DOL, "rb").read()
    offs = struct.unpack_from(">7I", dol, 0)
    addrs = struct.unpack_from(">7I", dol, 0x48)
    sizes = struct.unpack_from(">7I", dol, 0x90)
    return dol, [(offs[i], addrs[i], sizes[i]) for i in range(7) if sizes[i]]


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--gqr":
        dol, secs = text_sections()
        for off, addr, size in secs:
            for k in range(0, size, 4):
                w = struct.unpack_from(">I", dol, off + k)[0]
                if w >> 26 == 31 and ((w >> 1) & 1023) == 467:
                    spr = ((w >> 16) & 31) | (((w >> 11) & 31) << 5)
                    if 912 <= spr <= 919:
                        a = addr + k
                        ctx = [decode(a - 4 * j, word(a - 4 * j)) or "?" for j in (3, 2, 1)]
                        print("%08x mtspr GQR%d  <- %s" % (a, spr - 912, " ; ".join(ctx)))
        return 0
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    lo, hi = int(sys.argv[1], 16), int(sys.argv[2], 16)
    for a in range(lo, hi, 4):
        w = word(a)
        print("  %08x  %08x  %s" % (a, w, decode(a, w) or "???"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
