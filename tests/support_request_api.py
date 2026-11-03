"""Run a client using an isolated local Git compare-and-swap transport."""
import os
import subprocess
import tempfile
import time
from pathlib import Path
from unittest import mock

from scripts import green_bridge as bridge


class LocalRequestTransport:
    def __init__(self, git: bridge.GitTransport, settings: bridge.Settings) -> None:
        if os.environ.get("PINGPONG_REQUEST_API_UNAVAILABLE"):
            raise bridge.ApiPublicationError("synthetic API is unavailable")
        self.remote = Path(git.run(["remote", "get-url", settings.remote]).decode("utf-8").strip())
        self.git = settings.git_executable

    def run(self, *arguments: str, data: bytes = None, check: bool = True,
            env: dict = None) -> subprocess.CompletedProcess:
        result = subprocess.run([self.git, "--git-dir", str(self.remote), *arguments], input=data,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, check=False)
        if check and result.returncode:
            raise bridge.ApiPublicationError("local API operation failed")
        return result

    def snapshot(self, branch: str, group_id: str, known_heads: list) -> tuple:
        calls = os.environ.get("PINGPONG_REQUEST_API_READS")
        if calls:
            with Path(calls).open("ab") as output:
                output.write(b"X")
        rate_marker = os.environ.get("PINGPONG_REQUEST_API_RATE_LIMIT")
        if rate_marker and Path(rate_marker).exists():
            Path(rate_marker).unlink()
            raise bridge.ApiRateLimit(120)
        head = self.run("rev-parse", "refs/heads/" + branch).stdout.decode().strip()
        for previous in known_heads:
            if self.run("merge-base", "--is-ancestor", previous, head, check=False).returncode:
                raise bridge.ApiPublicationError("local request history differs")
        path = bridge.GROUP_REQUEST_PREFIX + group_id + ".json"
        result = self.run("show", head + ":" + path, check=False)
        return head, result.stdout if result.returncode == 0 else None

    def publish(self, branch: str, expected: str, path: str, content: bytes, headline: str) -> str:
        calls = os.environ.get("PINGPONG_REQUEST_API_CALLS")
        if calls:
            with Path(calls).open("ab") as output:
                output.write(b"X")
        environment = os.environ.copy()
        environment.update({"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"})
        with tempfile.TemporaryDirectory(prefix="request-api-index-") as directory:
            environment["GIT_INDEX_FILE"] = str(Path(directory) / "index")
            self.run("read-tree", expected, env=environment)
            blob = self.run("hash-object", "-w", "--stdin", data=content, env=environment).stdout.decode().strip()
            self.run("update-index", "--add", "--cacheinfo", "100644", blob, path, env=environment)
            tree = self.run("write-tree", env=environment).stdout.decode().strip()
            commit = self.run("commit-tree", tree, "-p", expected, "-m", headline,
                              env=environment).stdout.decode().strip()
            barrier = os.environ.get("PINGPONG_REQUEST_API_BARRIER")
            if barrier:
                folder = Path(barrier)
                (folder / str(os.getpid())).write_bytes(b"1")
                deadline = time.monotonic() + 10
                while len(list(folder.iterdir())) < 2 and time.monotonic() < deadline:
                    time.sleep(0.02)
                if len(list(folder.iterdir())) < 2:
                    raise bridge.ApiPublicationError("local API barrier timed out")
            result = self.run("update-ref", "refs/heads/" + branch, commit, expected,
                              env=environment, check=False)
            if result.returncode:
                raise bridge.ApiHeadConflict("local API branch head changed")
        marker = os.environ.get("PINGPONG_REQUEST_API_LOST_ACK")
        if marker and Path(marker).exists():
            Path(marker).unlink()
            raise bridge.ApiPublicationError("synthetic acknowledgement loss")
        return commit

    def close(self) -> None:
        return None


if __name__ == "__main__":
    with mock.patch.object(bridge, "GitHubRequestTransport", LocalRequestTransport):
        raise SystemExit(bridge.main())
