"""Token accounting from coding-CLI transcript JSONL files.

Claude Code transcripts live at ~/.claude/projects/<munged-cwd>/<session-id>.jsonl;
assistant entries carry an API `usage` block. We sum them tolerantly — unknown
lines and shapes are skipped, never fatal.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .model import TokenUsage


def _usage_block(entry: dict[str, Any]) -> dict[str, Any] | None:
    message = entry.get("message")
    if isinstance(message, dict) and isinstance(message.get("usage"), dict):
        return message["usage"]
    if isinstance(entry.get("usage"), dict):
        return entry["usage"]
    return None


def tally(transcript_path: Path) -> TokenUsage:
    total = TokenUsage()
    if not transcript_path.is_file():
        return total
    with transcript_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            usage = _usage_block(entry)
            if not usage:
                continue
            total.add(
                TokenUsage(
                    input_tokens=int(usage.get("input_tokens", 0) or 0),
                    output_tokens=int(usage.get("output_tokens", 0) or 0),
                    cache_read_tokens=int(usage.get("cache_read_input_tokens", 0) or 0),
                    cache_creation_tokens=int(usage.get("cache_creation_input_tokens", 0) or 0),
                )
            )
    return total
