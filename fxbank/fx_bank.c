/*
 * Swappable master reverb/delay for Polyend Tracker firmware 1.9.2.
 *
 * The master reverb and delay are one AudioStream node each:
 *   reverb  engine+0x1c18 = 0x1fffae9c, vtable 0xa9efc, update 0xcfdc4
 *           stereo in (receiveWritable 0/1), processed in place, stereo out
 *   delay   engine+0x1c1c = 0x1fffae48, vtable 0xa9f54, update 0xd037c
 *           mono in (receiveReadOnly 0), stereo out
 * Each vtable's slot 0 (update) is the only way the firmware reaches them, so
 * make_ptf points those two flash words at reverb_update / delay_update here.
 * Each one either runs the bank's algorithm or tail-calls the stock update,
 * chosen by the selection that master_fx.c's UI hook keeps in its state
 * (Shift + Master + any other button cycles it).
 *
 * The algorithms read the stock nodes' own parameter fields, which the stock
 * Master Reverb / Master Delay pages keep writing (engine setters 0x1587c..,
 * node setters 0xcfe88..):
 *   reverb +0x54 Size 0..1, +0x5c 1-Damping, +0x58 Diffusion 0..1,
 *          +0x6c Predelay (samples, 0..5000), +0x80 bypass flag
 *   delay  +0x28 time (samples, 3500 ms max; Sync is resolved by the firmware),
 *          +0x2c feedback (0..252 = 0..0.99 x 255), +0x2d bypass, +0x2e ping-pong,
 *          +0x30/+0x34/+0x38 loop filter b0, b1, a1 (Q10), +0x20 write index,
 *          +0x14/+0x18/+0x1c three 2^18-sample int16 lines in SDRAM
 *
 * Plate: Dattorro, "Effect Design Part 1" (JAES 1997), delays scaled from
 * 29761 Hz to the Tracker's 44117.6 Hz. Every delay is longer than one block,
 * so it runs stage by stage over whole 128-sample blocks (sequential SDRAM
 * reads and writes), like master_fx.c's EQ.
 * Tape: one read head on the stock delay's own lines (same write index, same
 * lines, so switching to/from the stock delay keeps the echoes), with delay
 * time glide (pitch bends on time changes), wow and flutter, head loss, the
 * stock loop filter, a DC blocker and soft saturation in the record path.
 *
 * Freestanding, no .data/.bss: state lives at FX_STATE in SDRAM (zeroed at
 * boot by master_fx.c's patch_boot), constants in flash.
 */
#include <stdint.h>

#define FS              44117.647f
#define BLOCK           128
#define TWO_PI_F        6.28318531f

/* ---------- firmware ---------- */
#define FW_RECEIVE_WRITABLE 0x000CE591u   /* (this, index) -> block or 0 */
#define FW_RECEIVE_READONLY 0x000CE579u
#define FW_ALLOCATE         0x000CE42Du   /* () -> block or 0 */
#define FW_RELEASE          0x000CE4D9u
#define FW_TRANSMIT         0x000CE531u   /* (this, block, output) */
#define FW_REVERB_UPDATE    0x000CFDC5u   /* stock update() */
#define FW_DELAY_UPDATE     0x000D037Du
#define FW_REVERB_RESET     0x000CFE25u   /* zero its lines and state */

typedef struct { uint32_t header; int16_t data[BLOCK]; } block_t;
typedef block_t *(*receive_fn)(void *self, uint32_t index);
typedef block_t *(*allocate_fn)(void);
typedef void (*release_fn)(block_t *block);
typedef void (*transmit_fn)(void *self, block_t *block, uint32_t index);
typedef void (*update_fn)(void *self);

#define F_U8(n, o)  (*(volatile uint8_t *)((uint8_t *)(n) + (o)))
#define F_U32(n, o) (*(volatile uint32_t *)((uint8_t *)(n) + (o)))
#define F_S32(n, o) (*(volatile int32_t *)((uint8_t *)(n) + (o)))
#define F_F32(n, o) (*(volatile float *)((uint8_t *)(n) + (o)))
#define F_PTR(n, o) (*(int16_t *volatile *)((uint8_t *)(n) + (o)))

/* ---------- selection (master_fx.c state, +20) ---------- */
#define PATCH_STATE     0x70FE0000u
#define FX_SELECT       (*(volatile uint32_t *)(PATCH_STATE + 20))
#define SEL_STOCK_REVERB 1u
#define SEL_STOCK_DELAY  2u

/* ---------- plate geometry ---------- */
#define K_RATE          (44117.647f / 29761.0f)
#define D(x)            (2u * (uint32_t)((x) * K_RATE * 0.5f + 0.5f))   /* even: pairs */
#define EXCURSION       24                     /* 16 at 29.7 kHz, peak samples */
#define MOD_HZ          0.9f

