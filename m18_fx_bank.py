"""M18: the FX bank (fxbank/fx_bank.c): plate reverb and tape delay in place of
the stock master reverb/delay nodes.

Usage: m18_fx_bank.py [--quick]

Node tests drive one node's update() directly: input blocks go into its
input queue, transmit() is replaced by a recorder. Same input, same stock
parameters, bank vs stock:
  1. reverb: level, decay time vs Size, stereo spread, cost, idle cost,
     silent clearing after a switch, peak with full-scale input
  2. delay: echo times and levels, ping-pong sides, a time change glides
     (no jump) where stock clicks, maximum feedback stays bounded, cost
  3. UI: Shift + Master + another button steps the selection, shows the
     pop-up and swallows that button; other input passes through
  4. whole engine: the project renders through the bank; with everything set
     to stock it is bit-identical to master_fx alone
"""
import math
import random
import struct
import sys

from m6_load_song import DEFAULT
from m7_render import LEFT, RIGHT
from tracker_emu import (ALLOCATE, BLOCK_SAMPLES, BTN_MASTER, BTN_SHIFT, DELAY_NODE,
                         FXBANK_STATE, LIBC_INIT_ARRAY, PATCH_STATE, RELEASE,
                         REVERB_NODE, TRANSMIT, TrackerEmu)

FS = 750000 / 17
QUICK = "--quick" in sys.argv


def boot(bank=True, project=True):
    emu = TrackerEmu()
    emu.reset_runtime()
    emu.call(LIBC_INIT_ARRAY)
    emu.load_project(DEFAULT)
    emu.apply_master_fx_patch()
    if bank:
        emu.apply_fxbank_patch()
    emu.init_engine()
    emu.init_sequencer()
    if bank:
        emu.set_fx_select(0)          # tests select explicitly; default to the bank
    if project:
        emu.load_instruments(DEFAULT)
        emu.install_time_source()
    return emu


class NodeRig:
    """Feed one node and record what it transmits."""

    def __init__(self, emu, node):
        self.emu, self.node, self.sent = emu, node, []
        emu.override(TRANSMIT, self._transmit)

    def _transmit(self, emu):
        this, block, index = emu.args(3)
        if this == self.node:
            self.sent.append((index, struct.unpack(f"<{BLOCK_SAMPLES}h",
                                                   emu.uc.mem_read(block + 4, 256))))

    def feed(self, index, samples):
        block = self.emu.call(ALLOCATE)
        self.emu.uc.mem_write(block + 4, struct.pack(f"<{BLOCK_SAMPLES}h", *samples))
        queue = self.emu.r32(self.node + 0xC)          # inputQueue (receive 0xce578)
        self.emu.w32(queue + 4 * index, block)

    def step(self, inputs):
        """inputs: list of sample blocks (or None) per input. Returns (outs, insns)."""
        for i, samples in enumerate(inputs):
            if samples is not None:
                self.feed(i, samples)
        self.sent = []
        start = self.emu.stats.insns
        self.emu.call(self.emu.r32(self.emu.r32(self.node)), self.node)
        outs = {i: d for i, d in self.sent}
        return [outs.get(0), outs.get(1)], self.emu.stats.insns - start


def rms(x):
    return math.sqrt(sum(v * v for v in x) / max(1, len(x))) + 1e-9


def db(x):
    return 20 * math.log10(x / 32768)


def run_reverb(emu, rig, blocks, burst_blocks, amp=8000, seed=1):
    rnd = random.Random(seed)
    out_l, out_r, cost = [], [], []
    for b in range(blocks):
        if b < burst_blocks:
            l = [int(rnd.gauss(0, amp)) for _ in range(BLOCK_SAMPLES)]
            r = [int(rnd.gauss(0, amp)) for _ in range(BLOCK_SAMPLES)]
            l = [max(-32768, min(32767, v)) for v in l]
            r = [max(-32768, min(32767, v)) for v in r]
        else:
            l = r = [0] * BLOCK_SAMPLES
        (ol, orr), n = rig.step([l, r])
        out_l += ol or [0] * BLOCK_SAMPLES
        out_r += orr or [0] * BLOCK_SAMPLES
        cost.append(n)
    return out_l, out_r, cost


def decay_time(x, start, db_drop=30):
    """Seconds from `start` until the 20 ms RMS falls db_drop below its level at start."""
    win = int(0.02 * FS)
    env = [rms(x[i:i + win]) for i in range(start, len(x) - win, win)]
    ref = max(env[:3])
    for i, e in enumerate(env):
        if e < ref * 10 ** (-db_drop / 20):
            return i * 0.02
    return None


def correlation(a, b):
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    return num / math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b) + 1e-9)


