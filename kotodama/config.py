"""Connection settings for an OpenAI-compatible chat endpoint.

Resolution order, first non-empty value wins:

1. ``kotodama/.env`` in ComfyUI's user directory, written by the Settings
   panel (Settings -> Kotodama). This is the one supported place.
2. a ``.env`` file next to the node package: read only as a legacy fallback
   for installs configured before the panel existed. Deprecated.

Process environment variables are NOT read (since 0.3.0): one source, set
from the panel and visible in ComfyUI.

The API key is DELIBERATELY not a node widget. ComfyUI serialises every
widget value into the saved workflow JSON *and* into the PNG metadata of
every image the graph produces, so a key in a widget leaks into every
picture created from that workflow.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

NODE_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = NODE_ROOT / ".env"

UNCONFIGURED_MODEL = "<configure endpoint or fallback model>"


def _read_env_file(path: Path) -> dict[str, str]:
    """Parse a minimal KEY=VALUE .env. Missing file is not an error."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        values[key.strip()] = value
    return values


def user_env_file() -> Path | None:
    """Per-user config survives replacement of the custom-node folder."""
    try:
        import folder_paths
        return Path(folder_paths.get_user_directory()) / "kotodama" / ".env"
    except (ImportError, AttributeError):
        return None


def config_path() -> Path:
    return user_env_file() or ENV_FILE


def config_location() -> str:
    """Where settings are saved, without the absolute path (which names the machine's user)."""
    if user_env_file() is not None:
        return "ComfyUI user directory (kotodama/.env)"
    return "custom node folder (.env)"


def _sources():
    user_file = user_env_file()
    if user_file is not None:
        yield "userdir", _read_env_file(user_file)
    yield "dotenv", _read_env_file(ENV_FILE)


def _lookup(name: str, default: str = "") -> str:
    for _, source in _sources():
        value = source.get(name, "").strip()
        if value:
            return value
    return default


def _setting(primary: str, legacy: str = "", default: str = "") -> str:
    """Read a new setting, retaining old LiteLLM names for existing installs."""
    return setting_with_source(primary, legacy, default)[0]


def setting_with_source(
    primary: str, legacy: str = "", default: str = ""
) -> tuple[str, str]:
    for label, source in _sources():
        value = source.get(primary, "").strip()
        if value:
            return value, label
        value = source.get(legacy, "").strip() if legacy else ""
        if value:
            return value, label
    return default, "none"


def base_url() -> str:
    return _setting("KOTODAMA_BASE_URL", "LITELLM_BASE_URL").rstrip("/")


def safe_base_url() -> str | None:
    """Return a display-safe endpoint; never reflect URL credentials or tokens."""
    value = base_url()
    if not value:
        return None
    try:
        parts = urlsplit(value)
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            return None
    except ValueError:
        return None
    return value


def api_key() -> str:
    return _setting("KOTODAMA_API_KEY", "LITELLM_API_KEY")


def request_timeout() -> float:
    """Seconds to wait on a completion. 300s is headroom for slow local generations.

    Floored at 1.0s - ``timeout=0`` makes urllib fail immediately and produces a
    misleading "unreachable" error instead of a real timeout.
    """
    raw = _lookup("KOTODAMA_TIMEOUT", "300")
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 300.0


def fallback_models() -> list[str]:
    """Models offered when the proxy cannot be reached at menu-build time."""
    raw = _lookup("KOTODAMA_FALLBACK_MODELS", "")
    listed = [m.strip() for m in raw.split(",") if m.strip()]
    return listed or [UNCONFIGURED_MODEL]


# --- writing settings from the ComfyUI Settings panel ---

# Keys the Settings panel may write into the user-directory file.
WRITABLE_KEYS = ("KOTODAMA_BASE_URL", "KOTODAMA_API_KEY", "KOTODAMA_FALLBACK_MODELS", "KOTODAMA_TIMEOUT")
# Old LiteLLM names the panel may delete (never write), so a legacy key cannot survive a URL change.
REMOVABLE_KEYS = ("LITELLM_BASE_URL", "LITELLM_API_KEY")


class SettingsWriteError(RuntimeError):
    """The settings could not be saved; ``code`` is a short machine-readable reason."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def valid_endpoint(value: str) -> bool:
    """An endpoint the panel may save: absolute http(s), no credentials, no query or fragment."""
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return (
        parts.scheme in ("http", "https")
        and bool(parts.hostname)
        and parts.username is None
        and parts.password is None
        and not parts.query
        and not parts.fragment
    )


def key_outside_panel() -> str | None:
    """Name the source of an API key the panel cannot remove, or None.

    A key in the node folder's legacy ``.env`` stays in force after the panel
    deletes its own copy, so it would follow a new endpoint.
    """
    names = ("KOTODAMA_API_KEY", "LITELLM_API_KEY")
    node_file = _read_env_file(ENV_FILE)
    if any(node_file.get(name, "").strip() for name in names):
        return "dotenv"
    return None


def allowed_origins() -> list[str]:
    """Extra browser origins allowed to save settings, e.g. behind a TLS reverse proxy.

    Set by hand as ``KOTODAMA_ALLOWED_ORIGINS=https://a.example,https://b.example``
    in the user-directory ``kotodama/.env``.

    Deliberately not writable from the panel.
    """
    raw = _lookup("KOTODAMA_ALLOWED_ORIGINS", "")
    return [o.strip() for o in raw.split(",") if o.strip()]


def write_user_settings(updates: dict[str, str | None]) -> Path:
    """Apply ``updates`` to the user-directory settings file; ``None`` removes a key.

    The file survives replacement of the custom-node folder. It is written
    atomically and readable only by its owner, because it may hold the API key.
    """
    path = user_env_file()
    if path is None:
        raise SettingsWriteError("no_user_directory", "ComfyUI's user directory is not available")
    for key, value in updates.items():
        if key not in WRITABLE_KEYS and not (key in REMOVABLE_KEYS and value is None):
            raise SettingsWriteError("unknown_setting", f"{key} is not a writable setting")
        if value is not None and ("\n" in value or "\r" in value):
            raise SettingsWriteError("invalid_value", f"{key} must be a single line")

    current = _read_env_file(path)
    for key, value in updates.items():
        if value is None or value == "":
            current.pop(key, None)
        else:
            current[key] = value

    path.parent.mkdir(parents=True, exist_ok=True)
    body = "# Written by the Kotodama settings panel. Environment variables override these values.\n"
    body += "".join(f"{key}={value}\n" for key, value in current.items())
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(body)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path
