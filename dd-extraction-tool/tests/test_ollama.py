from __future__ import annotations

import json

import httpx
import pytest

from dd_extraction.dataroom import Page
from dd_extraction.identify import ClassificationError, ProviderAuthError
from dd_extraction.ollama import OLLAMA_SYSTEM_PROMPT, make_ollama_classifier, parse_answer
from dd_extraction.schema import PageClassification

TEXT_PAGE = Page("accounts.pdf", 2, "Balance sheet as at 31 December 2025 ...", None)
SCANNED_PAGE = Page("scan.pdf", 1, "", b"%PDF-1.4")


def classifier(reply: str, status: int = 200, calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(json.loads(request.content))
        return httpx.Response(status, json={"message": {"role": "assistant", "content": reply}})

    client = httpx.Client(base_url="https://ollama.com", transport=httpx.MockTransport(handler))
    return make_ollama_classifier(client=client)


@pytest.mark.parametrize(
    "reply",
    [
        '{"is_financial_statement": true, "confidence": "high"}',
        '```json\n{"is_financial_statement": true, "confidence": "high"}\n```',
    ],
)
def test_parses_plain_and_fenced_json(reply: str):
    assert parse_answer(reply) == PageClassification(is_financial_statement=True, confidence="high")


@pytest.mark.parametrize("reply", ["Yes.", '{"is_financial_statement": "maybe"}'])
def test_unusable_answers_are_errors(reply: str):
    with pytest.raises(ClassificationError):
        parse_answer(reply)


def test_one_call_per_page_with_schema_and_untrusted_framing():
    calls = []
    classify = classifier('{"is_financial_statement": true, "confidence": "medium"}', calls=calls)

    assert classify(TEXT_PAGE).confidence == "medium"
    [body] = calls
    assert body["model"] == "gpt-oss:20b"
    assert body["stream"] is False
    assert body["format"]["required"] == ["is_financial_statement", "confidence"]
    assert body["messages"][0] == {"role": "system", "content": OLLAMA_SYSTEM_PROMPT}
    assert "<page_content>" in body["messages"][1]["content"]
    assert "tools" not in body


def test_scanned_page_is_not_sent():
    calls = []
    with pytest.raises(ClassificationError, match="scanned"):
        classifier("{}", calls=calls)(SCANNED_PAGE)
    assert calls == []


def test_rejected_key_stops_the_run():
    with pytest.raises(ProviderAuthError):
        classifier("unauthorized", status=401)(TEXT_PAGE)


def test_server_error_is_a_page_failure():
    with pytest.raises(ClassificationError):
        classifier("overloaded", status=503)(TEXT_PAGE)
