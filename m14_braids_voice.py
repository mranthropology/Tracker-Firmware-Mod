"""M14: Braids as a Tracker instrument, through the real player hooks.

Boot: master FX patch + braids.bin; engine init goes through braids_boot
(copies Braids' .data, zeroes slots) -> patch_boot -> engine init.

1. Tuning through the whole chain (player -> filter -> mixers -> master):
   "BR:SINETRIANGLE", notes 36..96, frequency at the output.
2. Model from the instrument name, and fallback for non-"BR:" names.
3. Four-track limit: 6 tracks of Braids notes -> 4 Braids, 2 play their sample;
   a slot is freed when its envelope ends.
4. Cost: instructions per block, one voice playing, Braids vs stock sample.
5. Song: pattern 1 with tracks 1 and 4 switched to Braids -> out/*_braids.wav,
   plus per-block cost vs the stock render.

Usage: m14_braids_voice.py [--quick]   (--quick: sections 1-3 only)
"""
import math
import struct
import sys
import wave

from m6_load_song import DEFAULT
from m7_render import LEFT, OUT, RATE, RIGHT, step_seconds
from tracker_emu import (BLOCK_SAMPLES, ENGINE, ENGINE_VOICE_UPDATE, LIBC_INIT_ARRAY,
                         TrackerEmu)

FS = 44117.647                       # real output rate (player increment 0.9996 at C4)
VOICE0, VOICE_STRIDE = 0x1FFFB800, 0x660
INSTR, INSTR_SIZE = 0x200001CC, 0x174
NOTE_ON, NOTE_CMD = 0xCCEFC, 0xCC998
SLOT_SIZE = 5                        # struct Slot in tracker_voice.cc


def boot(braids=True):
    emu = TrackerEmu()
    emu.reset_runtime()
    emu.call(LIBC_INIT_ARRAY)
    emu.load_project(DEFAULT)
    emu.apply_master_fx_patch()
    if braids:
        emu.apply_braids_patch()
    emu.init_engine()
    emu.init_sequencer()
    inst = emu.load_instruments(DEFAULT)
    emu.install_time_source()
    return emu, inst


def set_instrument(emu, idx, name=None, mode=None, position=None):
    a = INSTR + idx * INSTR_SIZE
    if name is not None:
        emu.uc.mem_write(a + 0x05, name.encode().ljust(32, b"\0")[:32])
    if mode is not None:
        emu.uc.mem_write(a + 0x3C, bytes([mode]))
    if position is not None:
        emu.uc.mem_write(a + 0x16C, struct.pack("<H", position))


def note_on(emu, track, idx, note):
    emu.call(NOTE_ON, VOICE0 + track * VOICE_STRIDE, idx, note, 0xFFFFFFFF, 0, 0, 0, 0)


def note_off(emu, track):
    emu.call(NOTE_CMD, VOICE0 + track * VOICE_STRIDE, 0xFC)


def wait_idle(emu, track, limit=2000):
    """Render until the track's player is idle (a note-on during a release is
    deferred by the firmware and retriggered on a later block)."""
    for _ in range(limit):
        if not emu.uc.mem_read(VOICE0 + track * VOICE_STRIDE + 0x120 + 0x20, 1)[0]:
            return
        render(emu, 1)
    raise RuntimeError(f"track {track} did not go idle")


def slots(emu):
    raw = bytes(emu.uc.mem_read(emu.braids_syms["__bss_start"], 9 * SLOT_SIZE))
    return [struct.unpack_from("<bBBBb", raw, i * SLOT_SIZE) for i in range(9)]


def render(emu, blocks):
    """Blocks without the sequencer: voice update (applies note gains) + audio graph."""
    out = []
    for _ in range(blocks):
        emu.call(ENGINE_VOICE_UPDATE, ENGINE)
        emu.render_block(LEFT, RIGHT)
        out += struct.unpack(f"<{BLOCK_SAMPLES}h", emu.uc.mem_read(LEFT, 256))
    return out


def frequency(samples):
    xs = [i + s0 / (s0 - s1) for i, (s0, s1) in enumerate(zip(samples, samples[1:]))
          if s0 < 0 <= s1]
    return (len(xs) - 1) / ((xs[-1] - xs[0]) / FS)


