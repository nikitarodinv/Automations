import pytest

from autonet.campaign import load_campaign
from autonet.errors import QuotaError
from autonet.ledger import Ledger, RunBudget
from autonet.providers import get_source, known
from autonet.store import Store, merge_records
from tests.conftest import CAMPAIGN


def test_budget_guard_checks_before_spending(tmp_path):
    store = Store(tmp_path / "x.db")
    store.start_run("r1", "c", False)
    led = Ledger(store, "r1", RunBudget(finder_credits=3, llm_calls=1, searches=0))
    for _ in range(3):
        led.require_finder_credits(1, ["hunter"])
        led.record("hunter", "email-finder", credits=1)
    with pytest.raises(QuotaError):
        led.require_finder_credits(1, ["hunter"])
    led.require_finder_credits(0.0, ["hunter"])  # free calls still fit
    led.require_llm_call()
    led.record("jev", "systemone", llm_calls=1)
    with pytest.raises(QuotaError):
        led.require_llm_call()
    with pytest.raises(QuotaError):
        led.require_search()
    assert led.run_total("credits") == 3


def test_merge_records_newest_nonempty_wins():
    old = {"headline": "Old", "summary": "kept", "company": {"name": "E", "domain": None}}
    new = {"headline": "New", "summary": "", "company": {"name": "E", "domain": "e.com"}}
    assert merge_records(old, new) == {"headline": "New", "summary": "kept", "company": {"name": "E", "domain": "e.com"}}


def test_csv_reads_linkedin_export_with_notes_preamble(tmp_path):
    f = tmp_path / "Connections.csv"
    f.write_text(
        "Notes:\n\"When exporting your connection data, you may notice that some of the email addresses are missing.\"\n\n"
        "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
        "Jordan,Lee,https://www.linkedin.com/in/jordan-lee,,Evercore,Associate,01 Jan 2026\n"
        "Sam,Rivera,https://www.linkedin.com/in/sam-rivera,sam@lazard.com,Lazard,Analyst,02 Jan 2026\n"
        ",NoFirst,,,,,\n"
    )
    ps = list(get_source("csv", path=str(f)).search(load_campaign(CAMPAIGN), 10))
    assert [p.full_name for p in ps] == ["Jordan Lee", "Sam Rivera"]
    assert ps[1].known_email == "sam@lazard.com" and ps[0].known_email is None
    assert ps[0].current_title == "Associate" and ps[0].company.name == "Evercore"


def test_registry_lists_known_providers():
    assert {"csv", "fixture", "fixture_finder"} <= set(known())
