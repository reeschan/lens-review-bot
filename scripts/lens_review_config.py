from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
from pathlib import Path

ALLOWED_KEYS = frozenset({
    "model",
    "max_turns",
    "min_severity",
    "post_rule_violations",
    "rules",
    "spec",
    "exclude",
    "allowed_tools",
})

ALLOWED_PR_ACTIONS = frozenset({"opened", "reopened", "ready_for_review", "synchronize"})
ALLOWED_PERMISSIONS = frozenset({"admin", "maintain", "write"})
ALLOWED_SEVERITIES = frozenset({"critical", "major", "minor"})
REVIEW_COMMAND = "/lens-review"
BOT_USER_TYPE = "Bot"

MODEL_RE = re.compile(r"^[A-Za-z0-9._:@/\[\]-]+$")
INT_RE = re.compile(r"^[+-]?\d+$")

FORBIDDEN_PATH_CHARS = frozenset({"'", '"', "`", "$", "\\"})
FORBIDDEN_EXCLUDE_CHARS = frozenset({"'", '"', "`", "$", "\\", "\n", "\r"})
FORBIDDEN_TOOL_CHARS = frozenset({",", "'", '"', "`", "$", "\\", "\n", "\r"})

DEFAULT_TOOLS = (
    "Read",
    "Grep",
    "Glob",
    "Agent",
    "Skill",
    "Bash(git status:*)",
    "Bash(git rev-parse:*)",
    "Bash(git merge-base:*)",
    "Bash(git blame:*)",
    "Bash(gh pr view:*)",
    "Bash(gh pr diff:*)",
    "Bash(gh pr comment:*)",
    "Bash(gh api:*)",
    "Write(//tmp/**)",
    "Write(//home/runner/work/_temp/lens-review-claude/**)",
)

OUTPUT_KEYS = (
    "decision",
    "reason",
    "pr_number",
    "head_sha",
    "base_sha",
    "claude_args",
    "prompt",
    "summary_body",
)

_DOUBLE_ESCAPES = {
    "\\": "\\",
    '"': '"',
    "/": "/",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "b": "\b",
    "f": "\f",
}


class ConfigError(ValueError):
    """Configuration parse or validation error."""


def _strip_inline_comment(line: str, lineno: int) -> str:
    quote: str | None = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote is None:
            if ch in ("'", '"'):
                quote = ch
            elif ch == "#" and i > 0 and line[i - 1] in (" ", "\t"):
                return line[:i].rstrip(" \t")
        elif quote == "'":
            if ch == "'":
                if i + 1 < len(line) and line[i + 1] == "'":
                    i += 2
                    continue
                quote = None
        elif ch == "\\":
            if i + 1 >= len(line):
                raise ConfigError(f"{lineno}行目: 引用符が閉じていません")
            i += 2
            continue
        elif ch == '"':
            quote = None
        i += 1
    if quote is not None:
        raise ConfigError(f"{lineno}行目: 引用符が閉じていません")
    return line.rstrip(" \t")


def _parse_single_quoted(s: str, lineno: int) -> str:
    chars: list[str] = []
    i = 1
    while i < len(s):
        ch = s[i]
        if ch == "'":
            if i + 1 < len(s) and s[i + 1] == "'":
                chars.append("'")
                i += 2
                continue
            if i != len(s) - 1:
                raise ConfigError(f"{lineno}行目: 引用符の後に余分な文字があります")
            return "".join(chars)
        chars.append(ch)
        i += 1
    raise ConfigError(f"{lineno}行目: 引用符が閉じていません")


def _parse_double_quoted(s: str, lineno: int) -> str:
    chars: list[str] = []
    i = 1
    while i < len(s):
        ch = s[i]
        if ch == "\\":
            if i + 1 >= len(s):
                raise ConfigError(f"{lineno}行目: 引用符が閉じていません")
            esc = s[i + 1]
            if esc not in _DOUBLE_ESCAPES:
                raise ConfigError(f"{lineno}行目: 不正なエスケープ '\\{esc}' です")
            chars.append(_DOUBLE_ESCAPES[esc])
            i += 2
            continue
        if ch == '"':
            if i != len(s) - 1:
                raise ConfigError(f"{lineno}行目: 引用符の後に余分な文字があります")
            return "".join(chars)
        chars.append(ch)
        i += 1
    raise ConfigError(f"{lineno}行目: 引用符が閉じていません")


