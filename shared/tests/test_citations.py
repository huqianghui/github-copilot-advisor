import pytest

from advisor_shared.citations import has_citation


@pytest.mark.parametrize(("text", "expected"), [
    ("https://example.com/12", True),
    ("[Source](https://example.com/12)", True),
    ("<https://example.com/12>", True),
    ("https://example.com/12\u3002", True),
    ("https://example.com/12. More text.", True),
    ("https://example.com/123", False),
    ("https://example.com/12.html", False),
    ("https://example.com/12/other", False),
    ("https://example.com/12?other=1", False),
    ("https://example.com/12#other", False),
    ("https://other.test/?url=https://example.com/12", False),
])
def test_citation_matches_complete_source_url(text, expected):
    assert has_citation(text, "https://example.com/12") is expected
