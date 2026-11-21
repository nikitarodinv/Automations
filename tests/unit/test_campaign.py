import pytest
import yaml

from autonet.campaign import load_campaign, parse_campaign
from autonet.schemas import ValidationFailed
from tests.conftest import CAMPAIGN


def raw():
    return yaml.safe_load(CAMPAIGN.read_text())


def test_sample_campaign_is_valid():
    c = load_campaign(CAMPAIGN)
    assert c.id == "ib-alumni-fall26"
    assert c.limits["max_drafts_per_run"] == 15


def test_defaults_fill_missing_blocks():
    r = raw()
    del r["limits"], r["email"]
    c = parse_campaign(r)
    assert c.limits["recontact_cooldown_days"] == 180
    assert c.email["min_score"] == 80


@pytest.mark.parametrize(
    "mutate, needle",
    [
        (lambda r: r.pop("sender"), "sender"),
        (lambda r: r.update(source="linkedin_bot"), "source"),
        (lambda r: r["targeting"]["seniority"].append("intern"), "seniority"),
        (lambda r: r["email"].update(min_score=120), "min_score"),
        (lambda r: r.update(unknown_key=1), "unknown_key"),
        (lambda r: r["templates"].update(fallback="nope"), "fallback"),
        (lambda r: r["source_options"].pop("path"), "source_options.path"),
        (lambda r: r["targeting"]["companies"]["include"].append({"name": "X", "domain": "Not A Domain"}), "domain"),
    ],
)
def test_invalid_campaigns_fail_with_location(mutate, needle):
    r = raw()
    mutate(r)
    with pytest.raises(ValidationFailed) as e:
        parse_campaign(r)
    assert needle in str(e.value)


def test_yaml_no_trap_fails_validation():
    r = raw()
    r["sender"]["year"] = False  # what an unquoted `no` loads as
    with pytest.raises(ValidationFailed):
        parse_campaign(r)
