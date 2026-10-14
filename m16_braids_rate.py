"""M16: which Braids models depend on the sample rate beyond pitch?

The Tracker build renders Braids directly at 44,117.647 Hz with a pitch
offset, so every pitch-indexed table (oscillator increments, filter cutoffs)
lands on the right frequency. Anything that decays or ramps per *sample*
(excitation pulses, partial decays, envelopes, grain rates, one-pole filters)
runs ~2.18x too long. Reference: the same binary rendering at Braids' native
96 kHz (pitch offset removed), as on the module.

For each model: strike at C3, Timbre/Color mid, envelope (10 ms RMS) for 2 s;
times to -20 dB and -40 dB below peak at both rates; ratio 44.1k/96k.
Ratios near 1 = rate-independent; near 2.18 = per-sample time constants.

--check: for the models tracker_voice.cc renders at 96 kHz, compare the
resampled path (braids_voice_render_96k) with the native 96 kHz reference
(decay times, tuning through the resampler, cost per block).
"""
import struct
import sys
import math

from m13_braids_cost import BLOCK_SAMPLES, MODELS, Braids

FS_TRACKER = 750000 / 17          # 44117.647 Hz
FS_NATIVE = 96000
PITCH_OFFSET = 1723               # added by braids_voice_render (see braids_voice.cc)


AT_96K = (32, 33, 36, 45)        # renders_at_96k() in tracker_voice.cc


def render96(b, shape, pitch, timbre, color):
    """One 128-sample block through braids_voice_render_96k (Tracker rate)."""
    from m13_braids_cost import OUT_BUF, VOICE
    start = b.emu.stats.insns
    b.emu.call(b.s["braids_voice_render_96k"], VOICE, shape, pitch, timbre, color, OUT_BUF, BLOCK_SAMPLES)
    insns = b.emu.stats.insns - start
    return struct.unpack(f"<{BLOCK_SAMPLES}h", b.emu.uc.mem_read(OUT_BUF, 256)), insns


def envelope_times(b, shape, native, resampled=False, secs=2.0):
    rate = FS_NATIVE if native else FS_TRACKER
    pitch = (48 << 7) - (PITCH_OFFSET if native else 0)
    b.init()
    b.strike()
    samples = []
    for _ in range(int(secs * rate / BLOCK_SAMPLES)):
        out, _ = (render96(b, shape, 48 << 7, 16384, 16384) if resampled
                  else b.render(shape, pitch, 16384, 16384))
        samples += out
    win = int(0.010 * rate)
    env = [math.sqrt(sum(v * v for v in samples[i:i + win]) / win) + 1e-9
           for i in range(0, len(samples) - win, win)]
    peak_i = max(range(len(env)), key=env.__getitem__)
    peak = env[peak_i]
    times = []
    for db in (20, 40):
        t = next((i for i in range(peak_i, len(env)) if env[i] < peak * 10 ** (-db / 20)), None)
        times.append(None if t is None else (t - peak_i) * 0.010)
    return times


def main():
    b = Braids()
    rows = []
    for shape, name in enumerate(MODELS):
        t44 = envelope_times(b, shape, False)
        t96 = envelope_times(b, shape, True)
        ratio = (t44[1] / t96[1]) if t44[1] and t96[1] else None
        rows.append((name, t44, t96, ratio))
        fmt = lambda t: " sustained" if t is None else f"{t:8.2f} s"
        print(f"{shape:2d} {name:20s} -40 dB: 44.1k {fmt(t44[1])}  96k {fmt(t96[1])}"
              + (f"  ratio {ratio:4.2f}" if ratio else ""))
    rate_dependent = [n for n, _, _, r in rows if r and r > 1.3]
    print("\nmodels whose decay depends on the sample rate (ratio > 1.3):")
    print("  " + ", ".join(rate_dependent))


def check():
    from m13_braids_cost import frequency
    b = Braids()
    ok = True
    print("resampled 96 kHz path vs native 96 kHz reference (time to -20/-40 dB):")
    for shape in AT_96K:
        ref = envelope_times(b, shape, True)
        new = envelope_times(b, shape, False, resampled=True)
        old = envelope_times(b, shape, False)
        good = all(r is None and n is None or (r and n and abs(n / r - 1) < 0.12)
                   for r, n in zip(ref, new))
        ok &= good
        f = lambda t: "  -  " if t is None else f"{t:4.2f}"
        print(f"  {'ok ' if good else 'BAD'} {MODELS[shape]:15s} ref {f(ref[0])}/{f(ref[1])} s   "
              f"resampled {f(new[0])}/{f(new[1])} s   (was {f(old[0])}/{f(old[1])} s)")
    print("tuning through the resampler (SINE_TRIANGLE, Timbre 0):")
    for note, expect in ((48, 130.81), (60, 261.63), (69, 440.0), (84, 1046.5)):
        b.init()
        buf = []
        for _ in range(40):
            out, _ = render96(b, 3, note << 7, 0, 0)
            buf += out
        fr = frequency(buf[1500:]) * (FS_TRACKER / 44100)
        cents = 1200 * math.log2(fr / expect)
        ok &= abs(cents) < 3
        print(f"  {'ok ' if abs(cents) < 3 else 'BAD'} note {note}: {fr:8.2f} Hz ({cents:+.1f} cents)")
    print("cost per 128-sample block (pitch C3, Timbre/Color mid):")
    for shape in AT_96K:
        b.init(); b.strike()
        for _ in range(4):
            render96(b, shape, 48 << 7, 16384, 16384)
        tot = sum(render96(b, shape, 48 << 7, 16384, 16384)[1] for _ in range(8)) / 8
        b.init(); b.strike()
        for _ in range(4):
            b.render(shape, 48 << 7, 16384, 16384)
        old = sum(b.render(shape, 48 << 7, 16384, 16384)[1] for _ in range(8)) / 8
        print(f"  {MODELS[shape]:15s} {tot:8,.0f} insns (was {old:,.0f} at 44.1 kHz)")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    check() if "--check" in sys.argv else main()
