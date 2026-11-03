"""Exercise conditional GitHub ref checks and safe fallback behavior."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import green_bridge as bridge


class FakeGit:
    def __init__(self, remote: str = "https://github.com/example/repository.git") -> None:
        self.remote = remote

    def run(self, arguments, data=None, check=True):
        if arguments[:2] == ["remote", "get-url"]:
            return self.remote.encode()
        if arguments == ["credential", "fill"]:
            return b"protocol=https\nhost=github.com\npassword=example-token\n\n"
        if arguments[:2] == ["config", "--get-urlmatch"]:
            return b"http://127.0.0.1:8765\n"
        raise AssertionError("unexpected Git request")


class Response:
    def __init__(self, status: int, body: bytes = b"", headers=None) -> None:
        self.status = status
        self.body = body
        self.headers = headers or {}

    def read(self, size: int) -> bytes:
        return self.body[:size]

    def getheader(self, name: str):
        return self.headers.get(name)


class GitHubWatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="pingpong-watch-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        config = root / "config.json"
        config.write_text(json.dumps({"git_dir": str(root / "git"), "state_dir": str(root / "state"),
                                      "watch_mode": "github_api"}), encoding="utf-8")
        self.settings = bridge.load_settings(config)

    @staticmethod
    def response_for(head: str, etag: str) -> Response:
        body = json.dumps({"name": "pingpong/requests", "commit": {"sha": head}}).encode()
        return Response(200, body, {"ETag": etag})

    def test_reuses_connection_and_conditional_ref(self) -> None:
        first, second = "a" * 40, "b" * 40
        connection = mock.Mock()
        connection.getresponse.side_effect = [self.response_for(first, '"one"'), Response(304),
                                              self.response_for(second, '"two"')]
        with mock.patch.object(bridge.http.client, "HTTPSConnection", return_value=connection) as factory:
            watcher = bridge.GitHubRefWatcher(FakeGit(), self.settings)
            self.assertEqual(watcher.check(), first)
            self.assertEqual(watcher.check(), first)
            self.assertEqual(watcher.check(), second)
            watcher.close()
        factory.assert_called_once_with("127.0.0.1", 8765, timeout=8)
        connection.set_tunnel.assert_called_once_with("api.github.com", 443, headers={})
        calls = connection.request.call_args_list
        self.assertEqual(calls[0].args[:2], ("GET", "/repos/example/repository/branches/pingpong%2Frequests"))
        self.assertNotIn("If-None-Match", calls[0].kwargs["headers"])
        self.assertEqual(calls[1].kwargs["headers"]["If-None-Match"], '"one"')
        self.assertEqual(calls[2].kwargs["headers"]["If-None-Match"], '"one"')
        connection.close.assert_called_once()

    def test_network_failure_closes_connection_and_can_reconnect(self) -> None:
        broken = mock.Mock()
        broken.request.side_effect = OSError("temporary disconnect")
        recovered = mock.Mock()
        recovered.getresponse.return_value = self.response_for("c" * 40, '"new"')
        with mock.patch.object(bridge.http.client, "HTTPSConnection", side_effect=[broken, recovered]):
            watcher = bridge.GitHubRefWatcher(FakeGit(), self.settings)
            with self.assertRaises(bridge.BridgeError):
                watcher.check()
            self.assertEqual(watcher.check(), "c" * 40)
            watcher.close()
        broken.close.assert_called_once()
        recovered.close.assert_called_once()

    def test_closed_keepalive_connection_is_retried_immediately(self) -> None:
        broken = mock.Mock()
        broken.request.side_effect = ConnectionResetError("connection closed")
        recovered = mock.Mock()
        recovered.getresponse.return_value = self.response_for("f" * 40, '"ref"')
        with mock.patch.object(bridge.http.client, "HTTPSConnection", side_effect=[broken, recovered]):
            watcher = bridge.GitHubRefWatcher(FakeGit(), self.settings)
            self.assertEqual(watcher.check(), "f" * 40)
            watcher.close()
        broken.close.assert_called_once()
        recovered.close.assert_called_once()

    def test_rate_limit_honors_retry_after_and_closes_connection(self) -> None:
        connection = mock.Mock()
        connection.getresponse.return_value = Response(429, headers={"Retry-After": "125"})
        with mock.patch.object(bridge.http.client, "HTTPSConnection", return_value=connection):
            watcher = bridge.GitHubRefWatcher(FakeGit(), self.settings)
            with self.assertRaises(bridge.WatchRateLimit) as error:
                watcher.check()
        self.assertEqual(error.exception.retry_seconds, 125)
        connection.close.assert_called_once()

    def test_rate_reset_uses_server_time_when_local_clock_differs(self) -> None:
        connection = mock.Mock()
        connection.getresponse.return_value = Response(403, headers={
            "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "400",
            "Date": "Thu, 01 Jan 1970 00:01:40 GMT",
        })
        with mock.patch.object(bridge.http.client, "HTTPSConnection", return_value=connection):
            watcher = bridge.GitHubRefWatcher(FakeGit(), self.settings)
            with self.assertRaises(bridge.WatchRateLimit) as error:
                watcher.check()
        self.assertEqual(error.exception.retry_seconds, 301)

    def test_server_poll_interval_and_invalid_ref(self) -> None:
        connection = mock.Mock()
        valid = self.response_for("d" * 40, '"ref"')
        valid.headers["X-Poll-Interval"] = "12"
        invalid = Response(200, json.dumps({"name": "wrong", "commit": {"sha": "e" * 40}}).encode())
        connection.getresponse.side_effect = [valid, invalid]
        with mock.patch.object(bridge.http.client, "HTTPSConnection", return_value=connection):
            watcher = bridge.GitHubRefWatcher(FakeGit(), self.settings)
            self.assertEqual(watcher.check(), "d" * 40)
            self.assertEqual(watcher.poll_interval, 12)
            with self.assertRaises(bridge.BridgeError):
                watcher.check()
        connection.close.assert_called_once()

    def test_rejects_non_github_remote_before_reading_credentials(self) -> None:
        with self.assertRaises(bridge.BridgeError):
            bridge.GitHubRefWatcher(FakeGit("https://example.invalid/repo.git"), self.settings)

    def test_repository_scoped_credential_helper(self) -> None:
        root = Path(self.tmp.name)
        repo = root / "control.git"
        helper = root / "credential-helper.sh"
        global_config = root / "empty.gitconfig"
        global_config.write_text("", encoding="utf-8")
        helper.write_text(
            '#!/bin/sh\n'
            '[ "$1" = get ] || exit 0\n'
            'while IFS= read -r line; do\n'
            '  if [ "$line" = "path=example/repository.git" ]; then found=1; fi\n'
            'done\n'
            '[ "$found" = 1 ] && printf "username=git\\npassword=test-only\\n\\n"\n'
            'exit 0\n', encoding="utf-8")
        subprocess.run(["git", "init", "--bare", str(repo)], check=True, capture_output=True)
        for args in (
            ["remote", "add", "origin", "https://github.com/example/repository.git"],
            ["config", "--local", "credential.https://github.com.useHttpPath", "true"],
            ["config", "--local", "--add", "credential.helper", ""],
            ["config", "--local", "--add", "credential.helper", '!sh "%s"' % helper.as_posix()],
        ):
            subprocess.run(["git", "--git-dir", str(repo), *args], check=True, capture_output=True)
        config = root / "scoped.json"
        config.write_text(json.dumps({"git_dir": str(repo), "state_dir": str(root / "state"),
                                      "git_config_global": str(global_config)}), encoding="utf-8")
        settings = bridge.load_settings(config)
        watcher = bridge.GitHubRefWatcher(bridge.GitTransport(settings), settings)
        self.assertEqual(watcher.authorization, "Bearer test-only")


if __name__ == "__main__":
    unittest.main()
