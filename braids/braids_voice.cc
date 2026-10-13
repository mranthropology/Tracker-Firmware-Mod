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

using braids::MacroOscillator;
using braids::MacroOscillatorShape;

static const int16_t kPitchOffset = 1723;   // 128 * 12 * log2(96000 / 44117.647)
static const int kChunk = 24;               // Braids' internal buffer size

extern "C" {

uint32_t braids_voice_size(void) { return sizeof(MacroOscillator); }

void braids_voice_init(void* mem) {
  MacroOscillator* o = new (mem) MacroOscillator;
  o->Init();
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
  uint8_t sync[kChunk];
  memset(sync, 0, sizeof(sync));
  while (n > 0) {
    int c = n > kChunk ? kChunk : n;
    o->Render(sync, out, c);
    out += c;
    n -= c;
  }
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