enum { L_PRE, L_AP1, L_AP2, L_AP3, L_AP4, L_LMOD, L_LD1, L_LAP, L_LD2,
       L_RMOD, L_RD1, L_RAP, L_RD2, N_LINES };
/* ring sizes (powers of two, >= longest read + block) and offsets in floats */
#define SZ_PRE   8192u
#define SZ_AP1    256u
#define SZ_AP2    256u
#define SZ_AP3   1024u
#define SZ_AP4    512u
#define SZ_LMOD  1024u
#define SZ_LD1   8192u
#define SZ_LAP   4096u
#define SZ_LD2   8192u
#define SZ_RMOD  2048u
#define SZ_RD1   8192u
#define SZ_RAP   4096u
#define SZ_RD2   8192u
#define OFF_PRE  0u
#define OFF_AP1  (OFF_PRE + SZ_PRE)
#define OFF_AP2  (OFF_AP1 + SZ_AP1)
#define OFF_AP3  (OFF_AP2 + SZ_AP2)
#define OFF_AP4  (OFF_AP3 + SZ_AP3)
#define OFF_LMOD (OFF_AP4 + SZ_AP4)
#define OFF_LD1  (OFF_LMOD + SZ_LMOD)
#define OFF_LAP  (OFF_LD1 + SZ_LD1)
#define OFF_LD2  (OFF_LAP + SZ_LAP)
#define OFF_RMOD (OFF_LD2 + SZ_LD2)
#define OFF_RD1  (OFF_RMOD + SZ_RMOD)
#define OFF_RAP  (OFF_RD1 + SZ_RD1)
#define OFF_RD2  (OFF_RAP + SZ_RAP)
#define PLATE_WORDS (OFF_RD2 + SZ_RD2)         /* 54,272 int16 samples, 106 KB */

#define PRE_MAX   5000u                         /* stock Predelay range */
#define OUT_GAIN  0.6f                          /* Dattorro's tap gain */
#define WET_GAIN  0.176f                        /* matches the stock reverb's level (m18) */
#define CLEAR_PER_BLOCK 8192u                   /* samples zeroed per block */
#define LINE_SCALE 16384.0f                     /* int16 lines: 1.0 -> 16384, +/-2 range */
#define IDLE_BLOCKS 16u

/* ---------- state ---------- */
#define FX_STATE        0x70F20000u             /* free SDRAM (see README) */
#define FX_MAGIC        0x4B4E4246u             /* 'FBNK' */
#define PLATE_MEM       ((int16_t *)(FX_STATE + 0x1000))
/* Internal SRAM for the short lines (input, modulated and tank all-passes):
 * 26 KB from the firmware's heap, allocated once by patch_boot (master_fx.c)
 * and stored at patch state +28. SDRAM is used if it is 0.
 * Idea credit: OpenTracker (https://github.com/sandlbn/polyend-tracker-re, MIT) keeps its
 * stock-style reverb's input all-passes in an SRAM ring and documents the
 * Tracker's SDRAM as 16-bit. No code was taken from it; the allocation and
 * line layout here are this project's own. */
#define FX_SRAM         (*(int16_t *volatile *)(PATCH_STATE + 28))
#define SRAM_SAMPLES    (SZ_AP1 + SZ_AP2 + SZ_AP3 + SZ_AP4 + SZ_LMOD + SZ_RMOD + SZ_LAP + SZ_RAP)   /* 13312 */

enum { MODE_NONE = 0, MODE_BANK, MODE_STOCK };

/* Real cost per call from the DWT cycle counter (enabled by the firmware at
 * 0x68ac). master_fx.c shows these (fixed offsets) in its pop-up. */
typedef struct {
    uint32_t avg;              /* cycles, running average (1/16) */
    uint32_t peak;             /* largest call in the last ~1 s window */
    uint32_t win_max, win_n;
    uint32_t over;             /* bank calls over the guard in a row */
    uint32_t bank_max;         /* largest bank call since it was selected */
} fx_meter_t;

typedef struct {
    uint32_t magic;            /* +0, zeroed at boot by patch_boot */
    fx_meter_t rev;            /* +4  (master_fx.c reads avg +4, bank_max +24) */
    fx_meter_t dly;            /* +28 (avg +28, bank_max +48) */
    uint32_t guard;            /* +52 bit0 plate, bit1 tape switched off for CPU */
    uint32_t rev_mode;         /* MODE_* currently running */
    uint32_t dly_mode;
    uint32_t clear_pos;        /* plate lines zeroed so far (floats) */
    uint32_t t;                /* plate time, advances by BLOCK */
    uint32_t idle;             /* silent blocks in a row */
    float bw_z, damp_l, damp_r, lfo_phase;
    /* tape */
    uint32_t tape_init;
    float d_cur;               /* glided delay (samples) */
    float wow_phase, flutter_phase, drift_phase;
    float mod;                 /* read-head offset at the end of the last block */
    float fx1[2], fy1[2];      /* stock loop filter, per line */
    float head[2];             /* head loss one-pole */
    float dc[2];               /* DC blocker low-pass */
    uint32_t calls_rev, calls_dly;
} fx_state_t;

