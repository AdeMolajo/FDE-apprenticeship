"""Retries for rate limits and transient failures.

Before this, a 429 put the page in `skipped` and the run carried on, so a rate-limited
run finished looking successful apart from a long skipped list.
"""

from __future__ import annotations

from typing import List

import httpx
import pytest

from dd_extraction.dataroom import Page
from dd_extraction.extract import ExtractionError, make_ollama_extractor
from dd_extraction.identify import ClassificationError, ProviderAuthError
from dd_extraction.ollama import make_ollama_classifier
from dd_extraction.retrying import BASE_DELAY, MAX_DELAY, backoff_delay, post_with_retry

PAGE = Page("f.pdf", 2, "Income statement\nRevenue 1,250,000", None)
CLASSIFICATION = '{"is_financial_statement": true, "confidence": "high"}'
EXTRACTION = '{"figures": [{"metric": "revenue", "value": 1250000, "currency": "GBP", "confidence": "high"}]}'


def responder(statuses: List[int], content: str):
    """Returns those statuses in order, then 200 with the given content."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        i = len(seen)
        seen.append(request)
        if i < len(statuses):
            return httpx.Response(statuses[i], json={"error": "rate limited"})
        return httpx.Response(200, json={"message": {"role": "assistant", "content": content}})

    return handler, seen


def client_for(handler) -> httpx.Client:
    return httpx.Client(base_url="https://ollama.com", transport=httpx.MockTransport(handler))


def recorder():
    slept: List[float] = []
    return slept, slept.append


# ------------------------------------------------------------------ helper
def test_backoff_doubles_and_is_capped():
    assert backoff_delay(0, 0.0) == BASE_DELAY
    assert backoff_delay(1, 0.0) == BASE_DELAY * 2
    assert backoff_delay(2, 0.0) == BASE_DELAY * 4
    assert backoff_delay(10, 0.0) == MAX_DELAY
    assert BASE_DELAY < backoff_delay(0, 1.0) <= BASE_DELAY * 1.25  # jitter adds, never subtracts


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 529, 408])
def test_transient_statuses_are_retried(status):
    handler, seen = responder([status], CLASSIFICATION)
    slept, sleep = recorder()
    r = post_with_retry(client_for(handler), "/api/chat", {}, sleep=sleep, jitter=lambda: 0.0,
                        notify=lambda m: None)
    assert r.status_code == 200 and len(seen) == 2 and slept == [BASE_DELAY]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_client_errors_are_not_retried(status):
    handler, seen = responder([status], CLASSIFICATION)
    slept, sleep = recorder()
    r = post_with_retry(client_for(handler), "/api/chat", {}, sleep=sleep, notify=lambda m: None)
    assert r.status_code == status and len(seen) == 1 and slept == []


def test_retry_after_header_is_obeyed():
    def handler(request: httpx.Request) -> httpx.Response:
        if not seen:
            seen.append(request)
            return httpx.Response(429, headers={"retry-after": "7"}, json={})
        return httpx.Response(200, json={"message": {"content": CLASSIFICATION}})

    seen: List[httpx.Request] = []
    slept, sleep = recorder()
    post_with_retry(client_for(handler), "/api/chat", {}, sleep=sleep, notify=lambda m: None)
    assert slept == [7.0]


def test_unparseable_retry_after_falls_back_to_backoff():
    handler, _ = responder([429], CLASSIFICATION)
    slept, sleep = recorder()

    def dated(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "Wed, 23 Sep 2026 12:00:00 GMT"}, json={}) \
            if not slept else httpx.Response(200, json={"message": {"content": CLASSIFICATION}})

    post_with_retry(client_for(dated), "/api/chat", {}, sleep=sleep, jitter=lambda: 0.0,
                    notify=lambda m: None)
    assert slept == [BASE_DELAY]


def test_connection_errors_are_retried_then_raised():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ConnectError("connection refused")

    slept, sleep = recorder()
    with pytest.raises(httpx.ConnectError):
        post_with_retry(client_for(handler), "/api/chat", {}, retries=2, sleep=sleep,
                        jitter=lambda: 0.0, notify=lambda m: None)
    assert len(attempts) == 3 and slept == [BASE_DELAY, BASE_DELAY * 2]


def test_retries_are_announced():
    handler, _ = responder([429], CLASSIFICATION)
    messages: List[str] = []
    post_with_retry(client_for(handler), "/api/chat", {}, sleep=lambda d: None, jitter=lambda: 0.0,
                    notify=messages.append)
    assert "429" in messages[0] and "retrying" in messages[0]


# ------------------------------------------------------- through the pipeline
def test_classifier_recovers_from_rate_limits(monkeypatch):
    monkeypatch.setattr("dd_extraction.retrying.time.sleep", lambda d: None)
    handler, seen = responder([429, 429], CLASSIFICATION)
    classify = make_ollama_classifier(client=client_for(handler))
    assert classify(PAGE).is_financial_statement is True
    assert len(seen) == 3


def test_extractor_recovers_from_rate_limits(monkeypatch):
    monkeypatch.setattr("dd_extraction.retrying.time.sleep", lambda d: None)
    handler, seen = responder([503, 429], EXTRACTION)
    extract = make_ollama_extractor(client=client_for(handler))
    [figure] = extract(PAGE).figures
    assert figure.value == 1250000 and len(seen) == 3


def test_page_is_skipped_only_after_retries_are_exhausted(monkeypatch):
    monkeypatch.setattr("dd_extraction.retrying.time.sleep", lambda d: None)
    handler, seen = responder([429, 429, 429, 429], EXTRACTION)
    extract = make_ollama_extractor(client=client_for(handler))
    with pytest.raises(ExtractionError, match="429"):
        extract(PAGE)
    assert len(seen) == 4  # 1 attempt + 3 retries, then give up


def test_a_bad_key_still_fails_immediately(monkeypatch):
    monkeypatch.setattr("dd_extraction.retrying.time.sleep", lambda d: None)
    handler, seen = responder([401], CLASSIFICATION)
    classify = make_ollama_classifier(client=client_for(handler))
    with pytest.raises(ProviderAuthError):
        classify(PAGE)
    assert len(seen) == 1


def test_retries_can_be_turned_off(monkeypatch):
    handler, seen = responder([429], CLASSIFICATION)
    classify = make_ollama_classifier(client=client_for(handler), retries=0)
    with pytest.raises(ClassificationError, match="429"):
        classify(PAGE)
    assert len(seen) == 1
