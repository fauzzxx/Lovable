"""
llm.py
------
Thin, dependency-light wrapper around the Google Gemini REST API
(generativelanguage.googleapis.com, v1beta `generateContent`).

We talk to the REST endpoint directly with httpx instead of using an SDK so
that the builder doesn't break when Google ships new SDK versions, and so we
have full control over multimodal (text + image) input.

The public entry point is `generate_text()`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"


class LLMError(Exception):
    """Raised when the Gemini call fails in a way the user should see."""

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


def _build_parts(prompt: str, images: list[dict] | None) -> list[dict]:
    """
    images: list of {"mime_type": "image/png", "data": "<base64 string>"}
    The text part goes first, then any images.
    """
    parts: list[dict] = [{"text": prompt}]
    for img in images or []:
        data = img.get("data")
        if not data:
            continue
        parts.append(
            {
                "inline_data": {
                    "mime_type": img.get("mime_type", "image/png"),
                    "data": data,
                }
            }
        )
    return parts


def generate_text(
    *,
    api_key: str,
    model: str,
    prompt: str,
    system_instruction: str | None = None,
    images: list[dict] | None = None,
    temperature: float = 0.6,
    max_output_tokens: int = 32768,
    timeout: float = 240.0,
) -> LLMResult:
    """
    Call Gemini and return the generated text.

    Raises LLMError with a friendly message on auth / quota / model errors.
    """
    if not api_key:
        raise LLMError(
            "No Gemini API key configured. Open Settings and paste your key "
            "from https://aistudio.google.com/apikey",
            status=401,
        )
    if not model:
        raise LLMError("No model configured. Set one in Settings (e.g. gemini-2.5-flash).")

    url = f"{GEMINI_BASE}/models/{model}:generateContent"

    body: dict = {
        "contents": [
            {
                "role": "user",
                "parts": _build_parts(prompt, images),
            }
        ],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_output_tokens,
            # Plain text out — we parse our own file delimiters from it.
            "responseMimeType": "text/plain",
        },
    }
    if system_instruction:
        body["systemInstruction"] = {"parts": [{"text": system_instruction}]}

    headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, headers=headers, json=body)
    except httpx.TimeoutException as e:
        raise LLMError(
            f"Gemini request timed out after {int(timeout)}s. Try a smaller "
            f"prompt or a faster model.",
            retriable=True,
        ) from e
    except httpx.HTTPError as e:
        raise LLMError(f"Network error talking to Gemini: {e}", retriable=True) from e

    if resp.status_code != 200:
        raise _error_from_response(resp, model)

    try:
        data = resp.json()
    except json.JSONDecodeError as e:
        raise LLMError(f"Gemini returned a non-JSON response: {resp.text[:300]}") from e

    return _parse_success(data, model)


def _error_from_response(resp: httpx.Response, model: str) -> LLMError:
    status = resp.status_code
    detail = ""
    try:
        j = resp.json()
        detail = (j.get("error") or {}).get("message", "") or ""
    except Exception:
        detail = resp.text[:300]

    if status in (401, 403):
        return LLMError(
            "Gemini rejected your API key (401/403). Double-check the key in "
            f"Settings. Details: {detail}",
            status=status,
        )
    if status == 404:
        return LLMError(
            f"Model '{model}' was not found (404). Change the model name in "
            f"Settings — e.g. gemini-2.5-flash or gemini-2.5-flash-lite. "
            f"Details: {detail}",
            status=status,
        )
    if status == 429:
        return LLMError(
            "Gemini rate limit / quota hit (429). Wait a moment and retry, or "
            f"use a model with a higher quota. Details: {detail}",
            status=status,
            retriable=True,
        )
    if status >= 500:
        return LLMError(
            f"Gemini server error ({status}). This is usually temporary — retry. "
            f"Details: {detail}",
            status=status,
            retriable=True,
        )
    return LLMError(f"Gemini error {status}: {detail}", status=status)


def _parse_success(data: dict, model: str) -> LLMResult:
    candidates = data.get("candidates") or []
    if not candidates:
        # Often means the prompt was blocked by safety filters.
        feedback = data.get("promptFeedback") or {}
        block = feedback.get("blockReason")
        if block:
            raise LLMError(
                f"Gemini blocked the request (reason: {block}). Try rephrasing "
                f"your prompt."
            )
        raise LLMError("Gemini returned no candidates. Try again or rephrase.")

    cand = candidates[0]
    finish = cand.get("finishReason")
    content = cand.get("content") or {}
    parts = content.get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()

    if not text:
        if finish == "MAX_TOKENS":
            raise LLMError(
                "Gemini hit the output token limit before producing any text. "
                "Increase 'Max output tokens' in Settings."
            )
        if finish in ("SAFETY", "RECITATION"):
            raise LLMError(
                f"Gemini stopped early (reason: {finish}). Try rephrasing your prompt."
            )
        raise LLMError("Gemini returned an empty response. Try again.")

    return LLMResult(
        text=text,
        model=model,
        finish_reason=finish,
        truncated=(finish == "MAX_TOKENS"),
        usage=data.get("usageMetadata"),
    )


def list_models(api_key: str, timeout: float = 30.0) -> list[str]:
    """Best-effort list of models that support generateContent (for the UI)."""
    if not api_key:
        return []
    url = f"{GEMINI_BASE}/models"
    headers = {"x-goog-api-key": api_key}
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url, headers=headers)
        if resp.status_code != 200:
            return []
        out = []
        for m in resp.json().get("models", []):
            methods = m.get("supportedGenerationMethods", [])
            if "generateContent" in methods:
                name = m.get("name", "")
                out.append(name.split("/")[-1] if "/" in name else name)
        return sorted(out)
    except Exception:
        return []