_Static_assert(sizeof(fx_state_t) <= 0x1000, "state overlaps the plate lines");
_Static_assert(SRAM_SAMPLES * 2 == 26624, "master_fx.c FX_SRAM_BYTES must match");
_Static_assert(__builtin_offsetof(fx_state_t, rev) == 4 && __builtin_offsetof(fx_state_t, dly) == 28
               && __builtin_offsetof(fx_state_t, guard) == 52, "master_fx.c reads these offsets");

#define DWT_CYCCNT      (*(volatile uint32_t *)0xE0001004u)
#define BLOCK_CYCLES    522240u                 /* 180 MHz x 128 / 44117.6 Hz */
#define REV_GUARD       150000u                 /* bank reverb over this ... */
#define DLY_GUARD       80000u                  /* ... or bank delay over this ... */
#define GUARD_BLOCKS    4u                      /* ... this many blocks running -> stock */
#define WINDOW_BLOCKS   345u                    /* ~1 s */

static void meter(fx_state_t *s, fx_meter_t *m, uint32_t cycles, int bank, uint32_t limit, uint32_t bit)
{
    m->avg = m->avg - (m->avg >> 4) + (cycles >> 4);
    if (cycles > m->win_max) m->win_max = cycles;
    if (++m->win_n >= WINDOW_BLOCKS) { m->peak = m->win_max; m->win_max = 0; m->win_n = 0; }
    if (bank && cycles > m->bank_max) m->bank_max = cycles;
    m->over = (bank && cycles > limit) ? m->over + 1 : 0;
    if (m->over >= GUARD_BLOCKS) {                 /* protect the UI: back to stock */
        s->guard |= bit;                           /* until the selector is pressed again */
        m->over = 0;
    }
}

/* ---------- math ---------- */

static inline float clampf(float x, float lo, float hi) { return x < lo ? lo : (x > hi ? hi : x); }

static float sin_poly(float x)               /* |x| <= pi/2 */
{
    float x2 = x * x;
    return x * (1.0f + x2 * (-1.0f / 6 + x2 * (1.0f / 120 + x2 * (-1.0f / 5040
                + x2 * (1.0f / 362880)))));
}

static float sin_turns(float p)              /* sin(2 pi p), p in [0, 1) */
{
    float x = p * 4.0f;                      /* quarter turns */
    if (x < 1.0f) return sin_poly(x * 1.57079633f);
    if (x < 3.0f) return sin_poly((2.0f - x) * 1.57079633f);
    return sin_poly((x - 4.0f) * 1.57079633f);
}

static inline float wrap1(float p) { return p >= 1.0f ? p - 1.0f : p; }

static inline int32_t to_int_round_sat(float x)
{
    union { float f; int32_t i; } u = { __builtin_arm_vcvtr_f(x, 0) };
    return u.i;
}

static inline int16_t to_s16(float x)        /* x in sample units */
{
    int32_t v = to_int_round_sat(x);
    return (int16_t)(v > 32767 ? 32767 : (v < -32768 ? -32768 : v));
}

/* Tape saturation: cubic soft clip, no divide (the Pade tanh cost a VDIV,
 * 14 cycles, per sample per line). Unity gain at 0, smooth to +/-1 at 1.5. */
static inline float soft_sat(float x)
{
    if (x > 1.5f) return 1.0f;
    if (x < -1.5f) return -1.0f;
    return x - (4.0f / 27.0f) * x * x * x;
}

/* ---------- plate building blocks (block-wise, all delays >= BLOCK) ----------
 *
 * Lines are int16 in SDRAM, like the stock reverb's: on hardware every SDRAM
 * access is slow (the float version cost over 150k cycles a block), so each
 * 32-bit access moves two samples. Every delay, tap and block position is even
 * and every ring size is a power of two, so all accesses are aligned pairs.
 */

typedef uint32_t __attribute__((may_alias)) pair_t;

