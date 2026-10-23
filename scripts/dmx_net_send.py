#!/usr/bin/env python3
"""Send Art-Net or sACN (E1.31) DMX frames to the gateway from a PC, for testing the receivers.

Usage:
  dmx_net_send.py artnet <host> <port_address> [--fps 40] [--seconds 5] [--values 255,128,64]
  dmx_net_send.py sacn   <host|multicast> <universe> [--fps 40] [--seconds 5] [--priority 100] [--values ...]
  dmx_net_send.py poll   <host|broadcast>            send one ArtPoll and print the ArtPollReply

`--values` sets channels 1..N; the remaining channels ramp (channel i = i % 256) so a decoder can
tell frames apart. `sacn ... multicast` sends to the universe's 239.255.x.y group.
"""
import argparse
import socket
import struct
import sys
import time
import uuid

ARTNET_PORT = 6454
SACN_PORT = 5568


def universe_values(values):
    data = bytearray((i % 256) for i in range(1, 513))
    for i, v in enumerate(values):
        data[i] = v
    return bytes(data)


def artdmx(port_address, seq, data):
    return (b"Art-Net\0" + struct.pack("<H", 0x5000) + struct.pack(">H", 14) + bytes([seq, 0])
            + struct.pack("<H", port_address) + struct.pack(">H", len(data)) + data)


def artpoll():
    return b"Art-Net\0" + struct.pack("<H", 0x2000) + struct.pack(">H", 14) + bytes([0, 0])


def e131(universe, priority, seq, data, cid, source="wb-dmx-test", options=0):
    slots = len(data)
    root = (struct.pack(">HH", 0x0010, 0) + b"ASC-E1.17\0\0\0" + struct.pack(">H", 0x7000 | (110 + slots))
            + struct.pack(">I", 0x00000004) + cid)
    name = source.encode()[:63].ljust(64, b"\0")
    framing = (struct.pack(">H", 0x7000 | (88 + slots)) + struct.pack(">I", 0x00000002) + name
               + bytes([priority]) + struct.pack(">H", 0) + bytes([seq, options]) + struct.pack(">H", universe))
    dmp = (struct.pack(">H", 0x7000 | (11 + slots)) + bytes([0x02, 0xA1]) + struct.pack(">HHH", 0, 1, slots + 1)
           + b"\0" + data)
    return root + framing + dmp


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("proto", choices=["artnet", "sacn", "poll"])
    ap.add_argument("host")
    ap.add_argument("universe", nargs="?", type=int, default=0)
    ap.add_argument("--fps", type=float, default=40)
    ap.add_argument("--seconds", type=float, default=5)
    ap.add_argument("--priority", type=int, default=100)
    ap.add_argument("--values", default="")
    args = ap.parse_args(argv[1:])
    values = [int(v) for v in args.values.split(",") if v != ""]
    data = universe_values(values)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    if args.proto == "poll":
        host = "255.255.255.255" if args.host == "broadcast" else args.host
        sock.settimeout(2)
        sock.sendto(artpoll(), (host, ARTNET_PORT))
        try:
            while True:
                reply, addr = sock.recvfrom(1024)
                if reply[:8] == b"Art-Net\0" and struct.unpack("<H", reply[8:10])[0] == 0x2100:
                    short = reply[26:44].split(b"\0")[0].decode()
                    long_ = reply[44:108].split(b"\0")[0].decode()
                    net, sub, out = reply[18], reply[19], reply[190]
                    print(f"{addr[0]}: {short!r} / {long_!r} port-address {(net << 8) | (sub << 4) | out} "
                          f"bind {reply[211]} mac {reply[201:207].hex(':')}")
        except socket.timeout:
            pass
        return 0

    if args.proto == "sacn":
        host = f"239.255.{args.universe >> 8}.{args.universe & 0xFF}" if args.host == "multicast" else args.host
        port = SACN_PORT
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        cid = uuid.uuid4().bytes
    else:
        host, port, cid = args.host, ARTNET_PORT, None

    frames = int(args.fps * args.seconds)
    interval = 1 / args.fps
    t0 = time.perf_counter()
    for i in range(frames):
        seq = (i % 255) + 1
        pkt = artdmx(args.universe, seq, data) if args.proto == "artnet" else e131(args.universe, args.priority, seq, data, cid)
        sock.sendto(pkt, (host, port))
        next_t = t0 + (i + 1) * interval
        delay = next_t - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
    if args.proto == "sacn":
        sock.sendto(e131(args.universe, args.priority, 0, data, cid, options=0x40), (host, port))   # stream terminated
    print(f"sent {frames} {args.proto} frames to {host}:{port} universe {args.universe} at {args.fps} fps")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
