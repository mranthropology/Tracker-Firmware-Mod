"""M5: load a real project through the firmware's loader, then run engine init.

Usage: m5_project.py [project_dir]   (default: projects/burly ladybug 1-23-24)
"""
import math
import struct
import sys
from pathlib import Path

from m3_tone import LEFT, RIGHT, tone_source
from tracker_emu import (DRY_MIXER, LIBC_INIT_ARRAY, MASTER_EQ_GAINS,
                         MIXER_SECTION, PROJECT_SETTINGS, TrackerEmu)

DEFAULT = Path(__file__).parent / "projects" / "burly ladybug 1-23-24"
MASTER_MIXER = (MIXER_SECTION + 0x210, MIXER_SECTION + 0x258)


def main() -> None:
    project = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    emu = TrackerEmu()
    emu.reset_runtime()
    emu.call(LIBC_INIT_ARRAY)

    info = emu.load_project(project)
    print(f"loaded {project.name}: {info['bytes']} bytes, file version {info['version']}")
    print(f"  firmware read requests: {info['reads']}")
    tempo = struct.unpack("<f", emu.uc.mem_read(PROJECT_SETTINGS + 0x2C + 2, 4))[0]
    print(f"  float at settings+0x2e (file 0x13c, likely tempo): {tempo:.1f}")
    print(f"  master EQ gains: {[round(x, 3) for x in emu.floats(MASTER_EQ_GAINS, 5)]}")

    load_calls = len(emu.stub_calls)
    emu.init_engine()
    emu.stub_calls = emu.stub_calls[load_calls:]
    print("\ncodec calls during engine init (project loaded):")
    for line in emu.codec_log():
        print(f"  {line}")
    sat = [emu.uc.mem_read(m + 0x14, 1)[0] for m in MASTER_MIXER]
    print(f"master saturation mode: {sat}")

    make = tone_source(1000.0, 0.25)
    overrides = {DRY_MIXER[0]: make(0.0), DRY_MIXER[1]: make(1e-9)}
    peak = 0
    for _ in range(40):
        emu.render_block(LEFT, RIGHT, overrides=overrides)
        peak = max(peak, max(map(abs, struct.unpack("<128h", emu.uc.mem_read(LEFT, 256)))))
    gain = 20 * math.log10(peak / 8191) if peak else float("-inf")
    print(f"-12 dBFS tone through project-configured master chain: peak {peak} ({gain:+.2f} dB)")


if __name__ == "__main__":
    main()
