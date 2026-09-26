"""Tiny OpenAI-compatible chat client.

Stdlib only, so a portable ComfyUI install needs no extra package install.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request

from . import config


class LiteLLMError(RuntimeError):
    """Raised so a failure stops the graph instead of poisoning the render.

    An enhancer that returns "" on error still renders a picture — the
    WRONG picture, silently, and you find out at the image rather than at
    the node.
    """


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keep bearer credentials on the configured endpoint only."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def urlopen(req, timeout):
    return _OPENER.open(req, timeout=timeout)


def _request(path: str, payload: dict | None, timeout: float) -> dict:
    endpoint = config.safe_base_url()
    if not endpoint:
        raise LiteLLMError(
            "Kotodama needs a valid KOTODAMA_BASE_URL (http(s), no URL credentials, "
            "query, or fragment) in Settings -> Kotodama."
        )
    url = f"{endpoint}{path}"
    key = config.api_key()

    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Accept", "application/json")
    if data:
        req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", f"Bearer {key}")

    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        hint = ""
        if exc.code in (401, 403):
            hint = "  Check the API key in Settings -> Kotodama."
        raise LiteLLMError(
            f"Endpoint returned HTTP {exc.code} for {url}.{hint}"
        ) from exc
    except TimeoutError as exc:
        raise LiteLLMError(
            f"LiteLLM timed out after {timeout:.0f}s at {url}.\n"
            "  Big local models are slow on long generations - raise "
            "KOTODAMA_TIMEOUT in the node's .env, or lower max_tokens, "
            "or pick a faster model."
        ) from exc
    except urllib.error.URLError as exc:
        # urllib wraps socket timeouts in URLError(reason=socket.timeout(...)),
        # so the TimeoutError branch above rarely fires for real network stalls.
        # Surface them with the timeout hint instead of the unreachable hint -
        # the actionable fix is different (raise KOTODAMA_TIMEOUT vs check the URL).
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise LiteLLMError(
                f"LiteLLM timed out after {timeout:.0f}s at {url}.\n"
                "  Big local models are slow on long generations - raise "
                "KOTODAMA_TIMEOUT in the node's .env, or lower max_tokens, "
                "or pick a faster model."
            ) from exc
        raise LiteLLMError(
            f"Could not reach LiteLLM at {url} - {exc.reason}\n"
            "  Check KOTODAMA_BASE_URL and that this host can reach it."
        ) from exc

    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LiteLLMError(
            f"LiteLLM returned a non-JSON body from {url}.\n"
            "  Check KOTODAMA_BASE_URL points at an OpenAI-compatible endpoint."
        ) from exc
    if not isinstance(parsed, dict):
        raise LiteLLMError(
            f"LiteLLM returned JSON that is not an object from {url}."
        )
    return parsed


_MODEL_CACHE: tuple[float, str, list[str], bool] | None = None
_MODEL_CACHE_TTL = 300.0
_MODEL_CACHE_MISS_TTL = 30.0
_MODELS_TIMEOUT = 2.0
_MODEL_CACHE_LOCK = threading.Lock()


def list_models(force: bool = False) -> tuple[list[str], bool]:
    """Return (models, live).

    ``live`` is False when the proxy could not be reached and the caller is
    holding the configured fallback list instead. The distinction matters:
    a short fallback menu must not look like a live model listing.

    ComfyUI calls INPUT_TYPES() from the HTTP handler and the executor can
    run concurrently with that handler, so the cache read/fetch/write is
    guarded with a lock. The races are benign (two duplicate /v1/models
    requests, identical data) but the lock keeps it cheap to reason about.
    """
    with _MODEL_CACHE_LOCK:
        global _MODEL_CACHE

        now = time.monotonic()
        endpoint = config.safe_base_url()
        if not endpoint:
            return config.fallback_models(), False
        if not force and _MODEL_CACHE:
            cached_at, cached_endpoint, cached_models, live = _MODEL_CACHE
            ttl = _MODEL_CACHE_TTL if live else _MODEL_CACHE_MISS_TTL
            if cached_endpoint == endpoint and (now - cached_at) < ttl:
                return list(cached_models), live

        try:
            payload = _request("/v1/models", None, timeout=_MODELS_TIMEOUT)
            entries = payload.get("data") or []
            models = sorted(
                str(entry["id"])
                for entry in entries
                if isinstance(entry, dict) and entry.get("id")
            )
        except (LiteLLMError, TypeError, AttributeError):
            models = []

        if not models:
            fallback = config.fallback_models()
            _MODEL_CACHE = (now, endpoint, fallback, False)
            return list(fallback), False

        _MODEL_CACHE = (now, endpoint, models, True)
        return models, True


def complete(
    model: str,
    system_prompt: str,
    user_text: str,
    temperature: float,
    max_tokens: int,
    seed: int,
) -> str:
    """One chat completion. Returns the assistant's message content."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "seed": int(seed),
    }

    body = _request("/v1/chat/completions", payload, config.request_timeout())

    choices = body.get("choices") or []
    if not choices:
        raise LiteLLMError(
            f"LiteLLM returned no choices for model '{model}'.\n"
            f"  Raw response: {json.dumps(body)[:600]}"
        )

    try:
        choice = choices[0]
        message = choice.get("message") or {}
        content = message.get("content")
        finish_reason = choice.get("finish_reason")
    except (TypeError, AttributeError) as exc:
        raise LiteLLMError(
            f"LiteLLM returned a malformed choice for model '{model}'.\n"
            f"  Raw response: {json.dumps(body)[:600]}"
        ) from exc

    if content is None:
        raise LiteLLMError(
            f"LiteLLM returned a choice with no content for model '{model}'.\n"
            f"  Finish reason: {finish_reason!r}"
        )

    text = str(content).strip()
    if not text:
        raise LiteLLMError(
            f"Model '{model}' returned an empty prompt. "
            "Refusing to pass an empty string downstream - "
            "an empty conditioning renders a picture nobody asked for."
        )
    if finish_reason == "length":
        raise LiteLLMError(
            f"Model '{model}' stopped at max_tokens ({max_tokens}) before finishing.\n"
            "  Raise max_tokens on the node rather than wiring a truncated "
            "prompt into CLIP."
        )
    return text
