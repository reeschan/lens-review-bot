"""Build the bundle (束ね) that is sent to the model in one call.

The bundle holds the bot instruction, the lenses, the reviewer/validator prompts,
the rules, the spec, the diff, the changed files' full text (with line numbers)
and the findings already posted. When it exceeds the size limit, it is trimmed
(削り) in three stages, and what was trimmed is returned to the caller.
"""
from __future__ import annotations

import base64
import binascii
import codecs
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

# Size limit of the bundle in characters (instruction + text). Provisional value;
# change it here only.
BUNDLE_CHAR_LIMIT = 400_000

# Context lines kept around each change when the diff is shrunk (stage 2).
DIFF_SHRINK_CONTEXT = 1

LENS_REPO = "reeschan/claude-plugins-official"
LENS_ROOT = "plugins/lens-review/skills/lens-review"
LENS_DIRS = ("lenses", "prompts")
LENS_SKIP_NAMES = frozenset({"SKILL.md"})

RULE_FILE_NAMES = ("CLAUDE.md", "AGENTS.md")

INSTRUCTION_PATH = Path(__file__).resolve().parents[1] / "prompts" / "bot-instruction.md"

STAGE_FULL_TEXT = "full_text"
STAGE_DIFF_CONTEXT = "diff_context"
STAGE_DIFF_NAME_ONLY = "diff_name_only"
STAGE_LABELS = {
    STAGE_FULL_TEXT: "全文を外した",
    STAGE_DIFF_CONTEXT: "diff を前後数行に縮めた",
    STAGE_DIFF_NAME_ONLY: "diff をファイル名だけにした",
}

MARKER_RE = re.compile(
    r"<!--\s*lens-review:\s*(?P<file>\S+):(?P<line>\d+)\s+(?P<heading>.*?)"
    r"(?:\s+#(?P<hash>[0-9a-fA-F]{8}))?\s*-->"
)
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
DIFF_HEADER_RE = re.compile(r"^diff --git ")

GhRunner = Callable[[list], str]
LensFetcher = Callable[[], list]


class LensFetchError(RuntimeError):
    """The lenses or prompts could not be fetched. The run fails at stage "model"."""

    stage = "model"


@dataclass
class Trim:
    """One trimming step: which file was trimmed and at which stage."""

    path: str
    stage: str

    @property
    def label(self) -> str:
        return STAGE_LABELS[self.stage]


@dataclass
class FileDiff:
    path: str
    header: list[str]
    hunks: list[list[str]]
    deleted: bool = False

    def render(self, mode: str) -> str:
        if mode == STAGE_DIFF_NAME_ONLY:
            return f'<diff path="{self.path}" omitted="true"></diff>'
        hunks = self.hunks
        if mode == STAGE_DIFF_CONTEXT:
            hunks = [h for hunk in self.hunks for h in shrink_hunk(hunk, DIFF_SHRINK_CONTEXT)]
        body = "\n".join(self.header + [line for hunk in hunks for line in hunk])
        return f'<diff path="{self.path}">\n{body}\n</diff>'

    def body_lines(self, mode: str) -> list[str]:
        """Line contents (without the +/-/space prefix) present in the rendered diff."""
        if mode == STAGE_DIFF_NAME_ONLY:
            return []
        hunks = self.hunks
        if mode == STAGE_DIFF_CONTEXT:
            hunks = [h for hunk in self.hunks for h in shrink_hunk(hunk, DIFF_SHRINK_CONTEXT)]
        return [line[1:] for hunk in hunks for line in hunk[1:] if line[:1] in (" ", "+", "-")]


@dataclass
class Bundle:
    """The bundle sent to the model.

    `instruction` is the bot instruction, `text` is everything else. Both count
    toward the size limit. `file_lines` holds, per changed file, the lines that are
    actually present in the bundle (full text if kept, plus the diff lines), for
    checking the quotes of findings.
    """

    instruction: str
    text: str
    trimmed: list[Trim] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    file_lines: dict[str, list[str]] = field(default_factory=dict)
    over_limit: bool = False

    @property
    def size(self) -> int:
        return len(self.instruction) + len(self.text)


# ---------------------------------------------------------------------------
# Lenses and prompts


def run_gh(args: list) -> str:
    result = subprocess.run(
        ["gh", *args], capture_output=True, text=True, encoding="utf-8", check=True
    )
    return result.stdout


