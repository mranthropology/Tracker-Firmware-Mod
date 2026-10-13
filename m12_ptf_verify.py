"""M12: verify the built .ptf is exactly what the harness tested.

Usage: m12_ptf_verify.py [built.ptf] [--no-render]

1. Decode the .ptf and compare all of flash with the harness's own patched
   image (stock firmware + apply_master_fx_patch()).
2. Boot the decoded image as-is (no harness patching, patch state filled with
   0xff like power-on SDRAM; engine init through its own patch_boot), play
   pattern 1 with the Shift+Master+jog gesture, and compare the WAV with the
   harness-patched render.
"""
import sys
from pathlib import Path

from patch.make_ptf import read_hex
from tracker_emu import FLASH, PATCH_BASE as PATCH_DIR_BASE, PATCH_DIR, RETURN_MAGIC, TrackerEmu

OUT = Path(__file__).parent / "out"


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    ptf = Path(args[0]) if args else OUT / "PolyendTracker_1.9.2_masterfx.ptf"
    image, ranges = read_hex(ptf)
    emu = TrackerEmu()
    emu.apply_master_fx_patch()
    tested = bytes(emu.uc.mem_read(FLASH[0], FLASH[1]))
    # The harness writes its own 2-byte return trampoline at RETURN_MAGIC; on the
    # device those bytes are the stock ones (the .ptf carries the stock value).
    trampoline = range(RETURN_MAGIC & ~1, (RETURN_MAGIC & ~1) + 2)
    stock = Path(__file__).parent / "firmware" / "tracker_1.9.2.bin"
    stock_tramp = stock.read_bytes()[trampoline.start:trampoline.stop]
    written = sum(e - s for s, e in ranges)
    # Filler between the stock image end and the patch is 0xff in the .ptf
    # (erased flash); the harness leaves that never-executed gap zeroed.
    filler = range(len(stock.read_bytes()), PATCH_DIR_BASE)
    gap_ok = all(image[a] == 0xFF for a in filler if a < len(image))
    diff = [a for s, e in ranges for a in range(s, e)
            if a not in trampoline and a not in filler and image[a] != tested[a]]
    diff += [] if gap_ok else [filler.start]
    tramp_ok = bytes(image[trampoline.start:trampoline.stop]) == stock_tramp
    print(f"1. {ptf.name}: {written:,} bytes written by the .ptf vs harness-patched image: "
          f"{'identical' if not diff else f'{len(diff)} differ, first {diff[0]:#x}'} "
          f"(harness trampoline at {trampoline.start:#x} excluded; .ptf has the stock bytes there: "
          f"{'yes' if tramp_ok else 'NO'}; filler {filler.start:#x}-{filler.stop:#x} all 0xff: "
          f"{'yes' if gap_ok else 'NO'})")
    diff += [] if tramp_ok else [trampoline.start]
    if "--no-render" in sys.argv:
        sys.exit(0 if not diff else 1)

    firmware = OUT / "ptf_decoded.bin"
    firmware.write_bytes(bytes(image))
    syms = {k: int(v, 16) for k, v in (l.split() for l in
            (PATCH_DIR / "master_fx.bin.syms").read_text().splitlines())}
    from m10_dj_control import demo
    from_ptf = demo(firmware, syms["patch_boot"], "djcontrol_from_ptf")
    harness = OUT / "burly_ladybug_1-23-24_pattern01_djcontrol.wav"
    if not harness.exists():
        harness = demo()
    same = from_ptf.read_bytes() == harness.read_bytes()
    print(f"2. render from the .ptf image (power-on garbage state) vs harness-patched render: "
          f"{'bit-identical' if same else 'DIFFERENT'}")
    firmware.unlink()
    sys.exit(0 if not diff and same else 1)


if __name__ == "__main__":
    main()
