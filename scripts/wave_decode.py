"""Fetch CH1 (DMX line A) from the DHO804 and decode it as a DMX/UART byte stream (250 kbit/s, 8N2).

Usage: wave_decode.py <timebase_s_per_div> <offset_s>
"""
import sys, time
from scope import Scope

BIT_US = 4.0
sc = Scope()
scale, offset = float(sys.argv[1]), float(sys.argv[2])
sc.cmd(":BUS1:DISPlay OFF")
sc.cmd(f":TIMebase:MAIN:SCALe {scale}")
sc.cmd(f":TIMebase:MAIN:OFFSet {offset}")
sc.cmd(":RUN")
time.sleep(1.0)
sc.cmd(":STOP")
time.sleep(0.3)
sc.cmd(":WAVeform:SOURce CHANnel1")
sc.cmd(":WAVeform:MODE RAW")
sc.cmd(":WAVeform:FORMat BYTE")
pre = sc.query(":WAVeform:PREamble?").split(",")
# format,type,points,count,xincrement,xorigin,xreference,yincrement,yorigin,yreference
points = int(pre[2]); xinc = float(pre[4]); xorig = float(pre[5])
yinc = float(pre[7]); yorig = float(pre[8]); yref = float(pre[9])
print(f"points={points} xinc={xinc*1e9:.1f}ns xorigin={xorig*1e6:.1f}us")
raw = b""
start = 1
CHUNK = 250000
while start <= points:
    stop = min(points, start + CHUNK - 1)
    sc.cmd(f":WAVeform:STARt {start}")
    sc.cmd(f":WAVeform:STOP {stop}")
    raw += sc.query(":WAVeform:DATA?", binary=True)
    start = stop + 1
sc.cmd(":RUN")
volts = [(b - yorig - yref) * yinc for b in raw]
vmax, vmin = max(volts), min(volts)
thr = (vmax + vmin) / 2
print(f"samples={len(volts)} vmin={vmin:.2f} vmax={vmax:.2f} threshold={thr:.2f}")
bits = [1 if v > thr else 0 for v in volts]
dt_us = xinc * 1e6
t0_us = xorig * 1e6

# Walk the stream: a low run >= 88us is a BREAK; otherwise a falling edge starts a UART frame.
i = 0
out = []
n = len(bits)
while i < n:
    if bits[i] == 1:
        i += 1
        continue
    j = i
    while j < n and bits[j] == 0:
        j += 1
    low_us = (j - i) * dt_us
    if low_us >= 88:
        out.append((t0_us + i * dt_us, f"BREAK {low_us:.1f}us"))
        k = j
        while k < n and bits[k] == 1:
            k += 1
        out.append((t0_us + j * dt_us, f"MAB {(k - j) * dt_us:.1f}us"))
        i = k
        continue
    # UART frame: sample each bit at its centre
    base = i + int(round(BIT_US * 0.5 / dt_us))
    step = BIT_US / dt_us
    byte = 0
    for b in range(8):
        idx = int(round(base + (1 + b) * step))
        if idx < n and bits[idx]:
            byte |= (1 << b)
    stop_idx = int(round(base + 9 * step))
    stop_ok = (stop_idx < n and bits[stop_idx] == 1)
    out.append((t0_us + i * dt_us, f"0x{byte:02X}" + ("" if stop_ok else " (bad stop)")))
    i = int(round(i + 11 * step)) - 2
    while i < n and bits[i] == 0:
        i += 1

print("decoded:")
line = []
for t, s in out:
    if s.startswith("BREAK") or s.startswith("MAB"):
        if line:
            print("   " + " ".join(line)); line = []
        print(f"  t={t:8.1f}us  {s}")
    else:
        line.append(s)
        if len(line) == 16:
            print("   " + " ".join(line)); line = []
if line:
    print("   " + " ".join(line))
print(f"total frames (bytes): {sum(1 for _, s in out if s.startswith('0x'))}")
