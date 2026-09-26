"""Read-only settings contract. Needs aiohttp, as it is inside ComfyUI."""

import asyncio
import json
import os
import socket
import threading
from unittest.mock import patch

import pytest

pytest.importorskip("aiohttp")

from kotodama import client, config, settings
from tests.conftest import kotodama_settings  # noqa: E402

SENTINEL = "SENTINEL-KOTODAMA-KEY-7b5e"


class Request:
    def __init__(self, query="", body=b""):
        self.query_string = query
        self.body = body
        self.content_length = len(body)
        self.content = self

    async def read(self, amount):
        return self.body[:amount]


class Response:
    status = 200

    def read(self, _limit):
        return b'{"data": [{"id": "my-model"}]}'

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def configured():
    """A saved endpoint and key, with no user-dir or node .env interfering."""
    with kotodama_settings({"KOTODAMA_BASE_URL": "https://example.invalid", "KOTODAMA_API_KEY": SENTINEL}):
        settings._LAST_TEST = 0.0
        yield


def test_saved_endpoint_only_and_key_never_returned(configured) -> None:
    status = run(settings.get_status(Request()))
    captured = []

    def fake_open(req, timeout):
        captured.append((req.full_url, req.get_header("Authorization"), timeout))
        return Response()

    with patch.object(client, "urlopen", side_effect=fake_open):
        result = settings.probe_saved_endpoint()
    assert result == {"ok": True, "status": 200, "error": None}
    assert captured == [("https://example.invalid/v1/models", f"Bearer {SENTINEL}", 2.0)]
    for fragment in (SENTINEL, SENTINEL[:4], SENTINEL[-4:]):
        assert fragment.encode() not in status.body
    assert SENTINEL not in json.dumps(result)
    assert json.loads(status.body)["key_set"] is True

    settings._LAST_TEST = 0.0
    with patch.object(client, "urlopen", side_effect=fake_open):
        tested = run(settings.post_test(Request()))
    assert tested.status == 200
    assert SENTINEL.encode() not in tested.body
    throttled = run(settings.post_test(Request()))
    assert throttled.status == 429
    assert json.loads(throttled.body) == {"ok": False, "status": None, "error": "rate_limited"}


def test_key_reflection_mutation_is_caught(configured) -> None:
    # The sentinel assertion above detects the one-line key-reflection mutant.
    mutant = dict(settings.status_payload(), key=config.api_key())
    assert SENTINEL in json.dumps(mutant)


@pytest.mark.parametrize(
    "request_obj", [Request(query="url=https://evil.invalid"), Request(body=b"{}")], ids=["query", "body"]
)
def test_input_rejected_before_outbound_call(configured, request_obj) -> None:
    with patch.object(settings, "probe_saved_endpoint", side_effect=AssertionError("outbound call")):
        refused = run(settings.post_test(request_obj))
    assert refused.status == 400
    assert json.loads(refused.body) == {"ok": False, "status": None, "error": "input_not_allowed"}


def test_five_second_rate_limit() -> None:
    with patch.object(settings.time, "monotonic", side_effect=[10.0, 11.0, 16.0]):
        settings._LAST_TEST = 0.0
        assert settings.reserve_test() is True
        assert settings.reserve_test() is False
        assert settings.reserve_test() is True


@pytest.mark.parametrize("url", ["https://user:secret@example.invalid", "https://example.invalid/?token=secret"])
def test_credential_shaped_urls_are_refused(url: str) -> None:
    with patch.object(config, "base_url", return_value=url):
        assert config.safe_base_url() is None


