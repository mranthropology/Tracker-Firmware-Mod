# Polyend Tracker firmware 1.9.2: findings

Reverse engineering of the original Polyend Tracker's firmware, an emulator for
its audio engine, and a working patch that adds a software master EQ and a DJ
filter to the main mix. **Status (2026-10-13): the patch runs on real hardware.**
It also adds **Mutable Instruments Braids as a real-time synth engine**,
also running on hardware. Detailed addresses and procedures are in
[README.md](README.md).

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

| **br2: + 96 kHz bell/drum/snare/particle, 48 templates** | **booted "1.9.2 BR2"; "Braids Templates" project loads** |
| **br: EQ/DJ + Braids** | **booted "1.9.2 BR"; Braids instruments play; 3 Braids tracks, no audible crackling** |

Still open: confirming that WAV exports include the EQ and DJ filter; a CPU
stress test with 4 expensive Braids tracks.

## 7. Braids synth engine (`braids/`)

The Braids macro oscillator (MIT licence, Emilie Gillet) is compiled for the
K66 from an unmodified upstream checkout and runs in real time as a Tracker
instrument. It's not sample playback: the instrument's sample is never read.

- **Player:** each track has one sample player (vtable `0xa9fb0`, update
  `0xd074c`, at voice+0x120, voices at `0x1fffb800` + t×`0x660`), so tracks
  are monophonic. The chain after it is amp envelope (inside the player) →
  bit reducer → filter (feeds the reverb and delay sends) → gain → volume/pan
  → dry mixers.
- **Hooks:**
  - `0xd0758` (playMode read in render) → `braids_render_stub` fills the
    player's block and rejoins at `0xd0776`, so the envelope and everything
    after it still apply.
  - `0xd0a50` (after the mode-specific note-on) → `braids_note_stub` picks
    the model and retriggers.
  - Boot: `0xf3fa` → `braids_boot` (copies `.data`, zeroes `.bss`) →
    `patch_boot` → engine init.
- **Use:**
  - Name an instrument `BR-<model>`, by name or number 0–47. The on-device
    keyboard has no `:`; `BR:` also works.
  - Put it in Granular mode.
  - Pitch comes from player +0x74, so tune, finetune, glide and pitch FX
    all apply.
  - **Timbre** is Granular Position (with its envelope, LFO and the `p` step
    FX). **Color** is grain length (log scale).
  - Up to 4 Braids tracks; a 5th plays its sample.
- **Output rate is 44,117.647 Hz** (Teensy audio rate; the player increment
  is 0.9996 at C4). Braids renders directly at that rate with a pitch offset,
  so tuning is exact, but its 96 kHz-based decay times run about 2.2× long.
- **Measured in the emulator:**
  - 110 KB of flash at `0xD9000`, plus 17 KB of SDRAM per voice.
  - Tuning through the full chain is within ±0.1 cents.
  - 2.8k–31k instructions per block per model (median 13k).
  - Pattern 1 with 2 Braids tracks: 199k instructions per block, against
    180k stock.
  - Output is bit-identical to stock when no instrument is `BR-`.
- **Tools:** `patch/link.py` gained RAM `.data`/`.bss` support and
  `tracker_emu.call` gained stack arguments. `make_ptf.py --hooks ...,braids`
  builds the file; `m13`–`m15` test it.
- **Hardware (2026-10-13):** `PolyendTracker_br.ptf` boots and shows
  "1.9.2 BR". Braids instruments play, and 3 Braids tracks at once showed
  no audible crackling.

## 8. Swappable master reverb and delay (`fxbank/`) — experimental

> **⚠️ Experimental.** The first build froze the UI and crackled once a reverb
> or delay send was on. The current build (`bank5`) passed a first hardware
> test: plate + tape with sends on every instrument, no audible crackle and
> no clear UI slowdown. Plate + tape costs ~128k cycles a block against
> stock's ~116k (of ~522k), so headroom is a little lower than stock. Only
> one project has been tried. It still boots on the stock effects, shows its
> CPU cost on Shift + Master, and cuts an effect back to stock if it runs too
> long. Use with care.


The stock master reverb and delay can be replaced by other algorithms,
chosen on the device, like the Korg NTS's effect slots. Version 1 is a
built-in bank with a plate reverb and a tape delay.

- **Hook:** each effect is one audio node, and the firmware reaches it only
  through one word in flash (slot 0 of its vtable: `0xa9efc` reverb,
  `0xa9f54` delay). The patch changes those two words; no code is rewritten.
  Routing, sends and WAV export are unchanged.
- **Controls:** the replacements read the stock nodes' parameter fields, so
  the stock Master Reverb page (Size, Damping, Predelay, Diffusion) and Master
  Delay page (Time/Sync/Rate, Feedback, PingPong, Filter) drive them.
- **Selection:** hold Shift + Master and press any other button to step
  through Plate + Tape (default after boot), Stock rev + Tape, Plate + Stock
  dly, and all stock. The choice shows in the pop-up and isn't saved.
- **Plate:** Dattorro's plate (1997), scaled to 44.1 kHz, block-wise in
  float SDRAM lines. Size sets the decay, Damping the tank low-pass,
  Diffusion the input diffusers. Level matched to stock.
