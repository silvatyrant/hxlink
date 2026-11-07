"""Hxlight command encoding / decoding (codec ``hxlight_v0`` of ha-ble-adv).

Plain 12 byte layout::

    [0]     0x83 + arg3   (arg3: 0 default, 1 RGB light, 3 on / off / timer)
    [1:3]   controller id, big endian
    [3]     group index (1-4)
    [4]     param
    [5]     cmd
    [6]     arg0
    [7:11]  check bytes / extra args, depends on the command (see _check_bytes)
    [11]    tx counter

The bulb ignores a frame whose tx counter it has already seen, so every new
command must use a new counter.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .frame import from_adv, seal, to_adv, unseal

TX_MODULO = 0xFE  # counter wraps back to 0 after 0xFD, like ha-ble-adv

KELVIN_WARM = 3000
KELVIN_COLD = 6500

PRESETS = {"bright": 1, "warm": 2, "comfortable": 3}


@dataclass(frozen=True)
class Cmd:
    """A bulb command, before identity and counter are applied."""

    cmd: int
    param: int = 0
    arg0: int = 0
    arg1: int = 0
    arg2: int = 0
    arg3: int = 0


@dataclass(frozen=True)
class Decoded:
    cmd: Cmd
    id: int
    index: int
    tx: int


def _check_bytes(c: Cmd) -> bytes:
    inv = lambda v: ~v & 0xFF  # noqa: E731
    if c.cmd == 0x65 and c.param == 0x07:  # colour temperature
        return bytes([c.arg1, inv(c.cmd), 0x55, 0x55])
    if c.param == 0x09:  # timer / cancel timer: minutes, hours
        return bytes([c.arg1, c.arg2, 0x55, 0x55])
    if c.arg3 == 0x01:  # RGB light
        return bytes([inv(c.param), inv(c.cmd), 0x55, 0x55])
    if (c.cmd, c.param) in ((0x01, 0), (0x65, 0x05)):  # on / off, brightness
        return bytes([inv(c.param), inv(c.cmd), inv(c.arg0), 0x00])
    # pair / unpair / night mode / presets, as sent by the current app
    return bytes([inv(c.param), inv(c.cmd), 0x55, 0x55])


def plain_frame(c: Cmd, ctrl_id: int, index: int, tx: int) -> bytes:
    if not 0 <= ctrl_id <= 0xFFFF:
        raise ValueError("controller id must fit in 16 bits")
    if not 1 <= index <= 4:
        raise ValueError("group index must be 1-4 (0 / 'all' is not usable)")
    if not 0 <= tx < TX_MODULO:
        raise ValueError("tx counter out of range")
    return bytes([0x83 + c.arg3, *ctrl_id.to_bytes(2, "big"), index, c.param, c.cmd, c.arg0, *_check_bytes(c), tx])


def encode(c: Cmd, ctrl_id: int, index: int, tx: int) -> bytes:
    """Full 31 byte advertising data for a command."""
    return to_adv(seal(plain_frame(c, ctrl_id, index, tx)))


def decode(adv: bytes) -> Decoded | None:
    """Parse a full advertising data back into a command; None if not a valid Hxlight frame."""
    body = from_adv(adv)
    plain = unseal(body) if body is not None else None
    if plain is None:
        return None
    cmd = Cmd(cmd=plain[5], param=plain[4], arg0=plain[6], arg3=plain[0] - 0x83)
    if cmd.cmd == 0x65 and cmd.param == 0x07:
        cmd = replace(cmd, arg1=plain[7])
    elif cmd.param == 0x09:
        cmd = replace(cmd, arg1=plain[7], arg2=plain[8])
    return Decoded(cmd, int.from_bytes(plain[1:3], "big"), plain[3], plain[11])


def next_tx(tx: int) -> int:
    return (tx + 1) % TX_MODULO


# --- command builders -------------------------------------------------------


def on() -> Cmd:
    return Cmd(0x01, arg0=0x01, arg3=3)


def off() -> Cmd:
    return Cmd(0x01, arg0=0x02, arg3=3)


def brightness(percent: int) -> Cmd:
    if not 1 <= percent <= 100:
        raise ValueError("brightness must be 1-100")
    return Cmd(0x65, param=0x05, arg0=percent)


def color_temp(kelvin: int) -> Cmd:
    """3000 K = warmest (arg1 100), 6500 K = coldest (arg0 100). Direction to be confirmed on the bulb."""
    kelvin = max(KELVIN_WARM, min(KELVIN_COLD, kelvin))
    warm = round((KELVIN_COLD - kelvin) * 100 / (KELVIN_COLD - KELVIN_WARM))
    return Cmd(0x65, param=0x07, arg0=100 - warm, arg1=warm)


def night() -> Cmd:
    return Cmd(0x03, arg0=0x01)


def preset(name: str) -> Cmd:
    return Cmd(0x0E, arg0=PRESETS[name])


def timer(minutes: int) -> Cmd:
    if not 1 <= minutes <= 23 * 60 + 59:
        raise ValueError("timer must be 1 minute to 23h59")
    return Cmd(0x01, param=0x09, arg1=minutes % 60, arg2=minutes // 60, arg3=3)


def cancel_timer() -> Cmd:
    return Cmd(0x02, param=0x09, arg3=3)


def pair() -> Cmd:
    return Cmd(0x02, arg0=0x01)


def unpair() -> Cmd:
    return Cmd(0x02, arg0=0x02)
