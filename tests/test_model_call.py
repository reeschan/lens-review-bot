import json
import os
import sys
import unittest
import urllib.error
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import model_call as m  # noqa: E402


@dataclass
class FakeBundle:
    instruction: str = "INSTRUCTION"
    text: str = "BUNDLE TEXT"


CONFIG = {"model": "claude-test", "max_output_tokens": 1234}
FINDING = {"file": "a.py", "line": 3, "severity": "major", "lens": "x", "heading": "h",
           "body": "b", "quote": "q", "rule_violation": False, "rule_source": ""}


def ok_payload(findings=None, stop_reason="end_turn", usage=None, content=None):
    if content is None:
        answer = {"findings": [FINDING] if findings is None else findings}
        content = [
            {"type": "thinking", "thinking": "", "signature": "s"},
            {"type": "text", "text": json.dumps(answer, ensure_ascii=False)},
        ]
    return json.dumps({
        "stop_reason": stop_reason,
        "content": content,
        "usage": {"input_tokens": 100, "output_tokens": 20} if usage is None else usage,
    }).encode()


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def run(*responses, waits=(0, 0, 0)):
    transport = FakeTransport(*responses)
    sleeps = []
    result = m.call_model(FakeBundle(), CONFIG, "KEY", transport=transport,
                          sleep=sleeps.append, retry_waits=waits)
    return result, transport, sleeps


class CallTestCase(unittest.TestCase):
    def call_failing(self, *responses, waits=(0, 0, 0)):
        transport = FakeTransport(*responses)
        sleeps = []
        with self.assertRaises(m.ModelError) as cm:
            m.call_model(FakeBundle(), CONFIG, "KEY", transport=transport,
                         sleep=sleeps.append, retry_waits=waits)
        return cm.exception, transport, sleeps


class RequestTest(unittest.TestCase):
    def test_request_shape(self):
        _, transport, _ = run((200, ok_payload()))
        req = transport.requests[0]
        body = json.loads(req.data.decode())
        self.assertEqual(req.full_url, m.API_URL)
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("X-api-key"), "KEY")
        self.assertEqual(req.get_header("Anthropic-version"), m.API_VERSION)
        self.assertEqual(body["model"], "claude-test")
        self.assertEqual(body["max_tokens"], 1234)
        self.assertEqual(body["system"], "INSTRUCTION")
        self.assertEqual(body["messages"], [{"role": "user", "content": "BUNDLE TEXT"}])
        self.assertNotIn("tools", body)
        self.assertNotIn("tool_choice", body)
        fmt = body["output_config"]["format"]
        self.assertEqual(fmt["type"], "json_schema")
        self.assertEqual(fmt["schema"]["required"], ["findings"])
        self.assertFalse(fmt["schema"]["additionalProperties"])
        item = fmt["schema"]["properties"]["findings"]["items"]
        self.assertFalse(item["additionalProperties"])
        self.assertEqual(sorted(item["required"]), sorted(FINDING))

    def test_instruction_not_repeated_in_text(self):
        _, transport, _ = run((200, ok_payload()))
        body = json.loads(transport.requests[0].data.decode())
        self.assertNotIn("INSTRUCTION", body["messages"][0]["content"])

    def test_no_prompt_caching(self):
        _, transport, _ = run((200, ok_payload()))
        self.assertNotIn("cache_control", transport.requests[0].data.decode())


class SuccessTest(unittest.TestCase):
    def test_returns_findings_and_tokens(self):
        result, _, sleeps = run((200, ok_payload()))
        self.assertEqual(result.findings, [FINDING])
        self.assertEqual(result.input_tokens, 100)
        self.assertEqual(result.output_tokens, 20)
        self.assertEqual(sleeps, [])

    def test_empty_findings_is_success(self):
        result, _, _ = run((200, ok_payload(findings=[])))
        self.assertEqual(result.findings, [])

    def test_unvalidated_items_pass_through(self):
        result, _, _ = run((200, ok_payload(findings=[{"x": 1}, "junk"])))
        self.assertEqual(result.findings, [{"x": 1}, "junk"])


class RetryTest(CallTestCase):
    def test_retries_then_succeeds(self):
        result, transport, sleeps = run(
            (429, b"{}"), (529, b"{}"), (503, b"{}"), (200, ok_payload()), waits=(1, 2, 3))
        self.assertEqual(len(transport.requests), 4)
        self.assertEqual(sleeps, [1, 2, 3])
        self.assertEqual(result.findings, [FINDING])

    def test_gives_up_after_three_retries(self):
        exc, transport, sleeps = self.call_failing((500, b""), (500, b""), (500, b""), (500, b""))
        self.assertEqual(len(transport.requests), 4)
        self.assertEqual(len(sleeps), 3)
        self.assertEqual(exc.stage, "model")

    def test_client_error_is_not_retried(self):
        _, transport, sleeps = self.call_failing((400, b"{}"))
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(sleeps, [])

    def test_client_error_carries_the_api_error_message(self):
        body = b'{"type":"error","error":{"type":"invalid_request_error","message":"bad model"}}'
        exc, _, _ = self.call_failing((400, body))
        self.assertIn("HTTP 400", str(exc))
        self.assertIn("invalid_request_error", str(exc))
        self.assertIn("bad model", str(exc))

    def test_client_error_without_json_body_still_names_the_status(self):
        exc, _, _ = self.call_failing((400, b"<html>oops</html>"))
        self.assertIn("HTTP 400", str(exc))

    def test_auth_error_is_not_retried(self):
        _, transport, _ = self.call_failing((401, b"{}"))
        self.assertEqual(len(transport.requests), 1)

    def test_network_error_is_retried(self):
        result, transport, _ = run(urllib.error.URLError("down"), (200, ok_payload()))
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(result.output_tokens, 20)


class FailureTest(CallTestCase):
    def test_max_tokens(self):
        exc, _, _ = self.call_failing((200, ok_payload(stop_reason="max_tokens")))
        self.assertIn("max_tokens", str(exc))

    def test_refusal(self):
        exc, _, _ = self.call_failing((200, ok_payload(stop_reason="refusal")))
        self.assertIn("refusal", str(exc))

    def test_no_text_block(self):
        content = [{"type": "thinking", "thinking": "", "signature": "s"}]
        self.call_failing((200, ok_payload(content=content)))

    def test_text_is_not_json(self):
        self.call_failing((200, ok_payload(content=[{"type": "text", "text": "hi"}])))

    def test_findings_not_a_list(self):
        content = [{"type": "text", "text": '{"findings": "x"}'}]
        self.call_failing((200, ok_payload(content=content)))

    def test_not_json(self):
        self.call_failing((200, b"not json"))

    def test_missing_usage(self):
        self.call_failing((200, ok_payload(usage={"input_tokens": 1})))

    def test_error_has_model_stage(self):
        self.assertEqual(m.ModelError.stage, "model")


if __name__ == "__main__":
    unittest.main()
