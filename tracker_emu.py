"""Polyend Tracker (fw 1.9.2, Kinetis K66 / Cortex-M4F) audio-engine harness core.

Loads the flattened firmware image into Unicorn and reproduces just enough of
the Teensy 3.6 reset sequence to run static constructors and the audio graph.
Addresses below come from static analysis of tracker_1.9.2.bin (see NOTES.md).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

from unicorn import (
    UC_ARCH_ARM, UC_HOOK_CODE, UC_HOOK_MEM_READ, UC_HOOK_MEM_UNMAPPED,
    UC_HOOK_MEM_WRITE, UC_MODE_MCLASS, UC_MODE_THUMB, UC_PROT_ALL, Uc, UcError,
)
from unicorn.arm_const import (
    UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2,
    UC_ARM_REG_R3, UC_ARM_REG_S0, UC_ARM_REG_SP, UC_CPU_ARM_CORTEX_M4,
)

FIRMWARE = Path(__file__).parent / "firmware" / "tracker_1.9.2.bin"

# --- memory map (K66 + 16 MB SDRAM via SDRAMC) ---
FLASH = (0x0000_0000, 0x0010_0000)
SRAM = (0x1FFF_0000, 0x0004_0000)          # SRAM_L 64 KB + SRAM_U 192 KB
SDRAM = (0x7000_0000, 0x0100_0000)
PERIPH = (0x4000_0000, 0x0010_0000)
BITBAND_PERIPH = (0x4200_0000, 0x0200_0000)
PPB = (0xE000_0000, 0x0010_0000)

# --- startup facts from the reset handler at 0x1d0 ---
DATA_LOAD, DATA_START, DATA_END = 0x000D_4F2C, 0x1FFF_5B58, 0x1FFF_708C
BSS_START, BSS_END = 0x1FFF_7090, 0x2001_03F4
RAM_VECTORS, VECTOR_LEN = 0x1FFF_0A00, 0x1D0
STACK_TOP = 0x2003_0000
LIBC_INIT_ARRAY = 0x0009_5110

# --- audio engine facts ---
UPDATE_LIST_HEAD = 0x2000_BC40   # AudioStream::first_update equivalent
ENGINE = 0x1FFF_9BD8             # top-level engine object (ctor 0x170d0)
AUDIO_MEMORY_INIT = 0x0005_D0B4  # initialize_memory(pool, count); engine init uses:
AUDIO_POOL, AUDIO_POOL_BLOCKS = 0x1FFF_0BD0, 70
MEMORY_USED = 0x2000_BC3C        # u16 blocks currently allocated
BLOCK_SAMPLES = 128
OUTPUT_NODE = ENGINE + 0x19D8 + 0xF8   # output section +0xf8 (node O, vtable 0xa9e00)
OUTPUT_SET_BUFFERS = 0x000C_E724       # O.setBuffers(left, right)
# AudioStream block API (Teensy-style)
ALLOCATE, RELEASE, TRANSMIT = 0x000C_E42C, 0x000C_E4D8, 0x000C_E530
# Mixer section (engine + 0xee0) dry 8-in mixers, one per channel
MIXER_SECTION = ENGINE + 0xEE0
DRY_MIXER = (MIXER_SECTION + 0x120, MIXER_SECTION + 0x198)

RETURN_MAGIC = 0x0000_0FF0 | 1   # call trampoline (bkpt); overwrites 2 stock bytes inside a DMA-setup routine (0xfc8) the harness never executes

# Universal stub object (HAL / codec / devices) in the unused FlexBus window.
STUB_REGION = (0x6000_0000, 0x0001_0000)
STUB_VTABLE = STUB_REGION[0]                 # 64 slots
STUB_CODE = STUB_REGION[0] + 0x1000          # slot i -> 'bx lr' at STUB_CODE + 4*i
STUB_OBJECT = STUB_REGION[0] + 0x2000        # word 0 = STUB_VTABLE, rest zero
STUB_SLOTS = 64
ENGINE_INIT = 0x0001_611C                    # engine.init(hal), called from 0xf3fa

# Project loading (fw 1.9.2)
PROJECT = 0x2000_01CC              # live mtProject struct
PROJECT_LOAD = 0x0007_3404         # load Workspace/project.mt into PROJECT
SD_READ_FILE = 0x0002_4AD0         # readFile(fs, path, buf, maxlen, ?) -> 2 on success
FILE_MANAGER = 0x1FFF_EBFC         # fileManager object; +0x1104 = helper object
PROJECT_SETTINGS = PROJECT + 0x46C2  # 0x700-byte settings block (file offset 0x110)
MASTER_EQ_GAINS = 0x2000_4C60      # 5 floats, -1..+1 (inside PROJECT_SETTINGS)

# Instruments: PROJECT starts with 48 x 0x174-byte instrument structs.
INSTRUMENT_SIZE, MAX_INSTRUMENTS = 0x174, 48
PTI_STRUCT_OFFSET, PTI_SAMPLE_OFFSET = 0x10, 0x188   # .pti = hdr | struct | crc | PCM
INSTR_SAMPLE_ADDR, INSTR_SAMPLE_LEN = 0x28, 0x2C     # fields inside the struct
SAMPLE_BASE = 0x7026_A000          # first sample address (0x22008)
SAMPLE_PTR_TABLE_A = 0x2000_4F94   # [i*8]  sample start   (0x22008)
SAMPLE_PTR_TABLE_B = 0x2000_01D0   # [(i+0x9b8)*8] sample start (0x22008)

# Sequencer / patterns
SEQUENCER = 0x1FFF_953C            # sequencer object
SEQUENCER_INIT = 0x0001_0F98       # sets pattern buffers 0x70daa000 / 0x70dac42c
SEQUENCER_PLAY_PATTERN = 0x0001_13D4   # playPattern(seq): +0x1c=0 (loop) -> play 0xc9230
SEQUENCER_PLAY_SELECTION = 0x0001_11D8 # preview/selection play (uses UI selection fields)
SEQUENCER_CLOCK = 0x000C_B640      # clock(seq, s0=dt seconds): tempo accumulator
SEQUENCER_PLAYING = SEQUENCER + 0x2A
ENGINE_VOICE_UPDATE = 0x000C_BAD0  # engine.update(): applies pending voice changes (0xcd70c x8)
TIME_SOURCE_PTR = 0x2000_BF4C      # *ptr = object, vtable[0]() -> micros (set by app setup)
HAL_DEVICE_PTRS = (0x2000_BF1C, 0x2000_BF28, 0x2000_BF40)   # other app-setup pointers
PATTERN_LOAD = 0x0002_2DE4         # loadPattern(fileManager, path, dest) -> ok
PATTERN_BUFFER_PTR = SEQUENCER + 0x14   # *ptr = current pattern buffer (0x242c bytes)
PATTERN_TRACK_STRIDE, PATTERN_TRACK_OFFSET = 0x301, 0x1C   # 8 tracks: len + 128 x 6 B

# Master EQ + DJ filter patch (patch/master_fx.c, linked by patch/link.py)
PATCH_DIR = Path(__file__).parent / "patch"
PATCH_BASE = 0x000D_7000           # free flash after the image (ends 0xd6460)
PATCH_HOOK_SITE = 0x0007_6C6A      # 'bl 0xd42f0' in master mixer update (class 0xc2b9c)
CODEC_EQ_SETTER = 0x0007_04C0      # eqSelect(3)+eqBands(); 'bx lr' disables codec EQ
PATCH_STATE = 0x70FE_0000          # patch_state_t (see master_fx.c)
PATCH_MAGIC = 0x5846_4D50
PATCH_STATE_SIZE = 0x400

# Input path (board callbacks -> interface 0x20005e04 -> function manager)
UI_INTERFACE = 0x2000_5E04         # mtInterface; word 0 = function manager
UI_KNOB_CB = 0x0000_F620           # knob(pot, value): tail-branch at 0xf628 -> 0x28f04
UI_PRESS_CB = 0x0000_F630          # press(btn):       tail-branch at 0xf636 -> 0x28f0c
UI_RELEASE_CB = 0x0000_F640        # release(btn):     tail-branch at 0xf646 -> 0x28f0c
UI_KNOB = 0x0002_8F04              # interface knob handler (iface, pot, value)
UI_BUTTON = 0x0002_8F0C            # interface button handler (iface, btn, state 0/1/2)
UI_HOOK_SITES = {0xF628: (UI_KNOB, "ui_knob"), 0xF636: (UI_BUTTON, "ui_button"),
                 0xF646: (UI_BUTTON, "ui_button")}
BOOT_HOOK_SITE = 0x0000_F3FA       # app setup's only call: 'bl 0x1611c' (engine init)
# Braids voice (braids/build/braids.bin, see braids/build.sh)
BRAIDS_BIN = Path(__file__).parent / "braids" / "build" / "braids.bin"
BRAIDS_HOOKS = {0xD0758: ("90f82130", "braids_render_stub"),   # ldrb.w r3,[r0,#0x21]
                0xD0A50: ("94f82c30", "braids_note_stub")}     # ldrb.w r3,[r4,#0x2c]
# FX bank (fxbank/fx_bank.bin): vtable slot 0 (update) of the master reverb/delay nodes
FXBANK_BIN = Path(__file__).parent / "fxbank" / "fx_bank.bin"
FXBANK_BASE = 0x000F_8000
FXBANK_VTABLES = {0xA9EFC: (0xCFDC5, "reverb_update"),   # reverb node 0x1fffae9c
                  0xA9F54: (0xD037D, "delay_update"),     # delay node 0x1fffae48
                  0x9FD04: (0x0F57D, "block_service_timed")}   # app table: audio block service
FXBANK_STATE = 0x70F2_0000
REVERB_NODE, DELAY_NODE = 0x1FFF_AE9C, 0x1FFF_AE48
UI_POPUP = 0x0002_A71C             # popup(popups 0x20005ec4, unused, text, seconds)
BTN_MASTER = 12                    # Master screen registers its press/release handler here
BTN_SHIFT = 29                     # handled by every screen; knob acceleration reads it
JOG_POT = 0

# Python-backed objects (vtables in the stub region)
PYOBJ_BASE = STUB_REGION[0] + 0x4000


@dataclass
class Stats:
    insns: int = 0
    mmio_reads: dict = field(default_factory=dict)
    mmio_writes: dict = field(default_factory=dict)
    unmapped: list = field(default_factory=list)


class TrackerEmu:
    def __init__(self, firmware: Path = FIRMWARE):
        self.image = firmware.read_bytes()
        self.uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
        self.uc.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M4)
        self.stats = Stats()
        for base, size in (FLASH, SRAM, SDRAM, PERIPH, BITBAND_PERIPH, PPB, STUB_REGION):
            self.uc.mem_map(base, size, UC_PROT_ALL)
        self.uc.mem_write(FLASH[0], self.image)
        self.stub_calls: list[tuple[int, int, int]] = []   # (slot, caller, r1)
        self._install_stub_object()
        # Trampoline: a 'bkpt' we stop on when a called function returns.
        self.uc.mem_write(RETURN_MAGIC & ~1, b"\x00\xbe")
        self.uc.hook_add(UC_HOOK_MEM_READ | UC_HOOK_MEM_WRITE, self._mmio_hook,
                         begin=PERIPH[0], end=PERIPH[0] + PERIPH[1] - 1)
        self.uc.hook_add(UC_HOOK_MEM_UNMAPPED, self._unmapped_hook)
        self.uc.hook_add(UC_HOOK_CODE, self._count_hook)

    # ---------- function overrides ----------
    def override(self, func: int, handler) -> None:
        """Replace firmware function `func` with handler(emu) -> r0.

        The function's first instruction is patched to 'bx lr'; a code hook on
        that address runs the handler (which may read args from r0-r3/stack)
        and sets r0 before the 'bx lr' executes.
        """
        addr = func & ~1
        self.uc.mem_write(addr, b"\x70\x47")

        def hook(uc, address, size, user):
            result = handler(self)
            if result is not None:
                uc.reg_write(UC_ARM_REG_R0, result & 0xFFFFFFFF)
        self.uc.hook_add(UC_HOOK_CODE, hook, begin=addr, end=addr)

    def make_object(self, methods: dict, slots: int = 32) -> int:
        """Create an emulated C++ object whose virtual methods run in Python.

        `methods` maps vtable byte offset -> handler(emu) -> r0 (or None).
        Unlisted slots behave like the universal stub (return this).
        Returns the object address (word 0 = vtable).
        """
        base = getattr(self, "_pyobj_next", PYOBJ_BASE)
        vtable, code, obj = base, base + 0x100, base + 0x200
        self._pyobj_next = base + 0x400
        for i in range(slots):
            self.w32(vtable + 4 * i, (code + 4 * i) | 1)
            self.uc.mem_write(code + 4 * i, b"\x70\x47\x00\xbf")   # bx lr; nop
        for offset, handler in methods.items():
            self.override(code + offset, handler)
        self.w32(obj, vtable)
        return obj

    def args(self, n: int = 4) -> list[int]:
        regs = (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)
        values = [self.uc.reg_read(r) for r in regs[:min(n, 4)]]
        sp = self.uc.reg_read(UC_ARM_REG_SP)
        values += [self.r32(sp + 4 * i) for i in range(max(0, n - 4))]
        return values

    # ---------- universal stub object ----------
    def _install_stub_object(self) -> None:
        for i in range(STUB_SLOTS):
            self.w32(STUB_VTABLE + 4 * i, (STUB_CODE + 4 * i) | 1)
            self.uc.mem_write(STUB_CODE + 4 * i, b"\x70\x47\x00\xbf")  # bx lr; nop
        self.w32(STUB_OBJECT, STUB_VTABLE)
        self.uc.hook_add(UC_HOOK_CODE, self._stub_hook,
                         begin=STUB_CODE, end=STUB_CODE + 4 * STUB_SLOTS - 1)

    def _stub_hook(self, uc, address, size, user):
        slot = (address - STUB_CODE) // 4
        caller = (uc.reg_read(UC_ARM_REG_LR) & ~1) - 4
        s0 = struct.unpack("<f", struct.pack("<I", uc.reg_read(UC_ARM_REG_S0)))[0]
        self.stub_calls.append((slot * 4, caller, uc.reg_read(UC_ARM_REG_R1), s0))

    def floats(self, addr: int, n: int) -> list[float]:
        return list(struct.unpack(f"<{n}f", self.uc.mem_read(addr, 4 * n)))

    def codec_log(self) -> list[str]:
        """Decode the codec (SGTL5000 control) calls made through the stub."""
        out = []
        for slot, caller, r1, s0 in self.stub_calls:
            if caller == 0x162C4 and slot == 0x18:
                out.append(f"volume({s0:.3f})")
            elif slot == 0x2C:
                out.append(f"eqSelect({r1})  # 3 = GRAPHIC_EQUALIZER")
            elif caller == 0x1639C and slot == 0x30:
                bands = ", ".join(f"{v:+.2f}" for v in self.floats(r1, 5))
                out.append(f"eqBands({bands})  # 115/330/990/3k/9.9k Hz")
            elif slot == 0x24:
                out.append(f"enhanceBassEnable({r1})")
            elif slot == 0x28:
                lr, bass = self.floats(r1, 2)
                extra = self.uc.mem_read(r1 + 8, 2)
                out.append(f"enhanceBass(lr={lr:.3f}, bass={bass:.3f}, "
                           f"hpf_bypass={extra[0]}, cutoff={extra[1]})")
            elif slot == 0x34:
                out.append(f"codec slot 0x34(r1={r1:#x})  # likely surround/'Space'")
            elif caller == 0x16394 and slot == 0x3C:
                out.append(f"codec slot 0x3c({r1})")
        return out

    # ---------- hooks ----------
    def _count_hook(self, uc, address, size, user):
        self.stats.insns += 1

    def _mmio_hook(self, uc, access, address, size, value, user):
        page = address & ~0xFFF
        bucket = self.stats.mmio_writes if access == 17 else self.stats.mmio_reads
        bucket[page] = bucket.get(page, 0) + 1

    def _unmapped_hook(self, uc, access, address, size, value, user):
        pc = uc.reg_read(UC_ARM_REG_PC)
        self.stats.unmapped.append((hex(pc), hex(address), access))
        return False

    # ---------- memory helpers ----------
    def r32(self, addr: int) -> int:
        return struct.unpack("<I", self.uc.mem_read(addr, 4))[0]

    def w32(self, addr: int, value: int) -> None:
        self.uc.mem_write(addr, struct.pack("<I", value & 0xFFFFFFFF))

    def flash_bytes(self, addr: int, n: int) -> bytes:
        return self.image[addr:addr + n]

    # ---------- execution ----------
    def call(self, func: int, *args: int, max_insns: int = 50_000_000,
             s0: float | None = None) -> int:
        """Call a Thumb function with integer args (+ optional float s0); return r0.

        Args 5+ go on the stack (AAPCS), 8-byte aligned; SP is restored after.
        """
        regs = (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)
        for reg, val in zip(regs, args):
            self.uc.reg_write(reg, val & 0xFFFFFFFF)
        saved_sp = self.uc.reg_read(UC_ARM_REG_SP)
        if len(args) > 4:
            sp = (saved_sp - 4 * (len(args) - 4)) & ~7
            for i, val in enumerate(args[4:]):
                self.w32(sp + 4 * i, val & 0xFFFFFFFF)
            self.uc.reg_write(UC_ARM_REG_SP, sp)
        if s0 is not None:
            self.uc.reg_write(UC_ARM_REG_S0, struct.unpack("<I", struct.pack("<f", s0))[0])
        self.uc.reg_write(UC_ARM_REG_LR, RETURN_MAGIC)
        start = self.stats.insns
        try:
            self.uc.emu_start(func | 1, RETURN_MAGIC & ~1, count=max_insns)
        except UcError as exc:
            pc = self.uc.reg_read(UC_ARM_REG_PC)
            raise RuntimeError(f"emulation fault at pc={pc:#x}: {exc}") from exc
        if self.uc.reg_read(UC_ARM_REG_PC) != RETURN_MAGIC & ~1:
            pc = self.uc.reg_read(UC_ARM_REG_PC)
            raise RuntimeError(
                f"did not return after {self.stats.insns - start} insns (pc={pc:#x})")
        self.uc.reg_write(UC_ARM_REG_SP, saved_sp)
        return self.uc.reg_read(UC_ARM_REG_R0)

    def reset_runtime(self) -> None:
        """Reproduce the memory side of ResetHandler (skips clock/PLL setup)."""
        self.uc.reg_write(UC_ARM_REG_SP, STACK_TOP)
        self.uc.mem_write(DATA_START, self.flash_bytes(DATA_LOAD, DATA_END - DATA_START))
        self.uc.mem_write(BSS_START, b"\x00" * (BSS_END - BSS_START))
        self.uc.mem_write(RAM_VECTORS, self.flash_bytes(0, VECTOR_LEN))

    # ---------- audio engine ----------
    def init_audio_memory(self) -> None:
        self.call(AUDIO_MEMORY_INIT, AUDIO_POOL, AUDIO_POOL_BLOCKS)

    def load_project(self, project_dir: Path) -> dict:
        """Load project.mt through the firmware's own loader (0x73404).

        The SD read (0x24ad0) is replaced by a host file read; the loader's
        defaults, version migration (0x2606c) and parser (0x22fa0) run as-is.
        Call before init_engine() so the engine applies the loaded settings.
        """
        data = (Path(project_dir) / "project.mt").read_bytes()
        info = {"bytes": len(data), "reads": []}

        def read_file(emu: "TrackerEmu") -> int:
            fs, path_ptr, buf, maxlen = emu.args(4)
            path = bytes(emu.uc.mem_read(path_ptr, 64)).split(b"\0")[0].decode()
            emu.uc.mem_write(buf, data[:maxlen])
            info["reads"].append((path, min(len(data), maxlen)))
            return 2

        self.override(SD_READ_FILE, read_file)
        self.w32(FILE_MANAGER + 0x1104, STUB_OBJECT)   # helper object -> stub
        self.call(PROJECT_LOAD, STUB_OBJECT)
        info["version"] = data[8]
        return info

    def load_instruments(self, project_dir: Path) -> list[dict]:
        """Load .pti instruments: struct -> PROJECT[i], PCM -> SDRAM.

        Mirrors the firmware loader 0x22008 (not executed): samples are packed
        from SAMPLE_BASE in instrument order, 2 bytes/frame mono, 4 stereo.
        File names follow the project folder convention '<n> <name>.pti'.
        """
        loaded, next_addr = [], SAMPLE_BASE
        files = {}
        for path in (Path(project_dir) / "instruments").glob("*.pti"):
            number = path.name.split(" ", 1)[0]
            if number.isdigit():
                files[int(number) - 1] = path
        for index in range(MAX_INSTRUMENTS):
            path = files.get(index)
            if path is None:
                continue
            data = path.read_bytes()
            struct_bytes = data[PTI_STRUCT_OFFSET:PTI_STRUCT_OFFSET + INSTRUMENT_SIZE]
            base = PROJECT + index * INSTRUMENT_SIZE
            self.uc.mem_write(base, struct_bytes)
            frames = struct.unpack_from("<I", struct_bytes, INSTR_SAMPLE_LEN)[0]
            pcm = data[PTI_SAMPLE_OFFSET:]
            stereo = frames and len(pcm) == frames * 4
            self.w32(base + INSTR_SAMPLE_ADDR, next_addr)
            self.w32(SAMPLE_PTR_TABLE_A + index * 8, next_addr)
            self.w32(SAMPLE_PTR_TABLE_B + (index + 0x9B8) * 8, next_addr)
            self.uc.mem_write(next_addr, pcm)
            name = struct_bytes[5:5 + 32].split(b"\0")[0].decode(errors="replace")
            loaded.append({"index": index + 1, "name": name, "frames": frames,
                           "stereo": bool(stereo), "addr": next_addr, "bytes": len(pcm)})
            next_addr += len(pcm)
        if next_addr > SDRAM[0] + SDRAM[1]:
            raise RuntimeError("samples exceed SDRAM")
        return loaded

    def make_file_system(self, root: Path) -> int:
        """Fake FS object for the fileManager vtable used by 0x22de4.

        Slots: +0x08 exists(path), +0x20 size(path), +0x2c open(ret*, path, ..),
        +0x30 read(buf, size, handle), +0x40 close(handle, ..).
        'Workspace/<sub>/<file>' paths are served from `root`.
        """
        handles: dict[int, tuple[bytes, list]] = {}
        log = self.fs_log = []

        def resolve(path_ptr: int) -> Path | None:
            raw = bytes(self.uc.mem_read(path_ptr, 128)).split(b"\0")[0].decode()
            rel = raw.split("Workspace/", 1)[-1]
            candidate = Path(root) / rel
            log.append(raw)
            return candidate if candidate.exists() else None

        def exists(emu):
            return 1 if resolve(emu.args(2)[1]) else 0

        def size(emu):
            path = resolve(emu.args(2)[1])
            return path.stat().st_size if path else 0

        def open_(emu):
            ret, _fs, path_ptr = emu.args(3)
            path = resolve(path_ptr)
            handle = 0
            if path:
                handle = len(handles) + 1
                handles[handle] = (path.read_bytes(), [0])
            emu.w32(ret, handle)
            emu.uc.mem_write(ret + 4, bytes([1 if path else 0]))
            return ret

        def read(emu):
            _fs, buf, count, handle = emu.args(4)
            data, pos = handles.get(handle, (b"", [0]))
            chunk = data[pos[0]:pos[0] + count]
            emu.uc.mem_write(buf, chunk)
            pos[0] += len(chunk)
            return len(chunk)

        def close(emu):
            handles.pop(emu.args(2)[1], None)
            return 1

        return self.make_object({0x08: exists, 0x20: size, 0x2C: open_,
                                 0x30: read, 0x40: close})

    def load_pattern(self, project_dir: Path, number: int) -> int:
        """Load pattern_<number>.mtp via the firmware's 0x22de4 (incl. fixups)."""
        fs = self.make_file_system(Path(project_dir))
        self.w32(FILE_MANAGER, fs)                       # fileManager->fs
        path = PYOBJ_BASE - 0x100                         # scratch string in stub region
        self.uc.mem_write(path, f"Workspace/patterns/pattern_{number:02d}.mtp\0".encode())
        dest = self.r32(PATTERN_BUFFER_PTR)
        if not dest:
            raise RuntimeError("pattern buffer not allocated; call init_sequencer() first")
        ok = self.call(PATTERN_LOAD, FILE_MANAGER, path, dest)
        return dest if ok & 0xFF else 0

    def pattern_steps(self, buffer: int, track: int, count: int = 16) -> list[bytes]:
        base = buffer + PATTERN_TRACK_OFFSET + track * PATTERN_TRACK_STRIDE + 1
        return [bytes(self.uc.mem_read(base + 6 * s, 6)) for s in range(count)]

    def install_time_source(self) -> None:
        """Provide the micros() time-source object the app normally sets up."""
        self.sim_seconds = 0.0
        source = self.make_object({0x00: lambda emu: int(emu.sim_seconds * 1e6)})
        self.w32(TIME_SOURCE_PTR, source)
        for ptr in HAL_DEVICE_PTRS:
            if not self.r32(ptr):
                self.w32(ptr, STUB_OBJECT)

    def play(self) -> bool:
        """Play the current pattern, looping (0x113d4, as pattern export does)."""
        if not self.r32(TIME_SOURCE_PTR):
            self.install_time_source()
        self.call(SEQUENCER_PLAY_PATTERN, SEQUENCER)
        return bool(self.uc.mem_read(SEQUENCER_PLAYING, 1)[0])

    def clock(self, seconds: float) -> None:
        """Advance the sequencer clock by `seconds` (firmware computes tempo)."""
        self.sim_seconds += seconds
        self.call(SEQUENCER_CLOCK, SEQUENCER, s0=seconds)
        # The device's per-block service (0xf57c) runs the voice update next.
        self.call(ENGINE_VOICE_UPDATE, ENGINE)

    def apply_master_fx_patch(self, disable_codec_eq: bool = True, build: str = "") -> dict:
        """Install patch/master_fx.bin and redirect the master saturator call.

        With disable_codec_eq the codec's graphic EQ setup (0x704c0) becomes a
        no-op, so the Master EQ is not applied twice on hardware. Apply before
        init_engine(). Rebuild the binary with patch/build.sh after edits.
        """
        from patch.link import thumb_branch
        stem = f"master_fx{'_' + build if build else ''}.bin"   # build="v1" for the old one
        binary = (PATCH_DIR / stem).read_bytes()
        syms = dict(line.split() for line in
                    (PATCH_DIR / f"{stem}.syms").read_text().splitlines())
        entry = int(syms["master_fx_entry"], 16)
        self.uc.mem_write(PATCH_BASE, binary)
        original = bytes(self.uc.mem_read(PATCH_HOOK_SITE, 4))
        if original != thumb_branch(PATCH_HOOK_SITE, 0xD42F0, link=True):
            raise RuntimeError(f"unexpected bytes at hook site: {original.hex()}")
        self.uc.mem_write(PATCH_HOOK_SITE, thumb_branch(PATCH_HOOK_SITE, entry & ~1, link=True))
        if disable_codec_eq:
            self.uc.mem_write(CODEC_EQ_SETTER, b"\x70\x47")       # bx lr
        if "ui_knob" in syms:                                    # Shift+Master+jog -> DJ
            for site, (original_target, name) in UI_HOOK_SITES.items():
                original = bytes(self.uc.mem_read(site, 4))
                if original != thumb_branch(site, original_target, link=False):
                    raise RuntimeError(f"unexpected bytes at UI hook {site:#x}: {original.hex()}")
                self.uc.mem_write(site, thumb_branch(site, int(syms[name], 16) & ~1, link=False))
        if "patch_boot" in syms:                                 # boot-time state reset
            original = bytes(self.uc.mem_read(BOOT_HOOK_SITE, 4))
            if original != thumb_branch(BOOT_HOOK_SITE, ENGINE_INIT, link=True):
                raise RuntimeError(f"unexpected bytes at boot hook: {original.hex()}")
            self._boot_entry = int(syms["patch_boot"], 16)
            self.uc.mem_write(BOOT_HOOK_SITE, thumb_branch(BOOT_HOOK_SITE, self._boot_entry & ~1, link=True))
        self.set_patch_params(dj=0.0)
        return {"bytes": len(binary), "entry": entry}

    def apply_braids_patch(self) -> dict:
        """Install braids.bin and its player hooks; boot then goes through braids_boot.

        Apply after apply_master_fx_patch() (braids_boot chains to patch_boot)
        and before init_engine().
        """
        from patch.link import thumb_branch
        syms = {k: int(v, 16) for k, v in (l.split() for l in
                BRAIDS_BIN.with_suffix(".bin.syms").read_text().splitlines())}
        base = syms["braids_voice_size"] & ~0xFFF
        self.uc.mem_write(base, BRAIDS_BIN.read_bytes())
        for site, (stock, name) in BRAIDS_HOOKS.items():
            if bytes(self.uc.mem_read(site, 4)).hex() != stock:
                raise RuntimeError(f"unexpected bytes at Braids hook {site:#x}")
            self.uc.mem_write(site, thumb_branch(site, syms[name] & ~1, link=False))
        expected = thumb_branch(BOOT_HOOK_SITE, getattr(self, "_boot_entry", 0) & ~1, link=True)
        if bytes(self.uc.mem_read(BOOT_HOOK_SITE, 4)) != expected:
            raise RuntimeError("apply_master_fx_patch() first (boot hook must call patch_boot)")
        self.uc.mem_write(BOOT_HOOK_SITE, thumb_branch(BOOT_HOOK_SITE, syms["braids_boot"] & ~1, link=True))
        self._boot_entry = syms["braids_boot"]
        self.braids_syms = syms
        return syms

    def apply_fxbank_patch(self) -> dict:
        """Install fx_bank.bin and point the reverb/delay vtables' update at it.

        Selection lives in the master_fx state (+20); apply after
        apply_master_fx_patch(). Works before or after init_engine().
        """
        syms = {k: int(v, 16) for k, v in (l.split() for l in
                FXBANK_BIN.with_suffix(".bin.syms").read_text().splitlines())}
        self.uc.mem_write(FXBANK_BASE, FXBANK_BIN.read_bytes())
        for vtable, (stock, name) in FXBANK_VTABLES.items():
            if self.r32(vtable) != stock:
                raise RuntimeError(f"unexpected update pointer in vtable {vtable:#x}")
            self.w32(vtable, syms[name])
        self.fxbank_syms = syms
        return syms

    def apply_ct_patch(self) -> None:
        """Conditional trigs: the five hooks of patch.make_ptf.CT_SITES (code in
        master_fx.bin, so apply after apply_master_fx_patch())."""
        from patch.link import thumb_branch
        from patch.make_ptf import CT_SITES
        syms = {k: int(v, 16) for k, v in (l.split() for l in
                (PATCH_DIR / "master_fx.bin.syms").read_text().splitlines())}
        for site, stock_hex, name, kind in CT_SITES:
            n = 6 if kind == "bw6" else 4
            if bytes(self.uc.mem_read(site, n)).hex() != stock_hex:
                raise RuntimeError(f"unexpected bytes at CT hook {site:#x}")
            new = thumb_branch(site, syms[name] & ~1, kind == "bl")
            self.uc.mem_write(site, new + (b"\x00\xbf" if kind == "bw6" else b""))
        self.ct_syms = syms

    def set_fx_select(self, value: int) -> None:
        """FX bank selection: bit0 = stock reverb, bit1 = stock delay."""
        self.w32(PATCH_STATE + 20, value)

    def record_ui_passthrough(self, skip_probe: bool = True) -> list:
        """Replace the interface's knob/button handlers with recorders.

        The UI (screens, function manager) is not initialised in the harness,
        so this captures what would reach it: ('knob', pot, value) and
        ('button', btn, state) tuples, in order, plus ('popup', text, seconds)
        for the firmware's pop-up.
        """
        log = []
        if skip_probe:   # master_fx's flash-probe pop-up (first press after boot): treat as shown
            self.w32(PATCH_STATE + 32, self.r32(PATCH_STATE + 32) | 0x40000000)

        def popup(emu):
            _, _, text, seconds = emu.args(4)
            raw = bytes(emu.uc.mem_read(text, 64))
            log.append(("popup", raw[:raw.index(0)].decode("ascii"), seconds))
        self.override(UI_POPUP, popup)
        self.override(UI_KNOB, lambda emu: log.append(
            ("knob", emu.args(3)[1], struct.unpack("<h", struct.pack("<H", emu.args(3)[2] & 0xFFFF))[0])))
        self.override(UI_BUTTON, lambda emu: log.append(("button", *emu.args(3)[1:3])))
        return log

    def press(self, btn: int) -> None:
        self.call(UI_PRESS_CB, btn)

    def release(self, btn: int) -> None:
        self.call(UI_RELEASE_CB, btn)

    def turn(self, value: int, pot: int = JOG_POT) -> None:
        """Jog wheel: value = signed detents, as the board callback receives it."""
        self.call(UI_KNOB_CB, pot, value & 0xFFFFFFFF)

    def patch_dj(self) -> float:
        return struct.unpack("<f", self.uc.mem_read(PATCH_STATE + 4, 4))[0]

    def set_patch_params(self, dj: float | None = None, enabled: bool = True) -> None:
        """Initialise/update patch state (magic, enabled, DJ knob -1..+1)."""
        if self.r32(PATCH_STATE) != PATCH_MAGIC:
            self.uc.mem_write(PATCH_STATE, b"\0" * PATCH_STATE_SIZE)
            self.w32(PATCH_STATE, PATCH_MAGIC)
            self.uc.mem_write(PATCH_STATE + 32, struct.pack("<f", 99.0))   # cached_dj
        if dj is not None:
            self.uc.mem_write(PATCH_STATE + 4, struct.pack("<f", dj))
        self.w32(PATCH_STATE + 8, int(enabled))

    def set_master_eq(self, gains: list[float]) -> None:
        """Write the firmware's Master EQ gains (5 floats, -1..+1)."""
        self.uc.mem_write(MASTER_EQ_GAINS, struct.pack("<5f", *gains))

    def init_sequencer(self) -> None:
        """Sequencer init (0x10f98), called right after engine init by app setup."""
        self.call(SEQUENCER_INIT, SEQUENCER)

    def init_engine(self, max_insns: int = 200_000_000) -> None:
        """Run the firmware's own engine init with the stub object as the HAL.

        This also calls AudioMemory(0x1fff0bd0, 70) itself (at 0x1611c+).
        With a patch that hooks boot, the call goes through the redirected
        'bl' at 0xf3fa (patch_boot), as app setup does on the device.
        """
        target = getattr(self, "_boot_entry", ENGINE_INIT)
        self.call(target, ENGINE, STUB_OBJECT, 0, max_insns=max_insns)

    def transmit_samples(self, node: int, samples, output: int = 0) -> None:
        """Send one 128-sample int16 block from `node` on `output`."""
        block = self.call(ALLOCATE)
        if not block:
            raise RuntimeError("audio block pool exhausted")
        self.uc.mem_write(block + 4, struct.pack(f"<{BLOCK_SAMPLES}h", *samples))
        self.call(TRANSMIT, node, block, output)
        self.call(RELEASE, block)

    def render_block(self, left: int, right: int, profile: dict | None = None,
                     overrides: dict | None = None) -> None:
        """One 128-frame engine tick, mirroring the scheduler at 0xcbbdc.

        `overrides` maps node address -> callable(emu, node) replacing update().
        """
        overrides = overrides or {}
        self.call(OUTPUT_SET_BUFFERS, OUTPUT_NODE, left, right)
        for node, vtable in self.update_list():
            if not self.uc.mem_read(node + 4, 1)[0]:      # AudioStream::active
                continue
            if node in overrides:
                overrides[node](self, node)
                continue
            before = self.stats.insns
            self.call(self.r32(vtable), node)            # virtual update()
            if profile is not None:
                profile[vtable] = profile.get(vtable, 0) + self.stats.insns - before

    # ---------- audio graph inspection ----------
    def update_list(self, limit: int = 512) -> list[tuple[int, int]]:
        """Walk the AudioStream update list: [(node, vtable), ...]."""
        nodes, node = [], self.r32(UPDATE_LIST_HEAD)
        while node and len(nodes) < limit:
            nodes.append((node, self.r32(node)))
            node = self.r32(node + 0x10)
        return nodes
