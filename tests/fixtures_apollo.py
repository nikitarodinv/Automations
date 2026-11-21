"""Apollo replies shaped like the documented examples (2026-11-21)."""

SEARCH = {"total_entries": 2, "people": [
    {"id": "a1", "first_name": "Jordan", "last_name_obfuscated": "L***e", "title": "Investment Banking Associate",
     "has_email": True, "organization": {"name": "Evercore"}},
    {"id": "a2", "first_name": "Kevin", "last_name_obfuscated": "W***t", "title": "Campus Recruiter",
     "has_email": True, "organization": {"name": "Goldman Sachs"}},
]}

MATCH = {"person": {
    "id": "a1", "first_name": "Jordan", "last_name": "Lee", "name": "Jordan Lee",
    "linkedin_url": "http://www.linkedin.com/in/jordan-lee", "title": "Associate",
    "headline": "Investment Banking Associate at Evercore", "email": "jordan.lee@evercore.com", "email_status": "verified",
    "match_confidence": "high", "city": "New York", "state": "New York", "country": "United States",
    "employment_history": [{"organization_name": "Evercore", "title": "Associate", "current": True}],
    "organization": {"id": "o1", "name": "Evercore", "primary_domain": "evercore.com", "website_url": "https://www.evercore.com"},
}}
