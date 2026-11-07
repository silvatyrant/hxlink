"""Command line sender: ``python -m hxlink on`` etc."""

from __future__ import annotations

import argparse
import os

from . import codec, state

DEFAULT_STATE = os.environ.get("HXLINK_STATE", os.path.expanduser("~/.hxlink/state.json"))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hxlink", description="Send Hxlight bulb commands as BLE advertisements")
    p.add_argument("--hci", type=int, default=0, help="adapter index (hci0 = 0)")
    p.add_argument("--state", default=DEFAULT_STATE, help="state file (controller id + tx counter)")
    p.add_argument("--id", type=lambda v: int(v, 0), help="override controller id (e.g. 0xF9F8)")
    p.add_argument("--index", type=int, help="group index 1-4")
    p.add_argument("--hold-ms", type=int, default=450, help="how long to advertise each command")
    p.add_argument("--dry-run", action="store_true", help="print the frame, don't transmit or touch the state file")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("on")
    sub.add_parser("off")
    sub.add_parser("night")
    sub.add_parser("pair")
    sub.add_parser("unpair")
    sub.add_parser("cancel-timer")
    sub.add_parser("show-id", help="print the controller id / index / counter")
    s = sub.add_parser("brightness")
    s.add_argument("percent", type=int)
    s = sub.add_parser("ct", help="colour temperature in kelvin (3000 warm - 6500 cold)")
    s.add_argument("kelvin", type=int)
    s = sub.add_parser("preset")
    s.add_argument("name", choices=sorted(codec.PRESETS))
    s = sub.add_parser("timer")
    s.add_argument("minutes", type=int)
    return p


def command_from_args(a: argparse.Namespace) -> codec.Cmd:
    c = a.command
    if c == "brightness":
        return codec.brightness(a.percent)
    if c == "ct":
        return codec.color_temp(a.kelvin)
    if c == "preset":
        return codec.preset(a.name)
    if c == "timer":
        return codec.timer(a.minutes)
    return {
        "on": codec.on,
        "off": codec.off,
        "night": codec.night,
        "pair": codec.pair,
        "unpair": codec.unpair,
        "cancel-timer": codec.cancel_timer,
    }[c]()


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    st = state.load(a.state, a.id, a.index)
    if a.command == "show-id":
        print(f"id=0x{st.id:04X} index={st.index} tx={st.tx} state={a.state}")
        return 0
    cmd = command_from_args(a)
    if a.dry_run:
        tx = codec.next_tx(st.tx)
        print(codec.encode(cmd, st.id, st.index, tx).hex(" "))
        return 0
    tx = state.advance(a.state, st)
    adv = codec.encode(cmd, st.id, st.index, tx)
    print(f"id=0x{st.id:04X} index={st.index} tx={tx} {a.command} -> {adv.hex(' ')}")
    from .mgmt import Mgmt  # imported late: needs AF_BLUETOOTH (Linux only)

    with Mgmt(a.hci) as m:
        m.advertise(adv, a.hold_ms / 1000)
    return 0
