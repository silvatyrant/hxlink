import pytest

from hxlink import codec
from hxlink.frame import HEADER, from_adv, rev_crc16_ccit, seal, unseal, whiten

from .vectors import VECTORS

# label -> command the app was sending, for frames our builders must reproduce byte for byte
BUILDERS = {
    "LIGHT OFF": codec.off(),
    "LIGHT ON": codec.on(),
    "LIGHT OFF - group 2": codec.off(),
    "BRIGHTNESS 44%": codec.brightness(44),
    "BRIGHTNESS 100%": codec.brightness(100),
    "BRIGHTNESS 1%": codec.brightness(1),
    "BRIGHTNESS 21% - group 2": codec.brightness(21),
    "COLOR TEMPERATURE COLD": codec.color_temp(6500),
    "COLOR TEMPERATURE WARM": codec.color_temp(3000),
    "PAIR": codec.pair(),
    "UNPAIR": codec.unpair(),
    "TIMER 1 H 12 minutes => 4320s": codec.timer(72),
    "NIGHT MODE - newer app check bytes": codec.night(),
    "PRESET BRIGHT": codec.preset("bright"),
    "PRESET COMFORTABLE": codec.preset("comfortable"),
    "PRESET WARM": codec.preset("warm"),
}
# The older app version sent different check bytes for night mode; we mimic the current app.
OLD_APP_ONLY = {"NIGHT MODE"}


def adv(hexstr: str) -> bytes:
    return bytes.fromhex(hexstr)


@pytest.mark.parametrize(("label", "hexstr", "ctrl_id", "index", "tx"), VECTORS, ids=[v[0] for v in VECTORS])
def test_real_frames_decode(label, hexstr, ctrl_id, index, tx):
    d = codec.decode(adv(hexstr))
    assert d is not None
    assert (d.id, d.index, d.tx) == (ctrl_id, index, tx)


@pytest.mark.parametrize(("label", "hexstr", "ctrl_id", "index", "tx"), VECTORS, ids=[v[0] for v in VECTORS])
def test_real_frames_reencode_identically(label, hexstr, ctrl_id, index, tx):
    if label in OLD_APP_ONLY:
        pytest.skip("older app variant of the check bytes, not reproduced on purpose")
    d = codec.decode(adv(hexstr))
    assert codec.encode(d.cmd, d.id, d.index, d.tx) == adv(hexstr)


@pytest.mark.parametrize(("label", "hexstr", "ctrl_id", "index", "tx"), [v for v in VECTORS if v[0] in BUILDERS], ids=[v[0] for v in VECTORS if v[0] in BUILDERS])
def test_builders_reproduce_real_frames(label, hexstr, ctrl_id, index, tx):
    assert codec.encode(BUILDERS[label], ctrl_id, index, tx) == adv(hexstr)


def test_color_temp_53_percent_frame():
    # real frame: arg0=53 (cold share), arg1=47 (warm share) -> 6500 - 0.47 * 3500 = 4855 K
    label, hexstr, ctrl_id, index, tx = next(v for v in VECTORS if v[0] == "COLOR TEMPERATURE 53%")
    assert codec.encode(codec.color_temp(4855), ctrl_id, index, tx) == adv(hexstr)


def test_frame_shape():
    frame = codec.encode(codec.on(), 0x1234, 1, 1)
    assert len(frame) == 31
    assert frame[:5] == bytes([0x02, 0x01, 0x01, 0x1B, 0xFF])
    assert frame[5:15] == HEADER
    assert frame[-1] == 0x18


def test_corrupted_frames_rejected():
    frame = bytearray(codec.encode(codec.on(), 0x1234, 1, 1))
    for pos in range(15, 30):  # any flipped bit in the whitened body breaks checksum or CRC
        bad = bytearray(frame)
        bad[pos] ^= 0x01
        assert codec.decode(bytes(bad)) is None
    assert codec.decode(bytes(frame[:-1])) is None
    wrong_flags = bytearray(frame)
    wrong_flags[2] = 0x06  # what BlueZ would write by itself
    assert from_adv(bytes(wrong_flags)) is None


def test_seal_unseal_roundtrip_and_whiten_involution():
    plain = bytes(range(12))
    assert unseal(seal(plain)) == plain
    assert whiten(whiten(plain, 0x59), 0x59) == plain
    assert 0 <= rev_crc16_ccit(plain) <= 0xFFFF


@pytest.mark.parametrize(
    "c",
    [codec.on(), codec.off(), codec.night(), codec.pair(), codec.unpair(), codec.cancel_timer()]
    + [codec.brightness(p) for p in (1, 2, 50, 99, 100)]
    + [codec.color_temp(k) for k in (3000, 3500, 5000, 6500)]
    + [codec.preset(n) for n in codec.PRESETS]
    + [codec.timer(m) for m in (1, 59, 60, 72, 1439)],
)
@pytest.mark.parametrize("index", [1, 2, 3, 4])
def test_roundtrip_every_command(c, index):
    d = codec.decode(codec.encode(c, 0xBEEF, index, 200))
    assert d == codec.Decoded(c, 0xBEEF, index, 200)


def test_color_temp_mapping_and_clamping():
    assert (codec.color_temp(3000).arg0, codec.color_temp(3000).arg1) == (0, 100)
    assert (codec.color_temp(6500).arg0, codec.color_temp(6500).arg1) == (100, 0)
    assert codec.color_temp(2000) == codec.color_temp(3000)
    assert codec.color_temp(9000) == codec.color_temp(6500)
    mid = codec.color_temp(4750)
    assert mid.arg0 + mid.arg1 == 100


def test_timer_split_and_limits():
    t = codec.timer(72)
    assert (t.arg1, t.arg2) == (12, 1)  # minutes, hours (matches the real app frame)
    with pytest.raises(ValueError):
        codec.timer(0)
    with pytest.raises(ValueError):
        codec.timer(24 * 60)


@pytest.mark.parametrize("bad", [0, 101, -5])
def test_brightness_limits(bad):
    with pytest.raises(ValueError):
        codec.brightness(bad)


def test_group_zero_and_five_rejected():
    for idx in (0, 5):
        with pytest.raises(ValueError):
            codec.encode(codec.on(), 1, idx, 1)


def test_tx_counter_wraps():
    assert codec.next_tx(1) == 2
    assert codec.next_tx(0xFD) == 0
    with pytest.raises(ValueError):
        codec.encode(codec.on(), 1, 1, 0xFE)
