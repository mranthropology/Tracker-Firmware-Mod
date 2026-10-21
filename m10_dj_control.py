"""M10: DJ filter on Shift + Master + jog wheel, snapping back to centre on release.

The patch redirects the board's knob/press/release callbacks (0xf628/0xf636/
0xf646). The UI itself is not initialised in the harness, so the interface
handlers are replaced by recorders: they show exactly what still reaches the
firmware.

1. Routing: which events reach the firmware and what the DJ value does.
2. Audio: 200 + 5000 Hz tones; Shift+Master+jog closes the low-pass, release
   snaps back. Levels, recovery time, and clicks above 8 kHz on 100+400 Hz tones.
3. Indicator: the pop-up text shown for each step of a gesture.
4. Boot: state filled with garbage, then engine init through the 0xf3fa hook.
5. Demo (with --demo): pattern 1 with a played gesture -> out/*_djcontrol.wav.
"""
import math
import struct
import sys
import wave

from m8_patch_test import LEFT, RATE, Tones, boot, level_db, run
from tracker_emu import (BLOCK_SAMPLES, BTN_MASTER, BTN_SHIFT, PATCH_STATE)

DJ_CUR = PATCH_STATE + 64          # patch_state_t.dj_cur (smoothed value)


def dj_cur(emu) -> float:
    return struct.unpack("<f", emu.uc.mem_read(DJ_CUR, 4))[0]


def routing() -> bool:
    emu = boot("")
    log = emu.record_ui_passthrough()
    ok = True

    def check(label, expect_dj, expect_log):
        nonlocal ok
        got = [e for e in log if e[0] != "popup"]
        log.clear()
        good = abs(emu.patch_dj() - expect_dj) < 1e-6 and got == expect_log
        ok &= good
        print(f"   {'ok ' if good else 'BAD'} {label:42s} dj {emu.patch_dj():+.2f}  firmware sees {got}")

    print("1. routing (what reaches the firmware)")
    emu.turn(3)
    check("jog alone", 0.0, [("knob", 0, 3)])
    emu.press(BTN_MASTER); emu.turn(2); emu.release(BTN_MASTER)
    check("Master + jog (stock: master volume)", 0.0,
          [("button", BTN_MASTER, 1), ("knob", 0, 2), ("button", BTN_MASTER, 0)])
    emu.press(BTN_SHIFT); emu.turn(-4); emu.release(BTN_SHIFT)
    check("Shift + jog (stock: selection edit)", 0.0,
          [("button", BTN_SHIFT, 1), ("knob", 0, -4), ("button", BTN_SHIFT, 0)])
    emu.press(BTN_SHIFT); emu.press(BTN_MASTER); emu.turn(-10)
    check("Shift + Master + jog -10", -0.2, [("button", BTN_SHIFT, 1), ("button", BTN_MASTER, 1)])
    emu.turn(-100)
    check("  ... -100 more (clamps)", -1.0, [])
    emu.turn(5, pot=1)
    check("  ... another pot index (passes through)", -1.0, [("knob", 1, 5)])
    emu.release(BTN_SHIFT)
    check("  release Shift -> snap to centre", 0.0, [("button", BTN_SHIFT, 0)])
    emu.turn(1)
    check("  Master still held, jog -> firmware", 0.0, [("knob", 0, 1)])
    emu.press(BTN_SHIFT); emu.turn(15); emu.release(BTN_MASTER)
    check("  Shift again, +15, release Master", 0.0,
          [("button", BTN_SHIFT, 1), ("button", BTN_MASTER, 0)])
    emu.release(BTN_SHIFT)
    check("  release Shift (nothing to undo)", 0.0, [("button", BTN_SHIFT, 0)])
    emu.press(BTN_MASTER); emu.press(BTN_SHIFT); emu.release(BTN_SHIFT)
    check("press/release without turning", 0.0,
          [("button", BTN_MASTER, 1), ("button", BTN_SHIFT, 1), ("button", BTN_SHIFT, 0)])
    emu.release(BTN_MASTER)
    log.clear()
    emu.set_patch_params(dj=0.3)
    emu.press(BTN_MASTER); emu.release(BTN_MASTER)
    check("DJ set elsewhere is left alone by a release", 0.3,
          [("button", BTN_MASTER, 1), ("button", BTN_MASTER, 0)])
    return ok


