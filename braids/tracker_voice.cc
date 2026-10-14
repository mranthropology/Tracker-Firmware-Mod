// Braids as a Tracker instrument (fw 1.9.2): hooks into the per-track sample
// player (vtable 0xa9fb0, one per track, so each track is monophonic).
//
// An instrument whose name starts with "BR-" (or "BR:") plays Braids instead
// of its sample, e.g. "BR-FM", "BR-KICK", "BR-12". The Tracker's on-screen
// keyboard has no ':' (keys: digits, letters, - + @ space ! " # $ % & ' ( )),
// so "BR-" is the form to type on the device. The stock note-on for the
// instrument's playback mode runs unchanged (pitch, tune, finetune, glide,
// envelope); then:
//   0xd0a50  after note-on  -> braids_note_stub: pick the model, retrigger
//   0xd0758  render         -> braids_render_stub: fill the player's block
// The amp envelope, bit reducer, filter, sends, volume and pan after the
// player all still apply.
//
// Controls (no new screens):
//   pitch  <- player increment +0x74 (note, tune, finetune, glide, pitch FX)
//   Timbre <- Granular Position (+0x9e, with its envelope/LFO/step FX) when
//             the instrument is in Granular mode, else the static position
//   Color  <- grain length (44..44100 samples, log scale)
// At most kMaxTracks tracks run Braids at once; a track without a slot plays
// its sample as usual. The instrument preview player always may.
#include <stdint.h>
#include "braids_voice.h"

