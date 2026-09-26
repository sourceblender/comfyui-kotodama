"""ComfyUI settings routes for Kotodama.

The Settings panel reads status and saves the endpoint, key, fallback models
and timeout into ComfyUI's user directory. ComfyUI has no login by default, so
the save route is written against the obvious abuses:

- it accepts only same-origin JSON from a browser: the Origin's scheme, host
  and port must match this server (or an origin listed in
  KOTODAMA_ALLOWED_ORIGINS, for a TLS reverse proxy); cross-site pages and
  requests without an Origin are refused. X-Forwarded-* headers are ignored,
  because any client can send them;
- the API key is write-only: it is never returned, logged or shown;
- changing the endpoint needs explicit confirmation AND clears the saved key
  unless a new key is sent with it, so redirecting the URL cannot forward your
  existing key to someone else's server. If the key is still in the node
  folder's legacy .env, which the panel cannot remove, the endpoint cannot be
  changed and the key cannot be cleared from the panel;
- the status never reports absolute paths;
- a connection test only ever uses saved configuration, so the server is not
  an arbitrary URL probe.

This is only as safe as your ComfyUI exposure: anyone who can use your ComfyUI
page can also change these settings.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from . import client, config

_TEST_INTERVAL = 5.0
_TEST_TIMEOUT = 2.0
# Large catalogues are big: OpenRouter's /v1/models is ~750 KB. Bounded, but not at 64 KB.
_TEST_MAX_BYTES = 8 * 1024 * 1024
_TEST_LOCK = threading.Lock()
_LAST_TEST = 0.0


def status_payload() -> dict:
    _, url_source = config.setting_with_source("KOTODAMA_BASE_URL", "LITELLM_BASE_URL")
    _, key_source = config.setting_with_source("KOTODAMA_API_KEY", "LITELLM_API_KEY")
    fallback = [m for m in config.fallback_models() if m != config.UNCONFIGURED_MODEL]
    return {
        "url": config.safe_base_url(),
        "key_set": bool(config.api_key()),
        "fallback_models": fallback,
        "timeout": config.request_timeout(),
        "source": {"url": url_source, "key": key_source},
        "writable": config.user_env_file() is not None,
        "config_location": config.config_location(),
    }


def reserve_test() -> bool:
    """Reserve one outbound probe per five seconds, including failed probes."""
    global _LAST_TEST
    with _TEST_LOCK:
        now = time.monotonic()
        if now - _LAST_TEST < _TEST_INTERVAL:
            return False
        _LAST_TEST = now
        return True


def probe_saved_endpoint() -> dict:
    endpoint = config.safe_base_url()
    if endpoint is None:
        return {"ok": False, "status": None, "error": "not_configured"}

    req = urllib.request.Request(f"{endpoint}/v1/models")
    req.add_header("Accept", "application/json")
    key = config.api_key()
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with client.urlopen(req, timeout=_TEST_TIMEOUT) as response:
            status = response.status
            raw = response.read(_TEST_MAX_BYTES + 1)
        if len(raw) > _TEST_MAX_BYTES:
            return {"ok": False, "status": status, "error": "too_large"}
        payload = json.loads(raw)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            return {"ok": False, "status": status, "error": "bad_response"}
        return {"ok": True, "status": status, "error": None}
    except urllib.error.HTTPError as exc:
        error = {401: "unauthorized", 403: "forbidden"}.get(exc.code, "bad_response")
        exc.close()
        return {"ok": False, "status": exc.code, "error": error}
    except TimeoutError:
        return {"ok": False, "status": None, "error": "timeout"}
    except urllib.error.URLError as exc:
        error = (
            "timeout"
            if isinstance(exc.reason, (TimeoutError, socket.timeout))
            else "unreachable"
        )
        return {"ok": False, "status": None, "error": error}
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return {"ok": False, "status": None, "error": "bad_response"}


async def get_status(request):
    from aiohttp import web
    return web.json_response(status_payload())


async def post_test(request):
    from aiohttp import web
    has_input = request.query_string or request.content_length not in (None, 0)
    if not has_input:
        has_input = bool(await request.content.read(1))
    if has_input:
        return web.json_response(
            {"ok": False, "status": None, "error": "input_not_allowed"}, status=400
        )
    if not reserve_test():
        return web.json_response(
            {"ok": False, "status": None, "error": "rate_limited"}, status=429
        )
    return web.json_response(await asyncio.to_thread(probe_saved_endpoint))


_MAX_SETTINGS_BODY = 16 * 1024
_SETTINGS_FIELDS = {"base_url", "api_key", "clear_api_key", "fallback_models", "timeout", "confirm_url_change"}


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin(scheme: str, netloc: str) -> tuple[str, str, int] | None:
    """Normalise to (scheme, host, port) as a browser compares origins; None if malformed."""
    scheme = (scheme or "").lower()
    if scheme not in _DEFAULT_PORTS or not netloc:
        return None
    try:
        parts = urlsplit(f"//{netloc}")
        port = parts.port
    except ValueError:
        return None
    if not parts.hostname or parts.username is not None or parts.password is not None:
        return None
    return scheme, parts.hostname, port or _DEFAULT_PORTS[scheme]


def _same_origin(request) -> bool:
    """A browser always sends Origin on a cross-site POST; require it to match this server."""
    try:
        sent = urlsplit(request.headers.get("Origin", ""))
    except ValueError:
        return False
    if sent.path or sent.query or sent.fragment:
        return False
    got = _origin(sent.scheme, sent.netloc)
    if got is None:
        return False
    if got == _origin(request.scheme, request.host or ""):
        return True
    for allowed in config.allowed_origins():
        try:
            parts = urlsplit(allowed)
        except ValueError:
            continue
        if got == _origin(parts.scheme, parts.netloc):
            return True
    return False


def _refuse(web, status: int, error: str):
    return web.json_response({"ok": False, "error": error}, status=status)


def plan_settings_update(body: dict) -> tuple[dict[str, str | None], str | None]:
    """Turn a panel request into file updates, or return an error code.

    Pure (no I/O beyond reading current config) so the rules are unit-testable.
    """
    unknown = set(body) - _SETTINGS_FIELDS
    if unknown:
        return {}, "unknown_field"
    updates: dict[str, str | None] = {}

    new_key = body.get("api_key")
    if new_key is not None and (not isinstance(new_key, str) or not new_key.strip()):
        return {}, "invalid_api_key"
    if body.get("clear_api_key") is True:
        # Clearing only the panel's copy would report success while the key stays in force.
        if config.key_outside_panel():
            return {}, "key_outside_panel"
        updates["KOTODAMA_API_KEY"] = None
        updates["LITELLM_API_KEY"] = None

    if "base_url" in body:
        url = body["base_url"]
        if not isinstance(url, str):
            return {}, "invalid_url"
        url = url.strip().rstrip("/")
        if url and not config.valid_endpoint(url):
            return {}, "invalid_url"
        if url != config.base_url():
            if body.get("confirm_url_change") is not True:
                return {}, "confirm_url_change"
            # Never let a new endpoint receive the key that was saved for the old one.
            # A key in the legacy node .env would follow the URL once the panel's
            # copy is deleted, so refuse until it is moved or removed.
            if config.key_outside_panel():
                return {}, "key_outside_panel"
            updates["KOTODAMA_API_KEY"] = None
            updates["LITELLM_API_KEY"] = None
            updates["LITELLM_BASE_URL"] = None
        updates["KOTODAMA_BASE_URL"] = url or None

    if new_key is not None:
        updates["KOTODAMA_API_KEY"] = new_key.strip()

    if "fallback_models" in body:
        models = body["fallback_models"]
        if isinstance(models, str):
            models = [m.strip() for m in models.split(",")]
        if not isinstance(models, list) or not all(isinstance(m, str) for m in models):
            return {}, "invalid_fallback_models"
        models = [m.strip() for m in models if m.strip()]
        if any("," in m for m in models):
            return {}, "invalid_fallback_models"
        updates["KOTODAMA_FALLBACK_MODELS"] = ",".join(models) or None

    if "timeout" in body:
        timeout = body["timeout"]
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 3600:
            return {}, "invalid_timeout"
        updates["KOTODAMA_TIMEOUT"] = str(timeout)

    return updates, None


async def post_settings(request):
    from aiohttp import web
    if not _same_origin(request):
        return _refuse(web, 403, "cross_origin")
    if request.content_type != "application/json":
        return _refuse(web, 415, "json_required")
    # read(n) returns whatever is buffered, which can be part of the body;
    # keep reading until EOF or one byte past the limit.
    raw = b""
    while len(raw) <= _MAX_SETTINGS_BODY:
        chunk = await request.content.read(_MAX_SETTINGS_BODY + 1 - len(raw))
        if not chunk:
            break
        raw += chunk
    if len(raw) > _MAX_SETTINGS_BODY:
        return _refuse(web, 413, "too_large")
    try:
        body = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _refuse(web, 400, "invalid_json")
    if not isinstance(body, dict):
        return _refuse(web, 400, "invalid_json")
    updates, error = plan_settings_update(body)
    if error:
        conflict = ("confirm_url_change", "key_outside_panel")
        return _refuse(web, 409 if error in conflict else 400, error)
    try:
        config.write_user_settings(updates)
    except config.SettingsWriteError as exc:
        return _refuse(web, 503 if exc.code == "no_user_directory" else 400, exc.code)
    client._MODEL_CACHE = None  # the model menu must follow the new endpoint
    return web.json_response({"ok": True, **status_payload()})


def register_routes(routes):
    routes.get("/kotodama/status")(get_status)
    routes.post("/kotodama/test")(post_test)
    routes.post("/kotodama/settings")(post_settings)
