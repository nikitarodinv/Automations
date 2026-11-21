from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class Delivered:
    draft_id: str
    ref: str | None        # where the draft now lives; None = waiting for an ack
    state: str             # delivered | drafted (pending ack)


class Delivery:
    name = "base"

    def __init__(self, out_dir: Path):
        self.out_dir = out_dir

    def deliver(self, drafts: list[dict[str, Any]]) -> list[Delivered]:
        raise NotImplementedError
