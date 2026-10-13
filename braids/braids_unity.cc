// Single translation unit so the one-object linker (patch/link.py) can link
// Braids: the wrapper plus the unmodified Braids/stmlib sources.
#include "braids_voice.cc"
#include "tracker_voice.cc"
#include "braids/macro_oscillator.cc"
#include "braids/analog_oscillator.cc"
// digital_oscillator.cc repeats three file-local constants from
// analog_oscillator.cc; rename them for this translation unit only.
#define kHighestNote kHighestNote_digital
#define kPitchTableStart kPitchTableStart_digital
#define kOctave kOctave_digital
#include "braids/digital_oscillator.cc"
#undef kHighestNote
#undef kPitchTableStart
#undef kOctave
#include "braids/resources.cc"
#include "stmlib/utils/random.cc"
