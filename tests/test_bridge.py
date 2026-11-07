import json
import queue

import pytest

from hxlink import bridge, codec, state

BASE = "hxlink/2ad8"


def route(router, topic, payload):
    return router.route(f"{BASE}/{topic}", payload if isinstance(payload, bytes) else json.dumps(payload).encode())


def test_turn_off():
    job = route(bridge.Router(BASE), "light/set", {"state": "OFF"})
    assert job.cmds == [codec.off()]
    assert json.loads(job.publish[0][1]) == {"state": "OFF"}
    assert job.publish[0][0] == f"{BASE}/light/state" and job.publish[0][2] is True  # retained


def test_turn_on_only():
    assert route(bridge.Router(BASE), "light/set", {"state": "ON"}).cmds == [codec.on()]


def test_turn_on_with_brightness_and_ct_sends_three_frames_in_order():
    job = route(bridge.Router(BASE), "light/set", {"state": "ON", "brightness": 70, "color_temp": 286})
    assert job.cmds == [codec.on(), codec.brightness(70), codec.color_temp(3497)]
    st = json.loads(job.publish[0][1])
    assert st["state"] == "ON" and st["brightness"] == 70 and st["color_temp"] == 286 and st["color_mode"] == "color_temp"


def test_brightness_only_while_on():
    assert route(bridge.Router(BASE), "light/set", {"brightness": 30}).cmds == [codec.brightness(30)]


def test_night_effect_sends_only_night():
    router = bridge.Router(BASE)
    job = route(router, "light/set", {"state": "ON", "effect": "Night"})
    assert job.cmds == [codec.night()]
    st = json.loads(job.publish[0][1])
    assert st["effect"] == "Night" and st["brightness"] == 2


@pytest.mark.parametrize(("effect", "name"), [("Bright", "bright"), ("Warm", "warm"), ("Comfortable", "comfortable")])
def test_preset_effects(effect, name):
    router = bridge.Router(BASE)
    job = route(router, "light/set", {"state": "ON", "effect": effect})
    assert job.cmds == [codec.on(), codec.preset(name)]
    assert json.loads(job.publish[0][1])["effect"] == effect


def test_brightness_after_night_clears_effect():
    router = bridge.Router(BASE)
    route(router, "light/set", {"state": "ON", "effect": "Night"})
    st = json.loads(route(router, "light/set", {"state": "ON", "brightness": 60}).publish[0][1])
    assert "effect" not in st and st["brightness"] == 60


def test_brightness_clamped_and_extremes():
    router = bridge.Router(BASE)
    assert route(router, "light/set", {"brightness": 0}).cmds == [codec.brightness(1)]
    assert route(router, "light/set", {"brightness": 250}).cmds == [codec.brightness(100)]


def test_color_temp_extremes_map_to_bulb_range():
    assert route(bridge.Router(BASE), "light/set", {"color_temp": 154}).cmds == [codec.color_temp(6494)]
    assert route(bridge.Router(BASE), "light/set", {"color_temp": 333}).cmds == [codec.color_temp(3003)]


@pytest.mark.parametrize("raw", [b"not json", b"[]", b'{"brightness": "x"}', b'{"color_temp": 0}', b"{}"])
def test_bad_or_empty_light_commands_are_ignored(raw):
    router = bridge.Router(BASE)
    assert route(router, "light/set", raw) is None
    assert router.light == {"state": "OFF"}  # state untouched


@pytest.mark.parametrize(
    ("key", "cmd"),
    [
        ("pair", codec.pair()),
        ("unpair", codec.unpair()),
        ("night", codec.night()),
        ("cancel_timer", codec.cancel_timer()),
    ],
)
def test_buttons(key, cmd):
    job = route(bridge.Router(BASE), f"button/{key}/press", b"PRESS")
    assert job.cmds == [cmd] and job.publish == []


def test_timer():
    router = bridge.Router(BASE)
    assert route(router, "timer/set", b"60").cmds == [codec.timer(60)]
    assert route(router, "timer/set", b"1").cmds == [codec.timer(1)]
    assert route(router, "timer/set", b"0").cmds == [codec.cancel_timer()]
    assert route(router, "timer/set", b"abc") is None


def test_unknown_topics_ignored():
    router = bridge.Router(BASE)
    assert route(router, "button/self_destruct/press", b"PRESS") is None
    assert router.route("other/topic", b"x") is None


def test_discovery_payloads():
    msgs = bridge.discovery(BASE, "2ad8", "Bedroom lamp")
    topics = [t for t, _ in msgs]
    assert len(topics) == len(set(topics)) == 2 + len(bridge.BUTTONS)
    light = json.loads(dict(msgs)["homeassistant/light/hxlink_2ad8/config"])
    assert light["schema"] == "json" and light["command_topic"] == f"{BASE}/light/set"
    assert light["supported_color_modes"] == ["color_temp"] and (light["min_kelvin"], light["max_kelvin"]) == (3000, 6500)
    assert light["effect_list"] == ["Night", "Bright", "Warm", "Comfortable"] and light["optimistic"] is True and light["brightness_scale"] == 100
    uids = [json.loads(p)["unique_id"] for _, p in msgs]
    assert len(uids) == len(set(uids))
    for _, p in msgs:
        cfg = json.loads(p)
        assert cfg["availability_topic"] == f"{BASE}/status" and cfg["device"]["identifiers"] == ["hxlink_2ad8"]
    assert json.loads(dict(msgs)["homeassistant/button/hxlink_2ad8_pair/config"])["entity_category"] == "config"