def reverb_tests():
    print("1. reverb node (bank: plate; stock: firmware reverb), noise burst 0.1 s, -12 dBFS")
    ok = True
    secs = 1.5 if QUICK else 3.0
    blocks, burst = int(secs * FS / BLOCK_SAMPLES), int(0.1 * FS / BLOCK_SAMPLES)
    results = {}
    for name, sel in (("stock", 1), ("plate", 0)):
        emu = boot()
        emu.set_fx_select(sel)
        rig = NodeRig(emu, REVERB_NODE)
        if sel == 0:                      # first blocks after a switch clear the lines
            silent = 0
            for _ in range(10):
                (ol, _), _ = rig.step([[0] * 128, [0] * 128])
                silent += ol is None
            results["clearing"] = silent
        l, r, cost = run_reverb(emu, rig, blocks, burst)
        results[name] = (l, r, cost, emu)
    for name in ("stock", "plate"):
        l, r, cost, emu = results[name]
        tail = burst * BLOCK_SAMPLES
        lvl = rms(l[tail:tail + int(0.3 * FS)] + r[tail:tail + int(0.3 * FS)])
        t30 = decay_time(l, tail)
        corr = correlation(l[tail:tail + int(0.5 * FS)], r[tail:tail + int(0.5 * FS)])
        busy = sum(cost[burst:burst + 50]) / 50
        print(f"   {name:5s} tail level {db(lvl):6.1f} dBFS  -30 dB in "
              f"{'>' + str(secs) if t30 is None else f'{t30:.2f}'} s  L/R corr {corr:+.2f}  "
              f"{busy:7,.0f} insns/block  peak {db(max(map(abs, l + r)) + 1):5.1f} dBFS")
    lvl = {n: rms(results[n][0][burst * 128:] + results[n][1][burst * 128:]) for n in ("stock", "plate")}
    diff = db(lvl["plate"]) - db(lvl["stock"])
    good = abs(diff) < 6
    ok &= good
    print(f"   {'ok ' if good else 'BAD'} plate vs stock tail level {diff:+.1f} dB (within 6 dB)")
    good = results["clearing"] >= 6
    ok &= good
    print(f"   {'ok ' if good else 'BAD'} silent while clearing the plate lines after a switch: "
          f"{results['clearing']} blocks")


    # idle: short tail, then silence -> the plate stops working
    emu = boot()
    emu.uc.mem_write(REVERB_NODE + 0x54, struct.pack("<f", 0.0))
    rig = NodeRig(emu, REVERB_NODE)
    for _ in range(8):
        rig.step([[0] * 128, [0] * 128])
    _, _, cost = run_reverb(emu, rig, int(2.5 * FS / 128), burst)
    idle = sum(cost[-10:]) / 10
    good = idle < 2000
    ok &= good
    print(f"   {'ok ' if good else 'BAD'} idle once the tail is gone (Size 0, 2.5 s): {idle:,.0f} insns/block")

    # decay vs Size
    times = []
    for size in (0.1, 0.5, 0.9):
        emu = boot()
        emu.uc.mem_write(REVERB_NODE + 0x54, struct.pack("<f", size))
        rig = NodeRig(emu, REVERB_NODE)
        for _ in range(8):
            rig.step([[0] * 128, [0] * 128])
        l, r, _ = run_reverb(emu, rig, blocks, burst)
        t = decay_time(l, burst * 128)
        times.append(t if t is not None else secs)
    good = times[0] < times[1] < times[2]
    ok &= good
    print(f"   {'ok ' if good else 'BAD'} -30 dB time grows with Size 0.1/0.5/0.9: "
          + " / ".join(f"{t:.2f} s" for t in times))

    # full-scale input
    emu = boot()
    rig = NodeRig(emu, REVERB_NODE)
    for _ in range(8):
        rig.step([[0] * 128, [0] * 128])
    l, r, _ = run_reverb(emu, rig, 60, 40, amp=30000)
    clipped = sum(1 for v in l + r if abs(v) >= 32767)
    print(f"   note full-scale noise in: peak {db(max(map(abs, l + r)) + 1):.1f} dBFS, "
          f"{clipped} clipped samples")
    return ok


def run_delay(rig, blocks, signal, at=None):
    """signal(i) -> input sample; at: {block: callable(emu)} applied before that block."""
    o0, o1, cost = [], [], []
    for b in range(blocks):
        if at and b in at:
            at[b](rig.emu)
        x = [signal(b * 128 + i) for i in range(128)]
        (a, c), n = rig.step([x])
        o0 += a or [0] * 128
        o1 += c or [0] * 128
        cost.append(n)
    return o0, o1, cost


