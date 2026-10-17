/*
 * Master EQ + DJ filter patch for Polyend Tracker firmware 1.9.2 (v2).
 *
 * Hooked in place of the master mixer's call to the saturation stage
 * (bl 0xd42f0 at 0x76c6a, class 0xc2b9c). Processes the 32-bit master
 * accumulator (128 frames, one channel per call) and then calls the
 * original saturator, so the result is in both live output and exports.
 *
 *  - 5-band bell EQ at the stock Master EQ centres (115/330/990/3k/9.9k Hz),
 *    gains from the firmware's Master EQ settings (5 floats at 0x20004c60,
 *    -1..+1 -> +/-12 dB).
 *  - DJ filter: one value in -1..+1 (state->dj). <0 sweeps a low-pass down,
 *    >0 sweeps a high-pass up, 0 = off.
 *  - DJ control: hold Shift + Master and turn the jog wheel; releasing either
 *    button snaps the filter back to centre (via the usual glide). The
 *    position is shown in the firmware's own pop-up ("DJ Low-pass 40%").
 *  - FX bank selection (fxbank/fx_bank.c): hold Shift + Master and press any
 *    other button to step through plate/stock reverb x tape/stock delay; the
 *    choice is shown in the pop-up. That press and its release are swallowed.
 *
 * v2 changes (vs master_fx_v1.c):
 *  - Topology-preserving-transform state-variable filters (Zavalishin/Simper):
 *    stable under per-sample coefficient modulation; a bell band's g is
 *    constant, so gain changes need no trig.
 *  - Smoothing: one-pole per-block parameter glide (~24 ms), per-sample linear
 *    coefficient ramps inside each block, and a DJ wet/dry crossfade around
 *    the centre so low-pass <-> high-pass changes pass through the dry signal.
 *  - Stage-major processing on a float block with coefficients/state in
 *    registers; separate steady-state and ramping loops.
 *
 * Freestanding: no libc/libm. Built with clang for thumbv7em/FPv4-SP and linked
 * by link.py at a fixed address (see build.sh).
 */
#include <stdint.h>

#define FS              44100.0f
#define PI_F            3.14159265f
#define HALF_PI_F       1.57079633f
#define BLOCK           128
#define BANDS           5
#define DJ_STAGE        BANDS
#define STAGES          (BANDS + 1)
#define EQ_RANGE_DB     12.0f
#define EQ_Q            0.9f
#define DJ_Q            0.8f
#define DJ_FADE         0.05f          /* |knob| below this crossfades to dry */
#define SMOOTH          0.12f          /* per-block glide (~24 ms time constant) */
#define SNAP            1e-4f

#define FW_EQ_GAINS     ((volatile const float *)0x20004C60)
#define FW_MASTER_LEFT  0x1FFFACC8u    /* master 4-in mixer, channel 0 */
#define FW_SATURATE     0x000D42F1u    /* original stage (Thumb) */
#define PATCH_STATE     0x70FE0000u
#define INIT_MAGIC      0x32584650u    /* 'PFX2' */
#define STATE_MAGIC     0x58464D50u    /* 'PMFX' */

/* Boot: SDRAM is not cleared at power-on, so the state (held buttons, DJ,
 * enabled) starts as garbage. The patch takes over app setup's only call to
 * engine init (bl 0x1611c at 0xf3fa): by then board init (0x721c -> 0x7540)
 * has brought up SDRAM (0x68e0 -> 0xea4) and registered the input callbacks,
 * and the audio engine has not started yet. */
#define FW_ENGINE_INIT  0x0001611Du

/* UI hooks. The board's input callbacks are tiny stubs that tail-branch into
 * the interface object (0x20005e04); the patch redirects those branches:
 *   0xf628 knob    (iface, pot, value) -> FW_KNOB
 *   0xf636 press   (iface, btn, 1)     -> FW_BUTTON
 *   0xf646 release (iface, btn, 0)     -> FW_BUTTON
 * Button numbers are the firmware's own (the Master screen registers its
 * press/release handler on 12; every screen handles 29, and the knob code
 * reads 29 as Shift for value acceleration). */
#define FW_KNOB         0x00028F05u
#define FW_BUTTON       0x00028F0Du
#define BTN_MASTER      12
#define BTN_SHIFT       29
#define JOG_POT         0
#define HELD_MASTER     1u
#define HELD_SHIFT      2u
#define DJ_STEP         0.02f          /* per jog detent, as the Master EQ uses */

