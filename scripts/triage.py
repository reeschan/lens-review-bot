"""Sort (仕分け) the findings returned by the model.

The stages run in this order:

1. shape: a finding with a missing or wrongly typed item, or a severity other
   than critical/major/minor, is dropped on its own; the others stay.
2. quote: the quote, with whitespace squeezed, must appear in the bundle's lines
   of the same file (`Bundle.file_lines`). Otherwise the finding is dropped and
   counted.
3. diff range: a finding whose `line` is not in the new side of the diff goes to
   "outside the diff" (listed in the summary) instead of being posted inline.
4. lower bound: a finding lighter than `min_severity` is not shown; only counted.
   When `post_rule_violations` is true, a `rule_violation` finding is shown
   regardless of the lower bound.
5. same finding: a finding that matches a marker of an existing inline finding
   is not posted and goes to "posted before". A marker with a hash matches on
   file, heading and hash; an old marker without a hash matches on file and
   heading. The line number in a marker is never compared.

Stages 3 to 5 decide the category of a finding that passed stages 1 and 2.
Each finding ends in exactly one category; when several apply, the earlier one
in this list wins: below the lower bound, posted before, repeated in this run,
outside the diff, inline.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Iterable

from bundle import HUNK_RE, MARKER_RE, parse_diff

SEVERITY_RANK = {"critical": 3, "major": 2, "minor": 1}

# Item name -> required type. `line` is checked separately (int, not bool, >= 1).
_STRING_ITEMS = ("file", "severity", "lens", "heading", "body", "quote", "rule_source")
_NON_EMPTY_ITEMS = ("file", "heading", "quote")

HASH_LENGTH = 8


def squeeze_whitespace(text: str) -> str:
    """Collapse every run of whitespace (including newlines) to one space and trim."""
    return " ".join(text.split())


# A copied line may keep the bundle's line-number prefix ("12: ") or a diff "+"/"-".
_QUOTE_PREFIX_RE = re.compile(r"^\s*(?:\d+:\s?)?[+-]?")


def normalize_quote(quote: str) -> str:
    """Drop the line-number prefix and a diff +/- from each line, then squeeze whitespace."""
    return squeeze_whitespace("\n".join(_QUOTE_PREFIX_RE.sub("", line) for line in quote.splitlines()))


def quote_hash(quote: str) -> str:
    """First 8 hex digits of SHA-256 of the normalized quote."""
    digest = hashlib.sha256(normalize_quote(quote).encode("utf-8")).hexdigest()
    return digest[:HASH_LENGTH]


@dataclass
class Finding:
    """One finding that passed the shape check."""

    file: str
    line: int
    severity: str
    lens: str
    heading: str
    body: str
    quote: str
    rule_violation: bool
    rule_source: str

    @property
    def hash(self) -> str:
        return quote_hash(self.quote)

    @property
    def heading_key(self) -> str:
        return squeeze_whitespace(self.heading)


@dataclass
class TriageResult:
    """The findings sorted into categories, and the counts of dropped ones."""

    inline: list[Finding] = field(default_factory=list)
    outside_diff: list[Finding] = field(default_factory=list)
    previous: list[Finding] = field(default_factory=list)
    below_min: list[Finding] = field(default_factory=list)
    repeated: list[Finding] = field(default_factory=list)
    malformed_count: int = 0
    quote_dropped_count: int = 0

    @property
    def below_min_count(self) -> int:
        return len(self.below_min)


# ---------------------------------------------------------------------------
# Stage 1: shape


def parse_finding(raw: object) -> Finding | None:
    """A Finding when `raw` has the right shape, else None. Extra keys are ignored."""
    if not isinstance(raw, dict):
        return None
    for key in _STRING_ITEMS:
        if not isinstance(raw.get(key), str):
            return None
    for key in _NON_EMPTY_ITEMS:
        if not raw[key].strip():
            return None
    line = raw.get("line")
    if isinstance(line, bool) or not isinstance(line, int) or line < 1:
        return None
    if not isinstance(raw.get("rule_violation"), bool):
        return None
    if raw["severity"] not in SEVERITY_RANK:
        return None
    heading = raw["heading"]
    # The heading goes into a one-line HTML comment marker.
    if "\n" in heading or "\r" in heading or "-->" in heading:
        return None
    return Finding(
        file=raw["file"],
        line=line,
        severity=raw["severity"],
        lens=raw["lens"],
        heading=heading,
        body=raw["body"],
        quote=raw["quote"],
        rule_violation=raw["rule_violation"],
        rule_source=raw["rule_source"],
    )


# ---------------------------------------------------------------------------
# Stage 2: quote


def quote_in_bundle(finding: Finding, file_lines: dict) -> bool:
    """True when the normalized quote is contained in the bundle's lines of the file."""
    lines = file_lines.get(finding.file)
    if not lines:
        return False
    quote = normalize_quote(finding.quote)
    if not quote:
        return False
    return quote in squeeze_whitespace("\n".join(lines))


