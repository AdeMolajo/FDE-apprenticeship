"""Ollama as an alternative model provider for the identification call.

Works with Ollama Cloud (https://ollama.com, needs OLLAMA_API_KEY) or a local Ollama
server (e.g. http://localhost:11434, no key). Same system prompt, same one call per
page, same PageClassification output as the Claude provider.

Ollama Cloud does not enforce the ``format`` schema, so the schema is also stated in
the prompt and every answer is validated in code; anything that does not parse is
raised as a ClassificationError and the page lands in ``skipped`` (Topic 3).
"""

from __future__ import annotations

import json
import re
from typing import Optional

import httpx
from pydantic import ValidationError

from .dataroom import Page
from .framing import CLASSIFY_AFTER_PAGE, wrap_page_text
from .retrying import DEFAULT_RETRIES, post_with_retry
from .identify import SYSTEM_PROMPT, ClassificationError, PageClassifier, ProviderAuthError
from .schema import PageClassification

DEFAULT_OLLAMA_MODEL = "gpt-oss:20b"
DEFAULT_OLLAMA_HOST = "https://ollama.com"

SCHEMA = PageClassification.model_json_schema()
OLLAMA_SYSTEM_PROMPT = (
    SYSTEM_PROMPT
    + "\n\nRespond with only a JSON object matching this schema, and nothing else:\n"
    + json.dumps(SCHEMA)
)


def parse_answer(content: str) -> PageClassification:
    """Validate the model's reply, tolerating Markdown code fences around the JSON."""
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        raise ClassificationError(f"no JSON in response: {content[:80]!r}")
    try:
        return PageClassification.model_validate_json(match.group(0))
    except ValidationError as exc:
        raise ClassificationError(f"response does not match schema: {content[:80]!r}") from exc


def make_ollama_classifier(
    model: str = DEFAULT_OLLAMA_MODEL,
    host: str = DEFAULT_OLLAMA_HOST,
    api_key: Optional[str] = None,
    client: Optional[httpx.Client] = None,
    retries: int = DEFAULT_RETRIES,
) -> PageClassifier:
    """Return a classifier that makes one Ollama chat call per page."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    client = client or httpx.Client(base_url=host, headers=headers, timeout=120)

    def classify(page: Page) -> PageClassification:
        if page.pdf_bytes is not None:
            raise ClassificationError("scanned page with no text layer; needs the Claude provider")
        user = (
            f"Source document: {page.source_document}, page {page.source_page}.\n\n"
            f"{wrap_page_text(page.text)}\n\n{CLASSIFY_AFTER_PAGE}"
        )
        payload = {
            "model": model,
            "stream": False,
            "format": SCHEMA,
            "messages": [
                {"role": "system", "content": OLLAMA_SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
        }
        try:
            response = post_with_retry(client, "/api/chat", payload, retries=retries)
        except httpx.HTTPError as exc:
            raise ClassificationError(f"request failed after retries: {exc}") from exc
        if response.status_code in (401, 403):
            raise ProviderAuthError(f"Ollama rejected the API key ({response.status_code})")
        if response.status_code != 200:
            raise ClassificationError(f"Ollama returned {response.status_code}: {response.text[:120]}")
        return parse_answer(response.json().get("message", {}).get("content", ""))

    return classify