def test_user_file_beats_node_dotenv_and_the_environment_is_ignored() -> None:
    user = {"KOTODAMA_BASE_URL": "https://user.example"}
    node = {"KOTODAMA_BASE_URL": "https://node.example", "KOTODAMA_API_KEY": "from-node"}
    with kotodama_settings(user, node):
        with patch.dict(os.environ, {"KOTODAMA_BASE_URL": "https://env.example", "KOTODAMA_API_KEY": "env-key"}):
            assert config.base_url() == "https://user.example"
            assert config.setting_with_source("KOTODAMA_BASE_URL")[1] == "userdir"
            # No key in the user file: the legacy node .env fills in, never the environment.
            assert config.api_key() == "from-node"
            assert config.setting_with_source("KOTODAMA_API_KEY")[1] == "dotenv"


def test_empty_user_value_falls_through_to_node_dotenv() -> None:
    with kotodama_settings({"KOTODAMA_API_KEY": ""}, {"KOTODAMA_API_KEY": "from-file"}):
        assert config.api_key() == "from-file"
        assert config.setting_with_source("KOTODAMA_API_KEY")[1] == "dotenv"


def _serve(reply: bytes):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(1)
    port = listener.getsockname()[1]
    seen: list[bytes] = []

    def run_server():
        try:
            connection, _ = listener.accept()
            seen.append(connection.recv(4096))
            connection.sendall(reply)
            connection.close()
        except (OSError, TimeoutError):
            pass
        finally:
            listener.close()

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    return port, seen, thread


def test_redirect_never_forwards_bearer_key() -> None:
    second_port, second_seen, second_thread = _serve(
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"
    )
    redirect = (
        f"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:{second_port}/stolen"
        "\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
    ).encode()
    first_port, first_seen, first_thread = _serve(redirect)
    with kotodama_settings({"KOTODAMA_BASE_URL": f"http://127.0.0.1:{first_port}", "KOTODAMA_API_KEY": SENTINEL}):
        result = settings.probe_saved_endpoint()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)
    assert result == {"ok": False, "status": 302, "error": "bad_response"}
    assert len(first_seen) == 1 and SENTINEL.encode() in first_seen[0]
    assert second_seen == []


# --- saving from the ComfyUI Settings panel ---


class SaveRequest:
    """Enough of an aiohttp request for post_settings."""

    def __init__(self, body, origin="http://127.0.0.1:8188", host="127.0.0.1:8188",
                 content_type="application/json", scheme="http", headers=None):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.headers = {"Origin": origin} if origin is not None else {}
        self.headers.update(headers or {})
        self.host = host
        self.scheme = scheme
        self.content_type = content_type
        self.content = self
        self._raw = raw

    async def read(self, amount):
        # Consume, like aiohttp's StreamReader: a second read gets the rest.
        chunk, self._raw = self._raw[:amount], self._raw[amount:]
        return chunk


@pytest.fixture
def user_dir(tmp_path):
    """A writable ComfyUI user directory and a clean environment."""
    env_file = tmp_path / "kotodama" / ".env"
    with patch.dict(os.environ, {}, clear=True):
        with patch.object(config, "user_env_file", return_value=env_file):
            with patch.object(config, "ENV_FILE", tmp_path / "node.env"):
                yield env_file


def save(body, **kw):
    response = run(settings.post_settings(SaveRequest(body, **kw)))
    return response.status, json.loads(response.body)


def test_save_writes_user_dir_and_never_returns_the_key(user_dir) -> None:
    status, body = save({"base_url": "https://llm.example", "confirm_url_change": True,
                         "api_key": SENTINEL, "fallback_models": "a, b", "timeout": 90})
    assert status == 200 and body["ok"] is True
    assert body["url"] == "https://llm.example" and body["key_set"] is True
    assert body["fallback_models"] == ["a", "b"] and body["timeout"] == 90.0
    assert SENTINEL not in json.dumps(body)
    assert SENTINEL not in run(settings.get_status(None)).body.decode()
    saved = config._read_env_file(user_dir)
    assert saved["KOTODAMA_API_KEY"] == SENTINEL and saved["KOTODAMA_BASE_URL"] == "https://llm.example"
    assert (user_dir.stat().st_mode & 0o777) == 0o600  # the file may hold the key


