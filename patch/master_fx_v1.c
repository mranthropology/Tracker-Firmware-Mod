/*
 * Master EQ + DJ filter patch for Polyend Tracker firmware 1.9.2.
 *
 * Hooked in place of the master mixer's call to the saturation stage
 * (bl 0xd42f0 at 0x76c6a, class 0xc2b9c). Processes the 32-bit master
 * accumulator (128 frames, one channel per call) and then calls the
 * original saturator, so the result is in both live output and exports.
 *
 *  - 5-band peaking EQ at the stock Master EQ centres (115/330/990/3k/9.9k Hz),
 *    gains read from the firmware's own Master EQ settings (5 floats at
 *    0x20004c60, -1..+1, mapped to +/-12 dB like the codec's graphic EQ).
 *  - DJ filter: one value in -1..+1 (state->dj). <0 sweeps a low-pass down,
 *    >0 sweeps a high-pass up, ~0 = off.
 *
 * Freestanding: no libc/libm; own sin/exp2 approximations. Built with clang
 * for thumbv7em/FPv4-SP and linked by link.py at a fixed address.
 */
#include <stdint.h>

#define FS              44100.0f
#define PI_F            3.14159265f
#define HALF_PI_F       1.57079633f
#define BLOCK           128
#define BANDS           5
#define STAGES          (BANDS + 1)          /* EQ bands + DJ filter */
#define EQ_RANGE_DB     12.0f
#define DJ_DEADZONE     0.02f

#define FW_EQ_GAINS     ((volatile const float *)0x20004C60)
#define FW_MASTER_LEFT  0x1FFFACC8u          /* master 4-in mixer, channel 0 */
#define FW_SATURATE     0x000D42F1u          /* original stage (Thumb) */
#define PATCH_STATE     0x70FE0000u
#define PATCH_MAGIC     0x58464D50u          /* 'PMFX' */

typedef void (*saturate_fn)(int16_t *out, int32_t *acc, uint32_t mode);

typedef struct { float b0, b1, b2, a1, a2; } coeffs_t;
typedef struct { float z1, z2; } biquad_state_t;

typedef struct {
    uint32_t magic;
    float dj;                    /* DJ filter control, -1..+1 (set externally) */
    uint32_t enabled;            /* 0 = pass straight to the saturator */
    float cached_eq[BANDS];
    float cached_dj;
    uint32_t dj_mode;            /* 0 off, 1 low-pass, 2 high-pass */
    uint32_t active;             /* any stage non-neutral */
    coeffs_t coeffs[STAGES];
    biquad_state_t state[2][STAGES];
    uint32_t calls;
} patch_state_t;

static const float band_hz[BANDS] = {115.0f, 330.0f, 990.0f, 3000.0f, 9900.0f};

/* ---------- math (freestanding) ---------- */

static float sin_poly(float x)           /* |x| <= pi/2 */
{
    float x2 = x * x;
    return x * (1.0f + x2 * (-1.0f / 6 + x2 * (1.0f / 120 + x2 * (-1.0f / 5040
                + x2 * (1.0f / 362880 + x2 * (-1.0f / 39916800))))));
}

static float sin_0_pi(float w)           /* w in [0, pi] */
{
    return sin_poly(w > HALF_PI_F ? PI_F - w : w);
}