static int16_t *line(uint32_t off)
{
    int16_t *sram = FX_SRAM;
    if (sram) {
        switch (off) {
        case OFF_AP1:  return sram;
        case OFF_AP2:  return sram + SZ_AP1;
        case OFF_AP3:  return sram + SZ_AP1 + SZ_AP2;
        case OFF_AP4:  return sram + SZ_AP1 + SZ_AP2 + SZ_AP3;
        case OFF_LMOD: return sram + SZ_AP1 + SZ_AP2 + SZ_AP3 + SZ_AP4;
        case OFF_RMOD: return sram + SZ_AP1 + SZ_AP2 + SZ_AP3 + SZ_AP4 + SZ_LMOD;
        case OFF_LAP:  return sram + SZ_AP1 + SZ_AP2 + SZ_AP3 + SZ_AP4 + SZ_LMOD + SZ_RMOD;
        case OFF_RAP:  return sram + SZ_AP1 + SZ_AP2 + SZ_AP3 + SZ_AP4 + SZ_LMOD + SZ_RMOD + SZ_LAP;
        }
    }
    return PLATE_MEM + off;
}

static inline uint32_t pack(float a, float b)
{
    int32_t x = __builtin_arm_ssat(to_int_round_sat(a * LINE_SCALE), 16);
    int32_t y = __builtin_arm_ssat(to_int_round_sat(b * LINE_SCALE), 16);
    return (uint32_t)(x & 0xFFFF) | ((uint32_t)y << 16);
}

/* out[0..n) = line[pos..pos+n) (ring), pos and n even */
static __attribute__((noinline)) void load(uint32_t off, uint32_t mask, uint32_t pos, uint32_t n, float *out)
{
    const int16_t *b = line(off);
    const float k = 1.0f / LINE_SCALE;
    pos &= mask;
    while (n) {
        uint32_t run = mask + 1 - pos;
        if (run > n) run = n;
        const pair_t *w = (const pair_t *)(b + pos);
        for (uint32_t i = 0; i < run / 2; i++) {
            uint32_t v = w[i];
            out[2 * i] = (float)(int16_t)v * k;
            out[2 * i + 1] = (float)(int16_t)(v >> 16) * k;
        }
        out += run; n -= run; pos = (pos + run) & mask;
    }
}

/* line[pos..pos+n) = in[0..n) (saturating), pos and n even */
static __attribute__((noinline)) void store(uint32_t off, uint32_t mask, uint32_t pos, uint32_t n, const float *in)
{
    int16_t *b = line(off);
    pos &= mask;
    while (n) {
        uint32_t run = mask + 1 - pos;
        if (run > n) run = n;
        pair_t *w = (pair_t *)(b + pos);
        for (uint32_t i = 0; i < run / 2; i++) w[i] = pack(in[2 * i], in[2 * i + 1]);
        in += run; n -= run; pos = (pos + run) & mask;
    }
}

/* out[i] = line[t + i - d] */
static inline void tap(uint32_t off, uint32_t mask, uint32_t t, uint32_t d, float *out)
{
    load(off, mask, t - d, BLOCK, out);
}

/* acc[i] += g * line[t + i - d] */
static __attribute__((noinline)) void tap_add(uint32_t off, uint32_t mask, uint32_t t, uint32_t d, float g, float *acc)
{
    float x[BLOCK];
    load(off, mask, t - d, BLOCK, x);
    for (int i = 0; i < BLOCK; i++) acc[i] += g * x[i];
}

static inline void put(uint32_t off, uint32_t mask, uint32_t t, const float *in)
{
    store(off, mask, t, BLOCK, in);
}

/* Lattice allpass in place: w = x + g*w[n-d]; y = w[n-d] - g*w.
 * d >= BLOCK, so the block written (t..) never overlaps the block read. */
static __attribute__((noinline)) void allpass(uint32_t off, uint32_t mask, uint32_t t, uint32_t d, float g, float *x)
{
    float wd[BLOCK];
    load(off, mask, t - d, BLOCK, wd);
    for (int i = 0; i < BLOCK; i++) {
        float w = x[i] + g * wd[i];
        x[i] = wd[i] - g * w;
        wd[i] = w;
    }
    store(off, mask, t, BLOCK, wd);
}

/* Allpass with a modulated (fractional) delay d + exc * lfo, lfo linear over
 * the block from l0 to l1. All reads are before the block (d - exc > BLOCK). */
static __attribute__((noinline)) void allpass_mod(uint32_t off, uint32_t mask, uint32_t t, uint32_t d,
                        float g, float l0, float l1, float *x)
{
    enum { SPAN = BLOCK + 2 * EXCURSION + 4 };     /* even */
    float win[SPAN];
    load(off, mask, t - d - EXCURSION - 2, SPAN, win);   /* window[0] = line[t - d - exc - 2] */
    float w_out[BLOCK];
    float l = l0, dl = (l1 - l0) * (1.0f / BLOCK);
    for (int i = 0; i < BLOCK; i++) {
        /* read position relative to window: (t+i - (d + exc*l)) - start */
        float pos = (float)(i + EXCURSION + 2) - (float)EXCURSION * l;
        int32_t ip = (int32_t)pos;
        float f = pos - (float)ip;
        float wd = win[ip] + f * (win[ip + 1] - win[ip]);
        float w = x[i] + g * wd;
        x[i] = wd - g * w;
        w_out[i] = w;
        l += dl;
    }
    store(off, mask, t, BLOCK, w_out);
}