namespace {

const uint32_t kPlayer0 = 0x1FFFB920;      // track 0 sample player (voice + 0x120)
const uint32_t kPlayerStride = 0x660;
const uint32_t kPreviewPlayer = 0x1FFFA988;
const uint32_t kInstruments = 0x200001CC;  // 48 x 0x174
const uint32_t kInstrumentSize = 0x174;
const uint32_t kAllocate = 0x000CE42D;     // audio block allocate (Thumb)
const uint32_t kVoiceMemory = 0x70F00000;  // free SDRAM (fw ends 0x70ef8874)
const uint32_t kVoiceStride = 0x4400;      // oscillator + resampler state
static_assert(kVoiceStride >= kVoiceFootprint, "voice slot too small");
const int kSlots = 9;                      // 8 tracks + preview
const int kMaxTracks = 4;
const float kIncrementAtC4 = 0.9996f;      // player increment for note 60

struct Slot {
  int8_t model;        // -1: not a Braids instrument
  uint8_t licensed;    // holds one of the kMaxTracks slots
  uint8_t inited;
  uint8_t strike;
  int8_t voiced_model;  // model the oscillator was initialised for
};

Slot slots[kSlots];    // .bss, zeroed at boot by braids_boot

const char* const kModelNames[] = {
  "CSAW", "MORPH", "SAWSQUARE", "SINETRIANGLE", "BUZZ", "SQUARESUB", "SAWSUB",
  "SQUARESYNC", "SAWSYNC", "TRIPLESAW", "TRIPLESQUARE", "TRIPLETRIANGLE",
  "TRIPLESINE", "TRIPLERINGMOD", "SAWSWARM", "SAWCOMB", "TOY", "DIGITALFILTERLP",
  "DIGITALFILTERPK", "DIGITALFILTERBP", "DIGITALFILTERHP", "VOSIM", "VOWEL",
  "VOWELFOF", "HARMONICS", "FM", "FEEDBACKFM", "CHAOTICFEEDBACKFM", "PLUCKED",
  "BOWED", "BLOWN", "FLUTED", "STRUCKBELL", "STRUCKDRUM", "KICK", "CYMBAL",
  "SNARE", "WAVETABLES", "WAVEMAP", "WAVELINE", "WAVEPARAPHONIC", "FILTEREDNOISE",
  "TWINPEAKSNOISE", "CLOCKEDNOISE", "GRANULARCLOUD", "PARTICLENOISE",
  "DIGITALMODULATION", "QUESTIONMARK",
};
const int kNumModels = sizeof(kModelNames) / sizeof(kModelNames[0]);

inline uint8_t u8(uint32_t a) { return *reinterpret_cast<volatile uint8_t*>(a); }
inline uint16_t u16(uint32_t a) { return *reinterpret_cast<volatile uint16_t*>(a); }
inline float f32(uint32_t a) { return *reinterpret_cast<volatile float*>(a); }

int slot_of(uint32_t player) {
  if (player == kPreviewPlayer) return 8;
  uint32_t off = player - kPlayer0;
  if (off % kPlayerStride == 0 && off / kPlayerStride < 8) return off / kPlayerStride;
  return -1;
}

void* voice_memory(int slot) {
  return reinterpret_cast<void*>(kVoiceMemory + slot * kVoiceStride);
}

char upper(char c) { return (c >= 'a' && c <= 'z') ? c - 32 : c; }

// "BR-" or "BR:" prefix, then a model number or name (case, spaces, '_' and
// '-' ignored).
int parse_model(uint32_t name) {
  char sep = static_cast<char>(u8(name + 2));
  if (upper(u8(name)) != 'B' || upper(u8(name + 1)) != 'R' || (sep != ':' && sep != '-')) return -1;
  char token[24];
  int n = 0;
  for (uint32_t i = 3; i < 32 && n < 23; ++i) {
    char c = upper(static_cast<char>(u8(name + i)));
    if (c == 0) break;
    if (c == ' ' || c == '_' || c == '-') continue;
    token[n++] = c;
  }
  token[n] = 0;
  if (n == 0) return 0;
  if (token[0] >= '0' && token[0] <= '9') {
    int v = 0;
    for (int i = 0; i < n && token[i] >= '0' && token[i] <= '9'; ++i) v = v * 10 + token[i] - '0';
    return v < kNumModels ? v : 0;
  }
  for (int m = 0; m < kNumModels; ++m) {
    const char* s = kModelNames[m];
    int i = 0;
    while (s[i] && s[i] == token[i]) ++i;
    if (!s[i] && !token[i]) return m;
  }
  return 0;   // unknown name: first model
}

// log2 for x > 0: exponent from the float bits; log(1+m) = 2*atanh(m/(m+2))
// with m in [0, 1), so t <= 1/3 and the series to t^9 is within ~1e-6
// (far below a cent in the pitch below).
float log2_approx(float x) {
  union { float f; uint32_t u; } v = { x };
  int e = static_cast<int>((v.u >> 23) & 0xFF) - 127;
  v.u = (v.u & 0x007FFFFF) | 0x3F800000;        // mantissa in [1, 2)
  float m = v.f - 1.0f;
  float t = m / (m + 2.0f);
  float t2 = t * t;
  float ln = 2.0f * t * (1.0f + t2 * (1.0f / 3 + t2 * (1.0f / 5 + t2 * (1.0f / 7
             + t2 * (1.0f / 9)))));
  return static_cast<float>(e) + ln * 1.44269504f;
}

int16_t color_from_grain_length(uint16_t samples) {
  if (samples < 44) samples = 44;
  // log(len/44) / log(44100/44) -> 0..32767
  float c = log2_approx(samples / 44.0f) * (32767.0f / 9.96921f);
  return c > 32767.0f ? 32767 : static_cast<int16_t>(c);
}

void release(Slot& s) { s.licensed = 0; }

// Models whose decays are per-sample constants run at Braids' native 96 kHz
// and are resampled (m16_braids_rate.py: struck bell/drum 2.15x, snare 1.4x,
// particle noise 1.5x too long when rendered directly at 44.1 kHz).
bool renders_at_96k(int model) {
  return model == 32 || model == 33 || model == 36 || model == 45;
}

int licensed_tracks() {
  int n = 0;
  for (int i = 0; i < 8; ++i) n += slots[i].licensed;
  return n;
}

}  // namespace

