#!/usr/bin/env python3
"""
Import a GameCube/Wii main.dol into a Ghidra project with a correct memory map.

Ghidra has no built-in DOL loader, and importing the file as a flat binary puts
every section at the wrong address. This script parses the DOL header and
creates one Ghidra memory block per section at its real load address, carves the
.bss range around the .sdata/.sdata2 sections that overlap it, maps the GameCube
hardware register window, and sets the entry point.

Usage (from tools/ghidra-headless/.venv):
    python import_dol.py --dol orig/sys/main.dol \
        --project-dir ghidra --project-name NextEncounter [--analyze]
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
from pathlib import Path

# --- DOL format -------------------------------------------------------------
# 0x00  7  u32  text section file offsets
# 0x1C 11  u32  data section file offsets
# 0x48  7  u32  text section load addresses
# 0x64 11  u32  data section load addresses
# 0x90  7  u32  text section sizes
# 0xAC 11  u32  data section sizes
# 0xD8     u32  bss load address
# 0xDC     u32  bss size
# 0xE0     u32  entry point
NUM_TEXT = 7
NUM_DATA = 11
NUM_SECTIONS = NUM_TEXT + NUM_DATA
HEADER_SIZE = 0x100


class Section:
    def __init__(self, index: int, offset: int, addr: int, size: int):
        self.index = index
        self.offset = offset
        self.addr = addr
        self.size = size
        self.is_text = index < NUM_TEXT

    @property
    def end(self) -> int:
        """Exclusive end address."""
        return self.addr + self.size

    @property
    def name(self) -> str:
        # Conventional names: text0 -> .init, text1 -> .text,
        # data0/1 -> .extab/.extabindex, then .ctors/.dtors/.rodata/.data,
        # and the last two data sections are .sdata/.sdata2.
        if self.is_text:
            return ".init" if self.index == 0 else f".text{self.index - 1}" if self.index > 1 else ".text"
        return f".data{self.index - NUM_TEXT}"

    def __repr__(self) -> str:
        return (f"{self.name:<10} off=0x{self.offset:06x} "
                f"addr=0x{self.addr:08x} size=0x{self.size:06x}")


def parse_dol(data: bytes):
    if len(data) < HEADER_SIZE:
        raise ValueError(f"file too small to be a DOL ({len(data)} bytes)")

    offsets = struct.unpack_from(">18I", data, 0x00)
    addrs = struct.unpack_from(">18I", data, 0x48)
    sizes = struct.unpack_from(">18I", data, 0x90)
    bss_addr, bss_size, entry = struct.unpack_from(">3I", data, 0xD8)

    sections = [
        Section(i, offsets[i], addrs[i], sizes[i])
        for i in range(NUM_SECTIONS)
        if sizes[i] != 0 and offsets[i] != 0
    ]
    if not sections:
        raise ValueError("no populated sections - not a DOL?")

    for s in sections:
        if s.offset + s.size > len(data):
            raise ValueError(f"{s.name} runs past EOF ({s.offset + s.size} > {len(data)})")
        if not (0x80000000 <= s.addr < 0x81800000):
            raise ValueError(f"{s.name} load address 0x{s.addr:08x} outside MEM1")

    return sections, bss_addr, bss_size, entry


def subtract_ranges(base: tuple[int, int], holes: list[tuple[int, int]]):
    """Return `base` (start, end) minus every overlapping range in `holes`."""
    pieces = [base]
    for hs, he in sorted(holes):
        out = []
        for ps, pe in pieces:
            if he <= ps or hs >= pe:      # no overlap
                out.append((ps, pe))
                continue
            if ps < hs:
                out.append((ps, hs))      # piece before the hole
            if he < pe:
                out.append((he, pe))      # piece after the hole
        pieces = out
    return [(s, e) for s, e in pieces if e > s]


def describe(dol_path: Path) -> int:
    data = dol_path.read_bytes()
    sections, bss_addr, bss_size, entry = parse_dol(data)
    print(f"{dol_path}  ({len(data):,} bytes)")
    for s in sections:
        print("  ", s)
    print(f"   .bss       addr=0x{bss_addr:08x} size=0x{bss_size:06x}")
    print(f"   entry      0x{entry:08x}")
    holes = [(s.addr, s.end) for s in sections]
    for i, (s, e) in enumerate(subtract_ranges((bss_addr, bss_addr + bss_size), holes)):
        print(f"   .bss.{i}     addr=0x{s:08x} size=0x{e - s:06x}  (carved)")
    return 0


# --- Ghidra side ------------------------------------------------------------

def build_program(dol_path: Path, project_dir: Path, project_name: str,
                  program_name: str, language_id: str, compiler_id: str,
                  analyze: bool, overwrite: bool) -> int:
    import pyghidra
    pyghidra.start(verbose=False)

    from ghidra.base.project import GhidraProject
    from ghidra.program.database import ProgramDB
    from ghidra.program.flatapi import FlatProgramAPI
    from ghidra.program.model.lang import CompilerSpecID, LanguageID
    from ghidra.program.model.symbol import SourceType
    from ghidra.program.util import DefaultLanguageService
    from ghidra.util.task import ConsoleTaskMonitor
    from java.io import ByteArrayInputStream
    from java.lang import Object as JObject

    data = dol_path.read_bytes()
    sections, bss_addr, bss_size, entry = parse_dol(data)

    lang = DefaultLanguageService.getLanguageService().getLanguage(LanguageID(language_id))
    cspec = lang.getCompilerSpecByID(CompilerSpecID(compiler_id))
    monitor = ConsoleTaskMonitor()

    # Ghidra rejects relative project paths outright ("Absolute path required").
    project_dir = project_dir.resolve()
    project_dir.mkdir(parents=True, exist_ok=True)
    gpr = project_dir / f"{project_name}.gpr"
    if gpr.exists():
        project = GhidraProject.openProject(str(project_dir), project_name, True)
    else:
        project = GhidraProject.createProject(str(project_dir), project_name, False)

    consumer = JObject()
    try:
        # GhidraProject cannot mint a blank program; construct the DB directly.
        program = ProgramDB(program_name, lang, cspec, consumer)
        tx = program.startTransaction("import DOL")
        try:
            mem = program.getMemory()
            space = program.getAddressFactory().getDefaultAddressSpace()
            api = FlatProgramAPI(program, monitor)

            def addr(v):
                return space.getAddress(v)

            # 1. one initialized block per populated DOL section
            for s in sections:
                blob = data[s.offset:s.offset + s.size]
                blk = mem.createInitializedBlock(
                    s.name, addr(s.addr),
                    ByteArrayInputStream(blob), s.size, monitor, False)
                blk.setRead(True)
                blk.setWrite(not s.is_text)
                blk.setExecute(s.is_text)
                blk.setSourceName("DOL")
                blk.setComment(f"DOL section {s.index} @ file offset 0x{s.offset:x}")
                print(f"  block {s.name:<10} 0x{s.addr:08x}-0x{s.end - 1:08x} "
                      f"{'RX' if s.is_text else 'RW'}")

            # 2. .bss, carved around any DOL section that lands inside it
            #    (.sdata/.sdata2 live inside the bss range on most DOLs)
            if bss_size:
                holes = [(s.addr, s.end) for s in sections]
                for i, (s, e) in enumerate(
                        subtract_ranges((bss_addr, bss_addr + bss_size), holes)):
                    name = ".bss" if i == 0 else f".bss{i}"
                    blk = mem.createUninitializedBlock(name, addr(s), e - s, False)
                    blk.setRead(True); blk.setWrite(True); blk.setExecute(False)
                    blk.setSourceName("DOL")
                    print(f"  block {name:<10} 0x{s:08x}-0x{e - 1:08x} RW (uninitialized)")

            # 3. GameCube hardware register window + OS globals, so xrefs to
            #    MMIO resolve instead of dangling.
            for name, start, size, comment in [
                ("OSGlobals", 0x80000000, 0x3100, "OS low memory globals"),
                ("HW_REGS", 0xCC000000, 0x8000, "GameCube memory-mapped IO"),
            ]:
                if mem.getBlock(addr(start)) is None:
                    blk = mem.createUninitializedBlock(name, addr(start), size, False)
                    blk.setRead(True); blk.setWrite(True); blk.setExecute(False)
                    blk.setVolatile(name == "HW_REGS")
                    blk.setComment(comment)
                    print(f"  block {name:<10} 0x{start:08x}-0x{start + size - 1:08x} RW")

            # 4. entry point
            ep = addr(entry)
            program.getSymbolTable().createLabel(ep, "__start", SourceType.IMPORTED)
            program.getSymbolTable().addExternalEntryPoint(ep)
            api.disassemble(ep)
            print(f"  entry point __start @ 0x{entry:08x}")
        finally:
            program.endTransaction(tx, True)

        if analyze:
            print("  running auto-analysis (this takes a while on a 2 MB DOL)...")
            from ghidra.app.plugin.core.analysis import AutoAnalysisManager
            tx = program.startTransaction("analyze")
            try:
                mgr = AutoAnalysisManager.getAnalysisManager(program)
                mgr.initializeOptions()
                mgr.reAnalyzeAll(None)
                mgr.startAnalysis(monitor)
            finally:
                program.endTransaction(tx, True)
            print(f"  functions found: {program.getFunctionManager().getFunctionCount()}")

        project.saveAs(program, "/", program_name, overwrite)
        print(f"saved {program_name} into {gpr}")
        # GhidraProject.close() releases tracked objects with itself as the
        # consumer, so hand it one before dropping ours.
        program.addConsumer(project)
        program.release(consumer)
    finally:
        project.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dol", required=True, type=Path, help="path to main.dol")
    ap.add_argument("--project-dir", type=Path, help="directory holding the .gpr")
    ap.add_argument("--project-name", help="Ghidra project name")
    ap.add_argument("--program-name", default="main.dol", help="name inside the project")
    ap.add_argument("--language", default="PowerPC:BE:32:default",
                    help="Ghidra language id (use the Gekko/Broadway one if installed)")
    ap.add_argument("--compiler", default="default", help="compiler spec id")
    ap.add_argument("--analyze", action="store_true", help="run auto-analysis after import")
    ap.add_argument("--overwrite", action="store_true", help="replace an existing program")
    ap.add_argument("--describe", action="store_true",
                    help="just print the parsed memory map and exit (no Ghidra needed)")
    args = ap.parse_args()

    if not args.dol.is_file():
        print(f"error: {args.dol} not found", file=sys.stderr)
        return 2

    if args.describe:
        return describe(args.dol)

    if not args.project_dir or not args.project_name:
        print("error: --project-dir and --project-name are required "
              "unless --describe is used", file=sys.stderr)
        return 2

    if not os.environ.get("GHIDRA_INSTALL_DIR"):
        print("error: GHIDRA_INSTALL_DIR is not set", file=sys.stderr)
        return 2

    return build_program(args.dol, args.project_dir, args.project_name,
                         args.program_name, args.language, args.compiler,
                         args.analyze, args.overwrite)


if __name__ == "__main__":
    sys.exit(main())