/* Firmware pop-up (as used for "No undo data.", "Counting in"):
 * show(popups, unused, text, seconds). It keeps the text *pointer*, so the
 * text lives in patch state; showing again restarts the timer, and the
 * interface update (0x299cc) hides it once the seconds run out. */
#define FW_POPUP        0x0002A71Du
#define FW_POPUPS       0x20005EC4u
#define POPUP_SECONDS   1

/* FX bank state (fxbank/fx_bank.c): its header is zeroed at boot so the
 * bank re-initialises (SDRAM keeps old contents across a reset). */
#define FX_STATE        0x70F20000u
#define FX_STATE_HEADER 64

/* Firmware malloc (newlib, 0x9529c -> _malloc_r 0x952bc; sbrk 0xc9a0 lets the
 * heap grow up to 8 KB below the stack). The FX bank's short delay lines go
 * in internal SRAM, which is much faster than SDRAM; taking the memory from
 * the firmware's own heap at boot means nothing else can ever be given it.
 * Only at boot: malloc is not safe from the audio interrupt. */
#define FW_MALLOC       0x0009529Du
#define FX_SRAM_BYTES   26624u          /* fx_bank.c: SRAM_SAMPLES * 2 */
typedef void *(*malloc_fn)(uint32_t size);

/* Exported for patch/make_ptf.py: proof that patch_boot clears this address. */
const uint32_t patch_fx_state = FX_STATE;

typedef void (*saturate_fn)(int16_t *out, int32_t *acc, uint32_t mode);
typedef uint32_t (*engine_init_fn)(void *engine, void *hal, uint32_t arg);
typedef void (*input_fn)(void *iface, uint32_t id, int32_t value);
typedef void (*popup_fn)(void *popups, uint32_t unused, const char *text, uint32_t seconds);

typedef struct { float a1, a2, g, m0, m1, m2; } svf_t;   /* g = tan(pi f/fs) */
typedef struct { float ic1, ic2; } svf_state_t;

typedef struct {
    uint32_t magic;              /* +0  set by the loader (kept for v1 compat) */
    float dj;                    /* +4  DJ target, -1..+1 (set externally) */
    uint32_t enabled;            /* +8  0 = pass straight to the saturator */
    uint32_t held;               /* +12 HELD_* bits (UI hooks) */
    uint32_t dj_by_ui;           /* +16 DJ moved by Shift+Master+jog */
    uint32_t fx_select;          /* +20 FX bank: bit0 stock reverb, bit1 stock delay */
    uint32_t fx_button;          /* +24 button swallowed by the FX gesture, + 1 */
    void *fx_sram;               /* +28 SRAM for the FX bank's short lines (0: none) */
    uint32_t reserved[1];        /* +32 */
    uint32_t init;               /* +36 INIT_MAGIC once initialised */
    uint32_t calls;              /* +40 */
    float eq_cur[BANDS];         /* smoothed EQ gains, -1..+1 */
    float dj_cur;                /* smoothed DJ value */
    float band_g[BANDS];         /* tan(pi f / fs), constant */
    uint32_t stage_on[STAGES];
    svf_t from[STAGES], to[STAGES];
    float wet_from, wet_to;      /* DJ wet amount ramp */
    svf_state_t z[2][STAGES];
    char popup_text[24];         /* DJ indicator text (pop-up keeps the pointer) */
} patch_state_t;

static const float band_hz[BANDS] = {115.0f, 330.0f, 990.0f, 3000.0f, 9900.0f};

/* ---------- math (freestanding) ---------- */

static float sin_poly(float x)           /* |x| <= pi/2 */
{
    float x2 = x * x;
    return x * (1.0f + x2 * (-1.0f / 6 + x2 * (1.0f / 120 + x2 * (-1.0f / 5040
                + x2 * (1.0f / 362880 + x2 * (-1.0f / 39916800))))));
}

static float tan_0_halfpi(float w)       /* w in (0, pi/2) */
{
    return sin_poly(w) / sin_poly(HALF_PI_F - w);
}

static float exp2_approx(float x)
{
    if (x < -126.0f) return 0.0f;
    int32_t i = (int32_t)x;
    if ((float)i > x) i -= 1;            /* floor */
    float f = x - (float)i;
    float p = 1.0f + f * (0.69314718f + f * (0.24022650f + f * (0.05550411f
              + f * (0.00961813f + f * 0.00133336f))));
    union { float f; uint32_t u; } v = { p };
    v.u += (uint32_t)i << 23;
    return v.f;
}

