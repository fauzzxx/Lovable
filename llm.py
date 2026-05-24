"""
llm.py
------
Thin wrapper around the Anthropic (Claude) Messages API
(api.anthropic.com/v1/messages). Talks to the REST endpoint directly with httpx
so the builder doesn't depend on an SDK version, and so we have full control over
multimodal (text + image) input.

The public entry point is `generate_text()`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

ANTHROPIC_API = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"


class LLMError(Exception):
    """Raised when the Claude call fails in a way the user should see."""

    def __init__(self, message: str, *, status: int | None = None, retriable: bool = False):
        super().__init__(message)
        self.message = message
        self.status = status
        self.retriable = retriable


@dataclass
class LLMResult:
    text: str
    model: str
    finish_reason: str | None = None
    truncated: bool = False
    usage: dict | None = None


def _build_content(prompt: str, images: list[dict] | None) -> list[dict]:
    """
    images: list of {"mime_type": "image/png", "data": "<base64 string>"}
    Anthropic recommends images before the text instruction.
    """
    content: list[dict] = []
    for img in images or []:
        data = img.get("data")
        if not data:
            continue
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img.get("mime_type", "image/png"),
                    "data": data,
                },
            }
        )
    content.append({"type": "text", "text": prompt})
    return content


def generate_text(
    *,
    api_key: str,
    model: str,
    prompt: str,
    system_instruction: str | None = None,
    images: list[dict] | None = None,
    temperature: float = 0.6,
    max_output_tokens: int = 16000,
    timeout: float = 240.0,
) -> LLMResult:
    """
    Call Claude and return the generated text.

    Raises LLMError with a friendly message on auth / quota / model errors.
    """
    if not api_key:
        raise LLMError(
            "No Anthropic API key configured. Open Settings and paste your key "
            "from console.anthropic.com/settings/keys",
            status=401,
        )
    if not model:
        raise LLMError("No model configured. Set one in Settings (e.g. claude-sonnet-4-6).")

    # Anthropic requires max_tokens; clamp to a safe ceiling so we never 400 on it.
    max_tokens = max(256, min(int(max_output_tokens or 16000), 32000))

    body: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "user", "content": _build_content(prompt, images)}],
    }
    if system_instruction:
        body["system"] = system_instruction

    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(f"{ANTHROPIC_API}/messages", headers=headers, json=body)
    except httpx.TimeoutException as e:
        raise LLMError(
            f"Claude request timed out after {int(timeout)}s. Try a smaller prompt "
            f"or a faster model (e.g. claude-haiku-4-5-20251001).",
            retriable=True,
        ) from e
    except httpx.HTTPError as e:
        raise LLMError(f"Network error talking to Claude: {e}", retriable=True) from e

    if resp.status_code != 200:
        raise _error_from_response(resp, model)

    try:
        data = resp.json()
    except json.JSONDecodeError as e:
        raise LLMError(f"Claude returned a non-JSON response: {resp.text[:300]}") from e

    return _parse_success(data, model)


def _error_from_response(resp: httpx.Response, model: str) -> LLMError:
    status = resp.status_code
    detail = ""
    try:
        detail = (resp.json().get("error") or {}).get("message", "") or ""
    except Exception:
        detail = resp.text[:300]

    if status == 401:
        return LLMError(
            "Anthropic rejected your API key (401). Check the key in Settings "
            f"(console.anthropic.com/settings/keys). Details: {detail}",
            status=status,
        )
    if status == 403:
        return LLMError(f"Anthropic denied the request (403). Details: {detail}", status=status)
    if status == 404:
        return LLMError(
            f"Model '{model}' was not found (404). Set a valid model in Settings — "
            f"e.g. claude-sonnet-4-6, claude-opus-4-6, or claude-haiku-4-5-20251001. "
            f"Details: {detail}",
            status=status,
        )
    if status == 400:
        return LLMError(
            f"Claude rejected the request (400). Often the model name or a parameter. "
            f"Details: {detail}",
            status=status,
        )
    if status == 429:
        return LLMError(
            "Claude rate limit / quota hit (429). Wait a moment and retry, or check "
            f"your credit balance at console.anthropic.com/settings/billing. Details: {detail}",
            status=status,
            retriable=True,
        )
    if status in (500, 529):
        return LLMError(
            f"Claude is temporarily overloaded ({status}). Retry in a moment. Details: {detail}",
            status=status,
            retriable=True,
        )
    return LLMError(f"Claude error {status}: {detail}", status=status)


def _parse_success(data: dict, model: str) -> LLMResult:
    blocks = data.get("content") or []
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
    stop = data.get("stop_reason")

    if not text:
        if stop == "max_tokens":
            raise LLMError(
                "Claude hit the output token limit before producing any text. "
                "Increase 'Max output tokens' in Settings."
            )
        raise LLMError("Claude returned an empty response. Try again.")

    return LLMResult(
        text=text,
        model=data.get("model", model),
        finish_reason=stop,
        truncated=(stop == "max_tokens"),
        usage=data.get("usage"),
    )


def list_models(api_key: str, timeout: float = 30.0) -> list[str]:
    """Best-effort list of available Claude models (for the UI)."""
    if not api_key:
        return []
    headers = {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION}
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"{ANTHROPIC_API}/models?limit=100", headers=headers)
        if resp.status_code != 200:
            return []
        return [m.get("id", "") for m in resp.json().get("data", []) if m.get("id")]
    except Exception:
        return []
