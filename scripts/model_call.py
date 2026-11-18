"""Call the Messages API once (with retries) and return the findings.

The answer's shape is fixed with structured outputs (`output_config.format`, a JSON
Schema), so the reply is one text block holding `{"findings": [...]}`. No tools are
offered. Only the standard library (urllib) is used.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
REQUEST_TIMEOUT_SECONDS = 600

# Retries after the first attempt: 3 retries means up to 4 requests.
MAX_RETRIES = 3
RETRY_WAIT_SECONDS = (5.0, 15.0, 45.0)
RETRY_STATUSES = frozenset({429, 529})

FINDING_PROPERTIES = {
    "file": {"type": "string"},
    "line": {"type": "integer"},
    "severity": {"type": "string", "enum": ["critical", "major", "minor"]},
    "lens": {"type": "string"},
    "heading": {"type": "string"},
    "body": {"type": "string"},
    "quote": {"type": "string"},
    "rule_violation": {"type": "boolean"},
    "rule_source": {"type": "string"},
}

# Structured outputs need `additionalProperties: false` on every object.
ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": FINDING_PROPERTIES,
                "required": list(FINDING_PROPERTIES),
                "additionalProperties": False,
            },
        }
    },
    "required": ["findings"],
    "additionalProperties": False,
}

# (request, timeout) -> (status, body bytes). Replaceable in tests.
Transport = Callable[[urllib.request.Request, float], "tuple[int, bytes]"]
Sleeper = Callable[[float], None]


class ModelError(RuntimeError):
    """The model call failed. The run fails at stage "model"."""

    stage = "model"


@dataclass
class ModelResult:
    """Unvalidated findings (a list of dicts) and the tokens used."""

    findings: list
    input_tokens: int
    output_tokens: int


def _urllib_transport(request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read()
        finally:
            exc.close()


def build_request_body(instruction: str, text: str, model: str, max_output_tokens: int) -> dict:
    """The request body. No prompt caching (no cache_control anywhere)."""
    return {
        "model": model,
        "max_tokens": max_output_tokens,
        "system": instruction,
        "messages": [{"role": "user", "content": text}],
        "output_config": {"format": {"type": "json_schema", "schema": ANSWER_SCHEMA}},
    }


API_ERROR_MESSAGE_LIMIT = 300


def _api_error_detail(payload: bytes) -> str:
    """The API's error type and message, so the failure says why (empty when unreadable)."""
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return ""
    kind = " ".join(str(error.get("type") or "").split())
    message = " ".join(str(error.get("message") or "").split())[:API_ERROR_MESSAGE_LIMIT]
    detail = ": ".join(part for part in (kind, message) if part)
    return f" ({detail})" if detail else ""


def _is_retryable(status: int) -> bool:
    return status in RETRY_STATUSES or 500 <= status < 600


def _send(
    request: urllib.request.Request, transport: Transport, sleep: Sleeper, waits: tuple
) -> bytes:
    for attempt in range(MAX_RETRIES + 1):
        try:
            status, payload = transport(request, REQUEST_TIMEOUT_SECONDS)
        except (urllib.error.URLError, TimeoutError, OSError):
            status, payload = 0, b""
        if 200 <= status < 300:
            return payload
        if status != 0 and not _is_retryable(status):
            raise ModelError(f"Messages API が HTTP {status} を返しました{_api_error_detail(payload)}")
        if attempt == MAX_RETRIES:
            label = "接続に失敗" if status == 0 else f"HTTP {status}"
            raise ModelError(f"Messages API が {MAX_RETRIES} 回やり直しても {label} でした")
        sleep(waits[min(attempt, len(waits) - 1)])
    raise ModelError("Messages API を呼べませんでした")  # pragma: no cover


def _token_count(usage: object, key: str) -> int:
    value = usage.get(key) if isinstance(usage, dict) else None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ModelError(f"応答の usage に {key} がありません")
    return value


def _parse_response(payload: bytes) -> ModelResult:
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelError("応答が JSON ではありません") from exc
    if not isinstance(data, dict):
        raise ModelError("応答が JSON の辞書ではありません")
    stop_reason = data.get("stop_reason")
    if stop_reason == "max_tokens":
        raise ModelError("出力が max_tokens に達して途切れました")
    if stop_reason == "refusal":
        raise ModelError("モデルが答えを断りました (stop_reason: refusal)")
    blocks = data.get("content")
    text = next(
        (
            b.get("text") for b in (blocks if isinstance(blocks, list) else [])
            if isinstance(b, dict) and b.get("type") == "text"
        ),
        None,
    )
    if not isinstance(text, str):
        raise ModelError("応答に答えの本文がありません")
    try:
        answer = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ModelError("答えの本文が JSON ではありません") from exc
    findings = answer.get("findings") if isinstance(answer, dict) else None
    if not isinstance(findings, list):
        raise ModelError("答えに findings の一覧がありません")
    usage = data.get("usage")
    return ModelResult(
        findings=list(findings),
        input_tokens=_token_count(usage, "input_tokens"),
        output_tokens=_token_count(usage, "output_tokens"),
    )


def call_model(
    bundle,
    config: dict,
    api_key: str,
    *,
    transport: Transport | None = None,
    sleep: Sleeper | None = None,
    retry_waits: tuple = RETRY_WAIT_SECONDS,
) -> ModelResult:
    """Call the Messages API for `bundle` and return findings and token counts.

    `bundle.instruction` goes to the system prompt and `bundle.text` to the user
    message. `config` supplies `model` and `max_output_tokens`. Raises ModelError
    (stage "model") on any failure.
    """
    body = build_request_body(
        bundle.instruction, bundle.text, config["model"], config["max_output_tokens"]
    )
    request = urllib.request.Request(
        API_URL,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": API_VERSION,
        },
    )
    payload = _send(request, transport or _urllib_transport, sleep or time.sleep, retry_waits)
    return _parse_response(payload)
