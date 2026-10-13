"""M11: is the patch state (0x70FE0000..+0x400) unused by the firmware?

Static map (from SDRAM literals in the code, see README):
  0x70000000-0x700e2000  buffer (0x2c9a2, length 0xe2000)
  ...-0x7026a000         other statics (0x700e2000, 0x70262000)
  0x7026a000-0x70daa000  sample pool (0x563f8: base 0x7026a000, size 0xb40000)
  0x70daa000-0x70ef8874  statics; top object 0x70ef8574 is 4 x 0xc0 bytes (0x84100)
  0x70ef8874-0x71000000  no references found (patch state at 0x70FE0000)

Runtime: a Unicorn memory hook on everything above the top object, over the
full run: ctors, engine init (through patch_boot), project, 16 instruments,
pattern load, and full-pattern playback with DJ gestures. Accesses from the
patch's own code (0xD7000+) and the harness's capture buffers (0x70FF0000,
written by the output node because the harness put them there) are reported
separately.
"""
import math

from unicorn import UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE, UC_MEM_WRITE

from m6_load_song import DEFAULT
from m7_render import LEFT, RATE, RIGHT, step_seconds
from tracker_emu import (BLOCK_SAMPLES, BTN_MASTER, BTN_SHIFT, LIBC_INIT_ARRAY,
                         PATCH_BASE, PATCH_STATE, TrackerEmu)
from unicorn.arm_const import UC_ARM_REG_PC

TOP_OBJECT_END = 0x70EF_8874
SDRAM_END = 0x7100_0000
CAPTURE = (LEFT, RIGHT + 0x200)
PATCH_CODE = (PATCH_BASE, PATCH_BASE + 0x2000)


def main() -> None:
    emu = TrackerEmu()
    hits = {"firmware": {}, "patch": {}, "capture": 0}

    def hook(uc, access, address, size, value, user):
        if CAPTURE[0] <= address < CAPTURE[1]:
            hits["capture"] += 1
            return
        pc = uc.reg_read(UC_ARM_REG_PC)
        who = "patch" if PATCH_CODE[0] <= pc < PATCH_CODE[1] else "firmware"
        kind = "W" if access == UC_MEM_WRITE else "R"
        key = (pc, kind, address & ~0xFFF)
        hits[who][key] = hits[who].get(key, 0) + 1

    emu.uc.hook_add(UC_HOOK_MEM_READ | UC_HOOK_MEM_WRITE, hook,
                    begin=TOP_OBJECT_END, end=SDRAM_END - 1)

    emu.reset_runtime()
    emu.call(LIBC_INIT_ARRAY)
    emu.load_project(DEFAULT)
    emu.apply_master_fx_patch()
    emu.record_ui_passthrough()
    emu.init_engine()                       # through patch_boot (0xf3fa hook)
    emu.init_sequencer()
    emu.load_instruments(DEFAULT)
    buffer = emu.load_pattern(DEFAULT, 1)
    steps = emu.uc.mem_read(buffer + 0x1C, 1)[0] + 1
    emu.play()
    blocks = math.ceil(steps * step_seconds(emu) * RATE / BLOCK_SAMPLES)
    for b in range(blocks):
        if b == 100: emu.press(BTN_SHIFT); emu.press(BTN_MASTER)
        if 100 < b < 140: emu.turn(-1)
        if b == 200: emu.release(BTN_MASTER)
        emu.clock(BLOCK_SAMPLES / RATE)
        emu.render_block(LEFT, RIGHT)

    print(f"watched 0x{TOP_OBJECT_END:08x}-0x{SDRAM_END:08x} over boot, load and "
          f"{blocks} blocks ({blocks * BLOCK_SAMPLES / RATE:.2f} s) of playback")
    fw = hits["firmware"]
    print(f"  firmware accesses: {sum(fw.values())}")
    for (pc, kind, page), n in sorted(fw.items())[:20]:
        print(f"    pc 0x{pc:05x} {kind} page 0x{page:08x} x{n}")
    pages = sorted({page for (_, _, page) in hits["patch"]})
    in_state = all(PATCH_STATE <= p < PATCH_STATE + 0x1000 for p in pages)
    print(f"  patch accesses: {sum(hits['patch'].values())} "
          f"(pages {', '.join(f'0x{p:08x}' for p in pages)}; "
          f"{'all inside the state page' if in_state else 'OUTSIDE the state page'})")
    print(f"  harness capture-buffer writes (harness only, not on the device): {hits['capture']}")


if __name__ == "__main__":
    main()
