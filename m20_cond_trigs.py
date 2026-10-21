"""M20: conditional trigs on the Chance FX (master_fx.c, make_ptf --hooks ...,ct).

Usage: m20_cond_trigs.py

1. Each condition (Chance 101-106 = 1:2, 2:2, 1:4, 2:4, 3:4, 4:4) on an
   8-step track: which of 8 passes play the note, against the expected cycle.
   Also on track 3, and restarted playback starts counting again.
2. Ordinary Chance values are untouched: 0 never, 100 always, 50 gives the
   same played/skipped sequence as stock (same random calls).
3. FX max for every type equals stock except Chance (100 -> 106); the FX
   value formatter gives stock text for every value except 101-106.
4. The project's pattern 1 (Chance 90 % on its notes) renders bit-identical to
   the build without conditional trigs.
"""
import struct
import sys

from m6_load_song import DEFAULT
from m7_render import LEFT, RIGHT, step_seconds
from tracker_emu import (BLOCK_SAMPLES, LIBC_INIT_ARRAY, PATTERN_TRACK_OFFSET,
                         PATTERN_TRACK_STRIDE, TrackerEmu)

CHANCE = 4
LABELS = {101: "1:2", 102: "2:2", 103: "1:4", 104: "2:4", 105: "3:4", 106: "4:4"}
EXPECT = {101: [1, 0] * 4, 102: [0, 1] * 4, 103: [1, 0, 0, 0] * 2, 104: [0, 1, 0, 0] * 2,
          105: [0, 0, 1, 0] * 2, 106: [0, 0, 0, 1] * 2}
FX_MAX, FX_TEXT = 0xCB750, 0x146A4
SCRATCH = 0x70FD0000


def boot(ct=True):
    emu = TrackerEmu()
    emu.reset_runtime()
    emu.call(LIBC_INIT_ARRAY)
    emu.load_project(DEFAULT)
    emu.apply_master_fx_patch()
    if ct:
        emu.apply_ct_patch()
    emu.init_engine()
    emu.init_sequencer()
    emu.load_instruments(DEFAULT)
    emu.install_time_source()
    return emu


def one_note(emu, buf, track, value, steps=8):
    for t in range(8):
        base = buf + PATTERN_TRACK_OFFSET + t * PATTERN_TRACK_STRIDE
        emu.uc.mem_write(base, bytes([steps - 1]) + bytes([0xFF, 0, 0, 0, 0, 0]) * 128)
    base = buf + PATTERN_TRACK_OFFSET + track * PATTERN_TRACK_STRIDE + 1
    emu.uc.mem_write(base, bytes([0x3C, 4, CHANCE, value, 0, 0]))


def passes(emu, n, steps=8):
    """Peak per pass of an `steps`-step pattern -> played flags."""
    blocks_per_step = step_seconds(emu) * 44100 / BLOCK_SAMPLES
    out, peak, done = [], 0, 0
    for b in range(int(blocks_per_step * steps * n) + 1):
        emu.clock(BLOCK_SAMPLES / 44100)
        emu.render_block(LEFT, RIGHT)
        peak = max(peak, max(abs(v) for v in struct.unpack("<128h", emu.uc.mem_read(LEFT, 256))))
        if b + 1 >= blocks_per_step * steps * (done + 1):
            out.append(peak); peak = 0; done += 1
            if done == n:
                break
    return out


def played(peaks):
    full = max(peaks) if max(peaks) else 1
    return [int(p > 0.3 * full) for p in peaks]


def gating():
    print("1. conditions over 8 passes of an 8-step track (1 = note played)")
    ok = True
    for value, expect in EXPECT.items():
        emu = boot()
        buf = emu.load_pattern(DEFAULT, 1)
        one_note(emu, buf, 0, value)
        emu.play()
        got = played(passes(emu, 8))
        good = got == expect
        ok &= good
        print(f"   {'ok ' if good else 'BAD'} {LABELS[value]} (Chance {value}): {got}")
    emu = boot()
    buf = emu.load_pattern(DEFAULT, 1)
    one_note(emu, buf, 2, 103)
    emu.play()
    got = played(passes(emu, 8))
    good = got == EXPECT[103]
    ok &= good
    print(f"   {'ok ' if good else 'BAD'} 1:4 on track 3: {got}")
    emu = boot()
    buf = emu.load_pattern(DEFAULT, 1)
    one_note(emu, buf, 0, 102)                       # 2:2
    emu.play()
    first = played(passes(emu, 3))
    emu.play()                                       # restart: count from pass 1 again
    again = played(passes(emu, 3))
    good = first == [0, 1, 0] and again == [0, 1, 0]
    ok &= good
    print(f"   {'ok ' if good else 'BAD'} 2:2 restarted after 3 passes: {first} then {again}")
    return ok