static float fabs_f(float x) { return x < 0 ? -x : x; }
static float clamp1(float x) { return x > 1.0f ? 1.0f : (x < -1.0f ? -1.0f : x); }

/* ---------- coefficient design (TPT SVF) ---------- */

static void svf_core(svf_t *c, float g, float k)
{
    c->a1 = 1.0f / (1.0f + g * (g + k));
    c->a2 = g * c->a1;
    c->g = g;
}

static void design_bell(svf_t *c, float g, float gain)      /* gain -1..+1 */
{
    float A = exp2_approx(gain * EQ_RANGE_DB * 0.0830482f);  /* 10^(dB/40) */
    float k = 1.0f / (EQ_Q * A);
    svf_core(c, g, k);
    c->m0 = 1.0f;
    c->m1 = k * (A * A - 1.0f);
    c->m2 = 0.0f;
}

static void design_dj(svf_t *c, float v)
{
    float t = fabs_f(v);
    float hz = v < 0 ? 20000.0f * exp2_approx(t * -7.9657843f)  /* 20 kHz -> 80 Hz */
                     : 20.0f * exp2_approx(t * 8.6438562f);      /* 20 Hz -> 8 kHz */
    float k = 1.0f / DJ_Q;
    svf_core(c, tan_0_halfpi(PI_F * hz / FS), k);
    if (v < 0) { c->m0 = 0.0f; c->m1 = 0.0f; c->m2 = 1.0f; }    /* low-pass */
    else       { c->m0 = 1.0f; c->m1 = -k;   c->m2 = -1.0f; }   /* high-pass */
}

static int same(const svf_t *a, const svf_t *b)
{
    return a->a1 == b->a1 && a->a2 == b->a2 && a->g == b->g &&
           a->m0 == b->m0 && a->m1 == b->m1 && a->m2 == b->m2;
}

static float glide(float cur, float target)
{
    float next = cur + (target - cur) * SMOOTH;
    return fabs_f(target - next) < SNAP ? target : next;
}

static void reset_stage(patch_state_t *s, int st)
{
    for (int ch = 0; ch < 2; ch++) { s->z[ch][st].ic1 = 0; s->z[ch][st].ic2 = 0; }
}

/* Once per block (on the left-channel call): glide parameters, set ramps. */
static void update(patch_state_t *s)
{
    for (int b = 0; b < BANDS; b++) {
        float cur = s->eq_cur[b];
        float next = glide(cur, clamp1(FW_EQ_GAINS[b]));
        int on = next != 0.0f || cur != 0.0f;
        s->from[b] = s->to[b];
        if (next != cur)                        /* steady: keep coefficients */
            design_bell(&s->to[b], s->band_g[b], next);
        if (on && !s->stage_on[b]) {            /* bell at 0 dB is identity */
            reset_stage(s, b);
            design_bell(&s->from[b], s->band_g[b], cur);
        }
        s->stage_on[b] = on;
        s->eq_cur[b] = next;
    }

    float cur = s->dj_cur, target = clamp1(s->dj);
    float next = glide(cur, target);
    if ((cur < 0 && next > 0) || (cur > 0 && next < 0))
        next = 0.0f;                            /* cross the centre via dry */
    float wet = fabs_f(next) / DJ_FADE;
    if (wet > 1.0f) wet = 1.0f;
    wet = wet * wet * (3.0f - 2.0f * wet);      /* smoothstep */
    int on = next != 0.0f || cur != 0.0f;
    s->from[DJ_STAGE] = s->to[DJ_STAGE];
    s->wet_from = s->wet_to;
    if (next != 0.0f && next != cur) design_dj(&s->to[DJ_STAGE], next);
    else if (next != 0.0f && !s->stage_on[DJ_STAGE]) design_dj(&s->to[DJ_STAGE], next);
    s->wet_to = wet;
    if (on && !s->stage_on[DJ_STAGE]) {
        reset_stage(s, DJ_STAGE);
        s->from[DJ_STAGE] = s->to[DJ_STAGE];
        s->wet_from = 0.0f;
    }
    s->stage_on[DJ_STAGE] = on;
    s->dj_cur = next;
}

/* ---------- processing ---------- */