def _parse_scalar(s: str, lineno: int) -> str | int | bool:
    if s.startswith("'"):
        return _parse_single_quoted(s, lineno)
    if s.startswith('"'):
        return _parse_double_quoted(s, lineno)
    if s in ("null", "Null", "NULL", "~"):
        raise ConfigError(f"{lineno}行目: null は使えません")
    if s[0] in ("[", "]", "{", "}"):
        raise ConfigError(f"{lineno}行目: 値として解釈できません: {s}")
    if s == "true":
        return True
    if s == "false":
        return False
    if INT_RE.fullmatch(s):
        return int(s)
    return s


def _split_flow_items(inner: str, lineno: int) -> list[str]:
    parts: list[str] = []
    quote: str | None = None
    start = 0
    i = 0
    while i < len(inner):
        ch = inner[i]
        if quote is None:
            if ch in ("'", '"'):
                quote = ch
            elif ch == ",":
                parts.append(inner[start:i])
                start = i + 1
            elif ch in ("[", "]", "{", "}"):
                raise ConfigError(f"{lineno}行目: 入れ子の構造は使えません")
        elif quote == "'":
            if ch == "'":
                if i + 1 < len(inner) and inner[i + 1] == "'":
                    i += 2
                    continue
                quote = None
        elif ch == "\\":
            i += 2
            continue
        elif ch == '"':
            quote = None
        i += 1
    if quote is not None:
        raise ConfigError(f"{lineno}行目: 引用符が閉じていません")
    parts.append(inner[start:])
    return parts


def _parse_flow_list(s: str, lineno: int) -> list[str | int | bool]:
    if not s.endswith("]"):
        raise ConfigError(f"{lineno}行目: リストの ']' が閉じていません")
    inner = s[1:-1].strip(" ")
    if not inner:
        return []
    items: list[str | int | bool] = []
    for raw_item in _split_flow_items(inner, lineno):
        item = raw_item.strip(" ")
        if not item:
            raise ConfigError(f"{lineno}行目: リストに空の要素があります")
        items.append(_parse_scalar(item, lineno))
    return items


def _parse_top_entry(stripped: str, lineno: int, result: dict) -> tuple[str, bool]:
    if ":" not in stripped:
        raise ConfigError(f"{lineno}行目: 'key: value' の形式ではありません")
    key, rest = stripped.split(":", 1)
    if not key or any(c.isspace() for c in key):
        raise ConfigError(f"{lineno}行目: キーの形式が不正です")
    if rest and not rest.startswith(" "):
        raise ConfigError(f"{lineno}行目: ':' の後に空白が必要です")
    if key in result:
        raise ConfigError(f"{lineno}行目: キー '{key}' が重複しています")
    val_str = rest.lstrip(" ")
    if not val_str:
        result[key] = []
        return key, True
    if val_str.startswith("["):
        result[key] = _parse_flow_list(val_str, lineno)
    else:
        result[key] = _parse_scalar(val_str, lineno)
    return key, False


def _advance_block_list(
    stripped: str, indent: int, lineno: int,
    pending_key: str, pending_indent: int | None, result: dict,
) -> tuple[bool, int | None]:
    if stripped == "-" or stripped.startswith("- "):
        if pending_indent is not None and indent != pending_indent:
            raise ConfigError(f"{lineno}行目: 一覧の字下げが揃っていません")
        item_str = stripped[1:].lstrip(" ")
        if not item_str:
            raise ConfigError(f"{lineno}行目: 一覧の項目が空です")
        result[pending_key].append(_parse_scalar(item_str, lineno))
        return True, indent if pending_indent is None else pending_indent
    if indent > 0:
        raise ConfigError(f"{lineno}行目: 不正な字下げまたは入れ子構造です")
    return False, pending_indent


