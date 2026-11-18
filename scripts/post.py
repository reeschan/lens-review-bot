"""Post (投稿) the sorted findings to the PR.

The inline findings go out as one review whose event is COMMENT; no review is
created when there is nothing to post. The summary (まとめ) is the bot's one PR
comment whose first line is `## lens-review`: it is created when the bot has
none yet, and rewritten otherwise. A failure of any gh call raises PostError,
whose stage is "post".
"""
from __future__ import annotations

import json
import re
import subprocess
import urllib.parse
from dataclasses import dataclass
from typing import Callable, Iterable

from bundle import Bundle
from triage import Finding, TriageResult

SUMMARY_TITLE = "## lens-review"
DEFAULT_BOT_LOGIN = "github-actions[bot]"
SEVERITY_ORDER = ("critical", "major", "minor")
# Lines shown before and after the finding's line in a link to the file.
LINK_CONTEXT_LINES = 1

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# (args, stdin text or None) -> stdout. Replaceable in tests.
GhRunner = Callable[[list, "str | None"], str]


class PostError(RuntimeError):
    """Posting failed. The run fails at stage "post"."""

    stage = "post"


@dataclass
class PostOutcome:
    """What was posted: the number of inline findings, and the summary comment."""

    inline_count: int
    summary_id: int
    summary_created: bool