@pytest.mark.parametrize(
    ("kw", "status", "error"),
    [
        ({"origin": None}, 403, "cross_origin"),
        ({"origin": "https://evil.example"}, 403, "cross_origin"),
        # Same host and port, different scheme: a different origin to a browser.
        ({"origin": "https://same.example:8188", "host": "same.example:8188"}, 403, "cross_origin"),
        # Forwarded headers come from the client, so they cannot vouch for the scheme.
        ({"origin": "https://same.example:8188", "host": "same.example:8188",
          "headers": {"X-Forwarded-Proto": "https", "Forwarded": "proto=https"}}, 403, "cross_origin"),
        ({"origin": "http://127.0.0.1:8188/x"}, 403, "cross_origin"),
        ({"origin": "null"}, 403, "cross_origin"),
        ({"content_type": "text/plain"}, 415, "json_required"),
    ],
    ids=["no origin", "cross-site origin", "scheme differs", "forwarded headers ignored",
         "origin with path", "opaque origin", "form post"],
)
def test_save_refuses_requests_a_malicious_page_could_send(user_dir, kw, status, error) -> None:
    got_status, body = save({"api_key": SENTINEL}, **kw)
    assert (got_status, body["error"]) == (status, error)
    assert not user_dir.exists()  # nothing written


def test_changing_the_endpoint_needs_confirmation(user_dir) -> None:
    status, body = save({"base_url": "https://llm.example"})
    assert (status, body["error"]) == (409, "confirm_url_change")
    assert not user_dir.exists()


def test_changing_the_endpoint_drops_the_old_key_unless_a_new_one_is_sent(user_dir) -> None:
    save({"base_url": "https://good.example", "confirm_url_change": True, "api_key": SENTINEL})
    status, body = save({"base_url": "https://attacker.example", "confirm_url_change": True})
    assert status == 200 and body["key_set"] is False
    assert "KOTODAMA_API_KEY" not in config._read_env_file(user_dir)
    # With a key sent alongside, the new endpoint keeps that new key.
    save({"base_url": "https://good.example", "confirm_url_change": True, "api_key": "new-key"})
    assert config._read_env_file(user_dir)["KOTODAMA_API_KEY"] == "new-key"


def test_saving_other_fields_keeps_the_key_and_endpoint(user_dir) -> None:
    save({"base_url": "https://good.example", "confirm_url_change": True, "api_key": SENTINEL})
    status, body = save({"base_url": "https://good.example", "timeout": 30})  # unchanged URL: no confirm needed
    assert status == 200 and body["key_set"] is True and body["timeout"] == 30.0


def test_clear_key(user_dir) -> None:
    save({"base_url": "https://good.example", "confirm_url_change": True, "api_key": SENTINEL})
    status, body = save({"clear_api_key": True})
    assert status == 200 and body["key_set"] is False


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({"base_url": "https://user:pw@llm.example", "confirm_url_change": True}, "invalid_url"),
        ({"base_url": "https://llm.example/?token=x", "confirm_url_change": True}, "invalid_url"),
        ({"base_url": "ftp://llm.example", "confirm_url_change": True}, "invalid_url"),
        ({"api_key": "   "}, "invalid_api_key"),
        ({"api_key": "a\nKOTODAMA_BASE_URL=https://evil.example"}, "invalid_value"),
        ({"timeout": 0}, "invalid_timeout"),
        ({"timeout": True}, "invalid_timeout"),
        ({"fallback_models": 5}, "invalid_fallback_models"),
        ({"url": "https://llm.example"}, "unknown_field"),
    ],
)
def test_invalid_input_is_refused_and_nothing_is_written(user_dir, body, error) -> None:
    status, got = save(body)
    assert status == 400 and got["error"] == error
    assert not user_dir.exists()


