"""Shared pytest fixtures: project path + a local mock movie site."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def mock_site(tmp_path):
    """Start a local mock movie site and write a matching site config.

    Yields (base_url, config_path). The server lives for the duration of the
    test; the config is written into ``tmp_path/configs/mock.json`` so the
    CLI can find it via the standard "configs" directory.
    """
    from tests import mock_site as mock

    base_url, thread = mock.start()
    config_dir = tmp_path / "configs"
    config_dir.mkdir(exist_ok=True)
    config_path = config_dir / "mock.json"
    mock.write_config(base_url, config_path)
    try:
        yield base_url, config_path
    finally:
        thread.join(timeout=2)