def set_delay(emu, samples=None, fb=None, pingpong=None):
    if samples is not None:
        emu.w32(DELAY_NODE + 0x28, samples)
    if fb is not None:
        emu.uc.mem_write(DELAY_NODE + 0x2C, bytes([fb]))
    if pingpong is not None:
        emu.uc.mem_write(DELAY_NODE + 0x2E, bytes([pingpong]))


def peaks(x, n, spacing, width=300):
    """Largest |x| near each multiple of `spacing` (1..n): (index, value)."""
    out = []
    for k in range(1, n + 1):
        lo, hi = k * spacing - width, k * spacing + width
        seg = x[lo:hi]
        if not seg:
            break
        i = max(range(len(seg)), key=lambda j: abs(seg[j]))
        out.append((lo + i, seg[i]))
    return out


def delay_tests():
    print("2. delay node (bank: tape; stock: firmware delay)")
    ok = True
    d = 4410                                        # 100 ms
    blocks = int(0.45 * FS / 128)
    impulse = lambda n: 16000 if n == 0 else 0
    res = {}
    for name, sel in (("stock", 2), ("tape", 0)):
        for pp in (0, 1):
            emu = boot()
            emu.set_fx_select(sel)
            set_delay(emu, d, 160, pp)              # feedback 0.63
            rig = NodeRig(emu, DELAY_NODE)
            o0, o1, cost = run_delay(rig, blocks, impulse)
            res[name, pp] = (o0, o1, sum(cost) / len(cost))
    for name in ("stock", "tape"):
        o0, o1, cost = res[name, 0]
        pk = peaks(o0, 3, d)
        print(f"   {name:5s} echoes at " + ", ".join(f"{i} ({db(abs(v) + 1):.1f} dB)" for i, v in pk)
              + f"  {cost:,.0f} insns/block")
    pk = peaks(res["tape", 0][0], 3, d)
    good = all(abs(i - (k + 1) * d) < 40 for k, (i, _) in enumerate(pk)) and \
        abs(pk[0][1]) > abs(pk[1][1]) > abs(pk[2][1])
    ok &= good
    print(f"   {'ok ' if good else 'BAD'} tape echoes every {d} samples (+/- wow), decaying")
    # echo levels with a 1 kHz tone burst (an impulse's peak is smeared by wow
    # and head loss without changing its level)
    burst = lambda n: int(8000 * math.sin(2 * math.pi * 1000 * n / FS)) if n < 1024 else 0
    lv = {}
    for name, sel in (("stock", 2), ("tape", 0)):
        emu = boot()
        emu.set_fx_select(sel)
        set_delay(emu, d, 160, 0)
        o0, _, _ = run_delay(NodeRig(emu, DELAY_NODE), blocks, burst)
        lv[name] = [db(rms(o0[k * d + 100:k * d + 900])) for k in (1, 2, 3)]
        print(f"   {name:5s} 1 kHz burst echoes: " + ", ".join(f"{v:.1f}" for v in lv[name]) + " dBFS")
    good = abs(lv["tape"][0] - lv["stock"][0]) < 2
    ok &= good
    print(f"   {'ok ' if good else 'BAD'} first echo level tape vs stock: {lv['tape'][0] - lv['stock'][0]:+.1f} dB")
    for name in ("stock", "tape"):
        o0, o1, _ = res[name, 1]
        a, b = peaks(o0, 2, d), peaks(o1, 2, d)
        print(f"   {name:5s} ping-pong: out0 {[db(abs(v) + 1).__round__(1) for _, v in a]} dB, "
              f"out1 {[db(abs(v) + 1).__round__(1) for _, v in b]} dB")
    o0, o1, _ = res["tape", 1]
    a, b = peaks(o0, 2, d), peaks(o1, 2, d)
    good = abs(b[0][1]) > 4 * abs(a[0][1]) and abs(a[1][1]) > 4 * abs(b[1][1])
    ok &= good
    print(f"   {'ok ' if good else 'BAD'} tape ping-pong: 1st echo on output 1, 2nd on output 0 (as stock)")

    # time change under a steady tone: largest sample-to-sample jump
    tone = lambda n: int(8000 * math.sin(2 * math.pi * 220 * n / FS))
    jumps = {}
    for name, sel in (("stock", 2), ("tape", 0)):
        emu = boot()
        emu.set_fx_select(sel)
        set_delay(emu, 13230, 0, 0)
        rig = NodeRig(emu, DELAY_NODE)
        n_blocks = int(0.6 * FS / 128)
        change = int(0.4 * FS / 128)
        o0, _, _ = run_delay(rig, n_blocks, tone, at={change: lambda e: set_delay(e, 6615)})
        seg = o0[change * 128 - 256: change * 128 + 4096]
        jumps[name] = max(abs(seg[i + 1] - seg[i]) for i in range(len(seg) - 1))
    steady = 2 * math.pi * 220 / FS * 8000
    good = jumps["tape"] < 2.5 * steady
    ok &= good
    print(f"   {'ok ' if good else 'BAD'} time change 300 -> 150 ms under a 220 Hz tone: largest step "
          f"tape {jumps['tape']} vs stock {jumps['stock']} (a clean sine steps <= {steady:.0f})")

    # maximum feedback stays bounded and finite
    emu = boot()
    set_delay(emu, 2205, 252, 0)
    rig = NodeRig(emu, DELAY_NODE)
    burst = lambda n: int(12000 * math.sin(n * 0.05)) if n < 2048 else 0
    o0, _, _ = run_delay(rig, int((1.0 if QUICK else 2.0) * FS / 128), burst)
    tail = o0[-int(0.2 * FS):]
    print(f"   note max feedback (0.99 on the knob): level after {1.0 if QUICK else 2.0:.0f} s "
          f"{db(rms(tail)):.1f} dBFS, peak {db(max(map(abs, tail)) + 1):.1f} dBFS")
    return ok


