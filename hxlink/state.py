"""Persistent controller identity and tx counter."""

from __future__ import annotations

import json
import os
import random
import tempfile
from dataclasses import asdict, dataclass

from .codec import next_tx


@dataclass
class State:
    id: int
    index: int = 1
    tx: int = 0


def load(path: str, ctrl_id: int | None = None, index: int | None = None) -> State:
    try:
        with open(path) as f:
            st = State(**json.load(f))
    except FileNotFoundError:
        st = State(id=random.randint(1, 0xFFFE), tx=random.randint(0, 0x7F))
    if ctrl_id is not None:
        st.id = ctrl_id
    if index is not None:
        st.index = index
    return st


def save(path: str, st: State) -> None:
    """Atomic write, so a crash can't leave a truncated state file."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory)
    with os.fdopen(fd, "w") as f:
        json.dump(asdict(st), f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def advance(path: str, st: State) -> int:
    """Bump the counter and persist it BEFORE transmitting, so a restart never replays a counter."""
    st.tx = next_tx(st.tx)
    save(path, st)
    return st.tx
