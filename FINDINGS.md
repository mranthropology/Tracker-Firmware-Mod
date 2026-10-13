# Polyend Tracker firmware 1.9.2: findings

Reverse engineering of the original Polyend Tracker's firmware, an emulator for
its audio engine, and a working patch that adds a software master EQ and a DJ
filter to the main mix. **Status (2026-10-13): the patch runs on real hardware.**
Detailed addresses and procedures are in [README.md](README.md).

No Polyend code is in this repository. Everything here is analysis, original
code, and tools that work on a firmware file you supply yourself.

## 1. Hardware

| | Tracker (fw 1.9.2) | Tracker+ (fw 1.2.0) |
|---|---|---|
| MCU | NXP Kinetis K66 (MK66FX1M0, Cortex-M4F, 180 MHz), Teensy-3.6-style | NXP i.MX RT106x (Cortex-M7) |
| Image | Intel HEX `.ptf` at `0x0`, ~877 KB of 1 MB flash | at `0x60030000` (FlexSPI) |
| RAM | 256 KB SRAM + 16 MB SDRAM at `0x70000000` | 64 MB SEMC SDRAM |
| Codec | SGTL5000 (I2C `0x0A`) | — |

## 2. Audio engine

- Teensy-Audio-style `AudioStream` graph: 113 nodes, 128-frame blocks at 44.1 kHz.
  Update list at `*(0x2000bc40)`; block allocate/release/transmit at
  `0xce42c`/`0xce4d8`/`0xce530`.
- **Master chain** (engine `0x1fff9bd8`, mixer section +`0xee0`):
  - The dry 8-input mixers feed the master 4-input mixers (class `0xc2b9c`).
  - The master mixers' update calls saturation `0xd42f0` (modes 0–4) at `0x76c6a`.
  - Then: limiter → final 7-input mixer → output.
  - Reverb is the +`0x3e4` send node; delay is +`0x390`.
- **Master EQ, Bass Boost and Space ("surround") are not DSP in the
  firmware.** They are SGTL5000 codec features: `0x704c0` sets the codec's
  5-band graphic EQ from the floats at `0x20004c60`, and `0x70574` sets bass
  enhance. So they apply only to the codec's analogue output. Static analysis
  shows WAV exports bypass them (not yet confirmed by an export test).

## 3. Emulator harness

Unicorn-based Cortex-M4 emulation of the stock image (`tracker_emu.py`):

- Runs C++ static constructors and the firmware's own engine init against a stub
  HAL and codec.
- Loads a real project, instruments and patterns through the firmware's own
  loaders.
- Plays a pattern with the real sequencer and voices, and renders it to WAV.
- Python overrides replace individual functions (file reads, the UI), so
  patches can be tested, profiled and compared bit for bit with stock.

## 4. The patch (`patch/master_fx.c`, ~5.9 KB at `0xD7000`)

- **Hook:** the master mixer's `bl 0xd42f0` at `0x76c6a` goes to the patch. The
  patch processes the 32-bit accumulator, then calls the original saturator,
  so it affects live output and should be in exports.
- **Master EQ:** 5 bells at 115 / 330 / 990 / 3000 / 9900 Hz, ±12 dB, driven
  by the firmware's own Master EQ settings. The codec EQ setter is made a
  no-op, so the EQ isn't applied twice.
- **DJ filter:** one control from −1 to +1. Turning down sweeps a low-pass from
  20 kHz to 80 Hz; turning up sweeps a high-pass from 20 Hz to 8 kHz. It
  crossfades through dry around centre.
- **DSP:** TPT state-variable filters, ~24 ms parameter glide, per-sample
  coefficient ramps.
  - Cost: +21.5k instructions per block for the EQ, +27k with the DJ filter.
  - Clicks when controls move: −101 dBFS, against a −104 dBFS floor.
  - With all settings neutral, output is bit-identical to stock.
- **Controls:** hold **Shift + Master** and turn the jog wheel. Releasing
  either button snaps the filter back to centre. The firmware's own pop-up
  shows "DJ Low-pass 40%" and similar. Hooks sit on the board's input
  callbacks (`0xf628` knob, `0xf636` press, `0xf646` release). Master is
  button 12 and Shift is button 29, both confirmed on hardware.
- **Boot reset:** SDRAM isn't cleared at power-on. App setup's only call to
  engine init (`0xf3fa`) runs `patch_boot` first, which clears the patch
  state. This happens after SDRAM init (`0xea4`) and before audio starts.
- **State memory:** 1 KB at `0x70FE0000`. Checked statically: the firmware's
  SDRAM use ends at `0x70ef8874`. Checked at runtime: zero firmware accesses
  above that over boot, load and playback.
- **Compiler pitfall:** Apple clang 17's Cortex-M4 alignment-hazard pass put a
  `nop` inside an IT block, making a conditional instruction unconditional.
  `build.sh` disables that pass and rejects any conditional `nop`.

## 5. Firmware files and updating

- A `.ptf` is plain Intel HEX: type-02 segment records and CRLF line endings.
  The app never checksums its own image.
- **Updates are flashed by a separate chip.** The app writes the chosen file
  name to `/Firmware/_fwinfo`, signals through GPIO 81, and waits. The app
  itself can only write its EEPROM emulation area.
- **The data in the file must not have gaps.** A patch placed after a gap
  (image ends at `0xd6460`, patch at `0xd7000`) hung at boot. With `0xff`
  filler from the image end, it boots. This was isolated with probe builds
  that change only the version text in Config → Firmware (p0, p1, p2).
- **File names:** the updater shows only 32 characters, so keep names at 31
  or fewer, starting with `PolyendTracker_` and ending with `.ptf`.
- **Recovery:** Polyend's emergency flash.
  - Reset button: flashes a random `.ptf` from `Firmware/`.
  - Polyend Tool: with the SD card out, choose the file, then press reset.

  Polyend Tool with the stock file is the dependable way back.
- `patch/make_ptf.py` builds the file. It keeps every stock record, rewrites
  only the records at hook sites, and adds the patch with no gap. It checks the
  stock bytes at every site and that the flash security field is unchanged.
  It refuses to build unless the unmodified image re-encodes to the stock
  file byte for byte. `m12_ptf_verify.py` proves the output equals the tested
  image.

## 6. Hardware test log

| Build | Result |
|---|---|
| Long name, patch after a gap | garbled name on the update screen, hung at boot |
| t1: marker only, after a gap | hung |
| p0: in-image change only | booted, "1.9.2 beta" |
| p1: 32 bytes right after the image | booted, "1.9.2 P1" |
| p2: marker at `0xd7000`, no gap | booted, "1.9.2 P2" |
| **fx: full patch, no gap** | **booted "1.9.2 FX"; DJ filter, pop-up, snap-back and Master EQ work; stock shortcuts unaffected; cold boot clean** |

Still open: confirming that WAV exports include the EQ and DJ filter, and a
CPU stress test.

## 7. Next ideas

- **Momentary performance effects on Master + pads:** beat repeat, tape stop,
  bitcrush and filter sweeps.
  - About 155 KB of flash and about 1 MB of SDRAM are free.
  - These need no saved settings.
- A bus compressor, which would need somewhere to save its settings in the
  project file.
- Song mode in the harness; checking renders against device exports.