def test_mireds_kelvin_roundtrip_stays_in_range():
    assert bridge.mireds(6500) == 154 and bridge.mireds(3000) == 333
    assert abs(bridge.kelvin(bridge.mireds(4000)) - 4000) < 20


def test_sender_persists_counter_before_transmit_and_encodes(tmp_path):
    path = str(tmp_path / "state.json")
    st = state.State(id=0x2AD8, index=1, tx=163)
    state.save(path, st)
    seen = []

    def transmit(adv):
        seen.append((adv, state.load(path).tx))  # counter on disk at the moment of transmission

    sender = bridge.Sender(path, st, transmit)
    sender.send(codec.off())
    sender.send(codec.on())
    assert [tx for _, tx in seen] == [164, 165]
    assert codec.decode(seen[0][0]).cmd == codec.off() and codec.decode(seen[0][0]).tx == 164
    assert codec.decode(seen[1][0]).id == 0x2AD8


def test_worker_publishes_state_only_after_success_and_survives_failure(tmp_path):
    path = str(tmp_path / "state.json")
    st = state.State(id=0x2AD8, tx=0)
    state.save(path, st)
    sent, published, calls = [], [], []

    def flaky(adv):
        calls.append(adv)
        if len(calls) == 1:
            raise OSError("adapter busy")  # first transmission fails, later ones work
        sent.append(adv)

    jobs = queue.Queue()
    jobs.put(bridge.Job([codec.on()], [("t", "bad", True)]))  # fails: must not publish
    jobs.put(bridge.Job([codec.off()], [("t", "good", True)]))
    jobs.put(None)
    bridge.worker(jobs, bridge.Sender(path, st, flaky), lambda t, p, r: published.append((t, p, r)))
    assert published == [("t", "good", True)]
    assert len(sent) == 1 and codec.decode(sent[0]).cmd == codec.off()
    assert state.load(path).tx == 2  # the failed attempt burned a counter, which is fine


def test_load_state_refuses_to_invent_identity(tmp_path, monkeypatch):
    monkeypatch.delenv("HXLINK_ID", raising=False)
    with pytest.raises(SystemExit):
        bridge.load_state(str(tmp_path / "missing.json"))


def test_load_state_seeds_from_env_once(tmp_path, monkeypatch):
    path = str(tmp_path / "state.json")
    monkeypatch.setenv("HXLINK_ID", "0x2AD8")
    monkeypatch.setenv("HXLINK_TX_INIT", "163")
    assert (bridge.load_state(path).id, bridge.load_state(path).tx) == (0x2AD8, 163)
    monkeypatch.setenv("HXLINK_TX_INIT", "5")  # an existing file wins over the env
    assert bridge.load_state(path).tx == 163


def test_parse_bulbs():
    assert bridge.parse_bulbs("", 1, "Lamp") == [(1, "Lamp")]
    assert bridge.parse_bulbs("1=Bedroom lamp, 2=Study", 1, "x") == [(1, "Bedroom lamp"), (2, "Study")]
    for bad in ("1=A,1=B", "5=A", "0=A", "Bedroom", "2="):
        with pytest.raises(ValueError):
            bridge.parse_bulbs(bad, 1, "x")


def test_second_bulb_topics_ids_and_slot():
    assert bridge.bulb_base(BASE, 1) == BASE and bridge.bulb_base(BASE, 2) == f"{BASE}/2"
    assert bridge.bulb_node("2ad8", 1) == "2ad8" and bridge.bulb_node("2ad8", 2) == "2ad8_2"
    second = bridge.Router(bridge.bulb_base(BASE, 2), 2)
    job = second.route(f"{BASE}/2/light/set", b'{"state":"OFF"}')
    assert job.index == 2 and job.cmds == [codec.off()]
    assert bridge.Router(BASE, 1).route(f"{BASE}/2/light/set", b'{"state":"OFF"}') is None
    assert second.route(f"{BASE}/light/set", b'{"state":"OFF"}') is None
    msgs = bridge.discovery(f"{BASE}/2", "2ad8_2", "Study", f"{BASE}/status")
    cfgs = [json.loads(p) for _, p in msgs]
    assert all(c["availability_topic"] == f"{BASE}/status" for c in cfgs)
    first_uids = {json.loads(p)["unique_id"] for _, p in bridge.discovery(BASE, "2ad8", "Bedroom lamp")}
    assert not first_uids & {c["unique_id"] for c in cfgs}


def test_sender_uses_job_slot(tmp_path):
    path = str(tmp_path / "s.json")
    st = state.State(id=0x2AD8, index=1, tx=0)
    state.save(path, st)
    sent: list[bytes] = []
    sender = bridge.Sender(path, st, sent.append)
    sender.send(codec.off(), 2)
    sender.send(codec.off())
    assert [codec.decode(a).index for a in sent] == [2, 1]
