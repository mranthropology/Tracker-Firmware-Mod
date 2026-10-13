"""M7: play a pattern with the real sequencer + voices and render it to WAV.

Usage: m7_render.py [project_dir] [pattern_number] [loops]
Timing comes from the firmware: tick period (0xc9054) x 12 ticks/sub-tick x
48 sub-ticks/step x the pattern's step count.
"""
import math
import struct
import sys
import time
import wave
from pathlib import Path

from unicorn.arm_const import UC_ARM_REG_S0

from m6_load_song import DEFAULT, boot_with_project
from tracker_emu import BLOCK_SAMPLES, SDRAM, SEQUENCER

RATE = 44100
LEFT, RIGHT = SDRAM[0] + 0x00FF_0000, SDRAM[0] + 0x00FF_0200
TICK_PERIOD = 0x000C_9054          # returns µs per sequencer tick in s0
TICKS_PER_STEP = 12 * 48
OUT = Path(__file__).parent / "out"


def step_seconds(emu) -> float:
    emu.call(TICK_PERIOD, SEQUENCER)
    micros = struct.unpack("<f", struct.pack("<I", emu.uc.reg_read(UC_ARM_REG_S0)))[0]
    return micros * TICKS_PER_STEP / 1e6


def main() -> None:
    project = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    pattern = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    loops = int(sys.argv[3]) if len(sys.argv) > 3 else 1

    emu, instruments, buffer = boot_with_project(project, pattern)
    steps = emu.uc.mem_read(buffer + 0x1C, 1)[0] + 1        # track 1 last step + 1
    if not emu.play():
        raise SystemExit("sequencer did not start")
    step = step_seconds(emu)
    seconds = steps * step * loops
    blocks = math.ceil(seconds * RATE / BLOCK_SAMPLES)
    print(f"pattern {pattern}: {steps} steps x {step * 1000:.1f} ms/step "
          f"(= {60 / (step * 4):.1f} BPM at 4 steps/beat) -> {seconds:.2f} s, {blocks} blocks")

    frames = bytearray()
    peak, sum_sq, started = 0, 0.0, time.time()
    for _ in range(blocks):
        emu.clock(BLOCK_SAMPLES / RATE)
        emu.render_block(LEFT, RIGHT)
        left = struct.unpack(f"<{BLOCK_SAMPLES}h", emu.uc.mem_read(LEFT, 256))
        right = struct.unpack(f"<{BLOCK_SAMPLES}h", emu.uc.mem_read(RIGHT, 256))
        for l, r in zip(left, right):
            frames += struct.pack("<hh", l, r)
            peak = max(peak, abs(l), abs(r))
            sum_sq += l * l + r * r

    OUT.mkdir(exist_ok=True)
    name = f"{project.name.replace(' ', '_')}_pattern{pattern:02d}.wav"
    with wave.open(str(OUT / name), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(frames))
    n = len(frames) // 4
    rms = math.sqrt(sum_sq / (2 * n)) if n else 0
    elapsed = time.time() - started
    print(f"wrote out/{name}: {n / RATE:.2f} s, peak {20 * math.log10(peak / 32767):.1f} dBFS, "
          f"RMS {20 * math.log10(rms / 32767):.1f} dBFS")
    print(f"emulation: {elapsed:.0f} s wall for {n / RATE:.1f} s audio "
          f"({elapsed / (n / RATE):.1f}x realtime), {emu.stats.insns:,} instructions")


if __name__ == "__main__":
    main()
