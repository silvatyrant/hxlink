import json
import struct

import pytest

from hxlink import cli, codec, mgmt, state


def test_state_persists_before_sending(tmp_path):
    path = str(tmp_path / "s" / "state.json")
    st = state.load(path, ctrl_id=0xABCD, index=2)
    st.tx = 10
    assert state.advance(path, st) == 11
    again = state.load(path)
    assert (again.id, again.index, again.tx) == (0xABCD, 2, 11)


def test_state_first_run_generates_valid_identity(tmp_path):
    st = state.load(str(tmp_path / "none.json"))
    assert 1 <= st.id <= 0xFFFE and st.index == 1 and 0 <= st.tx < codec.TX_MODULO


def test_state_override_does_not_touch_file(tmp_path):
    path = str(tmp_path / "state.json")
    state.save(path, state.State(id=1, index=1, tx=5))
    st = state.load(path, ctrl_id=0x2222)
    assert st.id == 0x2222 and json.load(open(path))["id"] == 1


def test_counter_wrap_persisted(tmp_path):
    path = str(tmp_path / "state.json")
    st = state.State(id=1, tx=codec.TX_MODULO - 1)
    assert state.advance(path, st) == 0


def test_add_advertising_packet_matches_kernel_format():
    adv = codec.encode(codec.on(), 0x1234, 1, 1)
    pkt = mgmt.add_advertising_packet(0, 5, adv, timeout_s=2)
    opcode, index, plen = struct.unpack_from("<HHH", pkt)
    assert (opcode, index, plen) == (0x003E, 0, 42)  # plen 42 == what btmon showed for btmgmt add-adv
    instance, flags, duration, timeout, dlen, slen = struct.unpack_from("<BIHHBB", pkt, 6)
    assert (instance, flags, duration, timeout, dlen, slen) == (5, 0, 0, 2, 31, 0)
    assert pkt[17:] == adv


def test_remove_advertising_packet():
    assert mgmt.remove_advertising_packet(0, 5) == bytes.fromhex("3f00" "0000" "0100" "05")


def test_cli_dry_run_prints_valid_frame_and_leaves_state_alone(tmp_path, capsys):
    path = str(tmp_path / "state.json")
    state.save(path, state.State(id=0x4F80, index=1, tx=173))
    assert cli.main(["--state", path, "--dry-run", "pair"]) == 0
    out = bytes.fromhex(capsys.readouterr().out.strip().replace(" ", ""))
    d = codec.decode(out)
    assert d.cmd == codec.pair() and (d.id, d.index, d.tx) == (0x4F80, 1, 174)
    assert json.load(open(path))["tx"] == 173
    # real app PAIR frame for the same id / index / counter
    assert out.hex().upper() == "0201011BFFF0FF6DB643593A60A7A166527EB951F82B4B1A81591884640E18"


@pytest.mark.parametrize(
    "argv",
    [["on"], ["off"], ["night"], ["pair"], ["unpair"], ["cancel-timer"], ["brightness", "50"], ["ct", "4000"], ["preset", "warm"], ["timer", "60"]],
)
def test_cli_every_command_dry_run(tmp_path, capsys, argv):
    assert cli.main(["--state", str(tmp_path / "s.json"), "--dry-run", *argv]) == 0
    assert codec.decode(bytes.fromhex(capsys.readouterr().out.strip().replace(" ", ""))) is not None