def _gh_json(gh: GhRunner, path: str):
    try:
        return json.loads(gh(["api", f"repos/{LENS_REPO}/contents/{path}"]))
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise LensFetchError(f"{path} を取得できません") from exc


def fetch_lens_docs(gh: GhRunner = run_gh) -> list:
    """Fetch every lenses/*.md and prompts/*.md from the default branch.

    Returns a list of (path, text), path relative to the lens-review skill folder.
    Raises LensFetchError when any of them cannot be fetched.
    """
    docs: list = []
    for folder in LENS_DIRS:
        entries = _gh_json(gh, f"{LENS_ROOT}/{folder}")
        if not isinstance(entries, list):
            raise LensFetchError(f"{folder} の一覧を読めません")
        names = sorted(
            e.get("name") for e in entries
            if isinstance(e, dict) and e.get("type") == "file"
            and isinstance(e.get("name"), str) and e["name"].endswith(".md")
            and e["name"] not in LENS_SKIP_NAMES
        )
        if not names:
            raise LensFetchError(f"{folder} に .md がありません")
        for name in names:
            data = _gh_json(gh, f"{LENS_ROOT}/{folder}/{name}")
            if not isinstance(data, dict) or data.get("encoding") != "base64":
                raise LensFetchError(f"{folder}/{name} の中身を読めません")
            try:
                text = base64.b64decode(data.get("content") or "").decode("utf-8")
            except (binascii.Error, ValueError) as exc:
                raise LensFetchError(f"{folder}/{name} の中身を読めません") from exc
            docs.append((f"{folder}/{name}", text))
    return docs


# ---------------------------------------------------------------------------
# Diff


def _unquote_git_path(raw: str) -> str:
    if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
        inner = raw[1:-1].encode("latin-1", "backslashreplace")
        raw = codecs.escape_decode(inner)[0].decode("utf-8", "replace")
    return raw


def _side_path(line: str, prefix: str) -> str | None:
    rest = _unquote_git_path(line[4:].rstrip("\t").strip())
    if rest == "/dev/null":
        return None
    if rest.startswith(prefix):
        return rest[len(prefix):]
    return rest


def _path_from_git_header(line: str) -> str:
    rest = line[len("diff --git "):]
    quoted = rest.rfind(' "b/')
    if quoted >= 0:
        return _unquote_git_path(rest[quoted + 1:])[2:]
    marker = rest.rfind(" b/")
    return rest[marker + 3:] if marker >= 0 else rest


def _finish_file(header: list[str], hunks: list[list[str]]) -> FileDiff:
    old_path = new_path = None
    has_plus = False
    for line in header:
        if line.startswith("--- "):
            old_path = _side_path(line, "a/")
        elif line.startswith("+++ "):
            has_plus = True
            new_path = _side_path(line, "b/")
        elif line.startswith("rename to "):
            new_path = new_path or line[len("rename to "):]
    deleted = any(line.startswith("deleted file mode") for line in header) or (
        has_plus and new_path is None
    )
    path = new_path or old_path or _path_from_git_header(header[0])
    return FileDiff(path=path, header=header, hunks=hunks, deleted=deleted)


def parse_diff(diff_text: str) -> list[FileDiff]:
    """Split a unified git diff into per-file sections."""
    files: list[FileDiff] = []
    header: list[str] | None = None
    hunks: list[list[str]] = []
    for line in diff_text.splitlines():
        if DIFF_HEADER_RE.match(line):
            if header is not None:
                files.append(_finish_file(header, hunks))
            header, hunks = [line], []
        elif header is None:
            continue
        elif line.startswith("@@"):
            hunks.append([line])
        elif hunks:
            hunks[-1].append(line)
        else:
            header.append(line)
    if header is not None:
        files.append(_finish_file(header, hunks))
    return files


