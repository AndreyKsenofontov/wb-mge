#!/usr/bin/env python3
"""Stress and soak tests for the WB-MGU DMX gateway, run from a PC on the same LAN.

Phases (pick with --phase, default all except soak):
  broadcast  Art-Net to the subnet broadcast and 255.255.255.255, ArtPoll by broadcast
  flood      Art-Net 200 fps + sACN 100 fps for 20 s; accepted fps and heap afterwards
  fuzz       2000 malformed / random datagrams to 6454 and 5568, then liveness
  modbus     200 connect-write-close cycles, then 8 parallel readers for 20 s
  churn      20x dmx -> disabled -> dmx and 10 settings changes (universe, slots)
  http       6 parallel pollers of /info and /ports/1/dmx for 30 s
  soak       --minutes N: light Art-Net stream + Modbus + HTTP polling, heap/fps log every 10 s

Every phase ends with a liveness check: /info answers, the DMX frame counter advances at the
configured rate, heap_free is reported. Exit code 1 if any check fails.

Usage: dmx_stress.py <gateway-ip> [--phase NAME ...] [--minutes 60] [--subnet-bcast 192.168.13.255]
"""
import argparse
import json
import os
import random
import socket
import struct
import sys
import threading
import time
import urllib.request
import http.cookiejar

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dmx_modbus_client import ModbusTcp          # noqa: E402
from dmx_net_send import artdmx, artpoll, e131, universe_values   # noqa: E402

ARTNET_PORT, SACN_PORT = 6454, 5568
FAILURES = []


def fail(msg):
    FAILURES.append(msg)
    print(f"  FAIL: {msg}")


def ok(msg):
    print(f"  ok: {msg}")


class Gateway:
    def __init__(self, host):
        self.host = host
        self.base = f"http://{host}"
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.call("POST", "/auth", {"login": "admin", "pass": "admin"})

    def call(self, method, path, body=None, timeout=8):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        with self.opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "null")

    def info(self):
        return self.call("GET", "/info")

    def dmx(self, port=1):
        return self.info()["dmx"][f"rs485_{port}"]

    def heap(self):
        i = self.info()
        return i["heap_free"], i["heap_min_free"]


def liveness(gw, label, expect_fps=40, seconds=3):
    """The device answers and the frame counter advances at the configured refresh rate."""
    try:
        d0 = gw.dmx()
        t0 = time.time()
        time.sleep(seconds)
        d1 = gw.dmx()
        dt = time.time() - t0
    except Exception as e:                        # noqa: BLE001
        fail(f"{label}: gateway unreachable: {e}")
        return
    fps = (d1["frames_sent"] - d0["frames_sent"]) / dt
    heap_free, heap_min = gw.heap()
    if not d1["active"]:
        fail(f"{label}: DMX output not active")
    elif abs(fps - expect_fps) > 2:
        fail(f"{label}: DMX frame rate {fps:.1f} fps, expected {expect_fps}")
    else:
        ok(f"{label}: alive, {fps:.1f} fps, heap_free {heap_free}, min {heap_min}")


# ---- phases -------------------------------------------------------------------------------------

def phase_broadcast(gw, host, subnet_bcast):
    print("== broadcast")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    data = universe_values([201, 202, 203])
    for target in (subnet_bcast, "255.255.255.255"):
        before = gw.dmx()["artnet"]["packets"]
        for i in range(20):
            sock.sendto(artdmx(0, i + 1, data), (target, ARTNET_PORT))
            time.sleep(0.025)
        time.sleep(0.5)
        after = gw.dmx()["artnet"]["packets"]
        if after - before >= 18:
            ok(f"ArtDmx to {target}: {after - before}/20 accepted")
        else:
            fail(f"ArtDmx to {target}: only {after - before}/20 accepted")
    mb = ModbusTcp(host)
    vals = mb.read_holding(0, 3)
    if vals == [201, 202, 203]:
        ok("broadcast data landed in the universe")
    else:
        fail(f"universe after broadcast: {vals}")
    sock.settimeout(2)
    sock.sendto(artpoll(), (subnet_bcast, ARTNET_PORT))
    got = False
    try:
        while True:
            reply, addr = sock.recvfrom(1024)
            if addr[0] == host and reply[:8] == b"Art-Net\0" and struct.unpack("<H", reply[8:10])[0] == 0x2100:
                got = True
    except socket.timeout:
        pass
    ok("ArtPollReply to a broadcast ArtPoll") if got else fail("no ArtPollReply to a broadcast ArtPoll")
    liveness(gw, "broadcast")