# ---------------------------------------------------------------------------
# Stage 3: diff range


def new_side_lines(diff_text: str) -> dict:
    """Per file, the set of new-side line numbers covered by the diff's hunks.

    Added and context lines count; removed lines do not. Deleted files have none.
    """
    ranges: dict = {}
    for file_diff in parse_diff(diff_text):
        lines: set = set()
        if not file_diff.deleted:
            for hunk in file_diff.hunks:
                lines.update(_hunk_new_lines(hunk))
        ranges.setdefault(file_diff.path, set()).update(lines)
    return ranges


def _hunk_new_lines(hunk: list) -> set:
    match = HUNK_RE.match(hunk[0])
    if match is None:
        return set()
    old_left = int(match.group(2) or "1")
    new_left = int(match.group(4) or "1")
    pos = int(match.group(3))
    found: set = set()
    for line in hunk[1:]:
        if old_left <= 0 and new_left <= 0:
            break
        if line.startswith("\\"):
            continue
        kind = line[:1]
        if kind == "-":
            old_left -= 1
        elif kind == "+":
            found.add(pos)
            pos += 1
            new_left -= 1
        elif kind in (" ", ""):
            # An empty line is a context line whose trailing space was stripped.
            found.add(pos)
            pos += 1
            old_left -= 1
            new_left -= 1
    return found


# ---------------------------------------------------------------------------
# Stage 4: lower bound


def is_shown(finding: Finding, min_severity: str, post_rule_violations: bool) -> bool:
    if post_rule_violations and finding.rule_violation:
        return True
    return SEVERITY_RANK[finding.severity] >= SEVERITY_RANK[min_severity]


# ---------------------------------------------------------------------------
# Stage 5: same finding


@dataclass(frozen=True)
class Marker:
    """A marker read from an existing inline finding. `hash` is None for the old form."""

    file: str
    heading: str
    hash: str | None


def read_markers(bodies: Iterable[str]) -> list[Marker]:
    """Markers in the bodies of the existing inline findings (the caller passes the bot's own)."""
    markers: list[Marker] = []
    for body in bodies:
        for match in MARKER_RE.finditer(body or ""):
            raw_hash = match.group("hash")
            marker = Marker(
                file=match.group("file"),
                heading=squeeze_whitespace(match.group("heading")),
                hash=raw_hash.lower() if raw_hash else None,
            )
            if marker not in markers:
                markers.append(marker)
    return markers


def matches_marker(finding: Finding, markers: Iterable[Marker]) -> bool:
    for marker in markers:
        if marker.file != finding.file or marker.heading != finding.heading_key:
            continue
        if marker.hash is None or marker.hash == finding.hash:
            return True
    return False


# ---------------------------------------------------------------------------
# All stages


def triage(
    findings: Iterable,
    file_lines: dict,
    diff_text: str,
    config: dict,
    inline_comment_bodies: Iterable[str] = (),
) -> TriageResult:
    """Sort the model's findings.

    `findings` is the unvalidated list from the model. `file_lines` is
    `Bundle.file_lines`. `diff_text` is the PR's unified diff. `config` supplies
    `min_severity` (default "minor") and `post_rule_violations` (default False).
    `inline_comment_bodies` are the bodies of the bot's existing inline comments.
    """
    min_severity = config.get("min_severity") or "minor"
    if min_severity not in SEVERITY_RANK:
        raise ValueError(f"min_severity が不正です: {min_severity!r}")
    post_rule_violations = bool(config.get("post_rule_violations"))
    ranges = new_side_lines(diff_text)
    markers = read_markers(inline_comment_bodies)

    result = TriageResult()
    seen: set = set()
    for raw in findings:
        finding = parse_finding(raw)
        if finding is None:
            result.malformed_count += 1
            continue
        if not quote_in_bundle(finding, file_lines):
            result.quote_dropped_count += 1
            continue
        in_diff = finding.line in ranges.get(finding.file, ())
        if not is_shown(finding, min_severity, post_rule_violations):
            result.below_min.append(finding)
            continue
        if matches_marker(finding, markers):
            result.previous.append(finding)
            continue
        key = (finding.file, finding.heading_key, finding.hash)
        if key in seen:
            result.repeated.append(finding)
            continue
        seen.add(key)
        (result.inline if in_diff else result.outside_diff).append(finding)
    return result
