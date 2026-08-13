"""Build a single AI context file from the whole project.

The file (`ai_context.md`) concatenates AGENTS.md, README.md and every source
/config/test file with headers, so an AI chat can be fed the full project in
one paste and then start programming the app.

Usage:
    python tools/ai_context.py                 # writes ai_context.md
    python tools/ai_context.py --out ctx.txt   # custom output path
    python tools/ai_context.py --no-configs    # skip site configs (privacy)

You can also pipe the output straight into a prompt:
    python tools/ai_context.py --stdout
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# files that never go into the context bundle
SKIP_PARTS = {
    "__pycache__",
    ".pytest_cache",
    ".git",
    ".venv",
    "venv",
    "output",
    "ai_context.md",
}

# path parts that are skipped only when --no-configs is set
CONFIG_PARTS = {"configs"}

_EXT_LABEL = {
    ".py": "python",
    ".json": "json",
    ".md": "markdown",
    ".txt": "text",
    ".toml": "toml",
}


def _collect(include_configs: bool) -> list[Path]:
    files: list[Path] = []
    for path in sorted(PROJECT_ROOT.rglob("*")):
        if not path.is_file():
            continue
        parts = path.relative_to(PROJECT_ROOT).parts
        if any(p in SKIP_PARTS for p in parts):
            continue
        if not include_configs and any(p in CONFIG_PARTS for p in parts):
            continue
        files.append(path)
    return files


def _header(path: Path) -> str:
    rel = path.relative_to(PROJECT_ROOT).as_posix()
    label = _EXT_LABEL.get(path.suffix, "text")
    return f"<!-- ============ FILE: {rel} ({label}) ============ -->"


def build(include_configs: bool = True) -> str:
    sections: list[str] = [
        "# Full Project Context — Movie-Serial-Scraper",
        "",
        "This bundle contains every file of the repository. Use it to",
        "understand the codebase, then implement the roadmap in AGENTS.md.",
        "",
    ]
    for path in _collect(include_configs):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if not text.strip():
            continue
        sections.append(_header(path))
        sections.append(text)
        sections.append("")
    return "\n".join(sections)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build an AI context bundle from the project.")
    parser.add_argument("--out", default="ai_context.md", help="Output path (default: ai_context.md)")
    parser.add_argument("--stdout", action="store_true", help="Print to stdout instead of writing a file")
    parser.add_argument("--no-configs", action="store_true", help="Exclude configs/*.json from the bundle")
    args = parser.parse_args()

    if args.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    bundle = build(include_configs=not args.no_configs)

    if args.stdout:
        sys.stdout.write(bundle)
        return 0
    out = PROJECT_ROOT / args.out
    out.write_text(bundle, encoding="utf-8")
    print(f"Wrote {len(bundle):,} chars -> {out}")
    print("Paste the whole file into your AI chat, then describe what you want to build.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
