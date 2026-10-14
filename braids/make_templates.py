"""Build 48 Braids template instruments (.pti) for a Tracker project.

Usage: python3 -m braids.make_templates <out dir> [reference.pti]

Each file is a normal Tracker instrument that the Braids patch plays as
Braids (see tracker_voice.cc):
  - settings from the firmware's own default-instrument routine 0x225dc
    (run in the emulator), then: name "BR-<MODEL>", playback mode Granular
    (7), Granular Position 32768 (Timbre mid), grain length 1393 samples
    (Color mid on the log scale), filter/envelopes as defaults
  - sample: 1 s of that Braids model at C4 (Timbre/Color mid), rendered by
    the same braids.bin in the emulator (through the 96 kHz path for the
    models the patch renders that way), so on stock firmware the instrument
    still plays something representative
  - file layout as observed in real .pti files: 16-byte header (copied from
    the reference file), 0x174-byte struct, CRC-32 (zlib) of bytes 0..0x183
    at 0x184, mono 16-bit PCM from 0x188; struct +0x2c = frame count
  - file names "<slot+1> BR-<MODEL>.pti" (the firmware formats
    "%s/%s/instruments/%d %s.pti" with slot+1, at 0x716f6)
"""
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from m13_braids_cost import MODELS, Braids  # noqa: E402
from m16_braids_rate import AT_96K, render96  # noqa: E402

DEFAULT_INSTRUMENT = 0x225DC     # (fm, instrument*) fills defaults
SCRATCH = 0x70FD0000             # free SDRAM for the default struct
STRUCT_SIZE = 0x174
CRC_AT, PCM_AT = 0x184, 0x188
GRANULAR = 7
TIMBRE_POSITION = 32768
COLOR_GRAIN = 1393               # 44 * (44100/44) ** 0.5 -> Color ~16384
SECONDS = 1.0
FS = 750000 / 17                 # Tracker output rate
FADE = 0.03

DISPLAY_NAMES = {
    "SAW_SQUARE": "SAW SQUARE", "SINE_TRIANGLE": "SINE TRIANGLE", "SQUARE_SUB": "SQUARE SUB",
    "SAW_SUB": "SAW SUB", "SQUARE_SYNC": "SQUARE SYNC", "SAW_SYNC": "SAW SYNC",
}


def display(name):
    return DISPLAY_NAMES.get(name, name.replace("_", " "))


def build(out_dir: Path, reference: Path):
    ref = reference.read_bytes()
    header = ref[:0x10]
    if zlib.crc32(ref[:CRC_AT]) != struct.unpack_from("<I", ref, CRC_AT)[0]:
        raise SystemExit(f"{reference.name}: CRC check failed; not a usable reference")
    b = Braids()
    emu = b.emu
    emu.uc.mem_write(SCRATCH, b"\0" * STRUCT_SIZE)
    emu.call(DEFAULT_INSTRUMENT, 0, SCRATCH)
    defaults = bytearray(emu.uc.mem_read(SCRATCH, STRUCT_SIZE))
    out_dir.mkdir(parents=True, exist_ok=True)
    frames_total = int(SECONDS * FS)
    blocks = -(-frames_total // 128)
    written = []
    for shape, model in enumerate(MODELS):
        name = f"BR-{display(model)}"
        assert len(name) <= 31, name
        s = bytearray(defaults)
        s[0x00] = 1                                   # active
        s[0x04] = 0                                   # sample type: sample
        s[0x05:0x25] = name.encode("ascii").ljust(32, b"\0")
        s[0x3C] = GRANULAR
        struct.pack_into("<H", s, 0x16A, COLOR_GRAIN)
        struct.pack_into("<H", s, 0x16C, TIMBRE_POSITION)
        b.init()
        b.strike()
        pcm = []
        for _ in range(blocks):
            out, _ = (render96(b, shape, 60 << 7, 16384, 16384) if shape in AT_96K
                      else b.render(shape, 60 << 7, 16384, 16384))
            pcm += out
        pcm = pcm[:frames_total]
        fade = int(FADE * FS)
        for i in range(fade):
            pcm[-fade + i] = int(pcm[-fade + i] * (fade - i) / fade)
        struct.pack_into("<I", s, 0x28, 0)             # sample address: set at load
        struct.pack_into("<I", s, 0x2C, len(pcm))
        body = header + bytes(s)
        data = body + struct.pack("<I", zlib.crc32(body)) + struct.pack(f"<{len(pcm)}h", *pcm)
        path = out_dir / f"{shape + 1} {name}.pti"
        path.write_bytes(data)
        written.append(path)
    return written


def main():
    out_dir = Path(sys.argv[1])
    reference = Path(sys.argv[2]) if len(sys.argv) > 2 else next(
        (ROOT / "projects").glob("*/instruments/1 *.pti"))
    files = build(out_dir, reference)
    print(f"wrote {len(files)} instruments to {out_dir}")


if __name__ == "__main__":
    main()
