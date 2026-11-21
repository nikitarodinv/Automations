"""Helpers behind `autonet provider new | record | smoke`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from .. import http
from ..models import Company, Prospect
from .contract import FIXTURES
from .manifest import MANIFEST_DIR

FINDER_TEMPLATE = """# {name}: fill this in from the provider's API docs, then:
#   autonet provider record {name} --first Jordan --last Lee --domain example.com
#   autonet provider test {name}
name: {name}
docs: https://example.com/api-docs        # where you checked the fields below
terms_checked: false                      # set true once you've checked the terms allow this use
capabilities: [{capability}]
auth: {{ type: header, name: X-API-Key, env: {env} }}   # header | bearer | query
free_plan:
  monthly: 100                            # credits
  unit: credit
  resets: calendar_month                  # or signup_day (+ reset_day: N)
  # balance: {{ method: GET, url: https://api.example.com/v1/account, field: data.credits_left }}
rate_limit: {{ per_second: 2, per_minute: 60 }}
errors: {{ "401": auth, "402": quota, "404": not_found, "429": transient, "5xx": transient, "400": data }}
# error_codes: {{ NO_MATCH: not_found, OUT_OF_CREDITS: quota }}   # when errors come back as a code in the body
"""

FINDER_BLOCK = """finder:
  request:
    method: GET
    url: https://api.example.com/v1/email-finder
    params: {{ first_name: "{{{{first_name}}}}", last_name: "{{{{last_name}}}}", domain: "{{{{company.domain}}}}" }}
  response:                               # where each shared field lives in the reply
    email: data.email
    score: data.confidence
    status: data.status
  score_scale: 100                        # provider's max score; rescaled to 0-100
  status_map: {{ verified: valid, catch_all: accept_all, risky: unknown, invalid: invalid }}
  cost: {{ found: 1, miss: 0 }}
"""

SOURCE_NOTE = """# Sources page through results and map records to prospects; that needs a short Python class:
#   autonet/providers/{name}.py  ->  @register("{name}") class ...(PeopleSource): def search(...)
"""

TEST_TEMPLATE = '''from autonet.providers.contract import run_contract


def test_{name}_contract():
    failed = [c for c in run_contract("{name}") if not c.ok]
    assert not failed, [(c.case, c.name, c.detail) for c in failed]
'''


def scaffold(name: str, capability: str, root: Path) -> list[Path]:
    manifest = MANIFEST_DIR / f"{name}.yaml"
    if manifest.exists():
        raise FileExistsError(manifest)
    env = f"{name.upper()}_API_KEY"
    text = FINDER_TEMPLATE.format(name=name, capability=capability, env=env)
    text += FINDER_BLOCK.format() if capability == "finder" else SOURCE_NOTE.format(name=name)
    manifest.write_text(text)
    fixtures = FIXTURES / name
    fixtures.mkdir(parents=True, exist_ok=True)
    (fixtures / ".gitkeep").write_text("")
    test = root / "tests" / "contract" / f"test_{name}.py"
    test.parent.mkdir(parents=True, exist_ok=True)
    test.write_text(TEST_TEMPLATE.format(name=name))
    return [manifest, fixtures, test]


class Recorder(httpx.BaseTransport):
    def __init__(self) -> None:
        self.inner = httpx.HTTPTransport()
        self.replies: list[dict[str, Any]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        r = self.inner.handle_request(request)
        r.read()
        try:
            body = json.loads(r.content or b"null")
        except ValueError:
            body = r.content.decode(errors="replace")
        keep = {k: v for k, v in r.headers.items() if k.lower().startswith(("x-credits", "retry-after"))}
        self.replies.append({"status": r.status_code, "headers": keep, "json": body})
        return r


def record_case(provider: str, kind: str, inp: dict[str, Any], key: str | None) -> dict[str, Any]:
    """One live call (key from the environment, or a deliberately bad one for the error case)."""
    from ..errors import AutonetError
    from .base import EmailFinder
    from .registry import build

    rec = Recorder()
    old_t = http.TRANSPORT
    http.TRANSPORT = rec
    expect: dict[str, Any] = {}
    try:
        inst = build(provider, EmailFinder, key=key)
        p = Prospect(first_name=inp["first_name"], last_name=inp["last_name"],
                     company=Company(inp.get("company", {}).get("name", ""), inp.get("company", {}).get("domain")),
                     source="record", linkedin_url=inp.get("linkedin_url"))
        try:
            r = inst.find(p)
            expect = {"status": r.status, "credits": r.credits_used}
            if r.email:
                expect.update(email=r.email, score=r.score)
        except AutonetError as e:
            expect = {"error": e.kind}
    finally:
        http.TRANSPORT = old_t
    case = {"role": "finder", "kind": kind, "input": inp, "replies": rec.replies, "expect": expect}
    path = FIXTURES / provider / f"{kind}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(case, indent=2) + "\n")
    return case
