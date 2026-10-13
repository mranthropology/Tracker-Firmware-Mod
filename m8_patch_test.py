"""M8: verify and compare the master EQ + DJ filter patch builds.

  v1 = patch/master_fx_v1.c (biquads, no smoothing)
  v2 = patch/master_fx.c    (TPT SVF, parameter glide + coefficient ramps)

1. Steady-state frequency response (v2): each EQ band at +/-1, DJ low/high-pass.
2. Cost: instructions per 128-frame block for stock / v1 / v2.
3. Smoothing: abrupt knob moves on a two-tone signal; energy that appears
   above 8 kHz (where the clean signal has none) measures zipper/clicks.
4. Bypass is bit-exact (v2): 2 s of a real pattern, unpatched vs flat.
"""
import math
import struct

from m6_load_song import DEFAULT, boot_with_project
from tracker_emu import (BLOCK_SAMPLES, DRY_MIXER, LIBC_INIT_ARRAY, SDRAM,
                         TrackerEmu)

RATE = 44100
LEFT, RIGHT = SDRAM[0] + 0x00FF_0000, SDRAM[0] + 0x00FF_0200
BANDS = (115, 330, 990, 3000, 9900)


class Tones:
    """Sum of sines on the dry bus (one source per channel)."""

    def __init__(self, freqs, level_db):
        self.freqs, self.amp, self.n = list(freqs), 10 ** (level_db / 20), 0

    def source(self, channel):
        def update(emu, node):
            samples = [int(self.amp * 32767 * sum(
                math.sin(2 * math.pi * f * (self.n + i) / RATE) for f in self.freqs))
                for i in range(BLOCK_SAMPLES)]
            emu.transmit_samples(node, samples)
            if channel == 1:
                self.n += BLOCK_SAMPLES
        return update


def boot(build=None):
    emu = TrackerEmu()
    emu.reset_runtime()
    emu.call(LIBC_INIT_ARRAY)
    if build is not None:
        emu.apply_master_fx_patch(build=build)
    emu.init_engine()
    return emu


def run(emu, tones, blocks, capture=False, on_block=None):
    overrides = {DRY_MIXER[0]: tones.source(0), DRY_MIXER[1]: tones.source(1)}
    out = []
    for b in range(blocks):
        if on_block:
            on_block(b)
        emu.render_block(LEFT, RIGHT, overrides=overrides)
        if capture:
            out += struct.unpack(f"<{BLOCK_SAMPLES}h", emu.uc.mem_read(LEFT, 256))
    return out


def level_db(samples, level_db_in):
    rms = math.sqrt(sum(s * s for s in samples) / len(samples))
    return 20 * math.log10(rms / (10 ** (level_db_in / 20) * 32767 / math.sqrt(2)))


def response() -> None:
    emu = boot("")
    tones = Tones([1000], -30)
    print("1. v2 steady-state response")
    print("   EQ band at +1 / -1, measured at its centre (expect +12 / -12 dB)")
    for b, hz in enumerate(BANDS):
        tones.freqs = [hz]
        row = []
        for g in (1.0, -1.0):
            gains = [0.0] * 5
            gains[b] = g
            emu.set_master_eq(gains)
            run(emu, tones, 90)                               # glide settles
            row.append(level_db(run(emu, tones, 10, capture=True), -30))
        emu.set_master_eq([0.0] * 5)
        run(emu, tones, 90)
        flat = level_db(run(emu, tones, 10, capture=True), -30)
        print(f"   {hz:5d} Hz: {row[0]:+6.2f} / {row[1]:+6.2f} dB   (flat {flat:+5.2f} dB)")
    for dj, freqs, label in ((-0.5, (200, 5000), "low-pass ~1.3 kHz"),
                             (0.5, (100, 3000), "high-pass ~380 Hz")):
        emu.set_patch_params(dj=dj)
        cells = []
        for hz in freqs:
            tones.freqs = [hz]
            run(emu, tones, 90)
            cells.append(f"{hz} Hz {level_db(run(emu, tones, 10, capture=True), -30):+6.1f} dB")
        print(f"   DJ {dj:+.1f} ({label}): " + ", ".join(cells))


