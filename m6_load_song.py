"""M6: load a full project (settings, instruments, a pattern) into the emulator.

Usage: m6_load_song.py [project_dir] [pattern_number]
Verifies sample PCM in SDRAM and pattern steps against the source files.
"""
import sys
from pathlib import Path

from tracker_emu import LIBC_INIT_ARRAY, PATTERN_TRACK_STRIDE, TrackerEmu

DEFAULT = Path(__file__).parent / "projects" / "burly ladybug 1-23-24"
FILE_TRACK_OFFSET = 0x1C   # .mtp track data starts here (same layout as the buffer)


def boot_with_project(project: Path, pattern: int = 1):
    emu = TrackerEmu()
    emu.reset_runtime()
    emu.call(LIBC_INIT_ARRAY)
    emu.load_project(project)
    emu.init_engine()
    emu.init_sequencer()
    instruments = emu.load_instruments(project)
    buffer = emu.load_pattern(project, pattern)
    return emu, instruments, buffer


def main() -> None:
    project = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    pattern = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    emu, instruments, buffer = boot_with_project(project, pattern)

    print(f"instruments loaded: {len(instruments)}")
    pcm_ok = 0
    for inst in instruments:
        src = next((project / "instruments").glob(f"{inst['index']} *.pti")).read_bytes()
        if bytes(emu.uc.mem_read(inst["addr"], 256)) == src[0x188:0x188 + 256]:
            pcm_ok += 1
        print(f"  #{inst['index']:2d} {inst['name'][:30]:30s} {inst['frames']:7d} frames "
              f"@ {inst['addr']:#x}")
    print(f"sample PCM verified against files: {pcm_ok}/{len(instruments)}")

    print(f"\npattern {pattern} loaded at {buffer:#x}  (fs requests: {emu.fs_log})")
    raw = (project / "patterns" / f"pattern_{pattern:02d}.mtp").read_bytes()
    span = 8 * PATTERN_TRACK_STRIDE
    loaded = bytes(emu.uc.mem_read(buffer + FILE_TRACK_OFFSET, span))
    same = sum(a == b for a, b in zip(loaded, raw[FILE_TRACK_OFFSET:FILE_TRACK_OFFSET + span]))
    print(f"track data identical to file: {same}/{span} bytes")
    for track in range(8):
        length = emu.uc.mem_read(buffer + FILE_TRACK_OFFSET + track * PATTERN_TRACK_STRIDE, 1)[0]
        steps = " ".join(s.hex() for s in emu.pattern_steps(buffer, track, 6))
        print(f"  track {track + 1}: last step {length:3d} | {steps}")


if __name__ == "__main__":
    main()