def test_environment_variables_no_longer_override_the_panel(user_dir) -> None:
    save({"base_url": "https://panel.example", "confirm_url_change": True, "api_key": SENTINEL})
    with patch.dict(os.environ, {"KOTODAMA_BASE_URL": "https://env.example", "KOTODAMA_API_KEY": "env-key"}):
        body = json.loads(run(settings.get_status(None)).body)
        assert config.api_key() == SENTINEL
    assert body["url"] == "https://panel.example" and body["source"] == {"url": "userdir", "key": "userdir"}
    assert "shadowed" not in body


def test_no_user_directory_means_no_save(tmp_path) -> None:
    with patch.object(config, "user_env_file", return_value=None):
        status, body = save({"timeout": 30})
    assert (status, body["error"]) == (503, "no_user_directory")


@pytest.mark.parametrize(
    ("kw", "env"),
    [
        ({"origin": "http://same.example", "host": "same.example:80"}, {}),
        ({"origin": "https://same.example", "host": "same.example:443", "scheme": "https"}, {}),
        # TLS reverse proxy: the browser sees https, ComfyUI sees plain http.
        ({"origin": "https://comfy.example", "host": "comfy.example"},
         {"KOTODAMA_ALLOWED_ORIGINS": "https://comfy.example, https://other.example"}),  # set by hand in the user file
    ],
    ids=["default http port", "default https port", "allowed proxy origin"],
)
def test_same_origin_accepts_the_page_itself(user_dir, kw, env) -> None:
    if env:
        user_dir.parent.mkdir(parents=True, exist_ok=True)
        user_dir.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
    status, _ = save({"timeout": 30}, **kw)
    assert status == 200


@pytest.mark.parametrize("name", ["LITELLM_API_KEY", "KOTODAMA_API_KEY"])
@pytest.mark.parametrize("new_key", [None, "replacement"], ids=["no new key", "with new key"])
def test_endpoint_change_refused_when_the_key_lives_in_the_legacy_node_dotenv(user_dir, name, new_key) -> None:
    # Yua's repro: deleting the panel's key would leave the node .env key in
    # force, and it would be sent to the new endpoint.
    config.write_user_settings({"KOTODAMA_BASE_URL": "https://good.example"})
    config.ENV_FILE.write_text(f"{name}=OLD-NODE-KEY\n")
    body = {"base_url": "https://new.example", "confirm_url_change": True}
    if new_key:
        body["api_key"] = new_key
    assert settings.plan_settings_update(body) == ({}, "key_outside_panel")
    status, got = save(body)
    assert (status, got["error"]) == (409, "key_outside_panel")
    assert config.base_url() == "https://good.example"
    assert config.api_key() == "OLD-NODE-KEY"


def test_an_environment_key_does_not_block_or_follow_an_endpoint_change(user_dir) -> None:
    config.write_user_settings({"KOTODAMA_BASE_URL": "https://good.example", "KOTODAMA_API_KEY": "PANEL-KEY"})
    with patch.dict(os.environ, {"KOTODAMA_API_KEY": "ENV-KEY", "KOTODAMA_BASE_URL": "https://env.example"}):
        status, body = save({"base_url": "https://new.example", "confirm_url_change": True})
        assert status == 200 and body["url"] == "https://new.example" and body["key_set"] is False
        assert config.api_key() == ""  # the env key is not used


def test_endpoint_change_removes_a_legacy_key_in_the_user_file(user_dir) -> None:
    user_dir.parent.mkdir(parents=True)
    user_dir.write_text("LITELLM_BASE_URL=https://good.example\nLITELLM_API_KEY=OLD-KEY\n")
    status, body = save({"base_url": "https://new.example", "confirm_url_change": True})
    assert status == 200 and body["url"] == "https://new.example" and body["key_set"] is False
    assert "LITELLM_API_KEY" not in config._read_env_file(user_dir)


def test_clear_key_also_clears_a_legacy_key(user_dir) -> None:
    user_dir.parent.mkdir(parents=True)
    user_dir.write_text("LITELLM_API_KEY=OLD-KEY\n")
    status, body = save({"clear_api_key": True})
    assert status == 200 and body["key_set"] is False


