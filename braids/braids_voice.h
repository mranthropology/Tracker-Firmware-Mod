// C interface to the Braids MacroOscillator wrapper (braids_voice.cc).
#pragma once
#include <stdint.h>
extern "C" {
uint32_t braids_voice_size(void);
void braids_voice_init(void* mem);
void braids_voice_strike(void* mem);
void braids_voice_render(void* mem, int shape, int pitch, int timbre, int color,
                         int16_t* out, int n);
void braids_voice_render_96k(void* mem, int shape, int pitch, int timbre, int color,
                             int16_t* out, int n);
uint32_t braids_voice_footprint(void);
}
