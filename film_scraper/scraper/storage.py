"""Save scraped items to JSON or CSV."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


def to_json(items: list[dict[str, Any]], path: str | Path) -> Path:
    path = Path(path)
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def to_csv(items: list[dict[str, Any]], path: str | Path) -> Path:
    path = Path(path)
    if not items:
        path.write_text("", encoding="utf-8")
        return path
    headers: list[str] = []
    for item in items:
        for key in item:
            if key not in headers:
                headers.append(key)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=headers)
        writer.writeheader()
        for item in items:
            writer.writerow({k: _flatten(v) for k, v in item.items()})
    return path


def _flatten(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return value
