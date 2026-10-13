#!/bin/sh
# Build the master EQ + DJ filter patch for Tracker fw 1.9.2 (Cortex-M4F).
set -e
cd "$(dirname "$0")"
clang --target=thumbv7em-none-eabihf -mcpu=cortex-m4 -mfpu=fpv4-sp-d16 \
      -mfloat-abi=hard -mthumb -O2 -ffp-contract=fast -ffreestanding -fno-builtin -nostdlib \
      -fno-common -fno-exceptions -fno-unwind-tables -Wall -Wextra \
      -mllvm --arm-postra-hazard-recognizer=false \
      -c master_fx.c -o master_fx.o
# Guard: an LLVM bug once put an alignment nop inside an IT block, turning a
# conditional instruction into an unconditional one. Refuse such output.
if objdump -d --triple=thumbv7em-none-eabihf --mattr=+vfp4 master_fx.o \
   | grep -qE '	nop(eq|ne|gt|lt|ge|le|mi|pl|hi|ls|cc|cs|vs|vc)'; then
  echo "build.sh: conditional nop inside an IT block - miscompiled output" >&2
  exit 1
fi
python3 link.py master_fx.o 0xD7000 master_fx.bin
