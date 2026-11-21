import pytest

from autonet.emails import decide
from autonet.models import EmailResult

RULES = {"min_score": 80, "accept_statuses": ["valid"], "min_score_accept_all": 92}


@pytest.mark.parametrize(
    "status, score, email, expected",
    [
        ("valid", 80, "a@b.com", "accepted"),
        ("valid", 79, "a@b.com", "unconfirmed"),
        ("accept_all", 92, "a@b.com", "accepted_catch_all"),
        ("accept_all", 91, "a@b.com", "unconfirmed"),
        ("unknown", 99, "a@b.com", "unconfirmed"),
        ("invalid", 99, "a@b.com", "invalid"),
        ("not_found", None, None, "not_found"),
        ("claimed", None, None, "claimed"),
    ],
)
def test_hunter_accept_rules(status, score, email, expected):
    assert decide(EmailResult(email, score, status, "hunter"), RULES) == expected
