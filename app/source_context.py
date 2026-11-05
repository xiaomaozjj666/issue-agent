"""Bounded source excerpts that preserve original line numbers.

Large files are represented by head, tail, and focused line ranges.  Omission
markers carry the original line span, so report generation, evidence validation,
and grep results never renumber tail evidence as if it followed the head.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

LineRange = tuple[int, int]

_OMISSION_RE = re.compile(r"^\[\.\.\. omitted L(\d+)-L(\d+) \.\.\.\]$")
_MIN_STRUCTURED_SAMPLE_CHARS = 256
_MAX_FOCUS_RANGES = 8


@dataclass(frozen=True)
class SourceSample:
    """A bounded excerpt and its deterministic coverage metadata."""

    text: str
    original_lines: int
    original_chars: int
    included_ranges: list[LineRange]
    omitted_ranges: list[LineRange]

    def coverage(self, path: str) -> dict:
        return {
            "path": path,
            "truncated": len(self.text) < self.original_chars,
            "original_lines": self.original_lines,
            "original_chars": self.original_chars,
            "captured_chars": len(self.text),
            "included_ranges": [{"start": start, "end": end} for start, end in self.included_ranges],
            "omitted_ranges": [{"start": start, "end": end} for start, end in self.omitted_ranges],
        }


def merge_line_ranges(ranges: Iterable[LineRange], total_lines: int) -> list[LineRange]:
    """Clamp, merge, and sort 1-based inclusive ranges."""
    normalized: list[LineRange] = []
    for start, end in ranges:
        start = max(1, min(start, total_lines))
        end = max(1, min(end, total_lines))
        if end < start:
            start, end = end, start
        normalized.append((start, end))
    merged: list[LineRange] = []
    for start, end in sorted(normalized):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _complement(ranges: list[LineRange], total_lines: int) -> list[LineRange]:
    omitted: list[LineRange] = []
    cursor = 1
    for start, end in ranges:
        if cursor < start:
            omitted.append((cursor, start - 1))
        cursor = end + 1
    if cursor <= total_lines:
        omitted.append((cursor, total_lines))
    return omitted


def _ranges_from_indices(indices: Iterable[int]) -> list[LineRange]:
    ranges: list[LineRange] = []
    for index in sorted(set(indices)):
        line_number = index + 1
        if ranges and line_number == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], line_number)
        else:
            ranges.append((line_number, line_number))
    return ranges


def _add_lines(
    lines: list[str],
    selected: dict[int, str],
    start: int,
    end: int,
    budget: int,
    *,
    reverse: bool = False,
) -> None:
    """Add as much of a range as fits, cropping one boundary line if needed."""
    indices = range(end - 1, start - 2, -1) if reverse else range(start - 1, end)
    used = 0
    for index in indices:
        if index in selected:
            continue
        line = lines[index]
        cost = len(line) + 1
        if used + cost <= budget:
            selected[index] = line
            used += cost
            continue
        # A single very long source line must not defeat head/tail coverage.
        # Keep a bounded fragment; coverage metadata still records the character gap.
        remaining = budget - used - 1
        if remaining >= 16:
            selected[index] = line[-remaining:] if reverse else line[:remaining]
        break


def sample_source(content: str, max_chars: int, focus_ranges: Iterable[LineRange] = ()) -> SourceSample:
    """Sample head, tail, and focused ranges within an exact character bound.

    Very small budgets retain the legacy prefix behaviour; normal application
    budgets (>=1000) always use structured head/tail sampling for multiline files.
    """
    total_lines = content.count("\n") + 1
    if len(content) <= max_chars:
        included = [(1, total_lines)]
        return SourceSample(content, total_lines, len(content), included, [])

    if max_chars < _MIN_STRUCTURED_SAMPLE_CHARS or total_lines <= 2:
        text = content[:max_chars]
        captured_lines = min(text.count("\n") + 1, total_lines)
        included = [(1, captured_lines)]
        omitted = [(captured_lines + 1, total_lines)] if captured_lines < total_lines else []
        return SourceSample(text, total_lines, len(content), included, omitted)

    lines = content.splitlines()
    focus = merge_line_ranges(focus_ranges, total_lines)[:_MAX_FOCUS_RANGES]
    # Reserve ample room for omission markers. Raw line selection additionally
    # counts one newline per line, making the final bound deterministic.
    marker_reserve = min(max_chars // 3, 80 * (len(focus) + 2))
    text_budget = max_chars - marker_reserve
    head_budget = text_budget * (3 if focus else 1) // (10 if focus else 2)
    tail_budget = head_budget
    focus_budget = text_budget - head_budget - tail_budget

    selected: dict[int, str] = {}
    _add_lines(lines, selected, 1, total_lines, head_budget)
    _add_lines(lines, selected, 1, total_lines, tail_budget, reverse=True)
    per_focus_budget = focus_budget // len(focus) if focus else 0
    for start, end in focus:
        _add_lines(lines, selected, start, end, per_focus_budget)

    included = _ranges_from_indices(selected)
    omitted = _complement(included, total_lines)
    parts: list[str] = []

    cursor = 1
    for start, end in included:
        if cursor < start:
            parts.append(f"[... omitted L{cursor}-L{start - 1} ...]")
        for line_number in range(start, end + 1):
            parts.append(selected[line_number - 1])
        cursor = end + 1
    if cursor <= total_lines:
        parts.append(f"[... omitted L{cursor}-L{total_lines} ...]")
    text = "\n".join(parts)

    # The marker reserve above is deliberately conservative. Keep a defensive
    # fallback for unusual cropped-line/newline combinations without dropping tail.
    if len(text) > max_chars:  # pragma: no cover - guarded by the accounting above
        return sample_source(content, max_chars - 1, focus)

    return SourceSample(text, total_lines, len(content), included, omitted)


def iter_source_lines(content: str) -> Iterator[tuple[int | None, str]]:
    """Yield (original line number, text); omission markers have no line number."""
    next_line = 1
    for line in content.splitlines():
        marker = _OMISSION_RE.fullmatch(line)
        if marker:
            yield None, line
            next_line = int(marker.group(2)) + 1
            continue
        yield next_line, line
        next_line += 1


def render_numbered_source(content: str) -> str:
    """Render an excerpt with original L-numbers and visible omission markers."""
    rendered: list[str] = []
    for line_number, line in iter_source_lines(content):
        rendered.append(line if line_number is None else f"L{line_number}: {line}")
    return "\n".join(rendered)


def extract_line_text(content: str, start: int, end: int) -> str:
    """Extract an original line range from either full content or a sampled excerpt."""
    return "\n".join(
        line
        for line_number, line in iter_source_lines(content)
        if line_number is not None and start <= line_number <= end
    )


def bound_text_head_tail(text: str, max_chars: int) -> str:
    """Bound an already-rendered context block without discarding its tail."""
    if len(text) <= max_chars:
        return text
    if max_chars < 96:
        return text[:max_chars]
    marker = "\n[... context truncated; beginning and end retained ...]\n"
    remaining = max_chars - len(marker)
    head = remaining // 2
    tail = remaining - head
    return text[:head] + marker + text[-tail:]
