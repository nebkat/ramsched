#!/usr/bin/env python3
"""fake_compiler.py <peak MB> <seconds> -o <output>

Grows its memory linearly to <peak MB> over <seconds>, then writes <output>.
"""

import signal
import sys
import time

signal.signal(signal.SIGINT, signal.SIG_DFL)

peak_megabytes, seconds = int(sys.argv[1]), float(sys.argv[2])
output = sys.argv[sys.argv.index('-o') + 1]
steps = 20
blocks = []
for _ in range(steps):
    blocks.append(bytearray(b'\1' * (peak_megabytes * (1 << 20) // steps)))
    time.sleep(seconds / steps)
with open(output, 'w') as file:
    file.write('ok')