def parse_config(text: str) -> dict:
    """Parse the supported YAML subset for lens-review config."""
    if text.startswith("\ufeff"):
        text = text[1:]
    result: dict = {}
    pending_key: str | None = None
    pending_lineno = 0
    pending_indent: int | None = None

    for lineno, raw_line in enumerate(text.split("\n"), start=1):
        line = raw_line[:-1] if raw_line.endswith("\r") else raw_line
        if not line.strip():
            continue
        leading = len(line) - len(line.lstrip(" \t"))
        if "\t" in line[:leading]:
            raise ConfigError(f"{lineno}行目: タブによる字下げは使えません")
        if not line.strip() or line.lstrip(" ").startswith("#"):
            continue
        clean = _strip_inline_comment(line, lineno)
        indent = len(clean) - len(clean.lstrip(" "))
        stripped = clean.lstrip(" ")

        if pending_key is not None:
            consumed, pending_indent = _advance_block_list(
                stripped, indent, lineno, pending_key, pending_indent, result
            )
            if consumed:
                continue
            if not result[pending_key]:
                raise ConfigError(f"{pending_lineno}行目: '{pending_key}' の値が空で一覧が続いていません")
            pending_key = None
            pending_indent = None

        if indent > 0:
            raise ConfigError(f"{lineno}行目: 最上位以外に字下げはできません")
        key, is_pending = _parse_top_entry(stripped, lineno, result)
        if is_pending:
            pending_key = key
            pending_lineno = lineno

    if pending_key is not None and not result[pending_key]:
        raise ConfigError(f"{pending_lineno}行目: '{pending_key}' の値が空で一覧が続いていません")
    return result


def _is_valid_path(path: str) -> bool:
    if not path or path.startswith("/"):
        return False
    if any(c.isspace() or c in FORBIDDEN_PATH_CHARS for c in path):
        return False
    return ".." not in path.split("/")


def _validate_str_list(key: str, val: object, check_fn, detail: str) -> None:
    if not isinstance(val, list):
        raise ConfigError(f"'{key}' は文字列の一覧でなければなりません")
    for item in val:
        if not isinstance(item, str) or not check_fn(item):
            raise ConfigError(f"'{key}' の要素が不正です: {detail}")


def _validate_config(cfg: dict) -> None:
    for key, val in cfg.items():
        if key not in ALLOWED_KEYS:
            raise ConfigError(f"不明な設定項目です: {key}")
        if key == "model":
            if not isinstance(val, str) or not MODEL_RE.fullmatch(val):
                raise ConfigError("'model' の形式が不正です")
        elif key == "max_turns":
            if not isinstance(val, int) or isinstance(val, bool) or not (1 <= val <= 500):
                raise ConfigError("'max_turns' は 1 以上 500 以下の整数でなければなりません")
        elif key == "min_severity":
            if not isinstance(val, str) or val not in ALLOWED_SEVERITIES:
                raise ConfigError("'min_severity' は critical・major・minor のいずれかでなければなりません")
        elif key == "post_rule_violations":
            if not isinstance(val, bool):
                raise ConfigError("'post_rule_violations' は真偽値でなければなりません")
        elif key == "rules":
            _validate_str_list(key, val, _is_valid_path, "相対パスの決まりに違反しています")
        elif key == "spec":
            if not isinstance(val, str) or not _is_valid_path(val):
                raise ConfigError("'spec' が相対パスの決まりに違反しています")
        elif key == "exclude":
            _validate_str_list(
                key, val,
                lambda s: bool(s) and not any(c in FORBIDDEN_EXCLUDE_CHARS for c in s),
                "空でない文字列で禁止文字を含まない必要があります",
            )
        elif key == "allowed_tools":
            _validate_str_list(
                key, val,
                lambda s: bool(s) and not any(c in FORBIDDEN_TOOL_CHARS for c in s),
                "空でない文字列で禁止文字を含まない必要があります",
            )


def _extract_pr_meta(pr_obj: dict | None, fallback_number: int | None = None) -> tuple[int | None, str | None, str | None]:
    if not pr_obj:
        return fallback_number, None, None
    number = pr_obj.get("number")
    if number is None:
        number = fallback_number
    head_sha = (pr_obj.get("head") or {}).get("sha")
    base_sha = (pr_obj.get("base") or {}).get("sha")
    return number, head_sha, base_sha


def _is_fork(pr_obj: dict) -> bool:
    head_repo = (pr_obj.get("head") or {}).get("repo")
    base_repo = (pr_obj.get("base") or {}).get("repo")
    if head_repo is None or base_repo is None:
        return True
    return head_repo.get("full_name") != base_repo.get("full_name")


def _check_pr_common(pr_obj: dict, bot_user_type: str) -> str | None:
    if (pr_obj.get("user") or {}).get("type") == bot_user_type:
        return "bot による PR のためスキップします"
    if _is_fork(pr_obj):
        return "fork からの PR のためスキップします"
    return None