#define RAMP_SETUP(f, t)                                                       \
    float a1 = (f)->a1, a2 = (f)->a2, g = (f)->g,                              \
          m0 = (f)->m0, m1 = (f)->m1, m2 = (f)->m2;                            \
    float d1 = 0, d2 = 0, d3 = 0, e0 = 0, e1 = 0, e2 = 0;                      \
    if (ramp) {                                                                \
        const float r = 1.0f / BLOCK;                                          \
        d1 = ((t)->a1 - a1) * r; d2 = ((t)->a2 - a2) * r; d3 = ((t)->g - g) * r;  \
        e0 = ((t)->m0 - m0) * r; e1 = ((t)->m1 - m1) * r; e2 = ((t)->m2 - m2) * r; \
    }

#define SVF_TICK(x)                                                            \
    float v3 = (x) - ic2;                                                      \
    float v1 = a1 * ic1 + a2 * v3;                                             \
    float v2 = ic2 + g * v1;        /* == ic2 + a2*ic1 + g*a2*v3 */            \
    ic1 = 2.0f * v1 - ic1;                                                     \
    ic2 = 2.0f * v2 - ic2;

/* Separate, non-inlined steady and ramping loops so the compiler cannot merge
 * them (steady state must not pay for coefficient increments). */
static __attribute__((noinline))
void bell_steady(float *buf, const svf_t *c, svf_state_t *z)
{
    const float a1 = c->a1, a2 = c->a2, g = c->g, m1 = c->m1;
    float ic1 = z->ic1, ic2 = z->ic2;
    for (int i = 0; i < BLOCK; i++) {
        float x = buf[i];
        SVF_TICK(x)
        (void)v2;
        buf[i] = x + m1 * v1;
    }
    z->ic1 = ic1; z->ic2 = ic2;
}

static __attribute__((noinline))
void bell_ramp(float *buf, const svf_t *f, const svf_t *t, svf_state_t *z)
{
    const int ramp = 1;
    RAMP_SETUP(f, t)
    (void)m0; (void)m2; (void)e0; (void)e2;
    float ic1 = z->ic1, ic2 = z->ic2;
    for (int i = 0; i < BLOCK; i++) {
        a1 += d1; a2 += d2; g += d3; m1 += e1;
        float x = buf[i];
        SVF_TICK(x)
        (void)v2;
        buf[i] = x + m1 * v1;
    }
    z->ic1 = ic1; z->ic2 = ic2;
}

static __attribute__((noinline))
void dj_steady(float *buf, const svf_t *c, svf_state_t *z, float w)
{
    const float a1 = c->a1, a2 = c->a2, g = c->g, m0 = c->m0, m1 = c->m1, m2 = c->m2;
    float ic1 = z->ic1, ic2 = z->ic2;
    for (int i = 0; i < BLOCK; i++) {
        float x = buf[i];
        SVF_TICK(x)
        float y = m0 * x + m1 * v1 + m2 * v2;
        buf[i] = x + w * (y - x);
    }
    z->ic1 = ic1; z->ic2 = ic2;
}

static __attribute__((noinline))
void dj_ramp(float *buf, const svf_t *f, const svf_t *t, svf_state_t *z, float w, float w_to)
{
    const int ramp = 1;
    RAMP_SETUP(f, t)
    float dw = (w_to - w) * (1.0f / BLOCK);
    float ic1 = z->ic1, ic2 = z->ic2;
    for (int i = 0; i < BLOCK; i++) {
        a1 += d1; a2 += d2; g += d3; m0 += e0; m1 += e1; m2 += e2; w += dw;
        float x = buf[i];
        SVF_TICK(x)
        float y = m0 * x + m1 * v1 + m2 * v2;
        buf[i] = x + w * (y - x);
    }
    z->ic1 = ic1; z->ic2 = ic2;
}

/* Round to nearest and saturate to int32 in one instruction (VCVTR).
 * build.sh disables LLVM's Cortex-M4 alignment-hazard recognizer: with it on,
 * Apple clang 17 placed a padding nop inside an IT block in this file, making a
 * conditional move unconditional. build.sh also rejects any such output. */
static inline int32_t to_int_round_sat(float x)
{
    union { float f; int32_t i; } u = { __builtin_arm_vcvtr_f(x, 0) };
    return u.i;
}