def stream(proto, host, universe, fps, seconds, values, stop=None, priority=100):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
    data = universe_values(values)
    cid = os.urandom(16)
    target = (host, ARTNET_PORT) if proto == "artnet" else (f"239.255.{universe >> 8}.{universe & 0xFF}", SACN_PORT)
    interval = 1 / fps
    t0 = time.perf_counter()
    i = 0
    sent = 0
    while (time.perf_counter() - t0) < seconds and not (stop and stop.is_set()):
        seq = (i % 255) + 1
        pkt = artdmx(universe, seq, data) if proto == "artnet" else e131(universe, priority, seq, data, cid)
        sock.sendto(pkt, target)
        sent += 1
        i += 1
        delay = t0 + i * interval - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
    return sent


def phase_flood(gw, host):
    print("== flood: Art-Net 200 fps + sACN 100 fps, 20 s")
    heap0, _ = gw.heap()
    results = {}
    th = [threading.Thread(target=lambda: results.__setitem__("a", stream("artnet", host, 0, 200, 20, [1, 2, 3]))),
          threading.Thread(target=lambda: results.__setitem__("s", stream("sacn", host, 1, 100, 20, [4, 5, 6])))]
    for t in th:
        t.start()
    time.sleep(10)
    mid = gw.dmx()
    for t in th:
        t.join()
    time.sleep(0.5)
    end = gw.dmx()
    heap1, heap_min = gw.heap()
    print(f"  sent artnet {results['a']}, sacn {results['s']}; accepted artnet {end['artnet']['packets']}, "
          f"sacn {end['sacn']['packets']}; mid-flood fps artnet {mid['artnet']['fps']}, sacn {mid['sacn']['fps']}")
    if mid["artnet"]["fps"] < 150:
        fail(f"Art-Net accepted only {mid['artnet']['fps']} fps during a 200 fps flood")
    else:
        ok(f"Art-Net kept up: {mid['artnet']['fps']} fps accepted")
    if mid["sacn"]["fps"] < 80:
        fail(f"sACN accepted only {mid['sacn']['fps']} fps during a 100 fps flood")
    else:
        ok(f"sACN kept up: {mid['sacn']['fps']} fps accepted")
    print(f"  heap before {heap0}, after {heap1}, min {heap_min}")
    liveness(gw, "flood")


def phase_fuzz(gw, host):
    print("== fuzz: 2000 malformed datagrams")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rnd = random.Random(1)
    good_art = artdmx(0, 1, universe_values([]))
    good_e131 = e131(1, 100, 1, universe_values([]), os.urandom(16))
    for i in range(2000):
        kind = i % 8
        if kind == 0:
            pkt = os.urandom(rnd.randint(1, 1400))
        elif kind == 1:
            pkt = good_art[: rnd.randint(1, len(good_art))]
        elif kind == 2:
            pkt = good_e131[: rnd.randint(1, len(good_e131))]
        elif kind == 3:
            b = bytearray(good_art); b[16] = 0xFF; b[17] = 0xFF; pkt = bytes(b)            # length 65535
        elif kind == 4:
            b = bytearray(good_e131); b[115 + 8] = 0xFF; b[115 + 9] = 0xFF; pkt = bytes(b)  # prop count 65535
        elif kind == 5:
            b = bytearray(good_art); b[rnd.randrange(len(b))] ^= 0xFF; pkt = bytes(b)
        elif kind == 6:
            b = bytearray(good_e131); b[rnd.randrange(len(b))] ^= 0xFF; pkt = bytes(b)
        else:
            pkt = good_art + os.urandom(200)                                                  # trailing junk
        sock.sendto(pkt, (host, ARTNET_PORT if kind in (0, 1, 3, 5, 7) else SACN_PORT))
        if i % 100 == 0:
            time.sleep(0.01)
    time.sleep(1)
    liveness(gw, "fuzz")


