"""M15: verify a built .ptf with the Braids instrument against what was tested.

Usage: m15_braids_ptf_verify.py [plain.ptf]  (default out/PolyendTracker_brplain.ptf)

1. Data has no address gaps (the updater needs that) and ends below 1 MB.
2. Every byte the .ptf writes equals the harness image (stock + master FX +
   Braids patches), except the harness's own call trampoline at 0xff0 and
   the 0xff filler between pieces (erased flash; zero in the harness).
3. Boot the decoded image as-is: SDRAM patch areas filled with 0xff (power-on
   garbage), engine init through the image's own boot hook (braids_boot ->
   patch_boot), tracks 1 and 4 renamed to "BR-WAVEMAP"/"BR-FM" (the form the
   on-device keyboard can type), 2 s of pattern 1 -> must be bit-identical to
   the harness-patched render.
"""
import struct
import sys
from pathlib import Path

from m6_load_song import DEFAULT
from m7_render import LEFT, RATE, RIGHT
from patch.make_ptf import read_hex
from tracker_emu import (BLOCK_SAMPLES, BRAIDS_BIN, FLASH, LIBC_INIT_ARRAY, PATCH_BASE,
                         PATCH_DIR, RETURN_MAGIC, TrackerEmu)

ROOT = Path(__file__).parent
INSTR, INSTR_SIZE = 0x200001CC, 0x174
GARBAGE = ((0x70F00000, 0x0F0000),   # Braids voices, patch state, Braids .data/.bss
           )


def syms(path):
    return {k: int(v, 16) for k, v in (l.split() for l in path.read_text().splitlines())}


def render_2s(emu):
    emu.uc.mem_write(INSTR + 3 * INSTR_SIZE + 0x05, b"BR-WAVEMAP".ljust(32, b"\0"))
    emu.uc.mem_write(INSTR + 3 * INSTR_SIZE + 0x3C, bytes([7]))
    emu.uc.mem_write(INSTR + 8 * INSTR_SIZE + 0x05, b"BR-FM".ljust(32, b"\0"))
    emu.uc.mem_write(INSTR + 8 * INSTR_SIZE + 0x3C, bytes([7]))
    emu.load_pattern(DEFAULT, 1)
    emu.play()
    out = bytearray()
    for _ in range(int(2.0 * RATE / BLOCK_SAMPLES)):
        emu.clock(BLOCK_SAMPLES / RATE)
        emu.render_block(LEFT, RIGHT)
        out += emu.uc.mem_read(LEFT, 256) + emu.uc.mem_read(RIGHT, 256)
    return bytes(out)


def boot(emu, patch_harness, boot_entry=None):
    emu.reset_runtime()
    emu.call(LIBC_INIT_ARRAY)
    emu.load_project(DEFAULT)
    if patch_harness:
        emu.apply_master_fx_patch()
        emu.apply_braids_patch()
    else:
        emu._boot_entry = boot_entry
        for base, size in GARBAGE:
            emu.uc.mem_write(base, b"\xff" * size)
    emu.init_engine()
    emu.init_sequencer()
    emu.load_instruments(DEFAULT)


def main():
    ok = True
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    ptf = Path(args[0]) if args else ROOT / "out" / "PolyendTracker_brplain.ptf"
    image, ranges = read_hex(ptf)
    contiguous = len(ranges) == 1 and ranges[0][0] == 0
    ok &= contiguous and len(image) <= FLASH[1]
    print(f"1. {ptf.name}: data ranges {[(hex(a), hex(b)) for a, b in ranges]} "
          f"({'one block from 0x0' if contiguous else 'GAPS'}); ends {len(image):#x}")

    harness = TrackerEmu()
    harness.apply_master_fx_patch()
    harness.apply_braids_patch()
    tested = bytes(harness.uc.mem_read(FLASH[0], FLASH[1]))
    stock = (ROOT / "firmware" / "tracker_1.9.2.bin").read_bytes()
    fx = PATCH_DIR / "master_fx.bin"
    bsyms = syms(BRAIDS_BIN.with_suffix(".bin.syms"))
    braids_base = bsyms["braids_voice_size"] & ~0xFFF
    filler = list(range(len(stock), PATCH_BASE)) + \
        list(range(PATCH_BASE + fx.stat().st_size, braids_base))
    filler_set = set(filler)
    trampoline = {RETURN_MAGIC & ~1, (RETURN_MAGIC & ~1) + 1}
    diff = [a for a in range(len(image))
            if a not in trampoline and a not in filler_set and image[a] != tested[a]]
    gap_ok = all(image[a] == 0xFF for a in filler)
    tramp_ok = all(image[a] == stock[a] for a in trampoline)
    ok &= not diff and gap_ok and tramp_ok
    print(f"2. {len(image):,} bytes vs harness image (stock + master FX + Braids): "
          f"{'identical' if not diff else f'{len(diff)} differ, first {diff[0]:#x}'}; "
          f"filler 0xff: {'yes' if gap_ok else 'NO'}; trampoline bytes stock: {'yes' if tramp_ok else 'NO'}")

    decoded = ROOT / "out" / "ptf_decoded.bin"
    decoded.write_bytes(bytes(image))
    try:
        from_ptf = TrackerEmu(decoded)
        boot(from_ptf, False, bsyms["braids_boot"])
        a = render_2s(from_ptf)
    finally:
        decoded.unlink()
    ref = TrackerEmu()
    boot(ref, True)
    b = render_2s(ref)
    same = a == b
    peak = max(abs(v) for v in struct.unpack(f"<{len(a) // 2}h", a))
    ok &= same and peak > 0
    print(f"3. decoded image booted as-is with garbage SDRAM, BR-WAVEMAP + BR-FM, 2 s: "
          f"{'bit-identical' if same else 'DIFFERENT'} to the harness-patched render (peak {peak})")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
