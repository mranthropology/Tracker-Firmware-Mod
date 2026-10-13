"""Build a patched Tracker .ptf (Intel HEX) from the stock 1.9.2 .ptf.

Usage: python3 -m patch.make_ptf <stock .ptf> <out .ptf> [--hooks LIST] [--marker TEXT]
  --hooks   comma list of: boot, audio, ui, codec (default: all). "none" =
            no patch code at all (marker only).
  --marker  version suffix shown in Config (e.g. "T1"): the version format
            strings move into the appended region, so the text appears only
            if the updater really programmed that region.
Output names must start with PolyendTracker_, end with .ptf and be at most
31 characters: the updater shows 32 characters of a 33-character name and
then garbage (seen on hardware), so its name field has no room for longer names.

Applies exactly what the harness tests (tracker_emu.apply_master_fx_patch):
  0xD7000  master_fx.bin (EQ + DJ filter, UI hooks, boot reset)
  0x76C6A  bl 0xd42f0 -> bl master_fx_entry   (master mixer saturator call)
  0x704C0  codec EQ setter -> bx lr            (EQ moves from codec to the patch)
  0x0F3FA  bl 0x1611c -> bl patch_boot         (state reset before audio starts)
  0x0F628  b.w 0x28f04 -> b.w ui_knob
  0x0F636  b.w 0x28f0c -> b.w ui_button        (press)
  0x0F646  b.w 0x28f0c -> b.w ui_button        (release)

Every site is checked against the stock bytes first. The stock file's records
(which follow the original linker sections) are kept as they are: only the
records covering patched addresses get new data and checksums, and the patch
is appended as 16-byte records before the end record, starting right at the
image end (0xff filler up to 0xD7000) so the data has no gap; hardware probes
showed the updater fails on a gap. The build is refused
unless re-encoding the unmodified image gives back the stock .ptf byte for
byte, and the output is read back and compared with the patched image.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from patch.link import thumb_branch

PATCH_DIR = Path(__file__).parent
FLASH_SIZE = 0x100000             # MK66FX1M0 program flash
FLASH_CONFIG = (0x400, 0x410)     # Kinetis flash configuration field (FSEC at 0x40C)
PATCH_BASE = 0xD7000


def read_hex(path: Path) -> tuple[bytearray, list[tuple[int, int]]]:
    """Return (image, data ranges). Image is 0xFF-filled up to the highest address."""
    data: dict[int, int] = {}
    base = 0
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if not line:
            continue
        rec = bytes.fromhex(line[1:])
        if line[0] != ":" or sum(rec) & 0xFF or rec[0] != len(rec) - 5:
            raise ValueError(f"{path.name}:{n}: bad record")
        count, addr, kind, payload = rec[0], int.from_bytes(rec[1:3], "big"), rec[3], rec[4:-1]
        if kind == 0:
            for i, b in enumerate(payload):
                data[base + addr + i] = b
        elif kind == 2:
            base = int.from_bytes(payload, "big") << 4
        elif kind == 4:
            base = int.from_bytes(payload, "big") << 16
        elif kind == 1:
            break
        else:
            raise ValueError(f"{path.name}:{n}: unsupported record type {kind}")
    image = bytearray(b"\xff" * (max(data) + 1))
    for a, b in data.items():
        image[a] = b
    ranges, start = [], None
    for a in range(len(image) + 1):
        present = a in data
        if present and start is None:
            start = a
        if not present and start is not None:
            ranges.append((start, a))
            start = None
    return image, ranges


def record(kind: int, addr: int, payload: bytes) -> str:
    body = bytes([len(payload), addr >> 8 & 0xFF, addr & 0xFF, kind]) + payload
    return ":" + (body + bytes([-sum(body) & 0xFF])).hex().upper()


def write_hex(stock_text: str, image: bytes, extra: tuple[int, int] | None = None) -> str:
    """Re-emit the stock records with data from `image`; append `extra` range."""
    out, base, segment = [], 0, 0
    for line in stock_text.splitlines():
        if not line:
            continue
        rec = bytes.fromhex(line[1:])
        count, addr, kind = rec[0], int.from_bytes(rec[1:3], "big"), rec[3]
        if kind == 2:
            base = int.from_bytes(rec[4:6], "big") << 4
            segment = base >> 16
        if kind == 0:
            a = base + addr
            out.append(record(0, addr, bytes(image[a:a + count])))
        elif kind == 1:
            if extra:
                a, end = extra
                while a < end:
                    if a >> 16 != segment:
                        segment = a >> 16
                        out.append(record(2, 0, (segment << 12).to_bytes(2, "big")))
                    chunk_end = min(end, a + 16, (segment + 1) << 16)
                    out.append(record(0, a & 0xFFFF, bytes(image[a:chunk_end])))
                    a = chunk_end
            out.append(line)
        else:
            out.append(line)
    return "\r\n".join(out) + "\r\n"


VERSION_FORMATS = {0x2887C: (0xA2B6C, b"%d.%d.%d beta %d"),   # literal pool -> format
                   0x288A0: (0xA2B60, b"%d.%d.%d")}           # (startup 0x2878c)
ALL_HOOKS = ("boot", "audio", "ui", "codec")


def patched_image(stock: bytearray, hooks=ALL_HOOKS, marker: str | None = None) -> tuple[bytearray, dict]:
    import struct
    binary = (PATCH_DIR / "master_fx.bin").read_bytes() if hooks else b""
    syms = {k: int(v, 16) for k, v in (l.split() for l in
            (PATCH_DIR / "master_fx.bin.syms").read_text().splitlines())}
    if marker:
        binary += b"\0" * (-len(binary) % 4)
        marker_at = {}
        for pool, (old, fmt) in VERSION_FORMATS.items():
            marker_at[pool] = PATCH_BASE + len(binary)
            binary += fmt + b" " + marker.encode("ascii") + b"\0"
    image = bytearray(stock) + b"\xff" * (PATCH_BASE + len(binary) - len(stock))
    if any(b != 0xFF for b in image[len(stock):PATCH_BASE]) or len(stock) > PATCH_BASE:
        raise RuntimeError("patch area overlaps the stock image")
    if PATCH_BASE + len(binary) > FLASH_SIZE:
        raise RuntimeError("patch does not fit in program flash")
    image[PATCH_BASE:PATCH_BASE + len(binary)] = binary

    sites = [  # (hook group, address, stock target, link, new target)
        ("audio", 0x76C6A, 0xD42F0, True, syms["master_fx_entry"]),
        ("boot", 0x0F3FA, 0x1611C, True, syms["patch_boot"]),
        ("ui", 0x0F628, 0x28F04, False, syms["ui_knob"]),
        ("ui", 0x0F636, 0x28F0C, False, syms["ui_button"]),
        ("ui", 0x0F646, 0x28F0C, False, syms["ui_button"]),
    ]
    sites = [site[1:] for site in sites if site[0] in hooks]
    for addr, old, link, new in sites:
        if bytes(image[addr:addr + 4]) != thumb_branch(addr, old, link):
            raise RuntimeError(f"unexpected stock bytes at {addr:#x}")
        image[addr:addr + 4] = thumb_branch(addr, new & ~1, link)
    if "codec" in hooks:
        if bytes(image[0x704C0:0x704C2]) != bytes.fromhex("70b5"):   # push {r4-r6, lr}
            raise RuntimeError("unexpected stock bytes at 0x704c0")
        image[0x704C0:0x704C2] = b"\x70\x47"                         # bx lr
    if marker:
        for pool, (old, _) in VERSION_FORMATS.items():
            if struct.unpack_from("<I", image, pool)[0] != old:
                raise RuntimeError(f"unexpected literal at {pool:#x}")
            struct.pack_into("<I", image, pool, marker_at[pool])
    if image[FLASH_CONFIG[0]:FLASH_CONFIG[1]] != stock[FLASH_CONFIG[0]:FLASH_CONFIG[1]]:
        raise RuntimeError("flash configuration field changed")
    return image, {"patch_bytes": len(binary), "syms": syms}


def main() -> None:
    args = sys.argv[1:]
    opts = {k: args[i + 1] for i, k in enumerate(args) if k.startswith("--")}
    stock_path, out_path = (Path(a) for a in [a for a in args if not a.startswith("--")
                                              and a not in opts.values()][:2])
    hooks = ALL_HOOKS if "--hooks" not in opts else (
        () if opts["--hooks"] == "none" else tuple(opts["--hooks"].split(",")))
    if set(hooks) - set(ALL_HOOKS):
        raise SystemExit(f"unknown hooks: {set(hooks) - set(ALL_HOOKS)}")
    marker = opts.get("--marker")
    name = out_path.name
    if not (name.startswith("PolyendTracker_") and name.endswith(".ptf") and len(name) <= 31):
        raise SystemExit(f"bad output name {name!r}: needs PolyendTracker_*.ptf, at most 31 characters")
    stock_text = stock_path.read_text(newline="")
    stock, ranges = read_hex(stock_path)
    if write_hex(stock_text, stock) != stock_text:
        raise SystemExit("re-encoding the stock image does not reproduce the stock .ptf; refusing")
    image, info = patched_image(stock, hooks, marker)
    # Data must run on from the image end without a gap: on hardware a file with
    # a gap (image end 0xd6460, patch at 0xd7000) hung after flashing (t1), and
    # the same marker with 0xff filler from the image end booted (probe p2).
    extra = (len(stock), PATCH_BASE + info["patch_bytes"])
    ranges = ranges + [extra]
    text = write_hex(stock_text, image, extra)
    out_path.write_bytes(text.encode("ascii"))

    check, check_ranges = read_hex(out_path)                     # round trip
    covered = lambda rs: {a for s, e in rs for a in range(s, e)}
    assert check == image and covered(check_ranges) == covered(ranges)
    changed = [a for a in range(len(stock)) if image[a] != stock[a]]
    print(f"wrote {out_path} ({len(text):,} bytes)")
    print(f"  stock image 0x0-{len(stock):#x} unchanged except {len(changed)} bytes at "
          + ", ".join(sorted({f'{a & ~3:#x}' for a in changed})))
    print(f"  hooks: {', '.join(hooks) or 'none'}; marker: {marker or '-'}")
    print(f"  patch {info['patch_bytes']:,} bytes at {PATCH_BASE:#x}-{PATCH_BASE + info['patch_bytes']:#x}")
    print(f"  flash config field 0x400-0x40f unchanged; image ends below {FLASH_SIZE:#x}")
    print(f"  sha256 {hashlib.sha256(text.encode()).hexdigest()}")


if __name__ == "__main__":
    main()
