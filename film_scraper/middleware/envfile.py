"""Tiny dependency-free .env loader.

Loads KEY=VALUE lines from a .env file next to this module (or the CWD) into
os.environ without overriding variables that are already set. Keeps secrets
out of the repo — .env is gitignored.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["load_env"]


def load_env(*paths: str | os.PathLike) -> None:
    candidates: list[Path] = []
    for p in paths:
        candidates.append(Path(p))
    if not paths:
        candidates.append(Path(__file__).resolve().parent.parent / ".env")
        candidates.append(Path.cwd() / ".env")
    for path in candidates:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value