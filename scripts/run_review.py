"""The entry point of one review run: bundle -> model -> triage -> post.

Exit codes: 0 when the review was posted or the run ended without posting
because the head moved on (cancelled); 1 when the run failed. A failed run
writes `失敗した段: <stage>` into the summary comment, except a cancelled run,
which writes nothing. Token counts go to stdout and $GITHUB_STEP_SUMMARY, never
to the PR.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Callable

import bundle as bundle_mod
import model_call
import post as post_mod
from bundle import LensFetchError
from lens_review_config import failure_body
from model_call import ModelError
from post import PostError
from triage import triage

STAGE_BUNDLE = "bundle"
STAGE_MODEL = "model"
STAGE_POST = "post"

DIFF_ACCEPT = "Accept: application/vnd.github.diff"

OUTCOME_POSTED = "posted"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_FAILED = "failed"


@dataclass
class RunOutcome:
    """How the run ended: posted, cancelled (head moved), or failed at `stage`."""

    kind: str
    stage: str | None = None
    reason: str | None = None

    @property
    def exit_code(self) -> int:
        return 1 if self.kind == OUTCOME_FAILED else 0


class StageError(RuntimeError):
    """A failure of the bundling inputs (diff, existing comments)."""

    stage = STAGE_BUNDLE


def _gh_call(gh: post_mod.GhRunner, args: list, what: str) -> str:
    return post_mod._call(gh, args, None, what)


def fetch_diff(gh: post_mod.GhRunner, repo: str, pr_number: int) -> str:
    try:
        return _gh_call(
            gh, ["api", f"repos/{repo}/pulls/{pr_number}", "-H", DIFF_ACCEPT], "PR の diff の取得"
        )
    except PostError as exc:
        raise StageError(str(exc)) from exc


def fetch_bot_inline_bodies(
    gh: post_mod.GhRunner, repo: str, pr_number: int, bot_login: str
) -> list[str]:
    """Bodies of the inline review comments written by `bot_login` only."""
    try:
        out = _gh_call(
            gh, ["api", f"repos/{repo}/pulls/{pr_number}/comments", "--paginate"],
            "PR のインラインコメントの取得",
        )
        pages = post_mod._json_values(out)
    except PostError as exc:
        raise StageError(str(exc)) from exc
    except ValueError as exc:
        raise StageError("PR のインラインコメントの一覧を読めませんでした") from exc
    bodies: list[str] = []
    for page in pages:
        if not isinstance(page, list):
            raise StageError("PR のインラインコメントの一覧の形が違います")
        for comment in page:
            if not isinstance(comment, dict):
                continue
            if (comment.get("user") or {}).get("login") != bot_login:
                continue
            body = comment.get("body")
            if isinstance(body, str):
                bodies.append(body)
    return bodies


def current_head_sha(gh: post_mod.GhRunner, repo: str, pr_number: int) -> str:
    out = _gh_call(
        gh, ["api", f"repos/{repo}/pulls/{pr_number}", "--jq", ".head.sha"], "head SHA の取得"
    )
    return out.strip()


def head_moved(gh: post_mod.GhRunner, repo: str, pr_number: int, head_sha: str) -> bool:
    """True only when the PR's head is known to differ from `head_sha`.

    When the head cannot be read, the failure is still reported (False).
    """
    try:
        return current_head_sha(gh, repo, pr_number) != head_sha
    except PostError:
        return False


def report_tokens(input_tokens: int, output_tokens: int, summary_path: str | None) -> None:
    """Print the token counts and append them to $GITHUB_STEP_SUMMARY when set."""
    line = f"使ったトークン: 入力 {input_tokens} / 出力 {output_tokens}"
    print(line)
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fp:
            fp.write(f"### lens-review\n{line}\n")


def write_failure(
    gh: post_mod.GhRunner, repo: str, pr_number: int, head_sha: str, run_url: str,
    bot_login: str, stage: str, reason: str | None,
) -> None:
    """Write `失敗した段: <stage>` into the summary comment. Never raises."""
    body = failure_body(stage, run_url, head_sha, reason)
    try:
        post_mod.upsert_summary(gh, repo, pr_number, body, bot_login)
    except PostError as exc:
        print(f"失敗のまとめを書けませんでした: {exc}", file=sys.stderr)


def run(
    *,
    repo: str,
    pr_number: int,
    head_sha: str,
    config: dict,
    root: str,
    run_url: str,
    api_key: str | None,
    gh: post_mod.GhRunner = post_mod.run_gh,
    fetch_lens: Callable[[], list] | None = None,
    model: Callable = model_call.call_model,
    bot_login: str = post_mod.DEFAULT_BOT_LOGIN,
    summary_path: str | None = None,
) -> RunOutcome:
    """Run one review. `gh` and `model` are replaceable in tests."""

    def fail(stage: str, reason: str) -> RunOutcome:
        print(f"失敗した段: {stage}: {reason}", file=sys.stderr)
        if head_moved(gh, repo, pr_number, head_sha):
            print("head が変わったため失敗のまとめを書かずに終わります")
            return RunOutcome(OUTCOME_CANCELLED)
        write_failure(gh, repo, pr_number, head_sha, run_url, bot_login, stage, reason)
        return RunOutcome(OUTCOME_FAILED, stage, reason)

    try:
        diff_text = fetch_diff(gh, repo, pr_number)
        bodies = fetch_bot_inline_bodies(gh, repo, pr_number, bot_login)
        review_bundle = bundle_mod.build_bundle(
            root, diff_text, config, bodies, fetch_lens=fetch_lens
        )
    except StageError as exc:
        return fail(exc.stage, str(exc))
    except LensFetchError as exc:
        return fail(exc.stage, str(exc))
    if review_bundle.over_limit:
        return fail(STAGE_MODEL, "削っても束ねが上限に収まらなかった")

    if not api_key:
        return fail(STAGE_MODEL, "ANTHROPIC_API_KEY がありません")
    try:
        answer = model(review_bundle, config, api_key)
    except ModelError as exc:
        return fail(exc.stage, str(exc))
    report_tokens(answer.input_tokens, answer.output_tokens, summary_path)

    sorted_findings = triage(answer.findings, review_bundle.file_lines, diff_text, config, bodies)

    try:
        if current_head_sha(gh, repo, pr_number) != head_sha:
            print("head が変わったため投稿せずに終わります")
            return RunOutcome(OUTCOME_CANCELLED)
        outcome = post_mod.post(
            repo, pr_number, head_sha, sorted_findings, review_bundle, gh=gh, bot_login=bot_login
        )
    except PostError as exc:
        return fail(exc.stage, str(exc))
    print(
        f"投稿しました: インラインの指摘 {outcome.inline_count} 件、"
        f"まとめ {'作成' if outcome.summary_created else '書き換え'}"
    )
    return RunOutcome(OUTCOME_POSTED)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="lens-review: one review run")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--pr-number", type=int, required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--config", required=True, help="the merged config as JSON")
    parser.add_argument("--root", default=".")
    parser.add_argument("--run-url", default=None)
    parser.add_argument("--bot-login", default=post_mod.DEFAULT_BOT_LOGIN)
    return parser


def _default_run_url() -> str:
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    return f"{server}/{repo}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not args.repo:
        print("--repo か GITHUB_REPOSITORY が必要です", file=sys.stderr)
        return 2
    try:
        config = json.loads(args.config)
    except json.JSONDecodeError:
        print("--config が JSON ではありません", file=sys.stderr)
        return 2
    if not isinstance(config, dict):
        print("--config が JSON の辞書ではありません", file=sys.stderr)
        return 2
    outcome = run(
        repo=args.repo,
        pr_number=args.pr_number,
        head_sha=args.head_sha,
        config=config,
        root=args.root,
        run_url=args.run_url or _default_run_url(),
        api_key=os.environ.get("ANTHROPIC_API_KEY"),
        bot_login=args.bot_login,
        summary_path=os.environ.get("GITHUB_STEP_SUMMARY"),
    )
    return outcome.exit_code


if __name__ == "__main__":
    sys.exit(main())
