"""Run state: delta link, processed item IDs, pending items, email contexts."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .graph import GraphClient


@dataclass
class State:
    delta_link: str | None = None
    processed: dict[str, dict] = field(default_factory=dict)   # item_id -> {status, at}
    pending: dict[str, dict] = field(default_factory=dict)     # item_id -> {reason, cycles, size, attempts}
    emails: dict[str, dict] = field(default_factory=dict)      # mailhook suffix N -> EmailContext dict

    @classmethod
    def from_dict(cls, data: dict) -> "State":
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=1, ensure_ascii=False)


class LocalStateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> State:
        if not self.path.exists():
            return State()
        return State.from_dict(json.loads(self.path.read_text(encoding="utf-8")))

    def save(self, state: State) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(state.to_json(), encoding="utf-8")
        os.replace(tmp, self.path)


class SharePointStateStore:
    """State kept in the drive (e.g. _system/state.json) so it survives redeploys."""

    def __init__(self, graph: GraphClient, path: str):
        self.graph = graph
        self.path = path

    def load(self) -> State:
        data, _ = self.graph.read_file(self.path)
        return State.from_dict(json.loads(data.decode("utf-8"))) if data else State()

    def save(self, state: State) -> None:
        self.graph.upload(self.path, state.to_json().encode("utf-8"))