static void process(patch_state_t *s, int32_t *acc, int ch)
{
    float buf[BLOCK];
    for (int i = 0; i < BLOCK; i++) buf[i] = (float)acc[i];

    for (int b = 0; b < BANDS; b++) {
        if (!s->stage_on[b]) continue;
        if (same(&s->from[b], &s->to[b]))
            bell_steady(buf, &s->to[b], &s->z[ch][b]);
        else
            bell_ramp(buf, &s->from[b], &s->to[b], &s->z[ch][b]);
    }
    if (s->stage_on[DJ_STAGE]) {
        const svf_t *f = &s->from[DJ_STAGE], *t = &s->to[DJ_STAGE];
        if (same(f, t) && s->wet_from == s->wet_to)
            dj_steady(buf, t, &s->z[ch][DJ_STAGE], s->wet_to);
        else
            dj_ramp(buf, f, t, &s->z[ch][DJ_STAGE], s->wet_from, s->wet_to);
    }

    for (int i = 0; i < BLOCK; i++) acc[i] = to_int_round_sat(buf[i]);
}

static int any_on(const patch_state_t *s)
{
    for (int st = 0; st < STAGES; st++)
        if (s->stage_on[st]) return 1;
    return 0;
}

void master_fx(int16_t *out, int32_t *acc, uint32_t mode, uint32_t mixer)
{
    patch_state_t *s = (patch_state_t *)PATCH_STATE;
    if (s->init != INIT_MAGIC) {                   /* keep dj/enabled, reset rest */
        uint32_t *w = (uint32_t *)&s->init;
        uint32_t words = (sizeof(*s) - __builtin_offsetof(patch_state_t, init)) / 4;
        for (uint32_t i = 0; i < words; i++) w[i] = 0;
        for (int b = 0; b < BANDS; b++) {
            s->band_g[b] = tan_0_halfpi(PI_F * band_hz[b] / FS);
            design_bell(&s->to[b], s->band_g[b], 0.0f);
        }
        s->init = INIT_MAGIC;
    }
    s->calls++;
    if (s->enabled) {
        int ch = mixer == FW_MASTER_LEFT ? 0 : 1;
        if (ch == 0) update(s);
        if (any_on(s)) process(s, acc, ch);
    }
    ((saturate_fn)FW_SATURATE)(out, acc, mode);
}

/* Entry from the patched 'bl 0x1611c' at 0xf3fa (app setup, once per boot):
 * clear all patch state, enable processing, then run the engine init. */
uint32_t patch_boot(void *engine, void *hal, uint32_t arg)
{
    volatile uint32_t *w = (volatile uint32_t *)PATCH_STATE;
#pragma clang loop unroll(disable)
    for (uint32_t i = 0; i < sizeof(patch_state_t) / 4; i++) w[i] = 0;
    patch_state_t *s = (patch_state_t *)PATCH_STATE;
    volatile uint32_t *fx = (volatile uint32_t *)FX_STATE;
#pragma clang loop unroll(disable)
    for (uint32_t i = 0; i < FX_STATE_HEADER / 4; i++) fx[i] = 0;
    s->magic = STATE_MAGIC;
    s->enabled = 1;
    s->fx_sram = ((malloc_fn)FW_MALLOC)(FX_SRAM_BYTES);
    s->fx_select = 3;            /* diagnostic build: start on the stock effects */
    return ((engine_init_fn)FW_ENGINE_INIT)(engine, hal, arg);
}

/* Entry from the patched 'bl' at 0x76c6a: pass the mixer's this (r8) as arg 4. */
__attribute__((naked)) void master_fx_entry(void)
{
    __asm volatile("mov r3, r8\n\tb master_fx\n");
}

/* ---------- UI: Shift + Master + jog wheel ---------- */

static uint32_t held_bit(uint32_t btn)
{
    return btn == BTN_MASTER ? HELD_MASTER : btn == BTN_SHIFT ? HELD_SHIFT : 0;
}

static char *put(char *p, const char *text)
{
    while (*text) *p++ = *text++;
    return p;
}

/* "DJ Low-pass 40%", "DJ High-pass 25%" or "DJ filter off" in the pop-up. */
static void show_dj(patch_state_t *s, float dj)
{
    char *p = s->popup_text;
    int pct = (int)(fabs_f(dj) * 100.0f + 0.5f);
    if (pct == 0) {
        p = put(p, "DJ filter off");
    } else {
        p = put(p, dj < 0 ? "DJ Low-pass " : "DJ High-pass ");
        if (pct >= 100) *p++ = '1';
        if (pct >= 10) *p++ = (char)('0' + (pct / 10) % 10);
        *p++ = (char)('0' + pct % 10);
        *p++ = '%';
    }
    *p = 0;
    ((popup_fn)FW_POPUP)((void *)FW_POPUPS, 0, s->popup_text, POPUP_SECONDS);
}

