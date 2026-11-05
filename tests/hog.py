"""Test helper: allocate MB in 10 MB steps, optionally hold, then exit.

usage: hog.py MEGABYTES [HOLD_SECONDS]
"""

import sys
import time

target = int(sys.argv[1])
hold = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
keep = []
while len(keep) * 10 < target:
    keep.append(bytearray(10 * 1024 * 1024))
    time.sleep(0.01)
time.sleep(hold)
print("survived", len(keep) * 10)