def phase_modbus(gw, host):
    print("== modbus: 200 connect/write/close cycles, then 8 parallel readers for 20 s")
    errors = 0
    t0 = time.time()
    for i in range(200):
        try:
            mb = ModbusTcp(host)
            mb.write_multiple(10, [i % 256, (i * 3) % 256])
            mb.sock.close()
        except Exception as e:                        # noqa: BLE001
            errors += 1
            if errors <= 3:
                print(f"  cycle {i}: {e}")
    print(f"  200 cycles in {time.time() - t0:.1f} s, errors {errors}")
    ok("connection churn") if errors == 0 else fail(f"connection churn: {errors} errors")

    counts = [0] * 8
    errs = [0] * 8
    stop = threading.Event()

    def reader(n):
        try:
            mb = ModbusTcp(host)
        except Exception as e:                        # noqa: BLE001
            errs[n] += 1
            print(f"  reader {n}: connect failed: {e}")
            return
        while not stop.is_set():
            try:
                mb.read_holding(0, 100)
                counts[n] += 1
            except Exception as e:                    # noqa: BLE001
                errs[n] += 1
                if errs[n] <= 2:
                    print(f"  reader {n}: {e}")
                try:
                    mb = ModbusTcp(host)
                except Exception:                     # noqa: BLE001
                    time.sleep(0.5)
    ths = [threading.Thread(target=reader, args=(n,)) for n in range(8)]
    for t in ths:
        t.start()
    time.sleep(20)
    stop.set()
    for t in ths:
        t.join()
    print(f"  reads per reader: {counts}, errors: {errs}")
    if sum(counts) < 8 * 20 * 5:
        fail(f"parallel readers achieved only {sum(counts)} reads")
    else:
        ok(f"{sum(counts)} reads from 8 parallel clients, {sum(errs)} errors")
    liveness(gw, "modbus")


def phase_churn(gw, host):
    print("== churn: 20x dmx->disabled->dmx, 10 settings changes")
    heap0, _ = gw.heap()
    for i in range(20):
        gw.call("POST", "/ports/1/mode", {"mode": "disabled"})
        time.sleep(0.3)
        gw.call("POST", "/ports/1/mode", {"mode": "dmx"})
        time.sleep(0.5)
    d = gw.dmx()
    if not d["active"]:
        fail("port not active after mode churn")
    for i in range(10):
        gw.call("POST", "/settings", {"rs485_1": {"dmx_artnet_universe": i % 3, "dmx_slots": 512 if i % 2 else 256,
                                                  "dmx_sacn_universe": 1 + (i % 2)}})
        time.sleep(1.5)
    gw.call("POST", "/settings", {"rs485_1": {"dmx_artnet_universe": 0, "dmx_slots": 512, "dmx_sacn_universe": 1}})
    time.sleep(2)
    heap1, heap_min = gw.heap()
    print(f"  heap before {heap0}, after {heap1}, min {heap_min}")
    if heap0 - heap1 > 4096:
        fail(f"heap dropped by {heap0 - heap1} bytes across the churn (leak?)")
    else:
        ok(f"heap stable across churn ({heap0} -> {heap1})")
    # network sources still alive after all the restarts
    stream("artnet", host, 0, 40, 1, [7, 8, 9])
    time.sleep(0.5)
    vals = ModbusTcp(host).read_holding(0, 3)
    ok("Art-Net still received after churn") if vals == [7, 8, 9] else fail(f"Art-Net after churn: {vals}")
    liveness(gw, "churn")