def _check_pull_request(event: dict) -> tuple[str | None, tuple[int | None, str | None, str | None]]:
    pr_obj = event.get("pull_request") or {}
    meta = _extract_pr_meta(pr_obj, event.get("number"))
    action = event.get("action")
    if action not in ALLOWED_PR_ACTIONS:
        return f"対象のイベント ({action}) ではありません", meta
    if pr_obj.get("draft"):
        return "draft の PR のためスキップします", meta
    common_skip = _check_pr_common(pr_obj, BOT_USER_TYPE)
    if common_skip is not None:
        return common_skip, meta
    return None, meta


def _is_review_command(body: str, prefix: str) -> bool:
    stripped = body.lstrip()
    if not stripped.startswith(prefix):
        return False
    return len(stripped) == len(prefix) or stripped[len(prefix)].isspace()


def _check_issue_comment(
    event: dict, pr: dict | None, permission: str | None
) -> tuple[str | None, tuple[int | None, str | None, str | None]]:
    if pr is None or permission is None:
        raise ValueError("issue_comment requires both pr and permission")
    issue = event.get("issue") or {}
    is_pr = "pull_request" in issue and issue.get("pull_request") is not None
    meta = _extract_pr_meta(pr, issue.get("number")) if is_pr else (None, None, None)

    action = event.get("action")
    if action != "created":
        return f"対象のイベント ({action}) ではありません", meta
    if not is_pr:
        return "PR へのコメントではないためスキップします", meta
    body = (event.get("comment") or {}).get("body") or ""
    if not _is_review_command(body, REVIEW_COMMAND):
        return "対象のコメントではないためスキップします", meta
    if permission not in ALLOWED_PERMISSIONS:
        return f"コメント投稿者に権限 ({permission}) がありません", meta
    if pr.get("state") == "closed":
        return "閉じた PR のためスキップします", meta
    common_skip = _check_pr_common(pr, BOT_USER_TYPE)
    if common_skip is not None:
        return common_skip, meta
    return None, meta


def _load_merged_config(config_text: str | None, defaults_text: str | None) -> dict:
    if defaults_text is None:
        defaults_path = Path(__file__).resolve().parents[1] / "config" / "defaults.yml"
        defaults_text = defaults_path.read_text(encoding="utf-8")
    merged = parse_config(defaults_text)
    _validate_config(merged)
    if config_text is not None:
        user_cfg = parse_config(config_text)
        _validate_config(user_cfg)
        merged.update(user_cfg)
    return merged


def _build_claude_args(cfg: dict) -> str:
    tools = list(DEFAULT_TOOLS)
    seen = set(tools)
    for tool in cfg.get("allowed_tools", []):
        if tool not in seen:
            tools.append(tool)
            seen.add(tool)
    joined_tools = ",".join(tools)
    return (
        f"--model {cfg['model']} "
        f"--max-turns {cfg['max_turns']} "
        "--permission-mode default "
        "--setting-sources user "
        f'--allowedTools "{joined_tools}"'
    )


def _build_prompt(pr_number: int | None, cfg: dict) -> str:
    parts = [
        "/lens-review:lens-review",
        str(pr_number),
        "--comment",
        "--min-severity",
        cfg["min_severity"],
    ]
    if cfg["post_rule_violations"]:
        parts.append("--post-rule-violations")
    for rule in cfg.get("rules", []):
        parts.extend(["--rules", rule])
    if "spec" in cfg and cfg["spec"] is not None:
        parts.extend(["--spec", cfg["spec"]])
    first_line = " ".join(parts)
    exclude = cfg.get("exclude", [])
    if exclude:
        return f"{first_line}\n次のパス (glob) に当たるファイルは対象から除く: {', '.join(exclude)}"
    return first_line


def _make_result(
    decision: str,
    meta: tuple[int | None, str | None, str | None],
    *,
    reason: str | None = None,
    claude_args: str | None = None,
    prompt: str | None = None,
    summary_body: str | None = None,
) -> dict:
    pr_number, head_sha, base_sha = meta
    return {
        "decision": decision,
        "reason": reason,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "base_sha": base_sha,
        "claude_args": claude_args,
        "prompt": prompt,
        "summary_body": summary_body,
    }