static __attribute__((noinline)) void plate(fx_state_t *s, void *node, int16_t *io_l, int16_t *io_r)
{
    const uint32_t t = s->t;
    float size = clampf(F_F32(node, 0x54), 0.0f, 1.0f);
    float damp = clampf(1.0f - F_F32(node, 0x5c), 0.0f, 1.0f);
    float diff = clampf(F_F32(node, 0x58), 0.0f, 1.0f);
    uint32_t pre = F_U32(node, 0x6c);
    if (pre > PRE_MAX) pre = PRE_MAX;
    pre &= ~1u;                                               /* aligned pairs */

    const float decay = clampf(0.30f + 0.68f * size, 0.0f, 0.97f);
    const float dd1 = 0.70f;                                  /* decay diffusion 1 */
    const float dd2 = clampf(decay + 0.15f, 0.25f, 0.50f);   /* Dattorro's rule */
    const float id1 = clampf(1.10f * diff, 0.0f, 0.85f);      /* 0.75 at the default 0.68 */
    const float id2 = clampf(0.92f * diff, 0.0f, 0.75f);      /* 0.625 */
    const float dmp = 0.85f * damp;
    const float bw = 0.9995f;

    float a[BLOCK], l[BLOCK], r[BLOCK];
    for (int i = 0; i < BLOCK; i++)
        a[i] = ((float)io_l[i] + (float)io_r[i]) * (0.5f / 32768.0f);

    /* predelay: write first, so any delay 0..PRE_MAX works */
    put(OFF_PRE, SZ_PRE - 1, t, a);
    tap(OFF_PRE, SZ_PRE - 1, t, pre, a);

    float z = s->bw_z;                                        /* input bandwidth */
    for (int i = 0; i < BLOCK; i++) { z += bw * (a[i] - z); a[i] = z; }
    s->bw_z = z;

    allpass(OFF_AP1, SZ_AP1 - 1, t, D(142), id1, a);
    allpass(OFF_AP2, SZ_AP2 - 1, t, D(107), id1, a);
    allpass(OFF_AP3, SZ_AP3 - 1, t, D(379), id2, a);
    allpass(OFF_AP4, SZ_AP4 - 1, t, D(277), id2, a);

    /* tank inputs: diffused input + decay * the other side's last delay */
    tap(OFF_RD2, SZ_RD2 - 1, t, D(3163), l);
    tap(OFF_LD2, SZ_LD2 - 1, t, D(3720), r);
    for (int i = 0; i < BLOCK; i++) {
        l[i] = a[i] + decay * l[i];
        r[i] = a[i] + decay * r[i];
    }

    float p0 = s->lfo_phase, p1 = wrap1(p0 + MOD_HZ * BLOCK / FS);
    s->lfo_phase = p1;
    float q0 = wrap1(p0 + 0.25f), q1 = wrap1(p1 + 0.25f);

    /* left half */
    allpass_mod(OFF_LMOD, SZ_LMOD - 1, t, D(672), -dd1, sin_turns(p0), sin_turns(p1), l);
    {
        float d1[BLOCK];
        tap(OFF_LD1, SZ_LD1 - 1, t, D(4453), d1);
        put(OFF_LD1, SZ_LD1 - 1, t, l);
        float y = s->damp_l;
        for (int i = 0; i < BLOCK; i++) { y = d1[i] + dmp * (y - d1[i]); l[i] = decay * y; }
        s->damp_l = y;
    }
    allpass(OFF_LAP, SZ_LAP - 1, t, D(1800), dd2, l);
    put(OFF_LD2, SZ_LD2 - 1, t, l);

    /* right half */
    allpass_mod(OFF_RMOD, SZ_RMOD - 1, t, D(908), -dd1, sin_turns(q0), sin_turns(q1), r);
    {
        float d1[BLOCK];
        tap(OFF_RD1, SZ_RD1 - 1, t, D(4217), d1);
        put(OFF_RD1, SZ_RD1 - 1, t, r);
        float y = s->damp_r;
        for (int i = 0; i < BLOCK; i++) { y = d1[i] + dmp * (y - d1[i]); r[i] = decay * y; }
        s->damp_r = y;
    }
    allpass(OFF_RAP, SZ_RAP - 1, t, D(2656), dd2, r);
    put(OFF_RD2, SZ_RD2 - 1, t, r);

    /* output taps: the first four of each side of Dattorro's table 2 (each
     * tap is an SDRAM block read); every tap is >= BLOCK, so all of them read
     * samples written in earlier blocks */
    const float g = OUT_GAIN;
    for (int i = 0; i < BLOCK; i++) { l[i] = 0; r[i] = 0; }
    tap_add(OFF_RD1, SZ_RD1 - 1, t, D(266),  g, l);
    tap_add(OFF_RD1, SZ_RD1 - 1, t, D(2974), g, l);
    tap_add(OFF_RAP, SZ_RAP - 1, t, D(1913), -g, l);
    tap_add(OFF_RD2, SZ_RD2 - 1, t, D(1996), g, l);

    tap_add(OFF_LD1, SZ_LD1 - 1, t, D(353),  g, r);
    tap_add(OFF_LD1, SZ_LD1 - 1, t, D(3627), g, r);
    tap_add(OFF_LAP, SZ_LAP - 1, t, D(1228), -g, r);
    tap_add(OFF_LD2, SZ_LD2 - 1, t, D(2673), g, r);

    const float out = WET_GAIN * 32768.0f;
    for (int i = 0; i < BLOCK; i++) {
        io_l[i] = to_s16(l[i] * out);
        io_r[i] = to_s16(r[i] * out);
    }
    s->t = t + BLOCK;
}

