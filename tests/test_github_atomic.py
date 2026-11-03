"""Check the exact-head GitHub API request and conservative failure handling."""
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import green_bridge as bridge
from tests.test_github_watch import FakeGit, Response


class GitHubAtomicTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="pingpong-atomic-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        config = root / "config.json"
        config.write_text(json.dumps({"git_dir": str(root / "git"), "state_dir": str(root / "state")}),
                          encoding="utf-8")
        self.settings = bridge.load_settings(config)

    def test_commit_contains_expected_head_and_exact_content(self) -> None:
        connection = mock.Mock()
        new_head = "b" * 40
        connection.getresponse.return_value = Response(200, json.dumps({
            "data": {"createCommitOnBranch": {"commit": {"oid": new_head}}}}).encode())
        with mock.patch.object(bridge.http.client, "HTTPSConnection", return_value=connection):
            publisher = bridge.GitHubAtomicTransport(FakeGit(), self.settings)
            self.assertEqual(publisher.publish("pingpong/results", "a" * 40, "one.json", b"hello"), new_head)
            publisher.close()
        request = connection.request.call_args
        self.assertEqual(request.args[:2], ("POST", "/graphql"))
        payload = json.loads(request.kwargs["body"])
        value = payload["variables"]["input"]
        self.assertEqual(value["branch"], {"repositoryNameWithOwner": "example/repository",
                                           "branchName": "pingpong/results"})
        self.assertEqual(value["expectedHeadOid"], "a" * 40)
        addition = value["fileChanges"]["additions"][0]
        self.assertEqual(addition["path"], "one.json")
        self.assertEqual(base64.b64decode(addition["contents"]), b"hello")
        connection.close.assert_called_once()

    def test_oversized_or_invalid_requests_are_not_sent(self) -> None:
        connection = mock.Mock()
        with mock.patch.object(bridge.http.client, "HTTPSConnection", return_value=connection):
            publisher = bridge.GitHubAtomicTransport(FakeGit(), self.settings)
            with self.assertRaises(bridge.ApiPublicationError):
                publisher.publish("pingpong/results", "a" * 40, "one.json",
                                  b"x" * bridge.MAX_API_COMMIT_BODY_BYTES)
            with self.assertRaises(bridge.ApiPublicationError):
                publisher.publish("pingpong/results", "short", "one.json", b"x")
            publisher.close()
        connection.request.assert_not_called()

    def test_existing_group_larger_than_the_old_limit_is_sent(self) -> None:
        connection = mock.Mock()
        connection.getresponse.return_value = Response(200, json.dumps({
            "data": {"createCommitOnBranch": {"commit": {"oid": "b" * 40}}}}).encode())
        content = b"x" * 70_000
        with mock.patch.object(bridge.http.client, "HTTPSConnection", return_value=connection):
            publisher = bridge.GitHubAtomicTransport(FakeGit(), self.settings)
            self.assertEqual(publisher.publish("pingpong/results", "a" * 40, "group.json", content), "b" * 40)
            publisher.close()
        body = json.loads(connection.request.call_args.kwargs["body"])
        encoded = body["variables"]["input"]["fileChanges"]["additions"][0]["contents"]
        self.assertEqual(base64.b64decode(encoded), content)

    def test_http_and_graphql_failures_do_not_expose_server_text(self) -> None:
        for status, body in ((403, b"private-secret"),
                             (200, b'{"errors":[{"message":"private-secret"}]}'),
                             (200, b"private-secret"),
                             (200, b'{"data":[]}')):
            with self.subTest(status=status, body=body):
                connection = mock.Mock()
                connection.getresponse.return_value = Response(status, body)
                with mock.patch.object(bridge.http.client, "HTTPSConnection", return_value=connection):
                    publisher = bridge.GitHubAtomicTransport(FakeGit(), self.settings)
                    with self.assertRaises(bridge.ApiPublicationError) as error:
                        publisher.publish("pingpong/results", "a" * 40, "one.json", b"x")
                    self.assertNotIn("private-secret", str(error.exception))
                    publisher.close()

    def test_disconnect_leaves_commit_outcome_unconfirmed(self) -> None:
        connection = mock.Mock()
        connection.request.side_effect = OSError("private-secret")
        with mock.patch.object(bridge.http.client, "HTTPSConnection", return_value=connection):
            publisher = bridge.GitHubAtomicTransport(FakeGit(), self.settings)
            with self.assertRaises(bridge.ApiPublicationError) as error:
                publisher.publish("pingpong/results", "a" * 40, "one.json", b"x")
            self.assertNotIn("private-secret", str(error.exception))
            publisher.close()

    def test_stale_expected_head_is_a_retryable_conflict(self) -> None:
        connection = mock.Mock()
        connection.getresponse.return_value = Response(200, b'{"errors":[{"type":"STALE_DATA"}]}')
        with mock.patch.object(bridge.http.client, "HTTPSConnection", return_value=connection):
            publisher = bridge.GitHubAtomicTransport(FakeGit(), self.settings)
            with self.assertRaises(bridge.ApiHeadConflict):
                publisher.publish("pingpong/results", "a" * 40, "one.json", b"x")
            publisher.close()

    def test_rate_limit_uses_retry_after_or_server_time(self) -> None:
        cases = ((429, {"Retry-After": "125"}, 125),
                 (403, {"Retry-After": "180"}, 180),
                 (200, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "400",
                        "Date": "Thu, 01 Jan 1970 00:01:40 GMT"}, 301),
                 (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "400",
                        "Date": "Thu, 01 Jan 1970 00:01:40 GMT"}, 301))
        for status, headers, expected in cases:
            with self.subTest(status=status):
                connection = mock.Mock()
                connection.getresponse.return_value = Response(status, headers=headers)
                with mock.patch.object(bridge.http.client, "HTTPSConnection", return_value=connection):
                    publisher = bridge.GitHubAtomicTransport(FakeGit(), self.settings)
                    with self.assertRaises(bridge.ApiRateLimit) as error:
                        publisher.publish("pingpong/results", "a" * 40, "one.json", b"x")
                    self.assertEqual(error.exception.retry_seconds, expected)
                    publisher.close()


if __name__ == "__main__":
    unittest.main()
