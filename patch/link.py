"""Minimal static linker for one Cortex-M4 ELF object -> raw image at a fixed address.

Lays out .text then .rodata*, resolves R_ARM_ABS32, R_ARM_THM_CALL,
R_ARM_THM_JUMP24 and MOVW/MOVT relocations. Refuses undefined symbols (the
patch must be self-contained).

.data/.bss are refused unless a RAM base is given: then .data* (and
.data.rel.ro*) are laid out at that RAM address, .bss after it, and the
initial .data contents are appended to the flash image. The exports then
include __data_load (flash), __data_start (RAM), __data_size, __bss_start,
__bss_size; something must copy/zero them before use (the harness does it,
on the device the boot hook must). .ARM.exidx unwind tables are dropped.
Usage:
    link.py <object.o> <base_hex> <out.bin> [ram_base_hex]   -> also writes <out.bin>.syms
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

R_ARM_ABS32, R_ARM_THM_CALL, R_ARM_THM_JUMP24 = 2, 10, 30
R_ARM_THM_MOVW_ABS_NC, R_ARM_THM_MOVT_ABS = 47, 48


def parse(path: Path):
    d = path.read_bytes()
    shoff, = struct.unpack_from("<I", d, 0x20)
    shentsize, shnum, shstrndx = struct.unpack_from("<HHH", d, 0x2E)
    secs = []
    for i in range(shnum):
        name, typ, flags, addr, off, size, link, info, align, entsize = \
            struct.unpack_from("<10I", d, shoff + i * shentsize)
        secs.append(dict(name=name, type=typ, flags=flags, off=off, size=size,
                         link=link, info=info, align=max(align, 1), entsize=entsize))
    names = secs[shstrndx]
    for s in secs:
        end = d.index(b"\0", names["off"] + s["name"])
        s["name"] = d[names["off"] + s["name"]:end].decode()
    return d, secs


def symbols(d, secs):
    symtab = next(s for s in secs if s["type"] == 2)
    strtab = secs[symtab["link"]]
    out = []
    for i in range(symtab["size"] // 16):
        name, value, size, info, other, shndx = struct.unpack_from(
            "<IIIBBH", d, symtab["off"] + 16 * i)
        end = d.index(b"\0", strtab["off"] + name)
        out.append(dict(name=d[strtab["off"] + name:end].decode(), value=value,
                        shndx=shndx, type=info & 0xF))
    return out


def thumb_branch(addr: int, target: int, link: bool) -> bytes:
    offset = target - (addr + 4)
    if not -(1 << 24) <= offset < (1 << 24):
        raise ValueError("branch out of range")
    s = (offset >> 24) & 1
    i1, i2 = (offset >> 23) & 1, (offset >> 22) & 1
    j1, j2 = (~i1 ^ s) & 1, (~i2 ^ s) & 1
    hi = 0xF000 | (s << 10) | ((offset >> 12) & 0x3FF)
    lo = (0xD000 if link else 0x9000) | (j1 << 13) | (j2 << 11) | ((offset >> 1) & 0x7FF)
    return struct.pack("<HH", hi, lo)


def thumb_movw_movt(insn: bytes, value: int) -> bytes:
    hi, lo = struct.unpack("<HH", insn)
    imm16 = value & 0xFFFF
    hi = (hi & 0xFBF0) | ((imm16 >> 12) & 0xF) | (((imm16 >> 11) & 1) << 10)
    lo = (lo & 0x8F00) | (((imm16 >> 8) & 7) << 12) | (imm16 & 0xFF)
    return struct.pack("<HH", hi, lo)


def link(obj: Path, base: int, ram_base: int | None = None):
    d, secs = parse(obj)
    is_data = lambda s: s["name"].startswith((".data", ".bss"))
    if ram_base is None:
        for s in secs:
            if s["flags"] & 2 and s["size"] and is_data(s):
                raise SystemExit(f"patch must not use {s['name']} (use the fixed state address)")
    layout, image, addr = {}, bytearray(), base
    alloc = [i for i, s in enumerate(secs) if s["flags"] & 2 and s["size"]
             and (s["name"].startswith(".text") or s["name"].startswith(".rodata"))]
    alloc.sort(key=lambda i: not secs[i]["name"].startswith(".text"))
    for i in alloc:
        s = secs[i]
        while (addr - base) % s["align"]:
            image.append(0)
            addr += 1
        layout[i] = addr
        image += d[s["off"]:s["off"] + s["size"]]
        addr += s["size"]

    extra_syms = {}
    data_spans = {}                                   # section -> offset in data image
    if ram_base is not None:
        data = bytearray()
        dsecs = [i for i, s in enumerate(secs) if s["flags"] & 2 and s["size"]
                 and s["name"].startswith(".data")]
        bsecs = [i for i, s in enumerate(secs) if s["flags"] & 2 and s["size"]
                 and s["name"].startswith(".bss")]
        for i in dsecs:
            s = secs[i]
            while len(data) % s["align"]:
                data.append(0)
            layout[i] = ram_base + len(data)
            data_spans[i] = len(data)
            data += d[s["off"]:s["off"] + s["size"]]
        bss_start = ram_base + len(data)
        bss_end = bss_start
        for i in bsecs:
            s = secs[i]
            while bss_end % s["align"]:
                bss_end += 1
            layout[i] = bss_end
            bss_end += secs[i]["size"]
        while len(image) % 4:
            image.append(0)
        load = base + len(image)
        extra_syms = {"__data_load": load, "__data_start": ram_base, "__data_size": len(data),
                      "__bss_start": bss_start, "__bss_size": bss_end - bss_start}
    syms = symbols(d, secs)

    def sym_addr(sym):
        if sym["shndx"] == 0:
            raise SystemExit(f"undefined symbol {sym['name']!r} (patch must be self-contained)")
        return layout[sym["shndx"]] + sym["value"]

    for rel in (s for s in secs if s["type"] == 9 and s["info"] in layout
                and secs[s["info"]]["type"] != 8):            # no relocs into .bss
        target_base = layout[rel["info"]]
        in_data = rel["info"] in data_spans
        buf = data if in_data else image
        for k in range(rel["size"] // 8):
            r_off, r_info = struct.unpack_from("<II", d, rel["off"] + 8 * k)
            rtype, sym = r_info & 0xFF, syms[r_info >> 8]
            place = target_base + r_off
            pos = data_spans[rel["info"]] + r_off if in_data else place - base
            image_saved, image = image, buf
            S = sym_addr(sym) | (1 if sym["type"] == 2 else 0)   # STT_FUNC -> Thumb bit
            if rtype == R_ARM_ABS32:
                A, = struct.unpack_from("<I", image, pos)
                struct.pack_into("<I", image, pos, (S + A) & 0xFFFFFFFF)
            elif rtype in (R_ARM_THM_CALL, R_ARM_THM_JUMP24):
                image[pos:pos + 4] = thumb_branch(place, S & ~1, rtype == R_ARM_THM_CALL)
            elif rtype == R_ARM_THM_MOVW_ABS_NC:
                image[pos:pos + 4] = thumb_movw_movt(bytes(image[pos:pos + 4]), S)
            elif rtype == R_ARM_THM_MOVT_ABS:
                image[pos:pos + 4] = thumb_movw_movt(bytes(image[pos:pos + 4]), S >> 16)
            else:
                raise SystemExit(f"unsupported relocation type {rtype} at {place:#x}")
            image = image_saved
    if ram_base is not None:
        image += data
    exports = {s["name"]: sym_addr(s) for s in syms
               if s["type"] == 2 and s["shndx"] in layout}
    exports.update(extra_syms)
    return bytes(image), exports


if __name__ == "__main__":
    obj, base, out = Path(sys.argv[1]), int(sys.argv[2], 16), Path(sys.argv[3])
    ram = int(sys.argv[4], 16) if len(sys.argv) > 4 else None
    image, exports = link(obj, base, ram)
    out.write_bytes(image)
    out.with_suffix(out.suffix + ".syms").write_text(
        "".join(f"{name} {addr:#010x}\n" for name, addr in sorted(exports.items())))
    print(f"{out.name}: {len(image)} bytes at {base:#x}; " +
          ", ".join(f"{n}={a:#x}" for n, a in sorted(exports.items())))