def chance_unchanged():
    print("2. ordinary Chance values")
    ok = True
    for value, expect in ((0, [0] * 8), (100, [1] * 8)):
        emu = boot()
        buf = emu.load_pattern(DEFAULT, 1)
        one_note(emu, buf, 0, value)
        emu.play()
        peaks = passes(emu, 8)
        got = [int(p > 3000) for p in peaks]
        good = got == expect
        ok &= good
        print(f"   {'ok ' if good else 'BAD'} Chance {value}: {got}")
    seqs = []
    for ct in (False, True):
        emu = boot(ct)
        buf = emu.load_pattern(DEFAULT, 1)
        one_note(emu, buf, 0, 50, steps=2)
        emu.play()
        seqs.append([int(p > 3000) for p in passes(emu, 24, steps=2)])
    good = seqs[0] == seqs[1] and 0 < sum(seqs[1]) < 24
    ok &= good
    print(f"   {'ok ' if good else 'BAD'} Chance 50 over 24 passes, stock vs patched: "
          f"{'identical' if seqs[0] == seqs[1] else 'DIFFERENT'} ({sum(seqs[1])}/24 played)")
    return ok


def ui_functions():
    print("3. FX max and FX value text")
    ok = True
    stock, ct = boot(False), boot()
    diffs = [t for t in range(46) if stock.call(FX_MAX, 0, t) != ct.call(FX_MAX, 0, t)]
    good = diffs == [CHANCE] and ct.call(FX_MAX, 0, CHANCE) == 106
    ok &= good
    print(f"   {'ok ' if good else 'BAD'} FX max differs only for Chance: types {diffs}, "
          f"Chance max {stock.call(FX_MAX, 0, CHANCE)} -> {ct.call(FX_MAX, 0, CHANCE)}")

    def text(emu, ftype, value, mode=0):
        emu.uc.mem_write(SCRATCH, b"\0" * 16)
        emu.call(FX_TEXT, 0, SCRATCH, 8, ftype, value, mode)
        raw = bytes(emu.uc.mem_read(SCRATCH, 16))
        return raw[:raw.index(0)].decode("latin1")
    same = all(text(stock, ftype, v) == text(ct, ftype, v)
               for ftype in (1, 2, 3, CHANCE, 5, 12, 27) for v in range(0, 101, 5))
    labels = {v: text(ct, CHANCE, v) for v in range(101, 107)}
    good = same and labels == LABELS
    ok &= good
    print(f"   {'ok ' if good else 'BAD'} stock text for values 0-100 "
          f"({'same' if same else 'DIFFERENT'}); Chance 101-106: {list(labels.values())}")
    return ok


def whole_project():
    print("4. project pattern 1 (Chance 90 % on its notes)")
    renders = []
    for ct in (False, True):
        emu = boot(ct)
        emu.load_pattern(DEFAULT, 1)
        emu.play()
        pcm = bytearray()
        for _ in range(300):
            emu.clock(BLOCK_SAMPLES / 44100)
            emu.render_block(LEFT, RIGHT)
            pcm += emu.uc.mem_read(LEFT, 256) + emu.uc.mem_read(RIGHT, 256)
        renders.append(bytes(pcm))
    good = renders[0] == renders[1]
    print(f"   {'ok ' if good else 'BAD'} 300 blocks bit-identical with and without conditional trigs")
    return good


def main():
    ok = True
    ok &= ui_functions()
    ok &= chance_unchanged()
    ok &= gating()
    ok &= whole_project()
    print("all ok" if ok else "FAILURES")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
