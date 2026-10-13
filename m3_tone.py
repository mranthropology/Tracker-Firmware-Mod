"""M3: inject a test tone on the dry bus and capture the master output as WAV.

Signal path exercised: dry 8-in mixer (replaced by a tone source) -> master
4-in mixer (saturation stage 0xd42f0) -> limiter E[ch] -> final 7-in mixer
X[ch] -> output node O -> capture buffers.

Note: engine init (0x1611c) is not run yet, so mixer gains, saturation mode
and limiter settings are constructor defaults, not a loaded project's values.
"""
import math
import struct
import sys
import wave
from pathlib import Path

from tracker_emu import (BLOCK_SAMPLES, DRY_MIXER, LIBC_INIT_ARRAY, SDRAM,
                         TrackerEmu)

RATE = 44100
LEFT, RIGHT = SDRAM[0] + 0x00FF_0000, SDRAM[0] + 0x00FF_0200
OUT = Path(__file__).parent / "out"


def tone_source(freq: float, amplitude: float):
    phase = {"n": 0}

    def make(channel_offset: float):
        def update(emu: TrackerEmu, node: int) -> None:
            n0 = phase["n"]
            samples = [int(amplitude * 32767 * math.sin(
                2 * math.pi * freq * (n0 + i) / RATE + channel_offset))
                for i in range(BLOCK_SAMPLES)]
            emu.transmit_samples(node, samples)
            if channel_offset:            # right channel runs last: advance clock
                phase["n"] += BLOCK_SAMPLES
        return update
    return make


def render(amplitude: float, seconds: float, name: str) -> tuple[int, int]:
    emu = TrackerEmu()
    emu.reset_runtime()
    emu.call(LIBC_INIT_ARRAY)
    emu.init_audio_memory()
    make = tone_source(1000.0, amplitude)
    overrides = {DRY_MIXER[0]: make(0.0), DRY_MIXER[1]: make(1e-9)}
    frames = bytearray()
    peak_in = int(amplitude * 32767)
    peak_out = 0
    for _ in range(int(seconds * RATE / BLOCK_SAMPLES)):
        emu.render_block(LEFT, RIGHT, overrides=overrides)
        left = struct.unpack(f"<{BLOCK_SAMPLES}h", emu.uc.mem_read(LEFT, 256))
        right = struct.unpack(f"<{BLOCK_SAMPLES}h", emu.uc.mem_read(RIGHT, 256))
        peak_out = max(peak_out, max(map(abs, left)), max(map(abs, right)))
        for l, r in zip(left, right):
            frames += struct.pack("<hh", l, r)
    OUT.mkdir(exist_ok=True)
    with wave.open(str(OUT / name), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(frames))
    return peak_in, peak_out


def main() -> None:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0
    for amp, name in ((0.25, "tone_-12dBFS.wav"), (1.0, "tone_0dBFS.wav")):
        pin, pout = render(amp, seconds, name)
        gain_db = 20 * math.log10(pout / pin) if pout else float("-inf")
        print(f"{name:18s} in peak {pin:6d}  out peak {pout:6d}  gain {gain_db:+.2f} dB")


if __name__ == "__main__":
    main()
