"""M13: Braids macro oscillator on the Tracker's CPU (route 2 feasibility).

Loads braids/build/braids.bin (see braids/build.sh) into the emulator at its
link address, copies its .data to SDRAM, and for each of Braids' 48 models:
instructions per 128-sample block (timbre/color at mid), level, and clipping.
Checks tuning (sine model at C4 and A4) and writes out/braids_models.wav
(0.5 s per model, C3, timbre/color sweeping) for listening.

Usage: m13_braids_cost.py [--no-wav]
"""
import math
import struct
import sys
import wave
from pathlib import Path

from tracker_emu import BLOCK_SAMPLES, TrackerEmu

ROOT = Path(__file__).parent
BIN = ROOT / "braids" / "build" / "braids.bin"
RATE = 44100
VOICE = 0x70FE_2000          # MacroOscillator object (free SDRAM)
OUT_BUF = 0x70FE_8000        # 128 int16 render buffer
BLOCK_CYCLES = 180e6 * BLOCK_SAMPLES / RATE   # ~522k cycles per block at 180 MHz

MODELS = """CSAW MORPH SAW_SQUARE SINE_TRIANGLE BUZZ SQUARE_SUB SAW_SUB SQUARE_SYNC
SAW_SYNC TRIPLE_SAW TRIPLE_SQUARE TRIPLE_TRIANGLE TRIPLE_SINE TRIPLE_RING_MOD SAW_SWARM
SAW_COMB TOY DIGITAL_FILTER_LP DIGITAL_FILTER_PK DIGITAL_FILTER_BP DIGITAL_FILTER_HP VOSIM
VOWEL VOWEL_FOF HARMONICS FM FEEDBACK_FM CHAOTIC_FEEDBACK_FM PLUCKED BOWED BLOWN FLUTED
STRUCK_BELL STRUCK_DRUM KICK CYMBAL SNARE WAVETABLES WAVE_MAP WAVE_LINE WAVE_PARAPHONIC
FILTERED_NOISE TWIN_PEAKS_NOISE CLOCKED_NOISE GRANULAR_CLOUD PARTICLE_NOISE
DIGITAL_MODULATION QUESTION_MARK""".split()


class Braids:
    def __init__(self):
        syms = {k: int(v, 16) for k, v in (l.split() for l in
                (BIN.with_suffix(".bin.syms")).read_text().splitlines())}
        self.s = syms
        self.emu = TrackerEmu()
        self.emu.reset_runtime()
        base = syms["braids_voice_size"] & ~0xFFF          # link base (page aligned)
        self.emu.uc.mem_write(base, BIN.read_bytes())
        data = bytes(self.emu.uc.mem_read(syms["__data_load"], syms["__data_size"]))
        self.emu.uc.mem_write(syms["__data_start"], data)
        self.emu.uc.mem_write(syms["__bss_start"], b"\0" * syms["__bss_size"])
        self.size = self.emu.call(syms["braids_voice_size"])

    def init(self):
        self.emu.call(self.s["braids_voice_init"], VOICE)

    def strike(self):
        self.emu.call(self.s["braids_voice_strike"], VOICE)

    def render(self, shape, pitch, timbre, color):
        """One 128-sample block; returns (samples, instructions)."""
        start = self.emu.stats.insns
        self.emu.call(self.s["braids_voice_render"], VOICE, shape, pitch, timbre, color,
                      OUT_BUF, BLOCK_SAMPLES)
        insns = self.emu.stats.insns - start
        return struct.unpack(f"<{BLOCK_SAMPLES}h", self.emu.uc.mem_read(OUT_BUF, 256)), insns


def frequency(samples):
    """Fundamental from rising zero crossings (sine input)."""
    xs = [i + s0 / (s0 - s1) for i, (s0, s1) in enumerate(zip(samples, samples[1:]))
          if s0 < 0 <= s1]
    return (len(xs) - 1) / ((xs[-1] - xs[0]) / RATE)


def main():
    b = Braids()
    print(f"braids.bin {BIN.stat().st_size:,} bytes; MacroOscillator object {b.size} bytes")

    print("tuning (SINE_TRIANGLE, timbre 0):")
    shape = MODELS.index("SINE_TRIANGLE")
    for note, expect in ((60, 261.63), (69, 440.0)):
        b.init()
        buf = []
        for _ in range(40):
            out, _ = b.render(shape, note << 7, 0, 0)
            buf += out
        f = frequency(buf[1000:])
        print(f"  MIDI {note}: {f:7.2f} Hz (expect {expect:.2f}, {1200 * math.log2(f / expect):+.1f} cents)")

    print("cost per 128-sample block (pitch C3, timbre/color 16384), after warm-up:")
    rows = []
    for shape, name in enumerate(MODELS):
        b.init()
        b.strike()
        for _ in range(4):
            b.render(shape, 48 << 7, 16384, 16384)
        total, peak, sq, n = 0, 0, 0, 0
        for k in range(16):
            if k == 8:
                b.strike()
            out, insns = b.render(shape, 48 << 7, 16384, 16384)
            total += insns
            peak = max(peak, max(abs(v) for v in out))
            sq += sum(v * v for v in out)
            n += len(out)
        per = total / 16
        rms = math.sqrt(sq / n)
        rows.append((per, name))
        print(f"  {shape:2d} {name:20s} {per:9,.0f} insns  ~{100 * per / BLOCK_CYCLES:4.1f}% "
              f"peak {peak:5d}  rms {20 * math.log10(rms / 32768 + 1e-9):6.1f} dBFS"
              f"{'  CLIP' if peak >= 32767 else ''}{'  SILENT' if peak == 0 else ''}")
    rows.sort()
    avg = sum(r[0] for r in rows) / len(rows)
    print(f"cheapest {rows[0][1]} {rows[0][0]:,.0f}; median {rows[len(rows)//2][0]:,.0f}; "
          f"most expensive {rows[-1][1]} {rows[-1][0]:,.0f}; mean {avg:,.0f} insns/block")
    print(f"(1 instruction ~ 1 cycle is optimistic; % = insns / {BLOCK_CYCLES:,.0f} cycles per block)")

    if "--no-wav" in sys.argv:
        return
    frames = []
    per_model = int(0.5 * RATE / BLOCK_SAMPLES)
    for shape in range(len(MODELS)):
        b.init()
        b.strike()
        for k in range(per_model):
            t = k / per_model
            out, _ = b.render(shape, 48 << 7, int(32767 * t), int(32767 * (1 - t)))
            frames += out
    path = ROOT / "out" / "braids_models.wav"
    path.parent.mkdir(exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
        w.writeframes(struct.pack(f"<{len(frames)}h", *frames))
    print(f"wrote {path.relative_to(ROOT)} ({len(frames) / RATE:.1f} s, models in order, 0.5 s each)")


if __name__ == "__main__":
    main()
