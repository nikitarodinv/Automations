from autonet.models import Company, Prospect, make_prospect_id


def test_prospect_id_normalizes_linkedin_url():
    a = make_prospect_id("https://www.linkedin.com/in/Jordan-Lee/?trk=abc", "J", "L", None)
    b = make_prospect_id("linkedin.com/in/jordan-lee", "X", "Y", "z.com")
    c = make_prospect_id("http://linkedin.com/in/jordan-lee/", "", "", None)
    assert a == b == c
    assert a.startswith("p_") and len(a) == 10


def test_prospect_id_without_url_uses_name_and_domain():
    a = make_prospect_id(None, "Jordan", "Lee", "evercore.com")
    assert a == make_prospect_id(None, " jordan", "LEE ", "Evercore.com")
    assert a != make_prospect_id(None, "Jordan", "Lee", "gs.com")


def test_summary_truncated_and_roundtrip():
    p = Prospect("A", "B", Company("C", "c.com"), source="t", summary="x" * 700)
    assert len(p.summary) == 600
    q = Prospect.from_dict(p.to_dict())
    assert q == p
