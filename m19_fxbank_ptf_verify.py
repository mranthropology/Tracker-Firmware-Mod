"""M19: verify a built .ptf with the FX bank (plus master FX and Braids) against
what was tested.

Usage: m19_fxbank_ptf_verify.py [file.ptf]  (default out/sdtest/PolyendTracker_bank.ptf)

1. Data has no address gaps (the updater needs that) and ends below 1 MB.
2. Every byte the .ptf writes equals the harness image (stock + master FX +
   Braids + FX bank), except the harness's call trampoline at 0xff0 and the
   0xff filler between pieces (erased flash; zero in the harness).
3. Boot the decoded image as-is with every patch SDRAM area (Braids voices,
   FX bank state and plate lines, patch state) filled with 0xff, as power-on
   garbage, through the image's own boot hook; render 2 s of pattern 1 with
   the reverb and delay fed -> bit-identical to the harness-patched render,
   and the plate and tape really ran.
"""
import struct
import sys
from pathlib import Path

from m6_load_song import DEFAULT
from m7_render import LEFT, RATE, RIGHT
from patch.make_ptf import read_hex
from tracker_emu import (BLOCK_SAMPLES, BRAIDS_BIN, FLASH, FXBANK_BASE, FXBANK_BIN,
                         LIBC_INIT_ARRAY, PATCH_BASE, PATCH_DIR,
                         RETURN_MAGIC, TrackerEmu)

ROOT = Path(__file__).parent
GARBAGE = ((0x70F00000, 0x0F0000),)   # Braids voices, FX bank, patch state, Braids .data/.bss


def syms(path):
    return {k: int(v, 16) for k, v in (l.split() for l in path.read_text().splitlines())}


def render_2s(emu):
    emu.load_pattern(DEFAULT, 1)
    emu.play()
    out, prof = bytearray(), {}
    for _ in range(int(2.0 * RATE / BLOCK_SAMPLES)):
        emu.clock(BLOCK_SAMPLES / RATE)
        emu.render_block(LEFT, RIGHT, profile=prof)
        out += emu.uc.mem_read(LEFT, 256) + emu.uc.mem_read(RIGHT, 256)
    return bytes(out), prof


def boot(emu, patch_harness, boot_entry=None):
    emu.reset_runtime()
    emu.call(LIBC_INIT_ARRAY)
    emu.load_project(DEFAULT)
    if patch_harness:
        emu.apply_master_fx_patch()
        emu.apply_braids_patch()
        emu.apply_fxbank_patch()
        emu.apply_ct_patch()
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
    ptf = Path(args[0]) if args else ROOT / "out" / "sdtest" / "PolyendTracker_bank.ptf"
    image, ranges = read_hex(ptf)
    contiguous = len(ranges) == 1 and ranges[0][0] == 0
    ok &= contiguous and len(image) <= FLASH[1]
    print(f"1. {ptf.name}: data ranges {[(hex(a), hex(b)) for a, b in ranges]} "
          f"({'one block from 0x0' if contiguous else 'GAPS'}); ends {len(image):#x}")

    harness = TrackerEmu()
    harness.apply_master_fx_patch()
    harness.apply_braids_patch()
    harness.apply_fxbank_patch()
    harness.apply_ct_patch()
    tested = bytes(harness.uc.mem_read(FLASH[0], FLASH[1]))
    stock = (ROOT / "firmware" / "tracker_1.9.2.bin").read_bytes()
    fx = PATCH_DIR / "master_fx.bin"
    bsyms = syms(BRAIDS_BIN.with_suffix(".bin.syms"))
    braids_base = bsyms["braids_voice_size"] & ~0xFFF
    filler = list(range(len(stock), PATCH_BASE)) + \
        list(range(PATCH_BASE + fx.stat().st_size, braids_base)) + \
        list(range(braids_base + BRAIDS_BIN.stat().st_size, FXBANK_BASE))
    filler_set = set(filler)
    trampoline = {RETURN_MAGIC & ~1, (RETURN_MAGIC & ~1) + 1}
    diff = [a for a in range(len(image))
            if a not in trampoline and a not in filler_set and image[a] != tested[a]]
    gap_ok = all(image[a] == 0xFF for a in filler)
    tramp_ok = all(image[a] == stock[a] for a in trampoline)
    vt_ok = all(struct.unpack_from("<I", image, vt)[0] == syms(FXBANK_BIN.with_suffix(".bin.syms"))[n]
                for vt, n in ((0xA9EFC, "reverb_update"), (0xA9F54, "delay_update")))
    ok &= not diff and gap_ok and tramp_ok and vt_ok
    print(f"2. {len(image):,} bytes vs harness image (stock + master FX + Braids + FX bank + CT): "
          f"{'identical' if not diff else f'{len(diff)} differ, first {diff[0]:#x}'}; "
          f"filler 0xff: {'yes' if gap_ok else 'NO'}; trampoline bytes stock: "
          f"{'yes' if tramp_ok else 'NO'}; vtables -> bank: {'yes' if vt_ok else 'NO'}")

    decoded = ROOT / "out" / "ptf_decoded.bin"
    decoded.write_bytes(bytes(image))
    try:
        from_ptf = TrackerEmu(decoded)
        boot(from_ptf, False, bsyms["braids_boot"])
        a, prof_a = render_2s(from_ptf)
    finally:
        decoded.unlink()
    ref = TrackerEmu()
    boot(ref, True)
    b, _ = render_2s(ref)
    same = a == b
    peak = max(abs(v) for v in struct.unpack(f"<{len(a) // 2}h", a))
    ok &= same and peak > 0
    blocks = int(2.0 * RATE / BLOCK_SAMPLES)
    rev, dly = prof_a.get(0xA9EFC, 0) / blocks, prof_a.get(0xA9F54, 0) / blocks
    print(f"3. decoded image booted as-is with garbage SDRAM, 2 s of pattern 1: "
          f"{'bit-identical' if same else 'DIFFERENT'} to the harness-patched render (peak {peak}); "
          f"bank reverb {rev:,.0f} / delay {dly:,.0f} insns/block")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