static const char *const fx_names[4] = {
    "Plate rev + Tape dly", "Stock rev + Tape dly",
    "Plate rev + Stock dly", "Stock rev + Stock dly",
};

static char *put_k(char *p, uint32_t cycles)      /* cycles -> "41" (thousands) */
{
    uint32_t k = (cycles + 500) / 1000;
    char tmp[6];
    int n = 0;
    do { tmp[n++] = (char)('0' + k % 10); k /= 10; } while (k && n < 5);
    while (n) *p++ = tmp[--n];
    return p;
}

/* Cost readout when Shift + Master are both held, in thousand cycles per
 * block (a block is ~522k): R = reverb now (average), P = plate's largest
 * block since it was selected, D = delay now, T = tape's largest block;
 * "!" = the guard switched that effect back to stock. "R92 P180! D25 T30k" */
static void show_cpu(patch_state_t *s)
{
    const volatile uint32_t *fx = (const volatile uint32_t *)FX_STATE;
    char *p = put(s->popup_text, "R");
    p = put_k(p, fx[1]);
    p = put(p, " P"); p = put_k(p, fx[6]);
    if (fx[13] & 1) *p++ = '!';
    p = put(p, " D"); p = put_k(p, fx[7]);
    p = put(p, " T"); p = put_k(p, fx[12]);
    if (fx[13] & 2) *p++ = '!';
    *p++ = 'k';
    *p = 0;
    ((popup_fn)FW_POPUP)((void *)FW_POPUPS, 0, s->popup_text, 2);
}

static void show_fx(patch_state_t *s)
{
    char *p = put(s->popup_text, fx_names[s->fx_select & 3]);
    *p = 0;
    ((popup_fn)FW_POPUP)((void *)FW_POPUPS, 0, s->popup_text, POPUP_SECONDS);
}

/* Press and release. Shift and Master are always forwarded, so they keep their
 * normal behaviour; releasing either one ends the DJ gesture and recentres the
 * DJ. Any other button pressed while both are held steps the FX bank and is
 * not forwarded (nor its hold/release). */
void ui_button(void *iface, uint32_t btn, int32_t state)
{
    patch_state_t *s = (patch_state_t *)PATCH_STATE;
    if (s->fx_button && s->fx_button == btn + 1) {
        if (state == 0) s->fx_button = 0;
        return;
    }
    uint32_t bit = held_bit(btn);
    if (bit && state == 1 && (s->held | bit) == (HELD_MASTER | HELD_SHIFT) && s->held != (s->held | bit)) {
        s->held |= bit;
        show_cpu(s);
    }
    if (!bit && state == 1 && (s->held & (HELD_MASTER | HELD_SHIFT)) == (HELD_MASTER | HELD_SHIFT)) {
        s->fx_select = (s->fx_select + 1) & 3;
        s->fx_button = btn + 1;
        ((volatile uint32_t *)FX_STATE)[13] = 0;    /* clear the bank's CPU guard: try again */
        show_fx(s);
        return;
    }
    if (bit) {
        if (state == 1) {
            s->held |= bit;
        } else if (state == 0) {
            s->held &= ~bit;
            if (s->dj_by_ui) { s->dj = 0.0f; s->dj_by_ui = 0; show_dj(s, 0.0f); }
        }
    }
    ((input_fn)FW_BUTTON)(iface, btn, state);
}

/* Jog wheel. With Shift and Master both held the turn moves the DJ filter and
 * is not forwarded (so it does not also change master volume); otherwise it
 * goes to the firmware unchanged. */
void ui_knob(void *iface, uint32_t pot, int32_t value)
{
    patch_state_t *s = (patch_state_t *)PATCH_STATE;
    if (pot == JOG_POT && (s->held & (HELD_MASTER | HELD_SHIFT)) == (HELD_MASTER | HELD_SHIFT)) {
        s->dj = clamp1(s->dj + DJ_STEP * (float)(int16_t)value);
        s->dj_by_ui = 1;
        show_dj(s, s->dj);
        return;
    }
    ((input_fn)FW_KNOB)(iface, pot, value);
}

