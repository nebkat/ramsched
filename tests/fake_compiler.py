#!/usr/bin/env python3
"""fake_compiler.py <peak MB> <seconds> [<hold seconds>] -o <output>

Grows its memory linearly to <peak MB> over <seconds>, holds it, then writes <output>.
"""

import signal
import sys
import time

signal.signal(signal.SIGINT, signal.SIG_DFL)

peak_megabytes, seconds = int(sys.argv[1]), float(sys.argv[2])
hold = float(sys.argv[3]) if sys.argv[3] != '-o' else 0.0
output = sys.argv[sys.argv.index('-o') + 1]
steps = 20
blocks = []
for _ in range(steps):
    blocks.append(bytearray(b'\1' * (peak_megabytes * (1 << 20) // steps)))
    time.sleep(seconds / steps)
time.sleep(hold)
with open(output, 'w') as file:
    file.write('ok')
