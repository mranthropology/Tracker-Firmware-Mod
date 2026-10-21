# Polyend Tracker audio-engine harness

Runs the stock Polyend Tracker firmware **1.9.2** audio engine on a computer
using [Unicorn](https://www.unicorn-engine.org/) (Cortex-M4 emulation). Only
the audio engine is emulated. There is no screen, pad, SD card or codec.
Purpose: test and profile firmware patches before going near the hardware.

Polyend's firmware is **not** in this repository. Bring your own
`PolyendTracker_1.9.2.ptf` (from Polyend's downloads). `firmware/tracker_1.9.2.bin`
is that Intel HEX flattened to a raw image loaded at 0x0. Built `.ptf` files
contain Polyend's firmware, so they are for personal use and are kept out of git
(see `.gitignore`). See [FINDINGS.md](FINDINGS.md) for the results summary.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install "unicorn>=2.1.2" numpy   # 2.1.1 crashes on Apple Silicon
mkdir -p firmware && .venv/bin/python -c "from pathlib import Path; from patch.make_ptf import read_hex; \
  img, _ = read_hex(Path('PolyendTracker_1.9.2.ptf')); Path('firmware/tracker_1.9.2.bin').write_bytes(img)"
```

Song tests (m5–m7, m9–m12) expect a Tracker project copied to `projects/<name>/`
(`project.mt`, `instruments/`, `patterns/`); the default name is set in
`m6_load_song.py`. Rebuilding the patch needs clang with the ARM target
(`patch/build.sh`); a prebuilt `patch/master_fx.bin` is included.

## Milestones (all passing)

| Script | What it proves |
|---|---|
| `m1_boot.py` | Static constructors build the audio graph: 113 nodes, class counts match the static map |
| `m2_run.py` | Block pool + scheduler run cleanly; silence out, no block leaks; per-node cost profile |
| `m3_tone.py [seconds]` | Tone injected on the dry bus reaches the output through master mixer → limiter → final mixer; writes `out/*.wav` |
| `m4_engine_init.py` | Firmware engine init (0x1611c) runs to completion against a stub HAL/codec (~869k instructions); logs and decodes every codec call |
| `m5_project.py [dir]` | Loads a real `project.mt` through the firmware's own loader (0x73404), including version migration, then runs engine init; the master chain takes on project settings |

| `m6_load_song.py [dir] [n]` | Full song load: project → engine init → sequencer init (0x10f98) → 16 instruments (PCM verified in SDRAM) → pattern n via firmware 0x22de4 (track bytes verified) |

| `m7_render.py [dir] [n] [loops]` | Plays pattern n with the real sequencer and voices and writes `out/<project>_patternNN.wav` (pattern 1: 8.21 s, peak −0.8 dBFS, ~30× slower than realtime) |

| `m8_patch_test.py` | Master EQ + DJ filter patch, v1 vs v2: response, cost, click/zipper level, bit-exact bypass |
| `m9_dj_demo.py [dir] [n]` | Renders a pattern through the patched firmware with an automated DJ sweep → `out/*_djsweep.wav` |
| `m12_ptf_verify.py [ptf] [--no-render]` | Checks the built `.ptf`: every byte it writes equals the harness-tested image; then boots the decoded image as-is (state filled with junk) and renders the gesture, which must be bit-identical to the harness-patched render |
| `m13_braids_cost.py` | Braids (Mutable Instruments, MIT) compiled for the K66: cost per model, tuning, `out/braids_models.wav` |
| `m14_braids_voice.py [--quick]` | Braids as a Tracker instrument through the player hooks: tuning through the full chain, model from name, 4-track limit, cost, song render `out/*_braids.wav` |
| `m15_braids_ptf_verify.py [ptf]` | Checks a `.ptf` with Braids: one gap-free block; every byte equals the tested image; the decoded image booted as-is (garbage SDRAM) renders bit-identical to the harness-patched render |
| `m16_braids_rate.py [--check]` | Which Braids models depend on the sample rate beyond pitch (decay times at 44.1 kHz vs native 96 kHz); `--check` verifies the resampled 96 kHz path (decays, tuning, cost) |
| `m17_braids_templates.py [dir]` | Checks the 48 `BR-` template instruments: CRC, frame count, header, name/mode, and that each plays as its model through the patched firmware |
| `m18_fx_bank.py [--quick]` | FX bank: plate vs stock reverb (level, decay vs Size, stereo, cost, idle, clearing), tape vs stock delay (echo times/levels, ping-pong, time-change glide, max feedback), the Shift + Master + button selector, and whole-engine renders (all-stock selection bit-identical to master_fx alone) |
| `m19_fxbank_ptf_verify.py [ptf]` | Checks a `.ptf` with the FX bank: one gap-free block; every byte equals the tested image; vtables point at the bank; the decoded image booted with garbage SDRAM renders bit-identical to the harness |
| `m20_cond_trigs.py` | Conditional trigs: each condition over 8 passes, another track, restart, ordinary Chance unchanged vs stock, FX max/text vs stock, project render bit-identical |
| `m11_sdram_audit.py` | Watches every SDRAM access above the firmware's top object (`0x70ef8874`–`0x71000000`) over boot, load and playback; the patch state must be the only thing used there |
| `m10_dj_control.py [--demo]` | DJ on Shift + Master + jog wheel with snap-back: input routing, audio, clicks; `--demo` plays the gesture over pattern 1 → `out/*_djcontrol.wav` |

### Master EQ + DJ filter patch (`patch/`)

- `master_fx.c` (v2): freestanding C for Cortex-M4F. `build.sh` compiles it with
  clang and links it with `link.py` (a minimal Python linker) to `master_fx.bin`
  at **0xD7000** (free flash after the image), 5,887 bytes. `master_fx_v1.*` is
  the first version, kept for A/B tests (`apply_master_fx_patch(build="v1")`).
- Hook: the `bl 0xd42f0` at **0x76c6a** (master mixer update, class 0xc2b9c)
  is redirected to `master_fx_entry`. That stub passes `this` (r8) so L/R state
  is kept apart, runs the EQ/DJ on the 32-bit accumulator, then calls the
  original saturator. The processing is therefore in live output and exports.
- EQ: 5 bell bands at 115/330/990/3k/9.9k Hz (Q 0.9) as topology-preserving
  (TPT) state-variable filters. Gains come from the firmware's Master EQ
  settings at `0x20004c60` (−1..+1 → ±12 dB). `0x704c0` is patched to `bx lr`
  so the codec's graphic EQ is not applied as well.
- DJ: one value −1..+1 at state+4 (`set_patch_params(dj=…)`). Low-pass sweeps
  20 kHz→80 Hz, high-pass 20 Hz→8 kHz (TPT SVF, Q 0.8). It crossfades to dry
  within ±0.05 of centre, so LP↔HP changes pass through the dry signal.
  Controlled from the panel by **Shift + Master + jog wheel** (below).
- Smoothing (v2): one-pole per-block glide of all parameters (~24 ms), per-sample
  linear coefficient ramps inside each block while moving, and states reset when
  a stage switches on (a bell at 0 dB is an identity).
- Speed (v2): stage-major processing on a float block; separate non-inlined
  steady and ramping loops (the steady bell loop is the 12-FLOP minimum);
  `v2 = ic2 + g·v1` simplification; no coefficient redesign when steady; VCVTR
  for round-and-saturate back to int32.
- **Compiler bug guard:** Apple clang 17's Cortex-M4 alignment-hazard pass put
  a padding `nop` inside an IT block, so a conditional move ran unconditionally
  (every EQ gain clamped to +1). `build.sh` disables that pass
  (`-mllvm --arm-postra-hazard-recognizer=false`) and refuses output that has
  any conditional `nop`.
- Measured (`m8_patch_test.py`), v1 → v2:

  | | v1 | v2 |
  |---|---|---|
  | EQ band ±1 at centre | ±12 dB | +11.98…+12.02 / −11.83…−12.01 dB |
  | DJ ±0.5 | −23.5 / −23.0 dB | −24.5 / −24.0 dB |
  | cost, 5 EQ bands (instr/block) | +35.4k | +21.5k (−39 %) |
  | cost, EQ + DJ | +40.6k | +27.0k (−33 %) |
  | cost, flat/bypass | +200 | +503 |
  | clicks/zipper on abrupt moves (>8 kHz, 100+400 Hz tones; floor −104.1) | −73.3 dBFS | −101.4 dBFS |
  | bypass vs stock | bit-identical | bit-identical |

  Instruction counts are not cycles. As a rough estimate +27k is about 5–8 % of
  the 180 MHz CPU per 2.9 ms block; it still needs measuring on hardware.
- **DJ control (Shift + Master + jog):** the board's input callbacks are stubs
  that tail-branch into the interface (`0x20005e04`); the patch redirects
  three of those `b.w`s: knob `0xf628` (→ `0x28f04`), press `0xf636` and
  release `0xf646` (→ `0x28f0c`) to `ui_knob` / `ui_button`.
  - Buttons: **Master = 12** (the Master screen registers its own
    press/release/hold handler `0x35c95` on 12; that handler sets the held flag
    the Master + jog volume path uses). **Shift = 29** (every screen registers a
    handler for 29; the knob dispatcher `0x2b9e0` reads `isPressed(29)` to
    accelerate values). Jog wheel = pot 0. Both are from static analysis and
    still need confirming on hardware.
  - Presses and releases are always forwarded, so Shift and Master work as
    before. With both held, jog turns move the DJ by 0.02 per detent (the
    Master EQ's step) and are **not** forwarded, so master volume doesn't change too.
    Releasing either button sets the DJ back to 0. The glide brings the cutoff
    above 10 kHz in about 40 ms and fully dry after ~200 ms (from −0.8). Other
    pots and every other combination pass through unchanged.
  - Held state and a "moved by UI" flag live at state +12/+16.
  - **Indicator:** each DJ change shows the firmware's own pop-up (the one
    used for "No undo data." / "Counting in"): `0x2a71c(0x20005ec4, 0, text,
    seconds)`. It keeps the text *pointer*, so the text lives in patch state
    (+504). Showing again restarts the timer, and the interface update
    (`0x299cc`) hides it after the seconds run out (1 s here). Text: "DJ Low-pass 40%",
    "DJ High-pass 25%", "DJ filter off" (on release). The firmware already
    shows "-" and "%" on screen ("Low-pass", "100%"). The harness can't render
    the screen, so the pop-up call and its text are checked, not its look.
  - The harness has no UI, so `record_ui_passthrough()` replaces `0x28f04`/
    `0x28f0c` with recorders; `press()`, `release()`, `turn()` drive the
    board callbacks `0xf630`/`0xf640`/`0xf620`.
- **Boot-time reset:** SDRAM isn't cleared at power-on, so the patch takes
  over app setup's only call to engine init (`bl 0x1611c` at `0xf3fa`) with
  `patch_boot`. It zeroes the whole state, sets magic and `enabled = 1`, then
  runs the engine init. Order on the device: `main` (`0x3ee8`) → board init
  `0x721c` → `0x7540`. That brings SDRAM up (`0x75a6` → `0x68e0` → `0xea4`,
  SDRAMC `0x4000F000`, base `0x70000000`) and registers the input callbacks. Then
  app `setup()` (`0xf25c`, vtable `0x9fcfc`) → `0xf3fa`: SDRAM is ready, and
  audio and input polling haven't started. The harness's `init_engine()` goes
  through the hook when it's installed. Test: state filled with `0xff` → after
  boot it's clean and audio is bit-identical to stock. Skipping the reset gives silence
  (NaN DJ value), which is what the device would have done.
  - Measured (`m10_dj_control.py`, `--quick` skips audio): 12/12 routing
    cases, 10/10 indicator steps, 3/3 boot checks; 5 kHz −53.3 dB with
    jog −40, back to −0.0 dB after release; clicks above 8 kHz with two
    gestures −102.2 dBFS (floor −104.1).
- **State memory (`0x70FE0000`, 1 KB) is unused by the firmware:**
  - Static map, from every SDRAM literal in the code:

    | Range | Use |
    |---|---|
    | `0x70000000`–`0x700e2000` | buffer (`0x2c9a2`, length `0xe2000`) |
    | up to `0x7026a000` | other statics |
    | `0x7026a000`–`0x70daa000` | sample pool, fixed size `0xb40000` (`0x563fc`) |
    | `0x70daa000`–`0x70ef8874` | statics; the top object, `0x70ef8574`, is 4 × `0xc0` (filled by `0x84100`) |
    | `0x70ef8874`–`0x71000000` | no reference found (~1.03 MB) |

    The largest base+offset reach found is about `0x70ef89bf`. No `movw/movt`
    builds an SDRAM address. The `0x8xxxxxxx` literals are stored values, not
    alias addresses. There is no SDRAM heap.
  - Runtime (`m11_sdram_audit.py`): over ctors, engine init through
    `patch_boot`, project load, 16 instruments, pattern load and 8.21 s of
    playback with DJ gestures, the firmware made **0** reads or writes in
    `0x70ef8874`–`0x71000000`. Every patch access was inside its state page.
  - Not covered at runtime: UI-only features the harness doesn't run
    (recording, sample editor, file browser, MIDI/USB, firmware update). Those
    either use the ranges above or end in a reboot, and `patch_boot` clears the
    state on every boot. Register-indexed accesses are bounded only by each
    object's allocation, so the static map relies on literal bases.
- **Firmware file:** `python3 -m patch.make_ptf <stock .ptf> out/PolyendTracker_1.9.2_masterfx.ptf`
  - **Writing the file.** The builder keeps every stock record unchanged (they
    follow the original linker sections, so they aren't all 16 bytes). It
    rewrites only the 6 records that cover hook sites and appends the patch as
    16-byte records before the end record. It refuses to build unless
    re-encoding the unmodified image reproduces the stock file byte for byte.
    It checks every hook site's stock bytes, confirms the patch doesn't overlap
    the image and fits in the 1 MB program flash, and confirms the flash config
    field (`0x400`–`0x40f`, FSEC `0xDE`) is unchanged. It reads the output back
    to check it.
  - **What changes.** 16 bytes of the stock image change, at `0x704c0`,
    `0x76c6a`, `0xf3fa`, `0xf628`, `0xf636` and `0xf646`. The patch adds
    5,887 bytes at `0xd7000`.
  - **Verified (`m12_ptf_verify.py`).** All 883,551 bytes the `.ptf` writes
    equal the harness-tested image. The harness's own trampoline at `0xff0` is
    excluded; it overwrites 2 stock bytes inside DMA setup `0xfc8`, and the
    `.ptf` carries the stock bytes there. Booting the decoded image as-is
    (state filled with junk, engine init through its own `patch_boot`) gives a
    render bit-identical to the harness-patched one.
  - **How updates are applied (static analysis).** The app has no main-flash
    programming code; its flash routines are EEPROM emulation only (FlexRAM
    `0x14000000`). On "flash new firmware" it writes the chosen file name to
    `/Firmware/_fwinfo`, sends a flag through `0x1fff756c`, pulses GPIO 81 and
    waits (`0x5084`–`0x512a`). An external chip, probably a Teensy-style
    bootloader MCU, does the programming. The app never checksums its own
    image.
  - **Hardware result: the data must have no gap.** The first patched file
    (33-character name, patch at `0xd7000` after a gap from the image end
    `0xd6460`) hung at boot. A short-named marker-only file with the same gap
    (t1) also hung. Probes (`patch/make_probes.py`, which change only the
    version-text format pointers shown in Config → Firmware):
    - p0, an in-image change only: booted, showed "1.9.2 beta".
    - p1, 32 bytes directly after the image: booted, showed "1.9.2 P1".
    - p2, data at `0xd7000` with `0xff` filler from `0xd6460`: booted, showed "1.9.2 P2".

    So the updater handles extra data only when it continues without a gap.
    The stock file is a single block from `0x0`. `make_ptf.py` now always
    fills the gap, and the builder's `--hooks`/`--marker` options make bisection
    builds (`out/sdtest/PolyendTracker_fx*.ptf`). The updater also shows only
    32 characters of the file name, so names are limited to 31.
  - **Running on hardware (2026-10-13):** `PolyendTracker_fx.ptf` (full patch,
    no gap, short name) boots and shows "1.9.2 FX". The DJ filter works from
    Shift + Master + jog, which confirms buttons 12 and 29. The software
    Master EQ audibly affects the sound. Also confirmed: the pop-up is
    readable and clears after about 1 s; the filter snaps back when Shift or
    Master is released; Master + jog (volume) and Shift + jog (selection edit)
    still work; a cold restart is clean (boot reset). CPU load seems fine so
    far, not yet stress-tested. Not yet tested: whether WAV exports include
    the EQ and DJ.
  - **Recovery (Polyend's official emergency flash; help-centre article
    929798).** The emergency updater lives on a separate chip that firmware
    updates don't overwrite, which agrees with the GPIO-81 handoff above and with
    RETracker's README (patois/RETracker, a community project that also
    patches Tracker firmware).
    - Method 1: press the hidden reset button. It flashes a **randomly chosen**
      `.ptf` from `Firmware/` on the SD card.
    - Method 2: with the SD card removed, connect USB directly (no hubs),
      power on, select the `.ptf` in Polyend Tool, and press the reset button
      next to USB-C with a pin. If it stalls at 100 %, power-cycle it.
    - Community reports: holding Reset while pressing Power also starts it.
      Updates can appear to hang near 100 %. A FAT32 (MBR) SD card fixed some
      failed updates.
    - Because Method 1 picks at random, keep **only the stock `.ptf`** in
      `Firmware/` when recovering.
    - File names must start with `PolyendTracker_` and end with `.ptf`.
  - **Personal use only.** This contains Polyend's firmware: don't redistribute it.
- **Open on hardware:** WAV export check; CPU stress test (busy project, all
  EQ bands off flat, DJ filter on).

### Braids instrument (`braids/`)

- Build: `braids/build.sh <absolute dir with eurorack/ and stmlib/>` (the path
  must not contain spaces; link it from a space-free path if needed) compiles an unmodified
  pichenettes/eurorack + stmlib checkout (MIT, Emilie Gillet) as one unit
  (`braids_unity.cc`; shim headers in `braids/shim/`). It links at `0xD9000`
  (110 KB). Writable data goes to SDRAM `0x70FE1000`, with its starting
  values stored in flash; `patch/link.py` now supports this. The build is
  two-pass, so `braids_boot` knows where the data lands.
- **Player hooks** (one sample player per track, vtable `0xa9fb0`, so each
  track is monophonic):
  - `0xd0758` (`ldrb r3,[r0,#0x21]` in render `0xd074c`) → `braids_render_stub`:
    for a Braids track it fills a block (allocate `0xce42c`, data at +4) and
    rejoins at `0xd0776`, so the envelope, bit reducer, filter, sends,
    volume and pan still apply.
  - `0xd0a50` (after the mode-specific note-on in `0xd0a28`) →
    `braids_note_stub`: picks the model and retriggers.
  - Boot: `0xf3fa` → `braids_boot` (copies `.data`, zeroes `.bss`) →
    `patch_boot` → engine init.
- **Use:** name an instrument `BR-<model>` or `BR:<model>`. The model is a
  name or a number 0–47; case, spaces, `_` and `-` are ignored, and an
  unknown name gives CSAW. The Tracker's on-screen keyboard has no `:` (keys:
  digits, letters, `- + @` space and `!"#$%&'()`), so type `BR-` on the device. Set its
  playback mode to Granular. Controls:
  - **Pitch** from player +0x74 (0.9996 × 2^((n−60)/12) at a 44,117.647 Hz output
    rate), so tune, finetune, glide and pitch FX all apply.
  - **Timbre** = Granular Position (player +0x9e, with its envelope, LFO and
    step FX). In other modes it uses the instrument's static position, +0x16c.
  - **Color** = grain length (+0x16a, 44–44,100 samples, log scale).
  - Braids output is −6 dB.
- **Voices:** up to 4 tracks at once. A track without a free slot plays its
  sample, and the slot frees when the envelope ends. The preview player is
  always allowed. Voice memory is 17 KB each at `0x70F00000` + slot × `0x4400`.
- **Measured** (`m13`, `m14`):
  - Tuning through the full chain is within ±0.1 cents (65 Hz–2 kHz).
  - Cost per model is 2.8k–31k instructions per block (median 13k).
  - One playing voice adds +21.4k (CSAW) or +16.9k (FM) per block, against
    +16.4k for a stock Granular sample voice.
  - Pattern 1 with 2 Braids tracks: mean 199k instructions per block,
    against 180k stock.
  - With the patch installed but no `BR:` instruments, output is
    bit-identical to stock.
- **Firmware file:** `make_ptf.py ... --hooks boot,audio,ui,codec,braids [--marker BR]`.
  It refuses a `braids.bin` built against a different `master_fx.bin`
  (`__next_boot` in its syms). The output is one gap-free block, `0x0`–`0xf3fb4`.
  `m15_braids_ptf_verify.py` passes.
- **Hardware (2026-10-13):** `PolyendTracker_br.ptf` boots ("1.9.2 BR"),
  Braids instruments play, and 3 Braids tracks show no audible crackling.
- **Sample rate.** Rendering directly at 44.1 kHz with the pitch offset puts
  every pitch-indexed table on the right frequency, including oscillator
  increments and filter cutoffs. Only per-sample time constants run long.
  `m16_braids_rate.py` found four affected models: struck bell and drum
  (2.15×), snare (1.4×) and particle noise.
  - Those four run at Braids' native 96 kHz, unmodified, and are resampled
    to 44,117.647 Hz: `braids_voice_render_96k`, 272/125 polyphase, 125 phases
    × 32 taps, Kaiser β 7, cutoff 20 kHz, using SMLAD
    (`braids/gen_resampler.py` → `resample_taps.h`).
  - Results: bell and snare decays now match the module exactly; drum is
    within about 10–15% (it has random noise excitation); tuning through the
    resampler is exact.
  - Cost per block: about 42–45k instructions for bell, drum and snare
    (was about 15k); 26k for particle noise.
  - Braids' percussion models render in pairs and overrun on odd sizes, as
    the module always renders 24. The 96 kHz path only asks for even counts
    and carries a spare sample to the next block.
  - Unaligned word loads in the filter are safe: the firmware never sets
    `CCR.UNALIGN_TRP`. Its only SCB write, at `0x68ac`, enables the DWT cycle
    counter, which is useful for measuring real cycles on hardware later.
- **Templates** (`braids/make_templates.py` → `out/braids_templates/instruments/`):
  48 instruments, `1 BR-CSAW.pti` … `48 BR-QUESTION MARK.pti`, built as follows:
  - **Settings:** the firmware's own defaults (`0x225dc` run in the
    emulator), plus Granular mode, Timbre and Color at mid.
  - **Sample:** a 1 s recording of each model at C4, so the instrument still
    plays something representative on stock firmware.
  - **File format:**
    - CRC-32 (zlib) of bytes 0–`0x183` at `0x184`; verified against all 16
      real project files.
    - File `N` is slot N−1 (`0x716f6`).
    - The 16-byte header is copied from a real file. Its last two bytes vary
      between real files (`00 00`, `06 00`, `ff ff`) and their meaning is unknown.
  - **Delivery:** the Tracker has no cross-project instrument import, so the
    templates ship as a project folder, `Projects/<name>/instruments`, plus a
    blank `project.mt`.
- **Hardware (2026-10-13):** `PolyendTracker_br2.ptf` boots ("1.9.2 BR2"),
  and the "Braids Templates" project loads on the device. It was built from
  `New Project`'s `project.mt` and its factory demo pattern, plus the 48
  templates.
- **Not done yet:** listening checks for the 96 kHz models on hardware; a
  4-track stress test with bell, drum or snare; a real-cycle cost measurement.

### Swappable master reverb/delay (`fxbank/`) — experimental

> **⚠️ Experimental.** The first build froze the UI and crackled once a reverb
> or delay send was on. The current build (`bank5`) passed a first hardware
> test: plate + tape with sends on every instrument, no audible crackle and
> no clear UI slowdown. Plate + tape costs ~128k cycles a block against
> stock's ~116k (of ~522k), so headroom is a little lower than stock. Only
> one project has been tried. It still boots on the stock effects, shows its
> CPU cost on Shift + Master, and cuts an effect back to stock if it runs too
> long. Use with care.


- **Where the effects live:** each master effect is one AudioStream node,
  reached only through slot 0 (update) of its vtable in flash:
  - reverb: `engine+0x1c18` = `0x1fffae9c`, vtable `0xa9efc`, update `0xcfdc4`.
    Stereo in (`receiveWritable` `0xce590`, inputs 0/1), processed in place by
    the core `0x5d1a8` (on node+`0x1c`), stereo out. Two 32 KB int16 lines
    (`0x70262000`, `0x70ec6cfc`); reset `0xcfe24`. Bypass flag +`0x80`.
    ~34k instructions per block, the costliest node in the graph.
  - delay: `engine+0x1c1c` = `0x1fffae48`, vtable `0xa9f54`, update `0xd037c`
    → process `0xd0224`. Mono in (`receiveReadOnly` `0xce578`), stereo out.
    Three 2^18-sample int16 lines (+`0x14`/+`0x18`/+`0x1c` →
    `0x700e2000`/`0x70162000`/`0x701e2000`), write index +`0x20`. Mono uses
    line +`0x1c` on both outputs; ping-pong feeds the input into line +`0x18`
    (output 1) and line +`0x14` (output 0) hears its echo. ~8k per block.
  - AudioStream layout here: +4 active/num_inputs, +8 destination list,
    +`0xc` input queue, +`0x10` next in the update list.
- **Stock parameters** (engine setters `0x1587c`… → node fields):
  - reverb (Master Reverb page: Size, Damping, Predelay, Diffusion):
    +`0x54` Size, +`0x5c` 1 − Damping, +`0x6c` Predelay × 5000 samples,
    +`0x58` Diffusion.
  - delay (Master Delay page: PingPong, Sync, Rate, Time, Feedback, Filter):
    time `0xcfee8` (ms, max 3500, × 44.1 → +`0x28` samples; Sync `0xcff50`
    computes it from the tempo), feedback `0xcfe88` (0..0.99 × 255 → +`0x2c`),
    +`0x2e` ping-pong, +`0x2d` off, filter `0xcffe0` (Hz → one-pole Q10
    coefficients +`0x30`/+`0x34`/+`0x38`, applied to every line read).
- **The bank** (`fxbank/fx_bank.c`, 11 KB at `0xF8000`, no .data/.bss):
  `make_ptf --hooks …,fx` points the two vtable words at `reverb_update` /
  `delay_update`. Each runs its algorithm or tail-calls the stock update.
  Both read the stock fields above, so the stock pages drive them.
  - **Plate:** Dattorro (JAES 1997), delays scaled 29761 → 44117.6 Hz and
    rounded to even lengths. int16 lines in free SDRAM (`0x70F21000`, 106 KB),
    read and written as aligned 32-bit pairs: on hardware SDRAM accesses
    dominate the cost (the first float version with 7 taps per side cost over
    150k cycles a block). Every delay and output tap is longer than a block,
    so it runs stage by stage over whole blocks. Size → decay 0.30–0.97,
    Damping → tank low-pass, Diffusion → input diffusers, Predelay as stock.
    Mono-summed input, 4 taps per side, level matched to stock. The short
    lines (4 input all-passes, 2 modulated all-passes: 5120 samples, 10 KB)
    live in internal SRAM: patch_boot takes them from the firmware's own heap
    with its `malloc` (`0x9529c`; newlib, sbrk `0xc9a0` allows growth to 8 KB
    below the stack), so nothing else can be given that memory. If the
    allocation fails they stay in SDRAM; the output is bit-identical either way.
    Since bank5 the tank all-passes are in SRAM too (26 KB in total); the plate
    costs ~106k cycles a block on hardware (stock reverb ~92k). Credit: the idea of putting the
    reverb's short all-pass lines in SRAM, and the note that the Tracker's
    SDRAM bus is 16-bit, come from OpenTracker, the clean-room firmware
    reimplementation at [sandlbn/polyend-tracker-re](https://github.com/sandlbn/polyend-tracker-re)
    (MIT; `src/core/audio/effects.h`, `src/hal/board.h`). No code was copied. Skips all
    work after 16 blocks of silent input and a tail within ±8; zeroes its
    lines over 7 silent blocks after boot or a switch (SDRAM holds garbage at power-on).
  - **Tape:** one read head on the stock delay's own lines and write index, so
    switching to/from stock keeps the echoes. Delay time glides (slew-limited
    to ±25 % pitch, ~90 ms time constant), wow 0.55 Hz + drift 0.13 Hz +
    flutter 6.3 Hz, head-loss low-pass ~8.5 kHz, the stock loop filter, a DC
    blocker, tanh saturation when recording. Feedback × 1.03, so the top of
    the Feedback range self-oscillates (bounded by the saturation).
  - **Selection:** hold Shift + Master and press any other button: Plate +
    Tape → Stock rev + Tape → Plate + Stock dly → all stock. Boots on all
    stock (diagnostic build). The pop-up shows the choice. That button's
    press and release are swallowed; Shift and Master are forwarded as
    before. Stored at patch state +20; patch_boot zeroes the bank's state
    header (`0x70F20000`). Switching back to the stock reverb runs its reset,
    so no stale tail.
  - **Diagnostics:** each update is timed with the DWT cycle counter.
    Pressing Shift + Master shows `L45/60% R92 P140 D25 T27k`: L = the whole
    audio block (average / peak over ~1 s, % of the 522,240-cycle budget), then
    reverb now, plate's largest block since selected, delay now, tape's
    largest block (thousand cycles). L times the firmware's per-block audio
    service `0xf57c` (sequencer clock, voice update, then update_all
    `0xce5d8`), which is reached only through the application callback table
    at `0x9fcfc` (entry `0x9fd04`); make_ptf points that entry at
    `block_service_timed` (idea from SIDTracker's render meter).
  - **Flash probe:** at boot patch_boot counts the non-erased (not 0xFF)
    bytes in 0xFC000–0xFFFFF, which no build writes (make_ptf refuses a patch
    that reaches it). The first button press after boot shows
    `Flash top 16K: blank` or `... N set`. SIDTracker/OpenTracker keep out of
    all flash above the stock image (0xd6460) in case bootloader or identity
    data lives there; this checks the most likely place. In the emulator,
    stock never reads above 0xd6460 or FlexNVM during boot, load or playback.
    **Hardware (2026-10-21):** `Flash top 16K: blank`, so nothing lives in the
    top 16 KB of program flash. The total load readout (`L`) works on the device. A guard puts an effect back on stock while it
    stays over 150k (plate) or 80k (tape) cycles for 4 blocks (`!` in the
    readout); the next selector press clears it.
- **Results (m18):** see FINDINGS.md §8.

### Conditional trigs (`patch/master_fx.c`, `--hooks ...,ct`) — experimental

Elektron-style trig conditions on the stock **Chance** FX (`C`, internal FX
type 4). Chance 0–100 % works as before; scrolling past 100 gives:

| Chance value | Shown | Plays on |
|---|---|---|
| 101 | `1:2` | passes 1, 3, 5, … |
| 102 | `2:2` | passes 2, 4, 6, … |
| 103 | `1:4` | passes 1, 5, 9, … |
| 104 | `2:4` | passes 2, 6, 10, … |
| 105 | `3:4` | passes 3, 7, 11, … |
| 106 | `4:4` | passes 4, 8, 12, … |

A pass is one time through that track's pattern; counting starts when
playback starts. Idea and condition set from Tracker+ Conditional Trigs CT6
([Psygoo/TrackerPlus-CT-Web](https://github.com/Psygoo/TrackerPlus-CT-Web),
MIT), which is Tracker+ 1.2.0 code; nothing of it is used here, the 1.9.2
hooks are this project's own.
- **Chance roll** `0xcadda`: the stock code calls random `0xcba18(0, max)`
  and skips the note if the result exceeds the value. The hook returns 0
  (play) or 0x7fffffff (skip) for 101–106 and calls the stock random with
  max 100 otherwise, so ordinary Chance is bit-identical to stock. The FX
  slot is in r5 (value at +0x6c) and the track in sb.
- **FX max** `0xcb750` (switch over FX types, default 100): Chance → 106.
  Other users of the maximum (e.g. Random FX value) can now produce 101–106.
- **Value text** `0x146a4(this, buf, size, type, value, mode)`: Chance
  101–106 → `1:2` … `4:4`; everything else stock.
- **Pass counter** `0x11ed8`: where a track's step index (sequencer
  `0x1fff953c` + t×0x68 + 0x80) wraps to the loop start; per-track counts in
  the patch state. **Reset** at playback start `0xc9230` (six callers:
  pattern, song and other starts).
- **Hardware (2026-10-21, `PolyendTracker_ct1.ptf`, "1.9.2 CT1"):** Chance
  scrolls past 100 and the pattern grid shows the condition labels; conditions
  alternate passes as expected; in song mode the pass count carries on across
  patterns (a pattern change counts as a pass, it does not restart the count).
  Not yet checked: the Performance screen.

### Playback

Mirrors the device's per-block service routine `0xf57c`:
`clock(dt)` = sequencer clock `0xcb640(seq, s0=dt)` (tempo accumulator; tick
period from `0xc9054`, 12 ticks/sub-tick, 48 sub-ticks/step), then
engine voice update `0xcbad0(engine)` (applies pending voice changes; without
it every note-on leaves the track amp at gain 0). Then `render_block()`.
- Play pattern (looping) = `0x113d4` (seq+0x1c = 0 → `0xc9230`), the same path
  pattern export uses. `0x111d8` is selection/preview play (uses UI selection
  fields; plays one step otherwise). Song mode = `0x11a9c`.
- `micros()` comes from a time-source object at `*(0x2000bf4c)`, provided by
  `install_time_source()` and driven by emulated time.
- Sample project, pattern 1: 64 steps × 128.2 ms = 117 BPM at 4 steps/beat
  (from firmware timing; so the 102.1 value in project.mt is probably not the tempo,
  unverified). Output is near-mono with a DC offset of −0.039, not yet explained.

### Instruments and patterns

- `.pti` = 0x10 header | 0x174-byte instrument struct | CRC | 16-bit PCM from
  0x188 (writer `0x224b4` confirms the layout). Instrument *i* lives at
  `0x200001cc + i*0x174`; `+0x28` sample address, `+0x2c` frames.
  `load_instruments()` mirrors loader `0x22008` (not executed): samples are
  packed from `0x7026a000`, and the start is stored in the struct and in tables
  `0x20004f94[i*8]` and `0x200001d0[(i+0x9b8)*8]`.
- Sequencer object `0x1fff953c`; init `0x10f98` sets pattern buffers
  `0x70daa000` (current, ptr at +0x14) and `0x70dac42c` (next, +0x18).
- `.mtp`: track data from 0x1c, 8 tracks × 0x301 (length byte + 128 steps × 6 B).
  Loaded by the firmware's `0x22de4` through a fake file system
  (`make_file_system()`: vtable +0x08 exists, +0x20 size, +0x2c open,
  +0x30 read, +0x40 close), built with `make_object()` (Python-backed vtables).
- Step bytes: byte 0 is the note (`0xff` empty, `0xfc` note-off, `0x3c` = C4).
  Bytes 1–5 look like instrument + two effect slots (unverified).

### Project loading

`load_project()` replaces only the SD read `0x24ad0` with a host file read,
using the new `override()` mechanism (patches the entry to `bx lr` and runs a
Python handler). Defaults, version migration (`0x2606c`, v15 → v17 for the
sample project) and the parser (`0x22fa0`) all run unmodified. The live project
struct is at `0x200001cc`; its 0x700-byte settings block is at `0x2000488e`
(file offset 0x110; shifted +2 bytes after migration). Master EQ gains are at
`0x20004c60`. One helper call on the file manager (`0x1fffebfc+0x1104`, slot 0
at `0x22fc0`) is stubbed.

Sample project "burly ladybug 1-23-24": version 15, likely tempo 102.1,
EQ flat, master saturation mode 2 (−12 dBFS tone → +3.34 dB, matching the curve).

`projects/` holds copies of the user's own projects and is not distributed.

### Stub HAL / codec

A universal stub object lives in the unused FlexBus window at `0x6000_2000`.
Its 64 vtable slots each point to a `bx lr` stub, so every method returns
`this`. The HAL's get-codec call (slot 0x18) therefore returns the stub too.
Every call is logged as (vtable offset, call site, r1, s0), and
`TrackerEmu.codec_log()` decodes the SGTL5000 control calls. With no project
loaded, init sends: `volume(0)`, `eqSelect(3)`, `eqBands(0,0,0,0,0)`, surround
(slot 0x34) off, `enhanceBassEnable(1)`, `enhanceBass(0.833, …)`. The tail of
the enhanceBass argument struct is not verified yet.

Saturation-mode check (−12 dBFS sine in, mode byte at master mixer `+0x14`):
modes 0–4 give +0.0 / −0.1 / +3.3 / +6.0 / +9.5 dB. These match the curves
decompiled from `0xd42f0`, so the patch hook site `0x76c6a` is on the emulated path.

## How it works (key addresses)

- Startup: `.data` 0xd4f2c → 0x1fff5b58..0x1fff708c, `.bss` to 0x200103f4,
  then `__libc_init_array` (0x95110). Clock/PLL setup is skipped.
- Audio pool: `initialize_memory(0x1fff0bd0, 70)` at 0x5d0b4.
- Block API: allocate 0xce42c, release 0xce4d8, transmit 0xce530.
- Scheduler: Python walks the update list at `*(0x2000bc40)` (node `+4` = active,
  `+0x10` = next) and calls each vtable slot 0, mirroring firmware `0xcbbdc`.
- Output: node O = engine+0x19d8+0xf8; `0xce724(O, left, right)` sets capture buffers.
- Engine object 0x1fff9bd8; mixer section +0xee0 (dry mixers +0x120/+0x198,
  master mixers +0x210/+0x258); output section +0x19d8.

## Limitations / next steps

1. **Global config not loaded.** The project now loads, but device-wide settings
   (for example master volume at `0x20005c7b`) are still zero. They are codec-side
   or global, so harness output is unaffected.
2. **Not yet compared with a device export.** The rendered pattern should be
   checked against the Tracker's own WAV export of the same pattern. Song mode
   (pattern chaining, `0x11a9c`) is not wired up yet.
3. **Instruction counts are not cycles.** Cortex-M4 cycles per instruction plus
   SDRAM wait states mean real cost is higher. Treat the profile as relative.
4. Speed: ~1 s of audio per ~10 s of emulation (Python per-node dispatch).