def test_legacy_names_can_be_deleted_but_never_written(user_dir) -> None:
    with pytest.raises(config.SettingsWriteError):
        config.write_user_settings({"LITELLM_API_KEY": "x"})


def test_status_never_reports_an_absolute_path(user_dir, tmp_path) -> None:
    status, body = save({"timeout": 30})
    for payload in (body, json.loads(run(settings.get_status(None)).body)):
        text = json.dumps(payload)
        assert str(tmp_path) not in text and "config_path" not in payload
        assert payload["config_location"] == "ComfyUI user directory (kotodama/.env)"


def test_clear_key_refused_when_the_key_lives_in_the_legacy_node_dotenv(user_dir) -> None:
    # Yua's repro: Clear returned 200 and the UI said "Key cleared." while the
    # node .env key stayed in force.
    config.write_user_settings({"KOTODAMA_API_KEY": "PANEL-KEY"})
    config.ENV_FILE.write_text("KOTODAMA_API_KEY=NODE-KEY\n")
    status, got = save({"clear_api_key": True})
    assert (status, got["error"]) == (409, "key_outside_panel")
    assert config._read_env_file(user_dir)["KOTODAMA_API_KEY"] == "PANEL-KEY"  # nothing written


def test_clear_key_ignores_an_environment_key(user_dir) -> None:
    config.write_user_settings({"KOTODAMA_API_KEY": "PANEL-KEY"})
    with patch.dict(os.environ, {"KOTODAMA_API_KEY": "ENV-KEY"}):
        status, body = save({"clear_api_key": True})
        assert status == 200 and body["key_set"] is False


class SizedResponse(Response):
    def __init__(self, body: bytes):
        self.body = body

    def read(self, limit):
        return self.body[:limit]


def _catalogue(count: int) -> bytes:
    return json.dumps({"data": [{"id": f"vendor/model-{i:06d}", "pad": "x" * 1500} for i in range(count)]}).encode()


def test_connection_test_accepts_a_large_model_catalogue(configured) -> None:
    body = _catalogue(600)  # ~0.9 MB, like OpenRouter's 459-model list
    assert len(body) > 65537
    with patch.object(client, "urlopen", return_value=SizedResponse(body)):
        assert settings.probe_saved_endpoint() == {"ok": True, "status": 200, "error": None}


def test_connection_test_bounds_the_read(configured) -> None:
    body = b" " * (settings._TEST_MAX_BYTES + 1)
    with patch.object(client, "urlopen", return_value=SizedResponse(body)):
        assert settings.probe_saved_endpoint() == {"ok": False, "status": 200, "error": "too_large"}


class TrickleRequest(SaveRequest):
    """read(n) returns at most 10 bytes, like a body still arriving over TCP."""

    async def read(self, amount):
        chunk, self._raw = self._raw[:min(amount, 10)], self._raw[min(amount, 10):]
        return chunk


def test_a_body_arriving_in_pieces_is_read_whole(user_dir) -> None:
    # Found by Aoi on the immich port: content.read(n) returns only what is buffered.
    response = run(settings.post_settings(TrickleRequest({"api_key": SENTINEL})))
    assert response.status == 200, response.body
    assert config._read_env_file(user_dir)["KOTODAMA_API_KEY"] == SENTINEL


@pytest.mark.parametrize("name", ["KOTODAMA_BASE_URL", "LITELLM_BASE_URL"])
def test_url_edit_applies_even_when_the_environment_sets_a_url(user_dir, name) -> None:
    # Before 0.3.0 this was refused as url_shadowed; the environment is no longer read.
    config.write_user_settings({"KOTODAMA_API_KEY": "PANEL-KEY"})
    with patch.dict(os.environ, {name: "https://env.example"}):
        status, got = save({"base_url": "https://panel.example", "confirm_url_change": True})
        assert status == 200 and got["url"] == "https://panel.example"
