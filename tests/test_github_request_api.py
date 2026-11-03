"""Verify API snapshots preserve the task bytes and observed branch history."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import green_bridge as bridge
from tests.test_github_watch import FakeGit, Response


class GitHubRequestApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="request-api-snapshot-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        config = root / "config.json"
        config.write_text(json.dumps({"git_dir": str(root / "git"), "state_dir": str(root / "state")}),
                          encoding="utf-8")
        self.settings = bridge.load_settings(config)
        self.content = bridge.json_bytes({"version": 2, "task_id": "project", "commands": []})
        self.old = "a" * 40
        self.head = "b" * 40
        self.oid = hashlib.sha1(b"blob " + str(len(self.content)).encode() + b"\0" + self.content).hexdigest()

    def response(self, *, ahead: int = 0, oid: str = None, missing: bool = False) -> bytes:
        entry = None if missing else {"object": {"oid": oid or self.oid, "byteSize": len(self.content),
                                                   "isBinary": False, "text": self.content.decode()}}
        value = {"data": {"repository": {"ref": {"name": "pingpong/requests",
                 "target": {"oid": self.head, "file": entry},
                 "c0": {"aheadBy": ahead, "baseTarget": {"oid": self.head},
                        "headTarget": {"oid": self.old}}}}}}
        return json.dumps(value).encode()

    def snapshot(self, body: bytes) -> tuple:
        connection = mock.Mock()
        connection.getresponse.return_value = Response(200, body)
        with mock.patch.object(bridge.http.client, "HTTPSConnection", return_value=connection):
            client = bridge.GitHubRequestTransport(FakeGit(), self.settings)
            try:
                result = client.snapshot("pingpong/requests", "project", [self.old])
            finally:
                client.close()
        request = json.loads(connection.request.call_args.kwargs["body"])
        self.assertEqual(request["variables"]["head0"], self.old)
        self.assertEqual(request["variables"]["path"], "pingpong/task-requests/project.json")
        return result

    def test_valid_snapshot_checks_bytes_and_history(self) -> None:
        self.assertEqual(self.snapshot(self.response()), (self.head, self.content))

    def test_missing_task_is_an_empty_snapshot(self) -> None:
        self.assertEqual(self.snapshot(self.response(missing=True)), (self.head, None))

    def test_diverged_history_requires_git_verification(self) -> None:
        with self.assertRaises(bridge.ApiPublicationError):
            self.snapshot(self.response(ahead=1))

    def test_changed_blob_bytes_are_rejected(self) -> None:
        with self.assertRaises(bridge.ApiPublicationError):
            self.snapshot(self.response(oid="c" * 40))


if __name__ == "__main__":
    unittest.main()
