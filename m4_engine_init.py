"""M4: run the firmware's engine init (0x1611c) against a stubbed HAL/codec.

Reports every HAL/codec virtual call the init makes, then re-runs the M3 tone
test through the initialised engine to compare against constructor defaults.
"""
import math
import struct
from collections import Counter

from m3_tone import LEFT, RIGHT, tone_source
from tracker_emu import (DRY_MIXER, LIBC_INIT_ARRAY, MEMORY_USED, MIXER_SECTION,
                         TrackerEmu)

MASTER_MIXER = (MIXER_SECTION + 0x210, MIXER_SECTION + 0x258)


def main() -> None:
    emu = TrackerEmu()
    emu.reset_runtime()
    emu.call(LIBC_INIT_ARRAY)
    before = emu.stats.insns
    emu.init_engine()
    print(f"engine init returned after {emu.stats.insns - before:,} instructions")
    print(f"audio blocks in use: {struct.unpack('<H', emu.uc.mem_read(MEMORY_USED, 2))[0]}")
    print(f"unmapped accesses: {emu.stats.unmapped[:5] or 'none'}")

    print("\nHAL / codec virtual calls (vtable offset @ call site, count):")
    for (slot, caller), n in Counter((s, c) for s, c, _ in emu.stub_calls).most_common():
        print(f"  slot {slot:#04x} from {caller:#07x}  x{n}")

    print("\ndecoded codec calls:")
    for line in emu.codec_log():
        print(f"  {line}")

    sat = [emu.uc.mem_read(m + 0x14, 1)[0] for m in MASTER_MIXER]
    print(f"\nmaster saturation mode after init: {sat}")

    make = tone_source(1000.0, 0.25)
    overrides = {DRY_MIXER[0]: make(0.0), DRY_MIXER[1]: make(1e-9)}
    peak = 0
    for _ in range(40):
        emu.render_block(LEFT, RIGHT, overrides=overrides)
        peak = max(peak, max(map(abs, struct.unpack("<128h", emu.uc.mem_read(LEFT, 256)))))
    gain = 20 * math.log10(peak / 8191) if peak else float("-inf")
    print(f"-12 dBFS tone through initialised engine: out peak {peak} ({gain:+.2f} dB)")


if __name__ == "__main__":
    main()