def shrink_hunk(hunk: list[str], context: int) -> list[list[str]]:
    """Keep only `context` lines around each change; split into new hunks."""
    match = HUNK_RE.match(hunk[0])
    if match is None:
        return [hunk]
    old_start, old_count = int(match.group(1)), int(match.group(2) or "1")
    new_start, new_count = int(match.group(3)), int(match.group(4) or "1")
    body = hunk[1:]
    changes = [i for i, line in enumerate(body) if line[:1] in ("+", "-")]
    if not changes:
        return []

    keep = [False] * len(body)
    for i, line in enumerate(body):
        if line.startswith("\\"):
            keep[i] = i > 0 and keep[i - 1]
            continue
        keep[i] = any(abs(i - j) <= context for j in changes)

    old_pos = old_start - 1 if old_count > 0 else old_start
    new_pos = new_start - 1 if new_count > 0 else new_start
    result: list[list[str]] = []
    current: list[str] | None = None
    cur_old_before = cur_new_before = 0
    for i, line in enumerate(body):
        if keep[i]:
            if current is None:
                current, cur_old_before, cur_new_before = [], old_pos, new_pos
            current.append(line)
        elif current is not None:
            result.append(_with_header(current, cur_old_before, cur_new_before))
            current = None
        if line[:1] in (" ", "-"):
            old_pos += 1
        if line[:1] in (" ", "+"):
            new_pos += 1
    if current is not None:
        result.append(_with_header(current, cur_old_before, cur_new_before))
    return result


def _with_header(lines: list[str], old_before: int, new_before: int) -> list[str]:
    old_n = sum(1 for line in lines if line[:1] in (" ", "-"))
    new_n = sum(1 for line in lines if line[:1] in (" ", "+"))
    old_s = old_before + 1 if old_n else old_before
    new_s = new_before + 1 if new_n else new_before
    return [f"@@ -{old_s},{old_n} +{new_s},{new_n} @@"] + lines


# ---------------------------------------------------------------------------
# Paths and files


def _glob_to_regex(pattern: str) -> re.Pattern:
    out: list[str] = []
    i = 0
    if pattern.endswith("/"):
        pattern += "**"
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def is_excluded(path: str, patterns: Iterable[str]) -> bool:
    """True when the path matches one of the exclude globs (`*`, `?`, `**`)."""
    return any(_glob_to_regex(p).match(path) for p in patterns)


def _read_inside(root: Path, rel: str) -> str | None:
    """Read a UTF-8 text file under root. None if missing, outside root or binary."""
    if not rel or rel.startswith("/") or "\\" in rel:
        return None
    base = root.resolve()
    target = (base / rel).resolve()
    if not target.is_relative_to(base) or not target.is_file():
        return None
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if "\x00" in text:
        return None
    return text


def number_lines(text: str) -> str:
    return "\n".join(f"{n}: {line}" for n, line in enumerate(text.splitlines(), start=1))


def existing_findings(bodies: Iterable[str]) -> list:
    """(file, heading) from the markers of the inline findings already posted.

    The caller passes only the bodies of the bot's own inline comments.
    """
    seen: list = []
    for body in bodies:
        for match in MARKER_RE.finditer(body or ""):
            item = (match.group("file"), match.group("heading").strip())
            if item not in seen:
                seen.append(item)
    return seen


def _rule_paths(changed: Iterable[str], extra_rules: Iterable[str]) -> list[str]:
    folders = sorted({str(Path(p).parent).replace("\\", "/") for p in changed} - {"."})
    paths = list(RULE_FILE_NAMES)
    for folder in folders:
        paths.extend(f"{folder}/{name}" for name in RULE_FILE_NAMES)
    for rule in extra_rules:
        if rule not in paths:
            paths.append(rule)
    return paths


# ---------------------------------------------------------------------------
# Building


def _doc(kind: str, path: str, text: str) -> str:
    return f'<document kind="{kind}" path="{path}">\n{text.rstrip()}\n</document>'


def _section(title: str, parts: list[str]) -> str:
    body = "\n\n".join(parts) if parts else "(なし)"
    return f"# {title}\n\n{body}"


@dataclass
class _ChangedFile:
    diff: FileDiff
    full_text: str | None
    diff_mode: str = "full"
    keep_full: bool = True
    _cache: dict = field(default_factory=dict, repr=False)

    @property
    def path(self) -> str:
        return self.diff.path

    def full_part(self) -> str:
        if self.full_text is None or not self.keep_full:
            return ""
        if "full_text" not in self._cache:
            self._cache["full_text"] = (
                f'<file path="{self.path}">\n{number_lines(self.full_text)}\n</file>'
            )
        return self._cache["full_text"]

    def diff_part(self) -> str:
        if ("diff", self.diff_mode) not in self._cache:
            self._cache["diff", self.diff_mode] = self.diff.render(self.diff_mode)
        return self._cache["diff", self.diff_mode]