- **Tape:** runs on the stock delay's own lines, so switching keeps the
  echoes. Time changes glide like a tape speed change (no clicks), with wow,
  flutter, head loss, the stock Filter, and saturation in the loop. The top
  of the Feedback range self-oscillates, bounded by the saturation.
- **Emulator results (m18, m19; current int16 plate):**

  | | stock | bank |
  |---|---|---|
  | reverb tail level (noise burst) | −37.7 dBFS | −37.5 dBFS |
  | reverb −30 dB time, Size 0.5 | 1.20 s | 1.32 s (0.74 s at Size 0.1, 3 s+ at 0.9) |
  | reverb L/R correlation | +0.16 | +0.01 |
  | reverb cost, noise burst (instructions; hardware cost is dominated by SDRAM waits) | 34.3k insns/block | 56.1k (~600 once idle) |
  | delay first echo, 1 kHz burst | −15.3 dBFS | −15.4 dBFS |
  | largest step on a 300 → 150 ms change (220 Hz tone; clean sine ≤ 251) | 353 (click) | 317 (pitch glide) |
  | delay cost, project pattern 1 | 8.3k | 10.8k |

  All-stock selection is bit-identical to the master FX patch alone. The
  built `.ptf` matches the tested image byte for byte and, booted with garbage
  SDRAM, renders bit-identical to the emulator.
- **Hardware (2026-10-14):**
  - `PolyendTracker_bank.ptf` boots ("1.9.2 BANK") and the selector steps
    through all four choices. With a reverb or delay send on, the UI froze
    and the audio crackled: out of CPU.
  - Diagnostic `bankd` (real cycle counts): the stock reverb costs ~92k
    cycles a block on hardware (34.5k emulated instructions, so ~2.7 cycles
    each: SDRAM waits). The first plate went over 150k and was cut back to
    stock by the guard; the tape costs ~25k. Crackling remained with both
    effects on stock and sends on every instrument, so part of it may be the
    pattern's load itself (stock 1.9.2 comparison pending).
  - `bank3` (int16 plate, 4 taps, readout of the plate's own cost, guard no
    longer changes the selection) is built but not tested on hardware.
  - `bank4` (experimental) adds the plate's short lines in internal SRAM
    (10 KB from the firmware heap at boot; ~37 % of its memory traffic). Boots
    on hardware ("1.9.2 BANK4", 2026-10-17), so the boot-time allocation is
    fine. With sends on: all stock has no crackle; plate + tape no longer
    freezes but crackles very slightly and the pattern animation drops frames
    (audio load near the limit). Project and sample loading work. Readout
    with plate + tape: `R112 P115 D25 T25k` (stock reverb was ~92k).
  - `bank5` (experimental): the tank all-passes join the SRAM lines (26 KB
    from the heap in total), and the tape saturation is a cubic instead of a
    divide, with both neighbours of each read in one 32-bit access. Boots
    ("1.9.2 BANK5"). Readout, thousand cycles per block: stock reverb 92,
    stock delay 24; plate 106 (was 115), tape 22 (was 25). The tape now costs
    less than the stock delay; plate + tape is ~12k over stock.
    With plate + tape and sends on every instrument: no audible crackle,
    no clear UI slowdown (first test, one project). The approach follows OpenTracker
    ([sandlbn/polyend-tracker-re](https://github.com/sandlbn/polyend-tracker-re), MIT), which keeps
    its reverb's input all-passes in SRAM and documents the SDRAM as 16-bit;
    the implementation here is independent.
- **Independent confirmation:** OpenTracker's source agrees with several
  findings here: the 44,117.647 Hz output rate, Master EQ/Bass Boost/Space in
  the SGTL5000, Master = button 12, reverb Predelay × 5000 and Damping as
  1 − value, the delay's three int16 lines (ping-pong A+B, mono C), and the
  `.pti` layout (body 0x174 at 0x10, CRC at 0x184, audio at 0x188). It also
  identifies the stock reverb as following the Clouds/Rings FxEngine reverb.
- **Not done yet:** bank3 on hardware; a stock-firmware crackle baseline;
  the reverb's own settings page (the firmware has Master Reverb / Master
  Delay sub-pages, opened by `0x351b8` / `0x350f4`; the button is not yet known) (SDRAM latency is not in
  instruction counts); loading effects from the SD card (option 2: the same
  two entry points, with code loaded at runtime).
- **Korg NTS units:** logue SDK v1 units are linked to fixed addresses on
  Korg's chip (code `0x20019000`, SDRAM `0xC0420000`, API `0x0807b000`), which
  don't exist on the K66, so prebuilt units can't run. Units with source can
  be rebuilt against this bank's entry points.

## 9. Next ideas

- **Momentary performance effects on Master + pads:** beat repeat, tape stop,
  bitcrush and filter sweeps.
  - About 155 KB of flash and about 1 MB of SDRAM are free.
  - These need no saved settings.
- A bus compressor, which would need somewhere to save its settings in the
  project file.
- Song mode in the harness; checking renders against device exports.
- Braids: 44.1 kHz tables, so decays match the module; template `BR-` `.pti`
  files (needs the `.pti` CRC); a real-cycle cost measurement.