static float cos_0_pi(float w)           /* w in [0, pi] */
{
    return sin_poly(HALF_PI_F - w);
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

/* ---------- RBJ cookbook biquads ---------- */

static void set_normalized(coeffs_t *c, float b0, float b1, float b2,
                           float a0, float a1, float a2)
{
    float inv = 1.0f / a0;
    c->b0 = b0 * inv; c->b1 = b1 * inv; c->b2 = b2 * inv;
    c->a1 = a1 * inv; c->a2 = a2 * inv;
}

static void design_peak(coeffs_t *c, float hz, float gain_db, float q)
{
    float A = exp2_approx(gain_db * 0.0830482f);        /* 10^(dB/40) */
    float w0 = 2.0f * PI_F * hz / FS;
    float alpha = sin_0_pi(w0) / (2.0f * q);
    float cw = cos_0_pi(w0);
    set_normalized(c, 1 + alpha * A, -2 * cw, 1 - alpha * A,
                   1 + alpha / A, -2 * cw, 1 - alpha / A);
}

static void design_pass(coeffs_t *c, float hz, float q, int highpass)
{
    float w0 = 2.0f * PI_F * hz / FS;
    float alpha = sin_0_pi(w0) / (2.0f * q);
    float cw = cos_0_pi(w0);
    float k = highpass ? (1 + cw) : (1 - cw);
    set_normalized(c, k * 0.5f, highpass ? -k : k, k * 0.5f,
                   1 + alpha, -2 * cw, 1 - alpha);
}

static void update_coeffs(patch_state_t *s)
{
    int changed = 0, active = 0;
    for (int b = 0; b < BANDS; b++) {
        float g = FW_EQ_GAINS[b];
        if (g > 1.0f) g = 1.0f;
        if (g < -1.0f) g = -1.0f;
        if (g != s->cached_eq[b]) { s->cached_eq[b] = g; changed = 1; }
        if (fabs_f(g) > 1e-4f) active = 1;
    }
    float dj = s->dj;
    if (dj != s->cached_dj) { s->cached_dj = dj; changed = 1; }
    float mag = fabs_f(dj);
    s->dj_mode = mag <= DJ_DEADZONE ? 0 : (dj < 0 ? 1 : 2);
    if (s->dj_mode) active = 1;
    s->active = active;
    if (!changed) return;

    for (int b = 0; b < BANDS; b++)
        design_peak(&s->coeffs[b], band_hz[b], s->cached_eq[b] * EQ_RANGE_DB, 0.9f);
    if (s->dj_mode) {
        float t = (mag - DJ_DEADZONE) / (1.0f - DJ_DEADZONE);
        float hz = s->dj_mode == 1
            ? 20000.0f * exp2_approx(t * -7.9657843f)    /* 20 kHz -> 80 Hz */
            : 20.0f * exp2_approx(t * 8.6438562f);       /* 20 Hz -> 8 kHz */
        design_pass(&s->coeffs[BANDS], hz, 0.8f, s->dj_mode == 2);
    }
}

/* ---------- processing ---------- */

static void process(patch_state_t *s, int32_t *acc, int ch)
{
    int last = s->dj_mode ? STAGES : BANDS;
    for (int i = 0; i < BLOCK; i++) {
        float x = (float)acc[i];
        for (int st = 0; st < last; st++) {
            if (st < BANDS && s->cached_eq[st] == 0.0f) continue;   /* flat band */
            const coeffs_t *c = &s->coeffs[st];
            biquad_state_t *z = &s->state[ch][st];
            float y = c->b0 * x + z->z1;
            z->z1 = c->b1 * x - c->a1 * y + z->z2;
            z->z2 = c->b2 * x - c->a2 * y;
            x = y;
        }
        if (x > 2147483520.0f) x = 2147483520.0f;
        if (x < -2147483520.0f) x = -2147483520.0f;
        acc[i] = (int32_t)(x < 0 ? x - 0.5f : x + 0.5f);
    }
}

void master_fx(int16_t *out, int32_t *acc, uint32_t mode, uint32_t mixer)
{
    patch_state_t *s = (patch_state_t *)PATCH_STATE;
    if (s->magic != PATCH_MAGIC) {                 /* first call: initialise */
        uint32_t *w = (uint32_t *)s;
        for (uint32_t i = 0; i < sizeof(*s) / 4; i++) w[i] = 0;
        s->magic = PATCH_MAGIC;
        s->enabled = 1;
        s->cached_dj = 99.0f;                      /* force first design */
    }
    s->calls++;
    if (s->enabled) {
        int ch = mixer == FW_MASTER_LEFT ? 0 : 1;
        if (ch == 0) update_coeffs(s);             /* once per block pair */
        if (s->active) process(s, acc, ch);
    }
    ((saturate_fn)FW_SATURATE)(out, acc, mode);
}

/* Entry from the patched 'bl' at 0x76c6a: pass the mixer's this (r8) as arg 4. */
__attribute__((naked)) void master_fx_entry(void)
{
    __asm volatile("mov r3, r8\n\tb master_fx\n");
}
