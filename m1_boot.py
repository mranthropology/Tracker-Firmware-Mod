"""M1: run static constructors and verify the audio graph matches the static map."""
from collections import Counter

from tracker_emu import ENGINE, LIBC_INIT_ARRAY, TrackerEmu

# vtable -> (label, expected count from static analysis)
KNOWN = {
    0xA9FB0: ("sample player", 9),          # 8 tracks + preview player
    0xA9F60: ("track filter", 8),
    0xA9E18: ("track node 0xa9e18", 8),
    0xC5088: ("2-gain stage", 10),          # 8 tracks + 2 in input section
    0xC52A4: ("track 4-gain split", 8),
    0xC2B84: ("10-in send bus", 2),
    0xC2B90: ("8-in dry mixer", 2),
    0xC2B9C: ("MASTER 4-in mixer (saturation)", 2),
    0xA9F54: ("delay (corrected)", 1),
    0xA9EF0: ("MASTER limiter E[ch]", 2),
    0xA9F6C: ("sidechain sum M", 1),
    0xC2B78: ("final 7-in mixer X[ch]", 2),
    0xA9E00: ("output node O", 1),
    0xC5298: ("AudioEffectDynamics (input)", 2),
    0xA9FC8: ("capture/record queue", None),
    0xA9E0C: ("tap / record queue", None),
}


def main() -> None:
    emu = TrackerEmu()
    emu.reset_runtime()
    emu.call(LIBC_INIT_ARRAY)
    nodes = emu.update_list()
    print(f"constructors ran: {emu.stats.insns:,} instructions")
    print(f"update list: {len(nodes)} nodes")
    counts = Counter(vt for _, vt in nodes)
    print(f"\n{'vtable':>9}  {'count':>5}  {'expected':>8}  label")
    for vt, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        label, expected = KNOWN.get(vt, ("?", None))
        mark = "" if expected in (None, n) else "  <-- MISMATCH"
        print(f"{vt:#9x}  {n:5d}  {str(expected):>8}  {label}{mark}")
    print(f"\nengine object vtable @ {ENGINE:#x}: {emu.r32(ENGINE):#x}")
    if emu.stats.unmapped:
        print("unmapped accesses:", emu.stats.unmapped[:10])
    top = sorted(emu.stats.mmio_writes.items(), key=lambda kv: -kv[1])[:8]
    print("MMIO write pages:", [(hex(p), n) for p, n in top])


if __name__ == "__main__":
    main()
