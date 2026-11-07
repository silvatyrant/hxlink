"""MQTT -> BLE bridge: Home Assistant (MQTT light, JSON schema) controls an Hxlight bulb.

HA publishes commands, we turn them into Hxlight advertisements, then publish an
optimistic state back (the bulb can't report its state, and the remote can't be heard).

Topics (``<base>`` = ``hxlink/<controller id, 4 hex digits>``)::

    <base>/status                 online / offline (retained, last will; shared by all bulbs)
    <base>/light/set              HA JSON light command
    <base>/light/state            optimistic state (retained)
    <base>/button/<key>/press     pair, unpair, night, cancel_timer
    <base>/timer/set              sleep timer in minutes (number entity)

The bulb on group slot 1 uses the topics above. Every further bulb (slot N, same controller id) uses
``<base>/<N>/...`` instead, and its entities get an ``_N`` suffix in their unique ids.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import signal
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from . import codec, state

log = logging.getLogger("hxlink")

NIGHT = "Night"
NIGHT_BRIGHTNESS = 2
HEARTBEAT_FILE = os.environ.get("HXLINK_HEARTBEAT", "/tmp/hxlink-heartbeat")
DISCOVERY_PREFIX = os.environ.get("MQTT_DISCOVERY_PREFIX", "homeassistant")

BUTTONS: dict[str, tuple[str, Callable[[], codec.Cmd]]] = {
    "night": ("Night mode", codec.night),
    "cancel_timer": ("Cancel sleep timer", codec.cancel_timer),
    "pair": ("Pair", codec.pair),
    "unpair": ("Unpair", codec.unpair),
}
CONFIG_BUTTONS = {"pair", "unpair"}
# presets are light effects; the name is what HA shows
PRESET_EFFECTS = {"Bright": "bright", "Warm": "warm", "Comfortable": "comfortable"}
EFFECTS = [NIGHT, *PRESET_EFFECTS]
# discovery entities from earlier versions, cleared from the broker on connect
REMOVED_BUTTONS = ("preset_bright", "preset_warm", "preset_comfortable")


def mireds(kelvin: int) -> int:
    return round(1_000_000 / kelvin)


def kelvin(mired: float) -> int:
    return round(1_000_000 / mired)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def plan_light(payload: dict) -> list[codec.Cmd]:
    """HA light command -> bulb commands. Order matters: on, then brightness, then colour temperature."""
    if str(payload.get("state", "")).upper() == "OFF":
        return [codec.off()]
    if payload.get("effect") == NIGHT:
        return [codec.night()]  # night mode switches the bulb on by itself
    if payload.get("effect") in PRESET_EFFECTS:
        # presets are ignored while the bulb is off, so switch it on first
        return [codec.on(), codec.preset(PRESET_EFFECTS[payload["effect"]])]
    cmds: list[codec.Cmd] = []
    if str(payload.get("state", "")).upper() == "ON":
        cmds.append(codec.on())
    if "brightness" in payload:
        cmds.append(codec.brightness(_clamp(round(float(payload["brightness"])), 1, 100)))
    if "color_temp" in payload:
        cmds.append(codec.color_temp(kelvin(float(payload["color_temp"]))))
    return cmds


def next_light_state(current: dict, payload: dict) -> dict:
    """Optimistic light state after a command."""
    new = dict(current)
    if str(payload.get("state", "")).upper() == "OFF":
        new["state"] = "OFF"
        return new
    new["state"] = "ON"
    new["color_mode"] = "color_temp"
    if payload.get("effect") == NIGHT:
        new.update(effect=NIGHT, brightness=NIGHT_BRIGHTNESS, color_temp=mireds(codec.KELVIN_WARM))
        return new
    if payload.get("effect") in PRESET_EFFECTS:
        new["effect"] = payload["effect"]  # the preset's brightness/colour are not known to us
        return new
    if "brightness" in payload:
        new["brightness"] = _clamp(round(float(payload["brightness"])), 1, 100)
        new.pop("effect", None)
    if "color_temp" in payload:
        new["color_temp"] = mireds(_clamp(kelvin(float(payload["color_temp"])), codec.KELVIN_WARM, codec.KELVIN_COLD))
        new.pop("effect", None)
    return new


def parse_bulbs(spec: str, default_index: int, default_name: str) -> list[tuple[int, str]]:
    """``"1=Bedroom lamp,2=Study lamp"`` -> [(1, "Bedroom lamp"), (2, "Study lamp")]; empty = one default bulb."""
    if not spec.strip():
        return [(default_index, default_name)]
    bulbs: list[tuple[int, str]] = []
    for item in spec.split(","):
        idx, sep, name = item.partition("=")
        if not sep or not name.strip():
            raise ValueError(f"bad BULBS entry {item!r}, expected INDEX=NAME")
        bulbs.append((int(idx), name.strip()))
    indexes = [i for i, _ in bulbs]
    if len(set(indexes)) != len(indexes) or not all(1 <= i <= 4 for i in indexes):
        raise ValueError(f"BULBS indexes must be unique and 1-4, got {indexes}")
    return bulbs


def bulb_base(base: str, index: int) -> str:
    return base if index == 1 else f"{base}/{index}"


def bulb_node(node: str, index: int) -> str:
    return node if index == 1 else f"{node}_{index}"


@dataclass
class Job:
    cmds: list[codec.Cmd]
    publish: list[tuple[str, str, bool]] = field(default_factory=list)  # (topic, payload, retain) after success
    index: int | None = None  # group slot to address; None = the controller's own


class Router:
    """Maps incoming MQTT messages to jobs; keeps the optimistic light state."""

    def __init__(self, base: str, index: int | None = None) -> None:
        self.base = base
        self.index = index
        self.light: dict = {"state": "OFF"}

    def route(self, topic: str, raw: bytes) -> Job | None:
        job = self._route(topic, raw)
        if job:
            job.index = self.index
        return job

    def _route(self, topic: str, raw: bytes) -> Job | None:
        text = raw.decode(errors="replace").strip()
        if topic == f"{self.base}/light/set":
            try:
                payload = json.loads(text)
                if not isinstance(payload, dict):
                    raise ValueError("expected a JSON object")
                cmds = plan_light(payload)
                new = next_light_state(self.light, payload)
            except (ValueError, TypeError, ZeroDivisionError) as err:
                log.warning("bad light command %r: %s", text, err)
                return None
            if not cmds:
                return None
            self.light = new
            return Job(cmds, [(f"{self.base}/light/state", json.dumps(new), True)])
        if topic == f"{self.base}/timer/set":
            try:
                minutes = round(float(text))
                return Job([codec.timer(minutes) if minutes > 0 else codec.cancel_timer()])
            except ValueError as err:
                log.warning("bad timer value %r: %s", text, err)
                return None
        prefix = f"{self.base}/button/"
        if topic.startswith(prefix) and topic.endswith("/press"):
            key = topic[len(prefix) : -len("/press")]
            if key in BUTTONS:
                return Job([BUTTONS[key][1]()])
        log.debug("ignoring %s", topic)
        return None


def discovery(base: str, node: str, name: str, status_topic: str | None = None) -> list[tuple[str, str]]:
    """Home Assistant MQTT discovery messages (topic, JSON payload)."""
    device = {
        "identifiers": [f"hxlink_{node}"],
        "name": name,
        "manufacturer": "DXC",
        "model": "Hxlight B01A (BLE advert bridge)",
    }
    common = {"availability_topic": status_topic or f"{base}/status", "device": device}
    msgs: list[tuple[str, dict]] = [
        (
            f"{DISCOVERY_PREFIX}/light/hxlink_{node}/config",
            {
                **common,
                "name": None,
                "unique_id": f"hxlink_{node}_light",
                "schema": "json",
                "command_topic": f"{base}/light/set",
                "state_topic": f"{base}/light/state",
                "brightness": True,
                "brightness_scale": 100,
                "supported_color_modes": ["color_temp"],
                "min_kelvin": codec.KELVIN_WARM,
                "max_kelvin": codec.KELVIN_COLD,
                "effect": True,
                "effect_list": EFFECTS,
                "optimistic": True,
            },
        ),
        (
            f"{DISCOVERY_PREFIX}/number/hxlink_{node}_timer/config",
            {
                **common,
                "name": "Sleep timer",
                "unique_id": f"hxlink_{node}_timer",
                "command_topic": f"{base}/timer/set",
                "min": 1,
                "max": 23 * 60 + 59,
                "step": 1,
                "mode": "box",
                "unit_of_measurement": "min",
                "optimistic": True,
            },
        ),
    ]
    for key, (label, _) in BUTTONS.items():
        cfg = {
            **common,
            "name": label,
            "unique_id": f"hxlink_{node}_{key}",
            "command_topic": f"{base}/button/{key}/press",
            "payload_press": "PRESS",
        }
        if key in CONFIG_BUTTONS:
            cfg["entity_category"] = "config"
        msgs.append((f"{DISCOVERY_PREFIX}/button/hxlink_{node}_{key}/config", cfg))
    return [(topic, json.dumps(cfg)) for topic, cfg in msgs]


class Sender:
    """Serialises all transmissions: persist the counter first, then transmit."""

    def __init__(self, state_path: str, st: state.State, transmit: Callable[[bytes], None]) -> None:
        self.state_path = state_path
        self.st = st
        self.transmit = transmit

    def send(self, cmd: codec.Cmd, index: int | None = None) -> None:
        index = self.st.index if index is None else index
        tx = state.advance(self.state_path, self.st)
        adv = codec.encode(cmd, self.st.id, index, tx)
        log.info("slot=%d tx=%d %s -> %s", index, tx, cmd, adv.hex())
        self.transmit(adv)


def load_state(path: str) -> state.State:
    """Existing state, or seed it from the environment. Never invent a new identity (that would unpair the bulb)."""
    if os.path.exists(path):
        return state.load(path)
    if "HXLINK_ID" not in os.environ:
        raise SystemExit(f"{path} does not exist and HXLINK_ID is not set; refusing to create a new identity")
    st = state.State(
        id=int(os.environ["HXLINK_ID"], 0),
        index=int(os.environ.get("HXLINK_INDEX", "1")),
        tx=int(os.environ.get("HXLINK_TX_INIT", "0")),
    )
    state.save(path, st)
    log.info("seeded state: id=0x%04X index=%d tx=%d", st.id, st.index, st.tx)
    return st


def worker(jobs: "queue.Queue[Job | None]", sender: Sender, publish: Callable[[str, str, bool], None]) -> None:
    while True:
        job = jobs.get()
        if job is None:
            return
        try:
            for cmd in job.cmds:
                sender.send(cmd, job.index)
        except Exception:  # noqa: BLE001 - keep the bridge alive, the next command may work
            log.exception("transmit failed, not publishing state")
            continue
        for topic, payload, retain in job.publish:
            publish(topic, payload, retain)


def main() -> int:
    import paho.mqtt.client as mqtt  # imported late so the pure logic is testable without it

    from .mgmt import Mgmt

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    state_path = os.environ.get("HXLINK_STATE", "/data/state.json")
    st = load_state(state_path)
    node = f"{st.id:04x}"
    base = f"hxlink/{node}"
    name = os.environ.get("BULB_NAME", "Hxlight bulb")
    hold_s = int(os.environ.get("HOLD_MS", "350")) / 1000
    hci = int(os.environ.get("HCI", "0"))

    mgmt_holder: dict[str, Mgmt] = {}

    def transmit(adv: bytes) -> None:
        try:
            if "m" not in mgmt_holder:
                mgmt_holder["m"] = Mgmt(hci)
            mgmt_holder["m"].advertise(adv, hold_s)
        except Exception:
            m = mgmt_holder.pop("m", None)
            if m:
                m.close()
            raise

    bulbs = parse_bulbs(os.environ.get("BULBS", ""), st.index, name)
    routers = [Router(bulb_base(base, i), i) for i, _ in bulbs]
    jobs: "queue.Queue[Job | None]" = queue.Queue()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"hxlink-{node}")
    client.username_pw_set(os.environ.get("MQTT_USERNAME"), os.environ.get("MQTT_PASSWORD"))
    client.will_set(f"{base}/status", "offline", retain=True)

    def publish(topic: str, payload: str, retain: bool) -> None:
        client.publish(topic, payload, retain=retain)

    def on_connect(c, _userdata, _flags, reason, _props) -> None:
        if reason.is_failure:
            log.error("MQTT connect refused: %s", reason)
            return
        log.info("MQTT connected")
        for (i, bulb_name), r in zip(bulbs, routers):
            for topic in (f"{r.base}/light/set", f"{r.base}/timer/set", f"{r.base}/button/+/press"):
                c.subscribe(topic)
            bulb_id = bulb_node(node, i)
            for topic, payload in discovery(r.base, bulb_id, bulb_name, f"{base}/status"):
                c.publish(topic, payload, retain=True)
            for key in REMOVED_BUTTONS:
                c.publish(f"{DISCOVERY_PREFIX}/button/hxlink_{bulb_id}_{key}/config", "", retain=True)
        c.publish(f"{base}/status", "online", retain=True)

    def on_message(_c, _userdata, msg) -> None:
        for router in routers:
            job = router.route(msg.topic, msg.payload)
            if job:
                jobs.put(job)

    client.on_connect = on_connect
    client.on_message = on_message

    sender = Sender(state_path, st, transmit)
    thread = threading.Thread(target=worker, args=(jobs, sender, publish), daemon=True)
    thread.start()

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    client.connect_async(os.environ["MQTT_HOST"], int(os.environ.get("MQTT_PORT", "1883")), keepalive=30)
    client.loop_start()
    log.info("hxlink bridge up: id=0x%04X bulbs=%s base=%s hold=%.0fms", st.id, bulbs, base, hold_s * 1000)
    while not stop.wait(15):
        if client.is_connected():  # heartbeat for the liveness probe
            with open(HEARTBEAT_FILE, "w") as f:
                f.write(str(time.time()))
    log.info("shutting down")
    client.publish(f"{base}/status", "offline", retain=True).wait_for_publish(2)
    client.loop_stop()
    client.disconnect()
    jobs.put(None)
    thread.join(5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