def _render(fixed: list[str], files: list[_ChangedFile]) -> str:
    diffs = [f.diff_part() for f in files]
    fulls = [p for p in (f.full_part() for f in files) if p]
    return "\n\n".join(fixed + [_section("diff", diffs), _section("変更後の全文", fulls)])


def _trim(instruction: str, fixed: list[str], files: list[_ChangedFile], limit: int) -> tuple:
    trimmed: list[Trim] = []

    def fits() -> bool:
        return len(instruction) + len(_render(fixed, files)) <= limit

    by_size = sorted(files, key=lambda f: (-len(f.full_text or ""), f.path))
    steps = (
        (STAGE_FULL_TEXT, by_size, lambda f: f.full_text is not None and f.keep_full,
         lambda f: setattr(f, "keep_full", False)),
        (STAGE_DIFF_CONTEXT, sorted(files, key=lambda f: (-len(f.diff_part()), f.path)),
         lambda f: f.diff_mode == "full", lambda f: setattr(f, "diff_mode", STAGE_DIFF_CONTEXT)),
        (STAGE_DIFF_NAME_ONLY, None, lambda f: f.diff_mode != STAGE_DIFF_NAME_ONLY,
         lambda f: setattr(f, "diff_mode", STAGE_DIFF_NAME_ONLY)),
    )
    for stage, order, applicable, apply in steps:
        if fits():
            break
        if order is None:
            order = sorted(files, key=lambda f: (-len(f.diff_part()), f.path))
        for f in order:
            if fits():
                break
            if applicable(f):
                apply(f)
                trimmed.append(Trim(f.path, stage))
    return trimmed, not fits()


def build_bundle(
    root: str | Path,
    diff_text: str,
    config: dict,
    inline_comment_bodies: Iterable[str] = (),
    *,
    fetch_lens: LensFetcher | None = None,
    instruction: str | None = None,
    limit: int = BUNDLE_CHAR_LIMIT,
) -> Bundle:
    """Build the bundle from the head checkout at `root` and the PR diff.

    `config` is the merged config (rules, spec, exclude are used).
    `fetch_lens` returns [(path, text)] for lenses/*.md and prompts/*.md; it
    defaults to fetching from GitHub and raises LensFetchError on failure.
    """
    root = Path(root)
    if instruction is None:
        instruction = INSTRUCTION_PATH.read_text(encoding="utf-8")
    docs = (fetch_lens or fetch_lens_docs)()
    lenses = [_doc("lens", p, t) for p, t in docs if p.startswith("lenses/")]
    prompts = [_doc("prompt", p, t) for p, t in docs if not p.startswith("lenses/")]

    exclude = list(config.get("exclude") or [])
    changed: list[_ChangedFile] = []
    excluded: list[str] = []
    for file_diff in parse_diff(diff_text):
        if is_excluded(file_diff.path, exclude):
            excluded.append(file_diff.path)
            continue
        full = None if file_diff.deleted else _read_inside(root, file_diff.path)
        changed.append(_ChangedFile(file_diff, full))

    missing: list[str] = []
    rules: list[str] = []
    configured_rules = list(config.get("rules") or [])
    for path in _rule_paths([f.path for f in changed], configured_rules):
        text = _read_inside(root, path)
        if text is not None:
            rules.append(_doc("rule", path, text))
        elif path in configured_rules:
            missing.append(path)

    specs: list[str] = []
    spec_path = config.get("spec")
    if spec_path:
        text = _read_inside(root, spec_path)
        if text is not None:
            specs.append(_doc("spec", spec_path, text))
        else:
            missing.append(spec_path)

    findings = [f"- {path}: {heading}" for path, heading in existing_findings(inline_comment_bodies)]

    fixed = [
        _section("観点", lenses),
        _section("担当・検証の指示", prompts),
        _section("規約", rules),
        _section("仕様", specs),
        "# 既出の指摘\n\n" + ("\n".join(findings) if findings else "(なし)"),
    ]
    trimmed, over_limit = _trim(instruction, fixed, changed, limit)

    file_lines: dict[str, list[str]] = {}
    for f in changed:
        lines = f.full_text.splitlines() if f.full_text is not None and f.keep_full else []
        file_lines[f.path] = lines + f.diff.body_lines(f.diff_mode)

    return Bundle(
        instruction=instruction,
        text=_render(fixed, changed),
        trimmed=trimmed,
        excluded=excluded,
        missing=missing,
        file_lines=file_lines,
        over_limit=over_limit,
    )
