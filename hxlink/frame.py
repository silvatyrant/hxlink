"""Low level Hxlight frame helpers: whitening, CRC, on-air framing.

Re-implemented from the MIT licensed `ha-ble-adv` project (codecs/utils.py and
codecs/hxlight.py, commit ec78210bd9b227bd633bbed988739645e9c7f6f4).
See LICENSE.ha-ble-adv.
"""

from __future__ import annotations

from binascii import crc_hqx

WHITEN_SEED = 0x59
CRC_SEED = 0xB12B

# company id 0xFFF0 (little endian) + 8 fixed bytes
HEADER = bytes([0xF0, 0xFF, 0x6D, 0xB6, 0x43, 0x59, 0x3A, 0x60, 0xA7, 0xA1])
FOOTER = bytes([0x18])
AD_FLAGS = bytes([0x02, 0x01, 0x01])
AD_TYPE_MANUFACTURER = 0xFF
BODY_LEN = 15  # 12 plain bytes + checksum + crc16


def whiten(buffer: bytes, seed: int) -> bytes:
    """Whiten / unwhiten a buffer (the operation is its own inverse)."""
    out = bytearray()
    r = seed
    for val in buffer:
        b = 0
        for j in range(8):
            r <<= 1
            if r & 0x80:
                r ^= 0x11
                b |= 1 << j
            r &= 0x7F
        out.append(val ^ b)
    return bytes(out)


def _reverse_8(x: int) -> int:
    x = ((x & 0x55) << 1) | ((x & 0xAA) >> 1)
    x = ((x & 0x33) << 2) | ((x & 0xCC) >> 2)
    return ((x & 0x0F) << 4) | ((x & 0xF0) >> 4)


def _reverse_16(x: int) -> int:
    x = ((x & 0x5555) << 1) | ((x >> 1) & 0x5555)
    x = ((x & 0x3333) << 2) | ((x >> 2) & 0x3333)
    x = ((x & 0x0F0F) << 4) | ((x >> 4) & 0x0F0F)
    return ((x & 0x00FF) << 8) | ((x >> 8) & 0x00FF)


def rev_crc16_ccit(buffer: bytes, seed: int = CRC_SEED) -> int:
    """CRC16 CCITT over the bit reversed buffer, result bit reversed."""
    return _reverse_16(crc_hqx(bytes(_reverse_8(x) for x in buffer), seed))


def seal(plain: bytes) -> bytes:
    """12 plain bytes -> 15 whitened bytes (plain + checksum + crc16 LE)."""
    if len(plain) != 12:
        raise ValueError("plain buffer must be 12 bytes")
    buf = plain + bytes([sum(plain) & 0xFF])
    buf += rev_crc16_ccit(buf).to_bytes(2, "little")
    return whiten(buf, WHITEN_SEED)


def unseal(body: bytes) -> bytes | None:
    """15 whitened bytes -> 12 plain bytes, or None if checksum / CRC fail."""
    if len(body) != BODY_LEN:
        return None
    buf = whiten(body, WHITEN_SEED)
    if rev_crc16_ccit(buf[:-2]) != int.from_bytes(buf[-2:], "little"):
        return None
    if sum(buf[:12]) & 0xFF != buf[12]:
        return None
    return buf[:12]


def to_adv(body: bytes) -> bytes:
    """Wrap a sealed body into the full 31 byte advertising data (flags + manufacturer AD)."""
    payload = HEADER + body + FOOTER
    return AD_FLAGS + bytes([len(payload) + 1, AD_TYPE_MANUFACTURER]) + payload


def from_adv(adv: bytes) -> bytes | None:
    """Full advertising data -> sealed body, or None if the framing doesn't match."""
    if len(adv) != 31 or adv[:3] != AD_FLAGS or adv[3] != 0x1B or adv[4] != AD_TYPE_MANUFACTURER:
        return None
    payload = adv[5:]
    if payload[: len(HEADER)] != HEADER or payload[-len(FOOTER) :] != FOOTER:
        return None
    return payload[len(HEADER) : -len(FOOTER)]
