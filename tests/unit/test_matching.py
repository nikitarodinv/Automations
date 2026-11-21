import pytest

from autonet.matching import (
    apply_filters, company_matches, infer_seniority, is_eu_uk, location_matches, qualification_score, term_in,
)
from autonet.models import Company
from autonet.util import normalize_company
from tests.conftest import make_prospect

TARGETING = {
    "keywords": {"any": ["investment banking", "M&A"], "all": [], "exclude": ["recruiter"]},
    "titles": {"include": ["Analyst", "Associate"], "exclude": ["Managing Director"]},
    "seniority": ["entry", "junior", "mid"],
    "companies": {"include": [{"name": "Evercore", "domain": "evercore.com"}], "exclude": []},
    "locations": ["New York, NY"],
    "schools": ["Your University"],
}


@pytest.mark.parametrize(
    "term, text, hit",
    [
        ("M&A", "M&A Associate", True),
        ("M&A", "SM&AX team", False),
        ("investment banking", "Investment  Banking Analyst", True),
        ('"investment banking"', "investment banking analyst", True),
        ("bank", "banking", False),
        ("analyst", "Senior Analyst", True),
    ],
)
def test_term_matching_is_whole_word_and_case_insensitive(term, text, hit):
    assert term_in(term, text) is hit


def test_filters_pass_and_report_hits():
    r = apply_filters(make_prospect(), TARGETING)
    assert r.passed, r.reasons
    assert r.keywords_hit == ["investment banking"]


def test_exclude_beats_include():
    p = make_prospect(headline="Investment Banking Recruiter at Evercore")
    r = apply_filters(p, TARGETING)
    assert not r.passed
    assert any("exclude" in x for x in r.reasons)


def test_title_exclude_beats_include():
    p = make_prospect(current_title="Managing Director", headline="Managing Director, M&A")
    assert not apply_filters(p, {**TARGETING, "titles": {"include": ["Director"], "exclude": ["Managing Director"]}}).passed


def test_keywords_all_and_any():
    t = {**TARGETING, "keywords": {"any": [], "all": ["investment banking", "restructuring"], "exclude": []}}
    assert not apply_filters(make_prospect(), t).passed
    assert apply_filters(make_prospect(summary="Restructuring group"), t).passed


def test_company_matches_domain_then_normalized_name():
    p = make_prospect(company=Company("Moelis & Company, LLC", None))
    assert company_matches(p, {"name": "Moelis & Company"})
    assert company_matches(make_prospect(company=Company("EVR", "evercore.com")), {"name": "Evercore", "domain": "evercore.com"})
    assert not company_matches(p, {"name": "Lazard", "domain": "lazard.com"})


def test_normalize_company_strips_legal_suffixes():
    assert normalize_company("Goldman Sachs Group, Inc.") == "goldman sachs"
    assert normalize_company("PJT Partners LLC") == "pjt partners"


def test_location_and_eu_uk():
    assert location_matches("New York City Metropolitan Area", ["New York, NY"])
    assert not location_matches("Boston, MA", ["New York, NY"])
    assert is_eu_uk("London, United Kingdom") and is_eu_uk("Paris, France")
    assert not is_eu_uk("New York, NY") and not is_eu_uk("Hong Kong")
    p = make_prospect(location="London, United Kingdom")
    t = {**TARGETING, "locations": []}
    assert not apply_filters(p, t).passed
    assert apply_filters(p, {**t, "allow_eu_uk": True}).passed


@pytest.mark.parametrize(
    "title, level",
    [("Analyst", "entry"), ("Associate", "junior"), ("Vice President", "mid"), ("Director", "senior"),
     ("Managing Director", "executive"), ("Partner", "executive"), ("Chef", None)],
)
def test_infer_seniority(title, level):
    assert infer_seniority(title) == level


def test_qualification_score():
    p = make_prospect(current_title="Associate", past_companies=["Houlihan Lokey"])
    # 1 keyword (+3), shared school (+5), shared employer (+5), exact title (+2)
    assert qualification_score(p, TARGETING, {"past_companies": ["Houlihan Lokey Inc."]}, ["M&A"]) == 15
    p2 = make_prospect(seniority="executive", education=[])
    assert qualification_score(p2, TARGETING, {}, []) == 2 - 5
