"""Shared test helpers: configure Kotodama through its settings files, never the process environment."""

from __future__ import annotations

import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from kotodama import config


def _write(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()))


@contextmanager
def kotodama_settings(user: dict[str, str] | None = None, node: dict[str, str] | None = None):
    """Point config at a temporary user-directory file and node .env with these values."""
    with tempfile.TemporaryDirectory(prefix="kotodama-test-") as tmp:
        root = Path(tmp)
        user_file = root / "user" / "kotodama" / ".env"
        node_file = root / "node" / ".env"
        if user is not None:
            _write(user_file, user)
        if node is not None:
            _write(node_file, node)
        with patch.object(config, "user_env_file", return_value=user_file), patch.object(config, "ENV_FILE", node_file):
            yield user_file