static fx_state_t *state(void)
{
    fx_state_t *s = (fx_state_t *)FX_STATE;
    if (s->magic != FX_MAGIC) {
        uint32_t *w = (uint32_t *)s;
        for (uint32_t i = 0; i < sizeof(*s) / 4; i++) w[i] = 0;
        s->magic = FX_MAGIC;
    }
    return s;
}

static int all_zero(const int16_t *x)
{
    const uint32_t *w = (const uint32_t *)x;
    for (int i = 0; i < BLOCK / 2; i++) if (w[i]) return 0;
    return 1;
}

/* |x| <= 8 (-72 dBFS) everywhere: int16 lines can keep a small residue
 * circulating (limit cycles), so the tail never reaches exact zero */
static int near_zero(const int16_t *x)
{
    for (int i = 0; i < BLOCK; i++) if (x[i] > 8 || x[i] < -8) return 0;
    return 1;
}

/* ---------- reverb node ---------- */

static void reverb_work(fx_state_t *s, void *node)
{
    s->calls_rev++;
    uint32_t want = ((FX_SELECT | s->guard) & SEL_STOCK_REVERB) ? MODE_STOCK : MODE_BANK;
    if (want != s->rev_mode) {
        if (want == MODE_STOCK && s->rev_mode == MODE_BANK)
            ((update_fn)FW_REVERB_RESET)(node);              /* no stale stock tail */
        if (want == MODE_BANK) { s->clear_pos = 0; s->idle = 0; s->rev.bank_max = 0; }
        s->rev_mode = want;
    }
    if (want == MODE_STOCK) { ((update_fn)FW_REVERB_UPDATE)(node); return; }
    if (F_U8(node, 0x80)) return;                            /* as stock */

    block_t *bl = ((receive_fn)FW_RECEIVE_WRITABLE)(node, 0);
    if (!bl) return;
    block_t *br = ((receive_fn)FW_RECEIVE_WRITABLE)(node, 1);
    if (!br) { ((release_fn)FW_RELEASE)(bl); return; }

    if (s->clear_pos < PLATE_WORDS) {                        /* zero lines, stay silent */
        uint32_t end = s->clear_pos + CLEAR_PER_BLOCK;
        if (end > PLATE_WORDS) end = PLATE_WORDS;
        pair_t *m = (pair_t *)PLATE_MEM;
        for (uint32_t i = s->clear_pos / 2; i < end / 2; i++) m[i] = 0;
        pair_t *sm = (pair_t *)FX_SRAM;
        if (sm && s->clear_pos == 0)                       /* first clearing block */
            for (uint32_t i = 0; i < SRAM_SAMPLES / 2; i++) sm[i] = 0;
        s->clear_pos = end;
        s->bw_z = s->damp_l = s->damp_r = 0.0f;
        ((release_fn)FW_RELEASE)(bl);
        ((release_fn)FW_RELEASE)(br);
        return;
    }

    /* idle: input silent and tail below 1 LSB for a while -> skip the work */
    int silent_in = all_zero(bl->data) && all_zero(br->data);
    if (silent_in && s->idle >= IDLE_BLOCKS) {
        ((release_fn)FW_RELEASE)(bl);
        ((release_fn)FW_RELEASE)(br);
        return;
    }
    plate(s, node, bl->data, br->data);
    s->idle = (silent_in && near_zero(bl->data) && near_zero(br->data)) ? s->idle + 1 : 0;

    ((transmit_fn)FW_TRANSMIT)(node, bl, 0);
    ((transmit_fn)FW_TRANSMIT)(node, br, 1);
    ((release_fn)FW_RELEASE)(bl);
    ((release_fn)FW_RELEASE)(br);
}

