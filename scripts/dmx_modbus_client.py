#!/usr/bin/env python3
"""Tiny Modbus TCP client for the WB-MGU DMX register map (no third-party modules).

Usage:
  dmx_modbus_client.py <host[:port]> read <start> <count>
  dmx_modbus_client.py <host[:port]> write <channel> <value> [<value> ...]   (1-based channel, FC16)
  dmx_modbus_client.py <host[:port]> single <register> <value>               (FC06)
  dmx_modbus_client.py <host[:port]> blackout
  dmx_modbus_client.py <host[:port]> status
"""
import socket
import struct
import sys

UNIT_ID = 1
REG_BLACKOUT = 1000
STATUS_NAMES = ["blackout", "active", "slots", "refresh_hz", "frames_lo", "frames_hi", "tx_disabled"]


class ModbusTcp:
    def __init__(self, host, port=502, timeout=3.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.tid = 0

    def _transact(self, pdu):
        self.tid = (self.tid + 1) & 0xFFFF
        adu = struct.pack(">HHHB", self.tid, 0, len(pdu) + 1, UNIT_ID) + pdu
        self.sock.sendall(adu)
        hdr = self._recv_exact(7)
        tid, proto, length, unit = struct.unpack(">HHHB", hdr)
        body = self._recv_exact(length - 1)
        if tid != self.tid:
            raise RuntimeError(f"transaction id mismatch: {tid} != {self.tid}")
        if body[0] & 0x80:
            raise RuntimeError(f"Modbus exception {body[1]:#04x} for FC {body[0] & 0x7F:#04x}")
        return body

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise RuntimeError("connection closed")
            buf += chunk
        return buf

    def read_holding(self, start, count):
        body = self._transact(struct.pack(">BHH", 0x03, start, count))
        return list(struct.unpack(f">{count}H", body[2:2 + count * 2]))

    def write_single(self, addr, value):
        self._transact(struct.pack(">BHH", 0x06, addr, value))

    def write_multiple(self, addr, values):
        pdu = struct.pack(">BHHB", 0x10, addr, len(values), len(values) * 2)
        pdu += struct.pack(f">{len(values)}H", *values)
        self._transact(pdu)


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    host, _, port = argv[1].partition(":")
    mb = ModbusTcp(host, int(port) if port else 502)
    cmd = argv[2]
    if cmd == "read":
        start, count = int(argv[3]), int(argv[4])
        print(mb.read_holding(start, count))
    elif cmd == "write":
        channel = int(argv[3])
        values = [int(v) for v in argv[4:]]
        mb.write_multiple(channel - 1, values)
        print("ok")
    elif cmd == "single":
        mb.write_single(int(argv[3]), int(argv[4]))
        print("ok")
    elif cmd == "blackout":
        mb.write_single(REG_BLACKOUT, 1)
        print("ok")
    elif cmd == "status":
        regs = mb.read_holding(REG_BLACKOUT, len(STATUS_NAMES))
        status = dict(zip(STATUS_NAMES, regs))
        status["frames_sent"] = status["frames_lo"] | (status["frames_hi"] << 16)
        print(status)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
