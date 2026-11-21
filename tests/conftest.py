from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from autonet.config import Config
from autonet.models import Prospect

REPO = Path(__file__).resolve().parents[1]
CAMPAIGN = REPO / "campaigns" / "ib-alumni-fall26.yaml"
KEY_ENVS = ["HUNTER_API_KEY", "PDL_API_KEY", "APOLLO_API_KEY", "PROSPEO_API_KEY", "TYPESAFE_API_KEY",
            "OPENROUTER_API_KEY", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"]


@pytest.fixture(autouse=True)
def _no_real_network_or_keys(monkeypatch):
    """Tests never see your real keys, never wait, and fail on any request they didn't mock."""
    import httpx

    from autonet import http

    for k in KEY_ENVS:
        monkeypatch.delenv(k, raising=False)

    def refuse(request):
        raise AssertionError(f"unmocked HTTP request: {request.method} {request.url}")

    monkeypatch.setattr(http, "TRANSPORT", httpx.MockTransport(refuse))
    monkeypatch.setattr(http, "SLEEP", lambda s: None)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """Repo config, but all state (databases, out/, logs/) goes to a temp folder."""
    c = Config.load(root=REPO)
    c.data["paths"] = {
        **c.data["paths"],
        "db": str(tmp_path / "autonet.db"),
        "dry_run_db": str(tmp_path / "dryrun.db"),
        "out": str(tmp_path / "out"),
        "logs": str(tmp_path / "logs"),
    }
    return c


@pytest.fixture
def campaign_file(tmp_path: Path):
    """Write a modified copy of the sample campaign; returns its path as a string."""

    def make(**overrides) -> str:
        data = yaml.safe_load(CAMPAIGN.read_text())
        for dotted, value in overrides.items():
            node = data
            keys = dotted.split("__")
            for k in keys[:-1]:
                node = node[k]
            node[keys[-1]] = value
        path = tmp_path / f"campaign_{len(list(tmp_path.glob('campaign_*.yaml')))}.yaml"
        path.write_text(yaml.safe_dump(data, sort_keys=False))
        return str(path)

    return make


def fixture_prospects() -> list[Prospect]:
    out = []
    for line in (REPO / "fixtures/dryrun/prospects.jsonl").read_text().splitlines():
        if line.strip() and not line.startswith("//"):
            out.append(Prospect.from_dict(json.loads(line)))
    return out


def make_prospect(**kw) -> Prospect:
    from autonet.models import Company

    base = dict(
        first_name="Jordan", last_name="Lee", company=Company("Evercore", "evercore.com"), source="test",
        current_title="Associate", headline="Investment Banking Associate at Evercore", seniority="junior",
        location="New York, NY", education=[{"school": "Your University"}],
        linkedin_url="https://www.linkedin.com/in/jordan-lee",
    )
    base.update(kw)
    return Prospect(**base)