/* ---------- delay node: tape ---------- */

#define LINE_MASK       0x3FFFFu                /* 2^18 samples, as stock */
#define GLIDE           (1.0f / 4000.0f)        /* per sample, ~90 ms */
#define WOW_HZ          0.55f
#define FLUTTER_HZ      6.3f
#define DRIFT_HZ        0.13f
#define WOW_DEPTH       0.0012f                 /* x delay, ~3.5 cents */
#define FLUTTER_DEPTH   0.00006f
#define HEAD_COEF       0.70f                   /* one-pole, ~8.5 kHz */
#define DC_COEF         0.004f                  /* one-pole, ~28 Hz */
#define FB_SCALE        1.03f                   /* max feedback just over unity */
#define MAX_SLEW        0.25f                   /* tape speed change, +/-25 % pitch */
#define MIN_DELAY       4.0f

/* Both neighbours in one 32-bit SDRAM access. The address is often odd-halfword
 * aligned: Cortex-M4 LDR handles unaligned normal memory (braids_voice.cc's
 * load_pair relies on the same on hardware); inline asm stops the compiler
 * splitting it into two LDRSH. */
static inline uint32_t load_pair_u(const int16_t *p)
{
    uint32_t v;
    __asm__ volatile("ldr %0, [%1]" : "=r"(v) : "r"(p) : "memory");
    return v;
}

static inline float read_frac(const int16_t *b, uint32_t w, float d)
{
    /* sample at time (w - d); w is the next write position */
    float pos = (float)w - d + (float)(LINE_MASK + 1);   /* keep positive */
    uint32_t ip = (uint32_t)pos;
    float f = pos - (float)ip;
    ip &= LINE_MASK;
    float x0, x1;
    if (ip != LINE_MASK) {
        uint32_t v = load_pair_u(b + ip);
        x0 = (float)(int16_t)v; x1 = (float)(int16_t)(v >> 16);
    } else {
        x0 = b[LINE_MASK]; x1 = b[0];
    }
    return x0 + f * (x1 - x0);
}

