"""Hardware probes for how the Tracker's updater handles data beyond the stock image.

Usage: python3 -m patch.make_probes <stock .ptf> <out dir>

Each probe only changes which format string the startup version text uses
(literal pools 0x288a0 "%d.%d.%d" and 0x2887c "%d.%d.%d beta %d", read by
0x2878c into the buffer Config -> Firmware shows). No code is changed.

  p0  control, nothing appended: release format -> the stock beta format
      string. Expect "1.9.2 beta <n>". Tests the probe mechanism alone.
  p1  marker strings appended directly after the image (0xd6460), in the
      same 4 KB sector as the image's last bytes. Expect "1.9.2 P1".
  p2  marker strings at 0xd7000 (next sector, where the patch went), with
      0xff filler records from 0xd6460 so the file's data is contiguous.
      Expect "1.9.2 P2".
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

from patch.make_ptf import FLASH_CONFIG, VERSION_FORMATS, read_hex, write_hex

RELEASE_POOL, BETA_POOL = 0x288A0, 0x2887C


def probe(stock: bytearray, kind: str) -> tuple[bytearray, tuple[int, int] | None]:
    image = bytearray(stock)
    for pool, (old, _) in VERSION_FORMATS.items():
        if struct.unpack_from("<I", image, pool)[0] != old:
            raise RuntimeError(f"unexpected literal at {pool:#x}")
    if kind == "p0":
        struct.pack_into("<I", image, RELEASE_POOL, VERSION_FORMATS[BETA_POOL][0])
        return image, None
    base = len(stock) if kind == "p1" else 0xD7000
    blob, at = b"", {}
    for pool, (_, fmt) in VERSION_FORMATS.items():
        at[pool] = base + len(blob)
        blob += fmt + b" " + kind.upper().encode() + b"\0"
    image += b"\xff" * (base + len(blob) - len(image))
    image[base:base + len(blob)] = blob
    for pool, addr in at.items():
        struct.pack_into("<I", image, pool, addr)
    return image, (len(stock), base + len(blob))      # contiguous from the image end


def main() -> None:
    stock_path, out_dir = Path(sys.argv[1]), Path(sys.argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)
    stock_text = stock_path.read_text(newline="")
    stock, ranges = read_hex(stock_path)
    if write_hex(stock_text, stock) != stock_text:
        raise SystemExit("re-encoding the stock image does not reproduce the stock .ptf; refusing")
    for kind in ("p0", "p1", "p2"):
        image, extra = probe(stock, kind)
        assert image[FLASH_CONFIG[0]:FLASH_CONFIG[1]] == stock[FLASH_CONFIG[0]:FLASH_CONFIG[1]]
        out = out_dir / f"PolyendTracker_{kind}.ptf"
        text = write_hex(stock_text, image, extra)
        out.write_bytes(text.encode("ascii"))
        check, check_ranges = read_hex(out)
        covered = lambda rs: {a for s, e in rs for a in range(s, e)}
        assert check == image and covered(check_ranges) == covered(ranges + ([extra] if extra else []))
        changed = sorted({a & ~3 for a in range(len(stock)) if image[a] != stock[a]})
        print(f"{out.name}: stock bytes changed at {', '.join(hex(a) for a in changed)}; "
              f"appended {'nothing' if not extra else f'{extra[0]:#x}-{extra[1]:#x}'}")


if __name__ == "__main__":
    main()