def _check_event(
    event_name: str, event: dict, pr: dict | None, permission: str | None
) -> tuple[str | None, tuple[int | None, str | None, str | None]]:
    if event_name == "pull_request":
        return _check_pull_request(event)
    if event_name == "issue_comment":
        return _check_issue_comment(event, pr, permission)
    return f"対象のイベント ({event_name}) ではありません", (None, None, None)


def _config_error_result(meta: tuple[int | None, str | None, str | None], reason: str) -> dict:
    base_short = (meta[2] or "")[:7]
    summary_body = (
        "## lens-review\n"
        f"設定ファイル `.github/lens-review.yml` (base {base_short}) を読めなかったため、レビューしていない。\n"
        f"理由: {reason}"
    )
    return _make_result("config-error", meta, reason=reason, summary_body=summary_body)


def decide(
    event_name: str,
    event: dict,
    pr: dict | None,
    permission: str | None,
    config_text: str | None,
    defaults_text: str | None = None,
) -> dict:
    """Decide whether to run, skip, or report config-error for a PR event."""
    skip_reason, meta = _check_event(event_name, event, pr, permission)
    if skip_reason is not None:
        return _make_result("skip", meta, reason=skip_reason)

    try:
        cfg = _load_merged_config(config_text, defaults_text)
    except ConfigError as exc:
        return _config_error_result(meta, str(exc))

    return _make_result(
        "run",
        meta,
        claude_args=_build_claude_args(cfg),
        prompt=_build_prompt(meta[0], cfg),
    )


def failure_body(stage: str, run_url: str, head_sha: str | None) -> str:
    """Format the summary comment body when a review run fails."""
    lines = [
        "## lens-review",
        f"レビューできなかった。失敗した段: {stage}",
        f"ログ: {run_url}",
    ]
    if head_sha:
        lines.append(f"対象の head: {head_sha[:7]}")
    return "\n".join(lines)


def _write_github_output(path: str, result: dict) -> None:
    lines: list[str] = []
    for key in OUTPUT_KEYS:
        val = result[key]
        s = "" if val is None else str(val)
        if "\n" in s or "\r" in s:
            delim = secrets.token_hex(16)
            while delim in s:
                delim = secrets.token_hex(16)
            lines.append(f"{key}<<{delim}\n{s}\n{delim}\n")
        else:
            lines.append(f"{key}={s}\n")
    with open(path, "a", encoding="utf-8") as fp:
        fp.writelines(lines)


def _read_json_file(path: str) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(2) from exc
    if not isinstance(data, dict):
        raise SystemExit(2)
    return data


def _read_config_arg(path: str | None) -> tuple[str | None, str | None]:
    if path is None:
        return None, None
    cfg_path = Path(path)
    if not cfg_path.exists():
        return None, None
    try:
        return cfg_path.read_text(encoding="utf-8"), None
    except UnicodeDecodeError:
        return None, "設定ファイルの文字コードが UTF-8 ではないため読めません"
    except OSError as exc:
        raise SystemExit(2) from exc


def _cmd_decide(args: argparse.Namespace) -> int:
    event = _read_json_file(args.event)
    pr = _read_json_file(args.pr) if args.pr is not None else None
    config_text, decode_error = _read_config_arg(args.config)
    try:
        if decode_error is not None:
            skip_reason, meta = _check_event(args.event_name, event, pr, args.permission)
            result = (
                _make_result("skip", meta, reason=skip_reason)
                if skip_reason is not None
                else _config_error_result(meta, decode_error)
            )
        else:
            result = decide(args.event_name, event, pr, args.permission, config_text)
    except ValueError:
        return 2
    print(json.dumps(result, ensure_ascii=False))
    if args.github_output is not None:
        _write_github_output(args.github_output, result)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="lens-review config and event evaluator")
    sub = parser.add_subparsers(dest="command", required=True)

    p_decide = sub.add_parser("decide")
    p_decide.add_argument("--event-name", required=True)
    p_decide.add_argument("--event", required=True)
    p_decide.add_argument("--pr", default=None)
    p_decide.add_argument("--permission", default=None)
    p_decide.add_argument("--config", default=None)
    p_decide.add_argument("--github-output", default=None)

    p_fail = sub.add_parser("failure-body")
    p_fail.add_argument("--stage", required=True)
    p_fail.add_argument("--run-url", required=True)
    p_fail.add_argument("--head-sha", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "decide":
        return _cmd_decide(args)
    if args.command == "failure-body":
        print(failure_body(args.stage, args.run_url, args.head_sha))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
