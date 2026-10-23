"""Minimal Modbus TCP client for the WB-MGU DMX register map (holding registers 0..511 = channels).

Only FC16 (write multiple registers) and FC03 (read holding registers) are needed; keeping the
client in the package avoids a pymodbus dependency the controller does not ship.
"""

from __future__ import annotations

import socket
import struct
import threading
from typing import Optional

FC_READ_HOLDING = 0x03
FC_WRITE_MULTIPLE = 0x10
MAX_WRITE_REGISTERS = 123          # Modbus limit per FC16 request
REG_BLACKOUT = 1000


class ModbusError(Exception):
    """Transport failure or a Modbus exception response."""


class DmxGatewayClient:
    """Thread-safe client that keeps one TCP connection open and reconnects on failure."""

    def __init__(self, host: str, port: int = 502, unit_id: int = 1, timeout_s: float = 2.0) -> None:
        self.host, self.port, self.unit_id, self.timeout_s = host, port, unit_id, timeout_s
        self._sock: Optional[socket.socket] = None
        self._tid = 0
        self._lock = threading.Lock()

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def _ensure_locked(self) -> socket.socket:
        if self._sock is None:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
            self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return self._sock

    def _recv_exact(self, sock: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ModbusError("connection closed")
            buf += chunk
        return buf

    def _transact(self, pdu: bytes) -> bytes:
        with self._lock:
            try:
                sock = self._ensure_locked()
                self._tid = (self._tid + 1) & 0xFFFF
                sock.sendall(struct.pack(">HHHB", self._tid, 0, len(pdu) + 1, self.unit_id) + pdu)
                tid, _proto, length, _unit = struct.unpack(">HHHB", self._recv_exact(sock, 7))
                body = self._recv_exact(sock, length - 1)
            except (OSError, ModbusError) as e:
                self._close_locked()
                raise ModbusError(str(e)) from e
            if tid != self._tid:
                self._close_locked()
                raise ModbusError(f"transaction id mismatch: {tid} != {self._tid}")
            if body[0] & 0x80:
                raise ModbusError(f"Modbus exception {body[1]:#04x} for FC {body[0] & 0x7F:#04x}")
            return body

    def write_channels(self, start_channel: int, values: bytes) -> None:
        """Write DMX channels (1-based start) with as many FC16 requests as needed."""
        offset = 0
        while offset < len(values):
            chunk = values[offset:offset + MAX_WRITE_REGISTERS]
            addr = start_channel - 1 + offset
            pdu = struct.pack(">BHHB", FC_WRITE_MULTIPLE, addr, len(chunk), len(chunk) * 2)
            pdu += struct.pack(f">{len(chunk)}H", *chunk)
            self._transact(pdu)
            offset += len(chunk)

    def read_channels(self, start_channel: int, count: int) -> list[int]:
        body = self._transact(struct.pack(">BHH", FC_READ_HOLDING, start_channel - 1, count))
        return list(struct.unpack(f">{count}H", body[2:2 + count * 2]))

    def blackout(self) -> None:
        self._transact(struct.pack(">BHH", 0x06, REG_BLACKOUT, 1))