def main():
    ok = True
    emu, inst = boot()
    assert emu.braids_syms["__bss_size"] == 9 * SLOT_SIZE
    idx = 1
    print("1. tuning through the full chain (BR:SINETRIANGLE, Timbre 0)")
    set_instrument(emu, idx, "BR:SINETRIANGLE", mode=7, position=0)
    for note in (36, 48, 60, 69, 84, 96):
        note_on(emu, 0, idx, note)
        out = render(emu, 30)[1500:]
        f = frequency(out)
        expect = 440.0 * 2 ** ((note - 69) / 12)
        cents = 1200 * math.log2(f / expect)
        good = abs(cents) < 3
        ok &= good
        print(f"   {'ok ' if good else 'BAD'} note {note}: {f:8.2f} Hz (expect {expect:8.2f}, {cents:+.1f} cents)")
        note_off(emu, 0)
        render(emu, 60)

    print("\n2. model from the instrument name")
    for name, expect in (("BR:FM", 25), ("br:kick", 34), ("BR: Wave Map", 38), ("BR:12", 12),
                         ("BR:nonsense", 0), ("Pad Mediavalish1", -1),
                         ("BR-FM", 25), ("BR-WAVE MAP", 38), ("BR-34", 34), ("BRASS 1", -1)):
        set_instrument(emu, idx, name)
        wait_idle(emu, 0)
        note_on(emu, 0, idx, 60)
        render(emu, 1)
        got = slots(emu)[0][0]
        good = got == expect
        ok &= good
        print(f"   {'ok ' if good else 'BAD'} {name!r:20s} -> model {got} (expect {expect})")
        note_off(emu, 0)

    print("\n3. four-track limit")
    for t in range(8):
        note_off(emu, t)
        wait_idle(emu, t)
    for t in range(6):
        set_instrument(emu, 10 + t, "BR:CSAW", mode=7)
        note_on(emu, t, 10 + t, 48 + t)
    render(emu, 2)
    s = slots(emu)
    braids_tracks = [t for t in range(8) if s[t][0] >= 0]
    good = braids_tracks == [0, 1, 2, 3] and sum(x[1] for x in s[:8]) == 4
    ok &= good
    print(f"   {'ok ' if good else 'BAD'} 6 Braids notes -> Braids on tracks {braids_tracks}, "
          f"tracks 4-5 play their sample")
    note_off(emu, 0)
    for _ in range(400):
        render(emu, 1)
        if not emu.uc.mem_read(VOICE0 + 0x120 + 0x20, 1)[0]:
            break
    render(emu, 1)
    s = slots(emu)
    good = s[0][1] == 0 and sum(x[1] for x in s[:8]) == 3
    ok &= good
    print(f"   {'ok ' if good else 'BAD'} after track 0's envelope ends its slot is free "
          f"({sum(x[1] for x in s[:8])} held)")
    for t in range(1, 6):
        note_off(emu, t)
    render(emu, 400)

    if "--quick" in sys.argv:
        sys.exit(0 if ok else 1)

    print("\n4. cost per block, one voice playing (instruction count)")
    costs = {}
    for label, braids in (("stock sample (granular)", False), ("Braids CSAW", True), ("Braids FM", True)):
        e, _ = boot(braids)
        set_instrument(e, idx, "BR:FM" if "FM" in label else ("BR:CSAW" if braids else None), mode=7)
        render(e, 1)
        idle_start = e.stats.insns
        render(e, 10)
        idle = (e.stats.insns - idle_start) / 10
        note_on(e, 0, idx, 48)
        render(e, 5)
        start = e.stats.insns
        render(e, 20)
        costs[label] = (e.stats.insns - start) / 20 - idle
        print(f"   {label:24s} +{costs[label]:8,.0f} insns/block over idle ({idle:,.0f})")

    print("\n5. song: pattern 1, tracks 1 and 4 as Braids")
    wav = song()
    print(f"   wrote {wav}")
    sys.exit(0 if ok else 1)


def song():
    results = {}
    for label, braids in (("stock", False), ("braids", True)):
        emu, _ = boot(braids)
        if braids:
            set_instrument(emu, 3, "BR:WAVEMAP", mode=7, position=20000)    # track 1 pad
            set_instrument(emu, 8, "BR:FM", mode=7, position=12000)         # track 4 synth
        buffer = emu.load_pattern(DEFAULT, 1)
        steps = emu.uc.mem_read(buffer + 0x1C, 1)[0] + 1
        emu.play()
        blocks = math.ceil(steps * step_seconds(emu) * RATE / BLOCK_SAMPLES)
        frames, per_block = bytearray(), []
        for _ in range(blocks):
            emu.clock(BLOCK_SAMPLES / RATE)
            start = emu.stats.insns
            emu.render_block(LEFT, RIGHT)
            per_block.append(emu.stats.insns - start)
            l = struct.unpack(f"<{BLOCK_SAMPLES}h", emu.uc.mem_read(LEFT, 256))
            r = struct.unpack(f"<{BLOCK_SAMPLES}h", emu.uc.mem_read(RIGHT, 256))
            for a, b in zip(l, r):
                frames += struct.pack("<hh", a, b)
        results[label] = (frames, per_block)
        print(f"   {label:6s}: mean {sum(per_block) / len(per_block):,.0f}, "
              f"max {max(per_block):,} insns/block (audio graph)")
    name = f"{DEFAULT.name.replace(' ', '_')}_pattern01_braids.wav"
    with wave.open(str(OUT / name), "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(RATE)
        w.writeframes(bytes(results["braids"][0]))
    return f"out/{name}"


if __name__ == "__main__":
    main()
