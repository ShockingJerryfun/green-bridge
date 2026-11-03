"""Exercise the adapter against a simulated CodeAgentCLI response."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ADAPTER = Path(__file__).resolve().parents[1] / "scripts" / "codeagent_adapter.py"
FAKE_CODEAGENT = r'''#!/usr/bin/env python3
import json
import sys
args = sys.argv[1:]
if "--session-id" in args:
    session_id = args[args.index("--session-id") + 1]
else:
    session_id = args[args.index("--resume") + 1]
prompt = sys.stdin.read()
result = json.dumps({"summary": "done", "command_evidence": [{"exit_code": 0}],
                     "context": {"compaction_detected": True}, "checkpoint": "snapshot"})
if "FENCED_RESULT" in prompt:
    result = "```json\n" + result + "\n```"
if "NON_ASCII_RESULT" in prompt:
    result = json.dumps({"summary": "已收到第一条消息"}, ensure_ascii=False)
payload = {"session_id": session_id, "is_error": False, "result": result,
           "usage": {"input_tokens": 100}}
if "MISSING_ERROR_FIELD" in prompt:
    del payload["is_error"]
json.dump(payload, sys.stdout)
'''


@unittest.skipIf(os.name == "nt", "fake executable uses a POSIX shebang")
class CodeAgentAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="codeagent-adapter-")
        self.root = Path(self.tmp.name)
        self.fake = self.root / "codeagent"
        self.fake.write_text(FAKE_CODEAGENT, encoding="utf-8")
        self.fake.chmod(0o755)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def invoke(self, request: dict) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(ADAPTER), "--executable", str(self.fake),
                               "--cwd", str(self.root)], input=json.dumps(request), text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)

    def test_new_session_then_resume(self) -> None:
        request = {"version": 1, "id": "new", "action": "session.new", "session": "project",
                   "native_session_id": "123e4567-e89b-12d3-a456-426614174000"}
        created = self.invoke(request)
        self.assertEqual(created.returncode, 0, created.stderr)
        new = json.loads(created.stdout)
        self.assertEqual(new["status"], "completed")
        self.assertIsNone(new["context"]["compaction_detected"])
        self.assertTrue(new["context"]["agent_reported"]["compaction_detected"])
        request.update({"id": "run", "action": "task.execute", "message": "inspect",
                        "native_session_id": new["native_session_id"]})
        resumed = self.invoke(request)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(json.loads(resumed.stdout)["native_session_id"], new["native_session_id"])

    def test_unrecognized_response_is_not_reported_as_success(self) -> None:
        request = {"version": 1, "id": "run", "action": "task.execute", "session": "project",
                   "native_session_id": "123e4567-e89b-12d3-a456-426614174000",
                   "message": "MISSING_ERROR_FIELD"}
        result = self.invoke(request)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no recognized is_error", result.stderr)

    def test_resume_requires_native_uuid(self) -> None:
        request = {"version": 1, "id": "run", "action": "session.switch", "session": "project",
                   "native_session_id": "not-a-uuid"}
        result = self.invoke(request)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a UUID", result.stderr)

    def test_json_checkpoint_in_code_fence_is_preserved(self) -> None:
        request = {"version": 1, "id": "run", "action": "task.execute", "session": "project",
                   "native_session_id": "123e4567-e89b-12d3-a456-426614174000",
                   "message": "FENCED_RESULT"}
        result = self.invoke(request)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["checkpoint"], "snapshot")

    def test_chinese_response_uses_utf8_with_non_utf8_stdout_locale(self) -> None:
        request = {"version": 1, "id": "run", "action": "task.execute", "session": "project",
                   "native_session_id": "123e4567-e89b-12d3-a456-426614174000",
                   "message": "NON_ASCII_RESULT"}
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "gbk"
        result = subprocess.run([sys.executable, str(ADAPTER), "--executable", str(self.fake),
                                 "--cwd", str(self.root)], input=json.dumps(request).encode("utf-8"),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
                                timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout.decode("utf-8"))
        self.assertEqual(response["summary"], "已收到第一条消息")


if __name__ == "__main__":
    unittest.main()
