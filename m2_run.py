"""M2: initialise the audio block pool and step the engine; profile node cost.

Without a playing instrument the output should be digital silence, and the
pool must not leak blocks between ticks.
"""
import struct

from m1_boot import KNOWN
from tracker_emu import (BLOCK_SAMPLES, LIBC_INIT_ARRAY, MEMORY_USED, SDRAM,
                         TrackerEmu)

LEFT, RIGHT = SDRAM[0] + 0x00FF_0000, SDRAM[0] + 0x00FF_0200  # scratch buffers
TICKS = 64


def main() -> None:
    emu = TrackerEmu()
    emu.reset_runtime()
    emu.call(LIBC_INIT_ARRAY)
    emu.init_audio_memory()
    profile: dict[int, int] = {}
    used = []
    peak = 0
    for _ in range(TICKS):
        emu.render_block(LEFT, RIGHT, profile)
        used.append(struct.unpack("<H", emu.uc.mem_read(MEMORY_USED, 2))[0])
        for buf in (LEFT, RIGHT):
            samples = struct.unpack(f"<{BLOCK_SAMPLES}h", emu.uc.mem_read(buf, 256))
            peak = max(peak, max(abs(s) for s in samples))
    active = sum(1 for n, _ in emu.update_list() if emu.uc.mem_read(n + 4, 1)[0])
    per_tick = sum(profile.values()) / TICKS
    print(f"active nodes: {active} / {len(emu.update_list())}")
    print(f"blocks in use after each tick: min={min(used)} max={max(used)}")
    print(f"output peak over {TICKS} ticks: {peak}  (expect 0 = silence)")
    print(f"engine cost: {per_tick:,.0f} instructions per 128-frame tick "
          f"(~{per_tick * 44100 / 128 / 1e6:.1f} M instr/s at 44.1 kHz)")
    print(f"\n{'vtable':>9} {'instr/tick':>11}  label")
    for vt, n in sorted(profile.items(), key=lambda kv: -kv[1])[:15]:
        print(f"{vt:#9x} {n / TICKS:11,.0f}  {KNOWN.get(vt, ('?',))[0]}")


if __name__ == "__main__":
    main()