def ui_tests():
    print("3. UI: Shift + Master + another button")
    emu = boot(project=False)
    emu.set_fx_select(3)              # as after a real boot (patch_boot)
    log = emu.record_ui_passthrough()
    ok = True
    seen = []
    start = emu.r32(PATCH_STATE + 20)
    emu.press(BTN_SHIFT); emu.press(BTN_MASTER)
    cpu = [e[1] for e in log if e[0] == "popup"]
    good = start == 3 and len(cpu) == 1 and cpu[0].startswith("R0 P0")
    ok &= good
    print(f"   {'ok ' if good else 'BAD'} boots on all stock (select {start}); Shift + Master shows the "
          f"cost readout {cpu}")
    for _ in range(4):
        emu.press(14); emu.release(14)
        seen.append((emu.r32(PATCH_STATE + 20), [e[1] for e in log if e[0] == "popup"][-1]))
    emu.release(BTN_MASTER); emu.release(BTN_SHIFT)
    emu.press(14); emu.release(14)
    buttons = [e[1:] for e in log if e[0] == "button"]
    expect_sel = [0, 1, 2, 3]
    good = [s for s, _ in seen] == expect_sel
    ok &= good
    print(f"   {'ok ' if good else 'BAD'} selection steps {[s for s, _ in seen]}; pop-ups: "
          + ", ".join(f'"{t}"' for _, t in seen))
    good = buttons == [(BTN_SHIFT, 1), (BTN_MASTER, 1), (BTN_MASTER, 0), (BTN_SHIFT, 0), (14, 1), (14, 0)]
    ok &= good
    print(f"   {'ok ' if good else 'BAD'} forwarded buttons {buttons} (14 swallowed only during the gesture)")
    return ok


def engine_tests():
    from m7_render import step_seconds
    print("4. whole engine, project pattern 1")
    ok = True
    blocks = 150 if QUICK else 400
    renders = {}
    for name, bank, sel in (("master_fx only", False, 0), ("bank, all stock", True, 3),
                            ("bank, plate + tape", True, 0)):
        emu = boot(bank=bank)
        emu.load_pattern(DEFAULT, 1)
        if bank:
            emu.set_fx_select(sel)
        emu.play()
        pcm, prof = [], {}
        for _ in range(blocks):
            emu.clock(BLOCK_SAMPLES / 44100)
            emu.render_block(LEFT, RIGHT, profile=prof)
            pcm += struct.unpack("<256h", emu.uc.mem_read(LEFT, 256) + emu.uc.mem_read(RIGHT, 256))
        rev = prof.get(0xA9EFC, 0) / blocks
        dly = prof.get(0xA9F54, 0) / blocks
        renders[name] = pcm
        print(f"   {name:18s} peak {db(max(map(abs, pcm)) + 1):5.1f} dBFS  reverb {rev:7,.0f}  "
              f"delay {dly:6,.0f} insns/block  fx state magic {emu.r32(FXBANK_STATE):#x}")
    good = renders["master_fx only"] == renders["bank, all stock"]
    ok &= good
    print(f"   {'ok ' if good else 'BAD'} all-stock selection is bit-identical to master_fx alone")
    diff = sum(a != b for a, b in zip(renders["master_fx only"], renders["bank, plate + tape"]))
    print(f"   note plate + tape differs from stock in {diff:,} of {len(renders['master_fx only']):,} samples")
    return ok


def main():
    ok = True
    ok &= ui_tests()
    ok &= reverb_tests()
    ok &= delay_tests()
    ok &= engine_tests()
    print("all ok" if ok else "FAILURES")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