def cost() -> None:
    print("\n2. cost, instructions per block (tone on the dry bus)")
    cases = (("flat / bypass", [0] * 5, 0.0), ("5 EQ bands", [0.5] * 5, 0.0),
             ("5 bands + DJ", [0.5] * 5, -0.5))
    stock = boot(None)
    tones = Tones([1000], -30)
    run(stock, tones, 5)
    start = stock.stats.insns
    run(stock, tones, 10)
    base = (stock.stats.insns - start) / 10
    print(f"   stock firmware: {base:,.0f}")
    for build in ("v1", ""):
        emu = boot(build)
        for label, gains, dj in cases:
            emu.set_master_eq(gains)
            emu.set_patch_params(dj=dj)
            run(emu, tones, 100)                              # settle (no ramps)
            start = emu.stats.insns
            run(emu, tones, 10)
            per = (emu.stats.insns - start) / 10
            print(f"   {build or 'v2'} {label:14s} +{per - base:8,.0f}  ({per:,.0f})")


def zipper() -> None:
    print("\n3. smoothing: abrupt knob moves on 100+400 Hz tones; energy above 8 kHz = clicks/zipper")
    moves = {40: ("dj", -0.8), 80: ("dj", 0.6), 120: ("dj", 0.0),
             160: ("eq", [1.0, 0, 0, -1.0, 0]), 200: ("eq", [0.0] * 5)}

    def hf_db(samples):
        n = len(samples)
        # energy above 8 kHz via a simple 4th-order high-pass (two biquads)
        out = list(samples)
        for _ in range(2):
            w0 = 2 * math.pi * 8000 / RATE
            alpha, cw = math.sin(w0) / (2 * 0.707), math.cos(w0)
            b0, b1, b2 = (1 + cw) / 2, -(1 + cw), (1 + cw) / 2
            a0, a1, a2 = 1 + alpha, -2 * cw, 1 - alpha
            x1 = x2 = y1 = y2 = 0.0
            for i in range(n):
                x = out[i]
                y = (b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2) / a0
                x2, x1, y2, y1 = x1, x, y1, y
                out[i] = y
        e = sum(v * v for v in out[2000:]) / (n - 2000)
        return 10 * math.log10(e / 32767 ** 2 + 1e-20)

    for build in ("v1", ""):
        results = []
        for with_moves in (False, True):
            emu = boot(build)
            tones = Tones([100, 400], -20)      # low tones: no leakage above 8 kHz

            def on_block(b, emu=emu, with_moves=with_moves):
                if with_moves and b in moves:
                    kind, value = moves[b]
                    if kind == "dj":
                        emu.set_patch_params(dj=value)
                    else:
                        emu.set_master_eq(value)
            results.append(hf_db(run(emu, tones, 240, capture=True, on_block=on_block)))
        print(f"   {build or 'v2'}: {results[1]:6.1f} dBFS above 8 kHz with knob moves "
              f"(no moves: {results[0]:6.1f})")


def bypass() -> None:
    blocks = int(2.0 * RATE / BLOCK_SAMPLES)
    renders = []
    for patched in (False, True):
        emu, _, _ = boot_with_project(DEFAULT, 1)
        if patched:
            emu.apply_master_fx_patch(disable_codec_eq=False)
        emu.play()
        out = bytearray()
        for _ in range(blocks):
            emu.clock(BLOCK_SAMPLES / RATE)
            emu.render_block(LEFT, RIGHT)
            out += emu.uc.mem_read(LEFT, 256) + emu.uc.mem_read(RIGHT, 256)
        renders.append(bytes(out))
    print(f"\n4. v2 bypass vs stock (2 s of pattern 1): "
          f"{'bit-identical' if renders[0] == renders[1] else 'DIFFERENT'}")


if __name__ == "__main__":
    response()
    cost()
    zipper()
    bypass()
