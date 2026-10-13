"""M9: render a pattern through the patched firmware with an automated DJ filter.

Usage: m9_dj_demo.py [project_dir] [pattern_number]
DJ knob over one loop: 0 -> -0.95 (low-pass closes) -> 0 -> +0.8 (high-pass) -> 0.
"""
import math
import struct
import sys
import wave
from pathlib import Path

from m6_load_song import DEFAULT
from m7_render import LEFT, OUT, RATE, RIGHT, step_seconds
from tracker_emu import BLOCK_SAMPLES, LIBC_INIT_ARRAY, TrackerEmu


def dj_curve(t: float) -> float:
    """t in [0,1): piecewise-linear sweep."""
    points = ((0.0, 0.0), (0.3, -0.95), (0.5, 0.0), (0.8, 0.8), (1.0, 0.0))
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if t0 <= t <= t1:
            return v0 + (v1 - v0) * (t - t0) / (t1 - t0)
    return 0.0


def main() -> None:
    project = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    pattern = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    emu = TrackerEmu()
    emu.reset_runtime()
    emu.call(LIBC_INIT_ARRAY)
    emu.load_project(project)
    emu.apply_master_fx_patch()
    emu.init_engine()
    emu.init_sequencer()
    emu.load_instruments(project)
    buffer = emu.load_pattern(project, pattern)
    steps = emu.uc.mem_read(buffer + 0x1C, 1)[0] + 1
    emu.play()
    seconds = steps * step_seconds(emu)
    blocks = math.ceil(seconds * RATE / BLOCK_SAMPLES)
    frames = bytearray()
    for b in range(blocks):
        emu.set_patch_params(dj=dj_curve(b / blocks))
        emu.clock(BLOCK_SAMPLES / RATE)
        emu.render_block(LEFT, RIGHT)
        left = struct.unpack(f"<{BLOCK_SAMPLES}h", emu.uc.mem_read(LEFT, 256))
        right = struct.unpack(f"<{BLOCK_SAMPLES}h", emu.uc.mem_read(RIGHT, 256))
        for l, r in zip(left, right):
            frames += struct.pack("<hh", l, r)
    name = f"{project.name.replace(' ', '_')}_pattern{pattern:02d}_djsweep.wav"
    OUT.mkdir(exist_ok=True)
    with wave.open(str(OUT / name), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(frames))
    print(f"wrote out/{name}: {len(frames) // 4 / RATE:.2f} s")


if __name__ == "__main__":
    main()