static __attribute__((noinline)) void tape(fx_state_t *s, void *node, const int16_t *in, int16_t *out0, int16_t *out1)
{
    int16_t *line0 = F_PTR(node, 0x14), *line1 = F_PTR(node, 0x18), *line2 = F_PTR(node, 0x1c);
    uint32_t w = F_U32(node, 0x20);
    float target = (float)F_U32(node, 0x28);
    if (target < MIN_DELAY) target = MIN_DELAY;
    const float fb = (float)F_U8(node, 0x2c) * (FB_SCALE / 255.0f);
    const int pingpong = F_U8(node, 0x2e) != 0;
    const float b0 = (float)F_S32(node, 0x30) * (1.0f / 1024.0f);
    const float b1 = (float)F_S32(node, 0x34) * (1.0f / 1024.0f);
    const float a1 = (float)F_S32(node, 0x38) * (1.0f / 1024.0f);

    if (!s->tape_init) {                                     /* start on the stock time */
        s->d_cur = target;
        s->mod = 0.0f;
        for (int c = 0; c < 2; c++) { s->fx1[c] = s->fy1[c] = s->head[c] = s->dc[c] = 0; }
        s->tape_init = 1;
    }

    /* modulation, linear across the block */
    float d = s->d_cur;
    float wp0 = s->wow_phase, fp0 = s->flutter_phase, dp0 = s->drift_phase;
    float wp1 = wrap1(wp0 + WOW_HZ * BLOCK / FS);
    float fp1 = wrap1(fp0 + FLUTTER_HZ * BLOCK / FS);
    float dp1 = wrap1(dp0 + DRIFT_HZ * BLOCK / FS);
    s->wow_phase = wp1; s->flutter_phase = fp1; s->drift_phase = dp1;
    (void)wp0; (void)fp0; (void)dp0;
    /* depth follows the glided time; the block starts where the last one
     * ended, so the head never jumps */
    float depth_w = WOW_DEPTH * d + 0.5f, depth_f = FLUTTER_DEPTH * d + 0.1f;
    float m1 = depth_w * (sin_turns(wp1) + 0.4f * sin_turns(dp1)) + depth_f * sin_turns(fp1);
    float m = s->mod, dm = (m1 - m) * (1.0f / BLOCK);
    s->mod = m1;

    float fx1a = s->fx1[0], fy1a = s->fy1[0], ha = s->head[0], dca = s->dc[0];
    float fx1b = s->fx1[1], fy1b = s->fy1[1], hb = s->head[1], dcb = s->dc[1];

    if (!pingpong) {
        for (int i = 0; i < BLOCK; i++) {
            d += clampf((target - d) * GLIDE, -MAX_SLEW, MAX_SLEW);
            float x = read_frac(line2, w, d + m);
            float y = b0 * x + b1 * fx1a - a1 * fy1a;         /* stock loop filter */
            fx1a = x; fy1a = y;
            ha += HEAD_COEF * (y - ha);
            dca += DC_COEF * (ha - dca);
            float rec = (float)in[i] + fb * (ha - dca);
            rec = 32767.0f * soft_sat(rec * (1.0f / 32767.0f));
            line2[w] = to_s16(rec);
            int16_t o = to_s16(ha);
            out0[i] = o; out1[i] = o;
            w = (w + 1) & LINE_MASK;
            m += dm;
        }
    } else {
        /* as stock: input into line 1 (output 1), line 0 hears line 1's echo */
        for (int i = 0; i < BLOCK; i++) {
            d += clampf((target - d) * GLIDE, -MAX_SLEW, MAX_SLEW);
            float x0 = read_frac(line0, w, d + m);
            float x1 = read_frac(line1, w, d - 0.7f * m);    /* heads drift apart a little */
            float y0 = b0 * x0 + b1 * fx1a - a1 * fy1a; fx1a = x0; fy1a = y0;
            float y1 = b0 * x1 + b1 * fx1b - a1 * fy1b; fx1b = x1; fy1b = y1;
            ha += HEAD_COEF * (y0 - ha);
            hb += HEAD_COEF * (y1 - hb);
            dca += DC_COEF * (ha - dca);
            dcb += DC_COEF * (hb - dcb);
            float rec1 = (float)in[i] + fb * (ha - dca);
            float rec0 = fb * (hb - dcb);
            line1[w] = to_s16(32767.0f * soft_sat(rec1 * (1.0f / 32767.0f)));
            line0[w] = to_s16(32767.0f * soft_sat(rec0 * (1.0f / 32767.0f)));
            out0[i] = to_s16(ha); out1[i] = to_s16(hb);
            w = (w + 1) & LINE_MASK;
            m += dm;
        }
    }
    s->fx1[0] = fx1a; s->fy1[0] = fy1a; s->head[0] = ha; s->dc[0] = dca;
    s->fx1[1] = fx1b; s->fy1[1] = fy1b; s->head[1] = hb; s->dc[1] = dcb;
    s->d_cur = d;
    F_U32(node, 0x20) = w;
}

static const int16_t zeros[BLOCK];

static void delay_work(fx_state_t *s, void *node)
{
    s->calls_dly++;
    uint32_t want = ((FX_SELECT | s->guard) & SEL_STOCK_DELAY) ? MODE_STOCK : MODE_BANK;
    if (want != s->dly_mode) {
        if (want == MODE_BANK) { s->tape_init = 0; s->dly.bank_max = 0; }   /* pick up the stock time */
        s->dly_mode = want;
    }
    if (want == MODE_STOCK) { ((update_fn)FW_DELAY_UPDATE)(node); return; }

    block_t *in = ((receive_fn)FW_RECEIVE_READONLY)(node, 0);
    block_t *o0 = ((allocate_fn)FW_ALLOCATE)();
    block_t *o1 = ((allocate_fn)FW_ALLOCATE)();
    if (o0 && o1 && !F_U8(node, 0x2d)) {                    /* +0x2d: off, as stock */
        tape(s, node, in ? in->data : zeros, o0->data, o1->data);
        ((transmit_fn)FW_TRANSMIT)(node, o0, 0);
        ((transmit_fn)FW_TRANSMIT)(node, o1, 1);
    }
    if (o0) ((release_fn)FW_RELEASE)(o0);
    if (o1) ((release_fn)FW_RELEASE)(o1);
    if (in) ((release_fn)FW_RELEASE)(in);
}

void reverb_update(void *node)
{
    fx_state_t *s = state();
    uint32_t t0 = DWT_CYCCNT;
    reverb_work(s, node);
    meter(s, &s->rev, DWT_CYCCNT - t0, s->rev_mode == MODE_BANK && s->clear_pos >= PLATE_WORDS,
          REV_GUARD, SEL_STOCK_REVERB);
}

void delay_update(void *node)
{
    fx_state_t *s = state();
    uint32_t t0 = DWT_CYCCNT;
    delay_work(s, node);
    meter(s, &s->dly, DWT_CYCCNT - t0, s->dly_mode == MODE_BANK, DLY_GUARD, SEL_STOCK_DELAY);
}