def run_gh(args: list, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["gh", *args],
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout


# ---------------------------------------------------------------------------
# Bodies


def marker(finding: Finding) -> str:
    return (
        f"<!-- lens-review: {finding.file}:{finding.line} {finding.heading}"
        f" #{finding.hash} [{finding.lens}] -->"
    )


def _fence(text: str) -> str:
    """A backtick fence longer than any backtick run inside `text`."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def inline_body(finding: Finding) -> str:
    """Marker -> **[severity] heading** -> body -> quote of the evidence."""
    parts = [marker(finding), f"**[{finding.severity}] {finding.heading}**"]
    return "\n\n".join(parts + _detail_parts(finding))


def _detail_parts(finding: Finding) -> list[str]:
    """Body -> rule source (for a rule violation) -> quote of the evidence."""
    parts = []
    if finding.body.strip():
        parts.append(finding.body.strip())
    if finding.rule_violation and finding.rule_source.strip():
        parts.append(f"規約: `{finding.rule_source.strip()}`")
    quote = finding.quote.strip("\n")
    fence = _fence(quote)
    parts.append(f"根拠:\n{fence}\n{quote}\n{fence}")
    return parts


def review_payload(head_sha: str, findings: list[Finding]) -> dict | None:
    """The payload of one COMMENT review, or None when there is nothing to post."""
    if not findings:
        return None
    return {
        "commit_id": head_sha,
        "event": "COMMENT",
        "body": f"lens-review: 新しい指摘 {len(findings)} 件",
        "comments": [
            {"path": f.file, "line": f.line, "side": "RIGHT", "body": inline_body(f)}
            for f in findings
        ],
    }


def file_link(repo: str, head_sha: str, path: str, line: int, context: int = LINK_CONTEXT_LINES) -> str:
    start = max(1, line - context)
    end = line + context
    quoted = urllib.parse.quote(path, safe="/")
    return f"https://github.com/{repo}/blob/{head_sha}/{quoted}#L{start}-L{end}"


def _item(finding: Finding) -> str:
    return f"- [{finding.severity}] {finding.file}:{finding.line} — {finding.heading}"


def _linked_item(finding: Finding, repo: str, head_sha: str) -> str:
    """A linked list item whose body, rule source and quote fold under <details>.

    These findings are not posted inline, so the summary is the only place that
    carries their content.
    """
    link = file_link(repo, head_sha, finding.file, finding.line)
    title = f"- [{finding.severity}] [{finding.file}:{finding.line}]({link}) — {finding.heading}"
    details = "\n\n".join(
        ["<details>\n<summary>本文と根拠</summary>", *_detail_parts(finding), "</details>"]
    )
    indented = "\n".join(f"  {line}" if line else "" for line in details.splitlines())
    return f"{title}\n\n{indented}"


def _block(title: str, lines: list[str]) -> list[str]:
    return ["", f"### {title}", *(lines or ["なし"])]


def _severity_counts(findings: Iterable[Finding]) -> str:
    counts = dict.fromkeys(SEVERITY_ORDER, 0)
    for f in findings:
        counts[f.severity] += 1
    return " / ".join(f"{name} {counts[name]}" for name in SEVERITY_ORDER)


def _trim_lines(bundle: Bundle) -> list[str]:
    lines = [f"- {trim.path}: {trim.label}" for trim in bundle.trimmed]
    if bundle.over_limit:
        lines.append("- 削っても束ねが上限に収まらなかった")
    lines.extend(f"- {path}: 読めなかった（規約・仕様）" for path in bundle.missing)
    lines.extend(f"- {path}: 設定の exclude で外した" for path in bundle.excluded)
    return lines


def summary_body(repo: str, head_sha: str, result: TriageResult, bundle: Bundle) -> str:
    """The summary comment body. The first line is `## lens-review`."""
    shown = result.inline + result.previous + result.outside_diff
    dropped = result.malformed_count + result.quote_dropped_count
    lines = [
        SUMMARY_TITLE,
        f"対象の head: {head_sha}",
        f"指摘: {_severity_counts(shown)}",
    ]
    lines += _block("今回の指摘", [_item(f) for f in result.inline])
    lines += _block("前に出した指摘", [_item(f) for f in result.previous])
    lines += _block(
        "diff の外の指摘", [_linked_item(f, repo, head_sha) for f in result.outside_diff]
    )
    lines += _block(
        "出さなかった・捨てた指摘",
        [
            f"- 下限より軽くて出さなかった指摘: {result.below_min_count} 件"
            f"（{_severity_counts(result.below_min)}）",
            f"- 捨てた指摘: {dropped} 件（形が違う {result.malformed_count} 件・"
            f"根拠の引用が束ねに無い {result.quote_dropped_count} 件）",
            f"- 同じ回で重なった指摘: {len(result.repeated)} 件",
        ],
    )
    lines += _block("削った中身", _trim_lines(bundle))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# gh calls


def _call(gh: GhRunner, args: list, input_text: str | None, what: str) -> str:
    try:
        return gh(args, input_text)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise PostError(f"{what}に失敗しました: {detail}" if detail else f"{what}に失敗しました") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise PostError(f"{what}に失敗しました: {exc}") from exc


def _json_values(text: str) -> list:
    """Every JSON value in `text` (gh --paginate prints one array per page)."""
    decoder = json.JSONDecoder()
    values: list = []
    pos = 0
    while True:
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text):
            return values
        value, pos = decoder.raw_decode(text, pos)
        values.append(value)


def list_issue_comments(gh: GhRunner, repo: str, pr_number: int) -> list:
    out = _call(
        gh,
        ["api", f"repos/{repo}/issues/{pr_number}/comments", "--paginate"],
        None,
        "PR のコメントの取得",
    )
    try:
        pages = _json_values(out)
    except ValueError as exc:
        raise PostError("PR のコメントの一覧を読めませんでした") from exc
    comments: list = []
    for page in pages:
        if not isinstance(page, list):
            raise PostError("PR のコメントの一覧の形が違います")
        comments.extend(c for c in page if isinstance(c, dict))
    return comments


def is_summary(comment: dict, bot_login: str) -> bool:
    """True when the comment is written by `bot_login` and starts with `## lens-review`."""
    user = comment.get("user") or {}
    if user.get("login") != bot_login:
        return False
    body = comment.get("body") or ""
    first = body.lstrip("﻿").splitlines()[0].strip() if body.strip() else ""
    return first == SUMMARY_TITLE


def find_summary(comments: Iterable[dict], bot_login: str) -> dict | None:
    """The first summary comment written by `bot_login`, if any."""
    for comment in comments:
        if is_summary(comment, bot_login):
            return comment
    return None


def post_review(gh: GhRunner, repo: str, pr_number: int, payload: dict) -> None:
    _call(
        gh,
        ["api", f"repos/{repo}/pulls/{pr_number}/reviews", "--method", "POST", "--input", "-"],
        json.dumps(payload, ensure_ascii=False),
        "レビューの投稿",
    )


def upsert_summary(
    gh: GhRunner, repo: str, pr_number: int, body: str, bot_login: str
) -> tuple[int, bool]:
    """Rewrite the bot's summary comment, or create it. Returns (comment id, created)."""
    existing = find_summary(list_issue_comments(gh, repo, pr_number), bot_login)
    data = json.dumps({"body": body}, ensure_ascii=False)
    if existing is not None:
        comment_id = existing.get("id")
        if isinstance(comment_id, bool) or not isinstance(comment_id, int):
            raise PostError("まとめのコメントの id を読めませんでした")
        _call(
            gh,
            ["api", f"repos/{repo}/issues/comments/{comment_id}", "--method", "PATCH", "--input", "-"],
            data,
            "まとめの書き換え",
        )
        return comment_id, False
    out = _call(
        gh,
        ["api", f"repos/{repo}/issues/{pr_number}/comments", "--method", "POST", "--input", "-"],
        data,
        "まとめの投稿",
    )
    try:
        created = json.loads(out)
        return int(created["id"]), True
    except (ValueError, TypeError, KeyError) as exc:
        raise PostError("投稿したまとめの id を読めませんでした") from exc


# ---------------------------------------------------------------------------
# All posting


def post(
    repo: str,
    pr_number: int,
    head_sha: str,
    result: TriageResult,
    bundle: Bundle,
    *,
    gh: GhRunner = run_gh,
    bot_login: str = DEFAULT_BOT_LOGIN,
) -> PostOutcome:
    """Post the inline findings as one COMMENT review, then create or rewrite the summary.

    `head_sha` is the full head SHA the review was made against. When the review
    fails, the summary is not touched. Raises PostError on any failure.
    """
    if not _REPO_RE.match(repo or ""):
        raise PostError(f"リポジトリの名前が不正です: {repo!r}")
    if not _SHA_RE.match(head_sha or ""):
        raise PostError(f"head の SHA が 40 桁ではありません: {head_sha!r}")
    payload = review_payload(head_sha, result.inline)
    if payload is not None:
        post_review(gh, repo, pr_number, payload)
    body = summary_body(repo, head_sha, result, bundle)
    summary_id, created = upsert_summary(gh, repo, pr_number, body, bot_login)
    return PostOutcome(
        inline_count=len(result.inline), summary_id=summary_id, summary_created=created
    )
