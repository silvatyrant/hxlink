"""Transmit advertising data through the kernel Bluetooth management socket.

Uses MGMT Add Advertising / Remove Advertising, i.e. the same path as
``btmgmt add-adv`` and BlueZ. The data is sent verbatim (flags AD included),
the adapter is never reset and bluetoothd / other scanners keep working.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import socket
import struct
import time

HCI_DEV_NONE = 0xFFFF
HCI_CHANNEL_CONTROL = 3
OP_ADD_ADVERTISING = 0x003E
OP_REMOVE_ADVERTISING = 0x003F
EV_CMD_COMPLETE = 0x0001
EV_CMD_STATUS = 0x0002
MAX_INSTANCE = 5  # this controller reports SupportedInstances = 5


def add_advertising_packet(hci: int, instance: int, adv: bytes, timeout_s: int) -> bytes:
    params = struct.pack("<BIHHBB", instance, 0, 0, timeout_s, len(adv), 0) + adv
    return struct.pack("<HHH", OP_ADD_ADVERTISING, hci, len(params)) + params


def remove_advertising_packet(hci: int, instance: int) -> bytes:
    return struct.pack("<HHHB", OP_REMOVE_ADVERTISING, hci, 1, instance)


class MgmtError(RuntimeError):
    pass


def _bind_control(sock: socket.socket) -> None:
    """Bind to the management (control) channel.

    Python < 3.11 can't express the HCI channel in bind(), so fill in
    ``struct sockaddr_hci`` ourselves.
    """
    try:
        sock.bind((HCI_DEV_NONE, HCI_CHANNEL_CONTROL))
        return
    except OSError:
        pass
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    addr = struct.pack("<HHH", socket.AF_BLUETOOTH, HCI_DEV_NONE, HCI_CHANNEL_CONTROL)
    if libc.bind(sock.fileno(), addr, len(addr)) != 0:
        err = ctypes.get_errno()
        raise OSError(err, f"bind(HCI control channel) failed: errno {err}")


class Mgmt:
    def __init__(self, hci: int = 0) -> None:
        self.hci = hci
        self.sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
        _bind_control(self.sock)

    def close(self) -> None:
        self.sock.close()

    def __enter__(self) -> "Mgmt":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _call(self, packet: bytes, opcode: int, timeout: float = 2.0) -> bytes:
        self.sock.send(packet)
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise MgmtError("timeout waiting for management response")
            self.sock.settimeout(left)
            data = self.sock.recv(1024)
            if len(data) < 6:
                continue
            event, index, plen = struct.unpack_from("<HHH", data)
            if event not in (EV_CMD_COMPLETE, EV_CMD_STATUS) or index != self.hci or plen < 3:
                continue
            op, status = struct.unpack_from("<HB", data, 6)
            if op != opcode:
                continue
            if status != 0:
                raise MgmtError(f"opcode 0x{opcode:04X} failed with status 0x{status:02X}")
            return data[9 : 6 + plen]

    def advertise(self, adv: bytes, hold_s: float, instance: int = MAX_INSTANCE) -> None:
        """Broadcast ``adv`` for ``hold_s`` seconds, then remove it (kernel timeout is a safety net)."""
        if len(adv) > 31:
            raise ValueError("advertising data too long")
        self._call(add_advertising_packet(self.hci, instance, adv, timeout_s=max(2, int(hold_s) + 2)), OP_ADD_ADVERTISING)
        try:
            time.sleep(hold_s)
        finally:
            try:
                self._call(remove_advertising_packet(self.hci, instance), OP_REMOVE_ADVERTISING)
            except MgmtError:
                pass  # already expired
