// Thin C interface to Mutable Instruments Braids' MacroOscillator for the
// Tracker patch. Braids (MIT licence, (c) Emilie Gillet) is compiled from an
// unmodified checkout; see build.sh.
//
// Braids runs at 96 kHz. Here it renders directly at the Tracker's output
// rate, 44117.647 Hz (Teensy audio rate; the player increment for note 60 is
// 44100/44117.647 = 0.9996): pitch is raised by 12*log2(96000/44117.647)
// semitones (Braids pitch is in 1/128 semitone) so phase increments land on
// the right note. Internal time
// constants (envelopes in the drum/pluck models) therefore run ~2.18x slower
// than on the module, and band-limiting assumes 96 kHz.
#include <new>
#include <cstring>
#include "braids/macro_oscillator.h"
#include "resample_taps.h"
#include <arm_acle.h>

using braids::MacroOscillator;
using braids::MacroOscillatorShape;

static const int16_t kPitchOffset = 1723;   // 128 * 12 * log2(96000 / 44117.647)
static const int kChunk = 24;               // Braids' internal buffer size

// 96 kHz path: state kept in the voice slot right after the oscillator.
struct ResampleState {
  int16_t history[RESAMPLE_TAPS - 1];       // last inputs, oldest first
  uint16_t frac;                            // position within an input sample, 1/125ths
  int16_t pending;                          // rendered sample not yet consumed
  uint16_t has_pending;
};

static ResampleState* resample_state(void* mem) {
  uint32_t a = reinterpret_cast<uint32_t>(mem) + sizeof(MacroOscillator);
  return reinterpret_cast<ResampleState*>((a + 3) & ~3u);
}

// Two adjacent int16 inputs as one word. The input window is only 2-byte
// aligned; the M4's plain LDR accepts that (the firmware leaves
// SCB->CCR.UNALIGN_TRP clear: its only SCB write, 0x68ac, sets DEMCR.TRCENA),
// but the compiler would otherwise split it into two halfword loads.
static inline uint32_t load_pair(const int16_t* p) {
  uint32_t v;
  __asm__("ldr %0, [%1]" : "=r"(v) : "r"(p) : "memory");
  return v;
}

static const uint32_t kVoiceFootprint =
    ((sizeof(MacroOscillator) + 3) & ~3u) + sizeof(ResampleState);
static void render_chunks(MacroOscillator* o, int16_t* out, int n) {
  uint8_t sync[kChunk];
  memset(sync, 0, sizeof(sync));
  while (n > 0) {
    int c = n > kChunk ? kChunk : n;
    o->Render(sync, out, c);
    out += c;
    n -= c;
  }
}

extern "C" {

uint32_t braids_voice_size(void) { return sizeof(MacroOscillator); }

// Bytes a voice slot needs (oscillator + 96 kHz resampler state).
uint32_t braids_voice_footprint(void) { return kVoiceFootprint; }

void braids_voice_init(void* mem) {
  MacroOscillator* o = new (mem) MacroOscillator;
  o->Init();
  memset(resample_state(mem), 0, sizeof(ResampleState));
}

void braids_voice_strike(void* mem) { static_cast<MacroOscillator*>(mem)->Strike(); }

// pitch: MIDI note * 128 (Braids units at 96 kHz before the offset);
// timbre, color: 0..32767.
void braids_voice_render(void* mem, int shape, int pitch, int timbre, int color,
                         int16_t* out, int n) {
  MacroOscillator* o = static_cast<MacroOscillator*>(mem);
  o->set_shape(static_cast<MacroOscillatorShape>(shape));
  o->set_pitch(static_cast<int16_t>(pitch + kPitchOffset));
  o->set_parameters(static_cast<int16_t>(timbre), static_cast<int16_t>(color));
  render_chunks(o, out, n);
}

// Same, but Braids runs at its native 96 kHz (unmodified tables and time
// constants, as on the module) and is resampled to 44117.647 Hz by a
// 125-phase x 32-tap polyphase low-pass (272/125). For models whose decays
// are per-sample constants (struck bell/drum, snare, particle noise).
// n <= 128.
void braids_voice_render_96k(void* mem, int shape, int pitch, int timbre, int color,
                             int16_t* out, int n) {
  MacroOscillator* o = static_cast<MacroOscillator*>(mem);
  ResampleState* st = resample_state(mem);
  o->set_shape(static_cast<MacroOscillatorShape>(shape));
  o->set_pitch(static_cast<int16_t>(pitch));            // 96 kHz tables: no offset
  o->set_parameters(static_cast<int16_t>(timbre), static_cast<int16_t>(color));

  const int kHist = RESAMPLE_TAPS - 1;
  uint32_t total = st->frac + static_cast<uint32_t>(n) * RESAMPLE_STEP;
  int n_in = static_cast<int>(total / RESAMPLE_PHASES);
  int16_t buf[kHist + (127 * RESAMPLE_STEP + RESAMPLE_STEP + RESAMPLE_PHASES) / RESAMPLE_PHASES + 2];
  memcpy(buf, st->history, sizeof(st->history));
  // Braids' percussion models render two samples per loop step and wrap on
  // odd sizes (the module always renders 24), so only ever ask for an even
  // count and carry a spare sample to the next block.
  int pos = 0;
  if (st->has_pending) {
    buf[kHist] = st->pending;
    st->has_pending = 0;
    pos = 1;
  }
  int rem = n_in - pos;
  if (rem > 0) {
    int even = (rem + 1) & ~1;
    render_chunks(o, buf + kHist + pos, even);
    if (even != rem) {
      st->pending = buf[kHist + n_in];
      st->has_pending = 1;
    }
  }

  uint32_t u = st->frac;
  for (int m = 0; m < n; ++m) {
    u += RESAMPLE_STEP;
    int k = static_cast<int>(u / RESAMPLE_PHASES);      // newest input used
    // taps are stored oldest-first: dot product of the 32 inputs ending at
    // the newest one used, two 16-bit products per SMLAD.
    const uint32_t* h = reinterpret_cast<const uint32_t*>(kResampleTaps[u % RESAMPLE_PHASES]);
    const int16_t* x = buf + kHist + k - (RESAMPLE_TAPS - 1);
    int32_t acc = 0;
    for (int t = 0; t < RESAMPLE_TAPS / 2; ++t) acc = __smlad(h[t], load_pair(x + 2 * t), acc);
    acc >>= 15;
    out[m] = static_cast<int16_t>(acc > 32767 ? 32767 : (acc < -32768 ? -32768 : acc));
  }
  st->frac = static_cast<uint16_t>(total % RESAMPLE_PHASES);
  memcpy(st->history, buf + n_in, sizeof(st->history));
}

// Freestanding runtime pieces Braids needs.
void* memset(void* dst, int value, size_t n) {
  unsigned char* d = static_cast<unsigned char*>(dst);
  while (n--) *d++ = static_cast<unsigned char>(value);
  return dst;
}

void* memcpy(void* dst, const void* src, size_t n) {
  unsigned char* d = static_cast<unsigned char*>(dst);
  const unsigned char* s = static_cast<const unsigned char*>(src);
  while (n--) *d++ = *s++;
  return dst;
}

}  // extern "C"