def indicator() -> bool:
    """Pop-up text for each gesture step (firmware pop-up, 1 s timeout)."""
    emu = boot("")
    log = emu.record_ui_passthrough()
    steps = [("press Shift, Master", lambda: (emu.press(BTN_SHIFT), emu.press(BTN_MASTER)),
              ["L0/0% R0 P0 D0 T0k"]),                      # FX bank cost readout (no DWT in the harness)
             ("jog -1", lambda: emu.turn(-1), ["DJ Low-pass 2%"]),
             ("jog -19", lambda: emu.turn(-19), ["DJ Low-pass 40%"]),
             ("jog -100 (clamps)", lambda: emu.turn(-100), ["DJ Low-pass 100%"]),
             ("jog +50", lambda: emu.turn(50), ["DJ filter off"]),
             ("jog +3", lambda: emu.turn(3), ["DJ High-pass 6%"]),
             ("jog +22", lambda: emu.turn(22), ["DJ High-pass 50%"]),
             ("release Master (snap)", lambda: emu.release(BTN_MASTER), ["DJ filter off"]),
             ("jog with only Shift held", lambda: emu.turn(4), []),
             ("release Shift", lambda: emu.release(BTN_SHIFT), [])]
    ok = True
    print("\n2. on-screen indicator (firmware pop-up)")
    for label, action, expect in steps:
        action()
        got = [e for e in log if e[0] == "popup"]
        log.clear()
        good = [t for _, t, _ in got] == expect and all(sec == (2 if t.startswith("L0/") else 1) for _, t, sec in got)
        ok &= good
        shown = ", ".join(f'"{t}" ({sec} s)' for _, t, sec in got) or "-"
        print(f"   {'ok ' if good else 'BAD'} {label:26s} {shown}")
    return ok


def boot_reset() -> bool:
    """Power-on: SDRAM holds garbage. patch_boot (the 'bl' at 0xf3fa) must clear it."""
    from tracker_emu import (BOOT_HOOK_SITE, DRY_MIXER, ENGINE, ENGINE_INIT,
                             LIBC_INIT_ARRAY, PATCH_MAGIC, STUB_OBJECT, TrackerEmu)
    print("\n3. boot-time state reset (state filled with 0xff before engine init)")
    ok = True
    outputs = {}
    for label in ("stock", "patched, via 0xf3fa", "patched, reset skipped"):
        emu = TrackerEmu()
        emu.reset_runtime()
        emu.call(LIBC_INIT_ARRAY)
        if label != "stock":
            emu.apply_master_fx_patch()
            emu.record_ui_passthrough()
            emu.uc.mem_write(PATCH_STATE, b"\xff" * 0x400)          # power-on garbage
        if label == "patched, reset skipped":
            emu.call(ENGINE_INIT, ENGINE, STUB_OBJECT, 0)           # bypass the hook
        else:
            emu.init_engine()
        tones = Tones([1000], -20)
        out = run(emu, tones, 20, capture=True)
        outputs[label] = out
        if label == "patched, via 0xf3fa":
            magic, dj, enabled, held, by_ui = struct.unpack("<IfIII", emu.uc.mem_read(PATCH_STATE, 20))
            good = (magic == PATCH_MAGIC and dj == 0.0 and enabled == 1 and held == 0 and by_ui == 0)
            ok &= good
            print(f"   {'ok ' if good else 'BAD'} state after boot: magic {magic:#x}, dj {dj}, "
                  f"enabled {enabled}, held {held}, moved-by-UI {by_ui}")
            log = emu.record_ui_passthrough()
            emu.turn(2)
            good = log == [("knob", 0, 2)]
            ok &= good
            print(f"   {'ok ' if good else 'BAD'} jog after boot reaches the firmware: {log}")
    same = outputs["patched, via 0xf3fa"] == outputs["stock"]
    ok &= same
    print(f"   {'ok ' if same else 'BAD'} audio after boot vs stock: "
          f"{'bit-identical' if same else 'DIFFERENT'}")
    bad = outputs["patched, reset skipped"]
    peak = max(abs(v) for v in bad)
    print(f"   (without the reset: peak {peak} vs {max(abs(v) for v in outputs['stock'])} stock, "
          f"{'same' if bad == outputs['stock'] else 'different'} audio)")
    return ok


def hf_db(samples):
    out = list(samples)
    for _ in range(2):                     # 4th-order high-pass at 8 kHz
        w0 = 2 * math.pi * 8000 / RATE
        alpha, cw = math.sin(w0) / (2 * 0.707), math.cos(w0)
        b0, b1, b2 = (1 + cw) / 2, -(1 + cw), (1 + cw) / 2
        a0, a1, a2 = 1 + alpha, -2 * cw, 1 - alpha
        x1 = x2 = y1 = y2 = 0.0
        for i, x in enumerate(out):
            y = (b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2) / a0
            x2, x1, y2, y1 = x1, x, y1, y
            out[i] = y
    e = sum(v * v for v in out[2000:]) / (len(out) - 2000)
    return 10 * math.log10(e / 32767 ** 2 + 1e-20)


