#!/bin/sh
# Build the master FX bank (plate reverb, tape delay) for Tracker fw 1.9.2.
# Linked at 0xF8000: after master_fx.bin (0xD7000) and braids.bin (0xD9000-~0xF6400).
# No .data/.bss: state lives at a fixed SDRAM address (see fx_bank.c).
set -e
cd "$(dirname "$0")"
FLASH_BASE=${FLASH_BASE:-0xF8000}
clang --target=thumbv7em-none-eabihf -mcpu=cortex-m4 -mfpu=fpv4-sp-d16 \
      -mfloat-abi=hard -mthumb -O2 -ffp-contract=fast -ffreestanding -fno-builtin -nostdlib \
      -fno-common -fno-exceptions -fno-unwind-tables -fno-asynchronous-unwind-tables -Wall -Wextra \
      -mllvm --arm-postra-hazard-recognizer=false \
      -c fx_bank.c -o fx_bank.o
# Same guard as patch/build.sh: no conditional nop inside an IT block.
if objdump -d --triple=thumbv7em-none-eabihf --mattr=+vfp4 fx_bank.o \
   | grep -qE '	nop(eq|ne|gt|lt|ge|le|mi|pl|hi|ls|cc|cs|vs|vc)'; then
  echo "build.sh: conditional nop inside an IT block - miscompiled output" >&2
  exit 1
fi
(cd .. && python3 -m patch.link fxbank/fx_bank.o "$FLASH_BASE" fxbank/fx_bank.bin)
