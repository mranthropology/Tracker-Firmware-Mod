#!/bin/sh
# Build the Braids voice for the Tracker's Cortex-M4F from an unmodified
# pichenettes/eurorack + stmlib checkout (MIT licence).
#   build.sh <dir containing eurorack/ and stmlib/>
set -e
cd "$(dirname "$0")"
EXT="$1"
[ -d "$EXT/eurorack/braids" ] && [ -d "$EXT/stmlib" ] || { echo "usage: build.sh <ext dir>" >&2; exit 1; }
mkdir -p build
rm -f build/*.o
CXX="clang++ --target=thumbv7em-none-eabihf -mcpu=cortex-m4 -mfpu=fpv4-sp-d16 -mfloat-abi=hard -mthumb
  -O2 -ffreestanding -fno-builtin -nostdlib -fno-exceptions -fno-rtti -fno-threadsafe-statics
  -fno-common -fno-unwind-tables -fno-asynchronous-unwind-tables -ffunction-sections -fdata-sections
  -Wno-unused-parameter -mllvm --arm-postra-hazard-recognizer=false
  -nostdinc++ -isystem shim -I $EXT/eurorack -I $EXT -DTEST"
FLASH_BASE=${FLASH_BASE:-0xD9000}      # after master_fx.bin (0xD7000-0xD86FF)
RAM_BASE=${RAM_BASE:-0x70FE1000}       # free SDRAM after the patch state (0x70FE0000)
NEXT_BOOT=$(awk '$1=="patch_boot"{print $2}' ../patch/master_fx.bin.syms)
[ -n "$NEXT_BOOT" ] || { echo "build.sh: build patch/master_fx.bin first" >&2; exit 1; }
sym() { awk -v n="$1" '$1==n{print $2}' build/braids.bin.syms; }
compile() {
  $CXX -DNEXT_BOOT="$NEXT_BOOT" -DBRAIDS_DATA_LOAD="$1" -DBRAIDS_DATA_START="$2" \
       -DBRAIDS_DATA_SIZE="$3" -DBRAIDS_BSS_START="$4" -DBRAIDS_BSS_SIZE="$5" \
       -c braids_unity.cc -o build/braids.o
  if objdump -d --triple=thumbv7em-none-eabihf --mattr=+vfp4 build/braids.o \
     | grep -qE '	nop(eq|ne|gt|lt|ge|le|mi|pl|hi|ls|cc|cs|vs|vc)'; then
    echo "build.sh: conditional nop inside an IT block - miscompiled output" >&2
    exit 1
  fi
  (cd .. && python3 -m patch.link braids/build/braids.o "$FLASH_BASE" braids/build/braids.bin "$RAM_BASE" >/dev/null)
}
# Each pass bakes the previous pass's .data/.bss layout into braids_boot;
# repeat until the layout no longer moves.
set -- 0 0 0 0 0
for pass in 1 2 3 4; do
  compile "$@"
  now="$(sym __data_load) $(sym __data_start) $(sym __data_size) $(sym __bss_start) $(sym __bss_size)"
  [ "$*" = "$now" ] && break
  set -- $now
  [ $pass = 4 ] && { echo "build.sh: layout did not settle" >&2; exit 1; }
done
echo "__next_boot $NEXT_BOOT" >> build/braids.bin.syms   # checked by patch/make_ptf.py
echo "braids.bin: $(wc -c < build/braids.bin | tr -d ' ') bytes at $FLASH_BASE; .data $3 bytes -> $2, .bss $5 bytes -> $4; boot chains to $NEXT_BOOT"