def phase_http(gw, host):
    print("== http: 6 parallel pollers for 30 s")
    counts = [0] * 6
    errs = [0] * 6
    stop = threading.Event()

    def poller(n):
        g = Gateway(host)
        while not stop.is_set():
            try:
                g.call("GET", "/info" if n % 2 else "/ports/1/dmx", timeout=10)
                counts[n] += 1
            except Exception as e:                    # noqa: BLE001
                errs[n] += 1
                if errs[n] <= 2:
                    print(f"  poller {n}: {e}")
                time.sleep(0.2)
    ths = [threading.Thread(target=poller, args=(n,)) for n in range(6)]
    for t in ths:
        t.start()
    time.sleep(30)
    stop.set()
    for t in ths:
        t.join()
    print(f"  requests per poller: {counts}, errors: {errs}")
    if sum(errs) > sum(counts) * 0.02:
        fail(f"HTTP errors {sum(errs)} of {sum(counts) + sum(errs)} requests")
    else:
        ok(f"{sum(counts)} requests, {sum(errs)} errors")
    liveness(gw, "http")


def phase_soak(gw, host, minutes, log_path):
    print(f"== soak: {minutes} min, Art-Net 40 fps + Modbus writes + /info polling")
    stop = threading.Event()
    th = threading.Thread(target=stream, args=("artnet", host, 0, 40, minutes * 60 + 5, [10, 20, 30], stop))
    th.start()

    def modbus_writer():
        mb = None
        n = 0
        while not stop.is_set():
            try:
                if mb is None:
                    mb = ModbusTcp(host)
                mb.write_multiple(100, [n % 256] * 8)
                n += 1
            except Exception:                         # noqa: BLE001
                mb = None
                time.sleep(1)
            time.sleep(0.2)
    tw = threading.Thread(target=modbus_writer)
    tw.start()

    t0 = time.time()
    last_frames = gw.dmx()["frames_sent"]
    last_t = t0
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"# soak start {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        while time.time() - t0 < minutes * 60:
            time.sleep(10)
            try:
                i = gw.info()
                d = i["dmx"]["rs485_1"]
                now = time.time()
                fps = (d["frames_sent"] - last_frames) / (now - last_t)
                last_frames, last_t = d["frames_sent"], now
                line = (f"{time.strftime('%H:%M:%S')} heap_free={i['heap_free']} min={i['heap_min_free']} "
                        f"dmx_fps={fps:.1f} artnet_fps={d['artnet']['fps']} artnet_pk={d['artnet']['packets']} "
                        f"active={d['active']} lost={d['signal_lost']}")
                print("  " + line)
                log.write(line + "\n")
                log.flush()
                if abs(fps - 40) > 2:
                    fail(f"soak: DMX rate {fps:.1f} fps at {time.strftime('%H:%M:%S')}")
                if not d["artnet"]["active"]:
                    fail(f"soak: Art-Net not active at {time.strftime('%H:%M:%S')}")
            except Exception as e:                    # noqa: BLE001
                fail(f"soak: gateway unreachable at {time.strftime('%H:%M:%S')}: {e}")
                log.write(f"{time.strftime('%H:%M:%S')} UNREACHABLE {e}\n")
    stop.set()
    th.join()
    tw.join()
    liveness(gw, "soak")


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("host")
    ap.add_argument("--phase", action="append", choices=["broadcast", "flood", "fuzz", "modbus", "churn", "http", "soak"])
    ap.add_argument("--minutes", type=int, default=60)
    ap.add_argument("--subnet-bcast", default="192.168.13.255")
    ap.add_argument("--log", default="dmx_soak.log")
    args = ap.parse_args(argv[1:])
    phases = args.phase or ["broadcast", "flood", "fuzz", "modbus", "churn", "http"]
    gw = Gateway(args.host)
    print(f"gateway {args.host}: firmware {gw.info()['firmware']}, heap_free {gw.heap()[0]}")
    for ph in phases:
        {"broadcast": lambda: phase_broadcast(gw, args.host, args.subnet_bcast),
         "flood": lambda: phase_flood(gw, args.host),
         "fuzz": lambda: phase_fuzz(gw, args.host),
         "modbus": lambda: phase_modbus(gw, args.host),
         "churn": lambda: phase_churn(gw, args.host),
         "http": lambda: phase_http(gw, args.host),
         "soak": lambda: phase_soak(gw, args.host, args.minutes, args.log)}[ph]()
    print("== summary")
    if FAILURES:
        for f in FAILURES:
            print("  FAIL:", f)
        return 1
    print("  all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