def audio() -> None:
    print("\n4. audio")
    emu = boot("")
    emu.record_ui_passthrough()
    tones = Tones([5000], -20)
    flat = level_db(run(emu, tones, 30, capture=True)[-1280:], -20)
    emu.press(BTN_SHIFT); emu.press(BTN_MASTER); emu.turn(-40)     # dj -0.8
    run(emu, tones, 60)
    closed = level_db(run(emu, tones, 10, capture=True), -20)
    emu.release(BTN_MASTER)
    blocks = 0
    while dj_cur(emu) != 0.0 and blocks < 200:
        run(emu, tones, 1)
        blocks += 1
    run(emu, tones, 30)
    back = level_db(run(emu, tones, 10, capture=True), -20)
    print(f"   5 kHz: flat {flat:+.1f} dB, Shift+Master+jog -40 (LP) {closed:+.1f} dB, "
          f"after release {back:+.1f} dB")
    print(f"   snap-back: dry again after {blocks} blocks ({blocks * BLOCK_SAMPLES / RATE * 1000:.0f} ms)")

    results = []
    for gesture in (False, True):
        emu = boot("")
        emu.record_ui_passthrough()
        tones = Tones([100, 400], -20)

        def on_block(b, emu=emu, gesture=gesture):
            if not gesture:
                return
            if b == 40: emu.press(BTN_SHIFT); emu.press(BTN_MASTER)
            if 40 <= b < 80: emu.turn(-1)                        # sweep down to -0.8
            if b == 120: emu.release(BTN_MASTER)                 # snap back
            if b == 160: emu.press(BTN_MASTER); emu.turn(40)     # jump to +0.8 HP
            if b == 200: emu.release(BTN_SHIFT)                  # snap back
        results.append(hf_db(run(emu, tones, 240, capture=True, on_block=on_block)))
    print(f"   clicks above 8 kHz (100+400 Hz tones): {results[1]:.1f} dBFS with gestures, "
          f"{results[0]:.1f} without")


def demo(firmware=None, boot_entry=None, suffix="djcontrol"):
    """Render the gesture over pattern 1.

    By default the harness applies the patch. With `firmware` (an already
    patched image, e.g. decoded from the built .ptf) nothing is patched: the
    state is filled with 0xff like power-on SDRAM and engine init goes
    through the image's own patch_boot (`boot_entry`).
    """
    from m6_load_song import DEFAULT
    from m7_render import OUT, RIGHT, step_seconds
    from tracker_emu import LIBC_INIT_ARRAY, TrackerEmu
    emu = TrackerEmu(firmware) if firmware else TrackerEmu()
    emu.reset_runtime()
    emu.call(LIBC_INIT_ARRAY)
    emu.load_project(DEFAULT)
    if firmware:
        emu._boot_entry = boot_entry
        emu.uc.mem_write(PATCH_STATE, b"\xff" * 0x400)
    else:
        emu.apply_master_fx_patch()
    emu.record_ui_passthrough()
    emu.init_engine()
    emu.init_sequencer()
    emu.load_instruments(DEFAULT)
    buffer = emu.load_pattern(DEFAULT, 1)
    steps = emu.uc.mem_read(buffer + 0x1C, 1)[0] + 1
    emu.play()
    blocks = math.ceil(steps * step_seconds(emu) * RATE / BLOCK_SAMPLES)
    sec = lambda t: int(t * RATE / BLOCK_SAMPLES)
    # (block, action) -- a played gesture: low-pass down, release; high-pass up, release
    script = {sec(1.0): lambda: (emu.press(BTN_SHIFT), emu.press(BTN_MASTER)),
              sec(3.6): lambda: emu.release(BTN_MASTER),
              sec(4.6): lambda: emu.press(BTN_MASTER),
              sec(6.8): lambda: emu.release(BTN_SHIFT)}
    turns = {**{b: -1 for b in range(sec(1.2), sec(2.8), 3)},   # 40 detents -> -0.8
             **{b: 1 for b in range(sec(4.8), sec(6.0), 3)}}    # ~33 detents -> +0.65
    frames = bytearray()
    for b in range(blocks):
        if b in script: script[b]()
        if b in turns: emu.turn(turns[b])
        emu.clock(BLOCK_SAMPLES / RATE)
        emu.render_block(LEFT, RIGHT)
        left = struct.unpack(f"<{BLOCK_SAMPLES}h", emu.uc.mem_read(LEFT, 256))
        right = struct.unpack(f"<{BLOCK_SAMPLES}h", emu.uc.mem_read(RIGHT, 256))
        for l, r in zip(left, right):
            frames += struct.pack("<hh", l, r)
    name = f"{DEFAULT.name.replace(' ', '_')}_pattern01_{suffix}.wav"
    OUT.mkdir(exist_ok=True)
    with wave.open(str(OUT / name), "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(RATE)
        w.writeframes(bytes(frames))
    print(f"\n5. wrote out/{name}: {len(frames) // 4 / RATE:.2f} s")
    return OUT / name


if __name__ == "__main__":
    ok = routing()
    ok &= indicator()
    ok &= boot_reset()
    if "--quick" not in sys.argv:
        audio()
    if "--demo" in sys.argv:
        demo()
    sys.exit(0 if ok else 1)