extern "C" {

// After the stock note-on (player has instrument +0x6f, note, pitch, gate).
void braids_after_note_on(uint32_t player) {
  int si = slot_of(player);
  if (si < 0) return;
  Slot& s = slots[si];
  uint32_t instr = kInstruments + u8(player + 0x6f) * kInstrumentSize;
  int model = parse_model(instr + 0x05);
  if (model < 0) {
    s.model = -1;
    release(s);
    return;
  }
  if (si < 8 && !s.licensed) {
    if (licensed_tracks() >= kMaxTracks) {   // no slot: play the sample
      s.model = -1;
      return;
    }
    s.licensed = 1;
  }
  if (!s.inited || s.voiced_model != model) {
    braids_voice_init(voice_memory(si));
    s.inited = 1;
    s.voiced_model = static_cast<int8_t>(model);
  }
  s.model = static_cast<int8_t>(model);
  s.strike = 1;
}

// Returns 0xFFFFFFFF if the stock renderer should run, else the audio block
// (or 0 when the voice is idle).
uint32_t braids_render(uint32_t player) {
  int si = slot_of(player);
  if (si < 0) return 0xFFFFFFFFu;
  Slot& s = slots[si];
  if (s.model < 0 || !s.inited) return 0xFFFFFFFFu;
  if (!u8(player + 0x20)) {                    // envelope finished
    release(s);
    s.model = -1;
    return 0;
  }
  uint32_t block = reinterpret_cast<uint32_t (*)(void)>(kAllocate)();
  if (!block) return 0;
  int16_t* out = reinterpret_cast<int16_t*>(block + 4);

  float inc = f32(player + 0x74);
  if (!(inc > 0.0f)) inc = kIncrementAtC4;
  float note = 60.0f + 12.0f * log2_approx(inc / kIncrementAtC4);
  int pitch = static_cast<int>(note * 128.0f + 0.5f);
  if (pitch < 0) pitch = 0;
  if (pitch > 127 * 128) pitch = 127 * 128;

  uint32_t instr = kInstruments + u8(player + 0x6f) * kInstrumentSize;
  uint16_t position = u8(player + 0x21) == 7 ? u16(player + 0x9e) : u16(instr + 0x16c);
  int timbre = position >> 1;
  int color = color_from_grain_length(u16(instr + 0x16a));

  void* mem = voice_memory(si);
  if (s.strike) {
    braids_voice_strike(mem);
    s.strike = 0;
  }
  if (renders_at_96k(s.model))
    braids_voice_render_96k(mem, s.model, pitch, timbre, color, out, 128);
  else
    braids_voice_render(mem, s.model, pitch, timbre, color, out, 128);
  for (int i = 0; i < 128; ++i) out[i] = static_cast<int16_t>(out[i] >> 1);   // -6 dB
  return block;
}

// Boot: copy .data, zero .bss (values from the first link pass, see build.sh),
// then continue to the master FX boot reset (which runs engine init).
void braids_boot(uint32_t engine, uint32_t hal, uint32_t arg) {
  const uint32_t* src = reinterpret_cast<const uint32_t*>(BRAIDS_DATA_LOAD);
  uint32_t* dst = reinterpret_cast<uint32_t*>(BRAIDS_DATA_START);
#pragma clang loop unroll(disable)
  for (uint32_t i = 0; i < (BRAIDS_DATA_SIZE + 3) / 4; ++i) dst[i] = src[i];
  uint8_t* bss = reinterpret_cast<uint8_t*>(BRAIDS_BSS_START);
#pragma clang loop unroll(disable)
  for (uint32_t i = 0; i < BRAIDS_BSS_SIZE; ++i) bss[i] = 0;
  for (int i = 0; i < kSlots; ++i) slots[i].model = -1;
  reinterpret_cast<void (*)(uint32_t, uint32_t, uint32_t)>(NEXT_BOOT | 1)(engine, hal, arg);
}

// 0xd0758 (render): r0 = player; the firmware already pushed {r4, lr}.
__attribute__((naked)) void braids_render_stub(void) {
  __asm volatile(
      "mov r4, r0\n\t"
      "bl braids_render\n\t"
      "adds r1, r0, #1\n\t"
      "beq 1f\n\t"
      "mov r1, r0\n\t"
      "ldr r3, =0x000d0777\n\t"     // -> halving check, envelope, transmit
      "bx r3\n"
      "1:\n\t"
      "mov r0, r4\n\t"
      "ldrb.w r3, [r0, #0x21]\n\t"  // displaced instruction
      "ldr r12, =0x000d075d\n\t"
      "bx r12\n\t"
      ".ltorg\n");
}

// 0xd0a50 (after note-on): r4 = player; r0-r3 live.
__attribute__((naked)) void braids_note_stub(void) {
  __asm volatile(
      "push {r0, r1, r2, r3, r12, lr}\n\t"
      "mov r0, r4\n\t"
      "bl braids_after_note_on\n\t"
      "pop {r0, r1, r2, r3, r12, lr}\n\t"
      "ldrb.w r3, [r4, #0x2c]\n\t"  // displaced instruction
      "ldr r12, =0x000d0a55\n\t"
      "bx r12\n\t"
      ".ltorg\n");
}

}  // extern "C"
