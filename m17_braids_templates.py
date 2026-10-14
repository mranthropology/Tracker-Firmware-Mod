"""M17: check the 48 Braids template instruments (braids/make_templates.py).

Usage: m17_braids_templates.py [project dir containing instruments/]
       (default out/braids_templates)

1. Files: CRC-32 at 0x184, frame count = PCM length, 16-byte header,
   name "BR-...", playback mode Granular, numbered "<slot+1> ...".
2. Instruments: load all 48 into the patched harness (master FX + Braids) as
   a project's instruments, play each slot, check Braids picked the right
   model from the name and the track makes sound.
"""
import struct
import sys
import zlib
from pathlib import Path

from m13_braids_cost import MODELS
from m14_braids_voice import NOTE_CMD, VOICE0, boot, note_on, render, slots, wait_idle

ROOT = Path(__file__).parent


def main():
    project = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "out" / "braids_templates"
    files = sorted((project / "instruments").glob("*.pti"), key=lambda p: int(p.name.split(" ")[0]))
    ok = len(files) == 48
    print(f"1. {len(files)} files in {project}/instruments")
    bad = []
    for slot, path in enumerate(files):
        d = path.read_bytes()
        s = d[0x10:0x184]
        checks = {
            "number": int(path.name.split(" ")[0]) == slot + 1,
            "crc": zlib.crc32(d[:0x184]) == struct.unpack_from("<I", d, 0x184)[0],
            "frames": struct.unpack_from("<I", s, 0x2C)[0] == (len(d) - 0x188) // 2,
            "header": d[:4] == b"TI\x01\x00",
            "name": s[0x05:0x08] == b"BR-",
            "granular": s[0x3C] == 7,
        }
        if not all(checks.values()):
            bad.append((path.name, [k for k, v in checks.items() if not v]))
    ok &= not bad
    print(f"   {'ok ' if not bad else 'BAD'} CRC, frame count, header, name, mode: "
          f"{'all 48' if not bad else bad}")

    print("2. play each template through the patched firmware")
    emu, inst = boot()
    loaded = emu.load_instruments(project)
    ok &= len(loaded) == 48
    wrong, silent = [], []
    for slot in range(48):
        note_on(emu, 0, slot, 60)
        out = render(emu, 6)
        model = slots(emu)[0][0]
        if model != slot:
            wrong.append((slot, MODELS[slot], model))
        if max(abs(v) for v in out) == 0:
            silent.append(MODELS[slot])
        emu.call(NOTE_CMD, VOICE0, 0xFD)            # note cut
        wait_idle(emu, 0)
    ok &= not wrong
    print(f"   {'ok ' if not wrong else 'BAD'} model chosen from the name: "
          f"{'48/48 correct' if not wrong else wrong}")
    print(f"   {'ok ' if not silent else 'note'} audible within 6 blocks: "
          f"{48 - len(silent)}/48" + (f" (quiet at mid Timbre/Color: {silent})" if silent else ""))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
