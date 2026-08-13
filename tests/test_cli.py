"""End-to-end CLI tests against a local mock movie site (no network needed)."""

from __future__ import annotations

import json
from pathlib import Path


def _run_cli(monkeypatch, tmp_path, *args):
    monkeypatch.chdir(tmp_path)
    import cli

    return cli.main(list(args))


def test_cli_search_end_to_end(monkeypatch, tmp_path, mock_site):
    base_url, config_path = mock_site
    out = tmp_path / "out.json"
    code = _run_cli(
        monkeypatch,
        tmp_path,
        "--site", "mock",
        "--query", "inception",
        "--format", "json",
        "--out", str(out),
        "--no-details",
        "--delay", "0",
    )
    assert code == 0
    assert out.exists()
    items = json.loads(out.read_text(encoding="utf-8"))
    assert items and items[0]["title"] == "Inception"
    assert items[0]["source"] == "mock"
    assert items[0]["detail_url"].startswith(base_url)


def test_cli_watch_only_drops_downloads(monkeypatch, tmp_path, mock_site):
    mock_site  # ensure server is up
    out = tmp_path / "out.json"
    code = _run_cli(
        monkeypatch,
        tmp_path,
        "--site", "mock",
        "--query", "inception",
        "--format", "json",
        "--out", str(out),
        "--watch-only",
        "--delay", "0",
    )
    assert code == 0
    items = json.loads(out.read_text(encoding="utf-8"))
    assert "download_servers" not in items[0]


def test_cli_list_sites(monkeypatch, tmp_path, mock_site, capsys):
    base_url, config_path = mock_site
    code = _run_cli(monkeypatch, tmp_path, "--list")
    assert code == 0
    assert "mock" in capsys.readouterr().out


def test_cli_unknown_site(monkeypatch, tmp_path, capsys):
    code = _run_cli(monkeypatch, tmp_path, "--site", "does-not-exist", "--query", "x")
    assert code == 1
