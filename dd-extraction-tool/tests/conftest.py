import pytest


@pytest.fixture(autouse=True)
def no_real_sleeping(monkeypatch):
    """Retry backoff must never make the test suite wait on real time.

    Tests that check the backoff itself pass their own sleep function to
    post_with_retry, so they still see the delays that would have been used.
    """
    monkeypatch.setattr("dd_extraction.retrying.time.sleep", lambda seconds: None)


@pytest.fixture(autouse=True)
def no_ambient_pricing(monkeypatch):
    """Tests must not depend on whether the developer has DD_PRICING exported.

    Tests that need prices set them themselves.
    """
    monkeypatch.delenv("DD_PRICING", raising=False)
