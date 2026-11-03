"""Run an isolated service with a local Git implementation of the API compare-and-swap."""
import os
import subprocess
import tempfile
import time
from pathlib import Path
from unittest import mock

from scripts import green_bridge as bridge


class LocalAtomicTransport:
    def __init__(self, git: bridge.GitTransport, settings: bridge.Settings) -> None:
        self.remote = Path(git.run(["remote", "get-url", settings.remote]).decode("utf-8").strip())
        self.git = settings.git_executable

    def publish(self, branch: str, expected: str, path: str, content: bytes) -> str:
        calls = os.environ.get("PINGPONG_TEST_API_CALLS")
        if calls:
            with Path(calls).open("ab") as output:
                output.write(b"X")
        rate_marker = os.environ.get("PINGPONG_TEST_RATE_LIMIT")
        if rate_marker and Path(rate_marker).exists():
            Path(rate_marker).unlink()
            raise bridge.ApiRateLimit(60)
        content_limit = os.environ.get("PINGPONG_TEST_CONTENT_LIMIT")
        if content_limit and len(content) > int(content_limit):
            raise bridge.ApiPublicationError("synthetic result exceeds the API limit")
        environment = os.environ.copy()
        environment.update({"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"})
        with tempfile.TemporaryDirectory(prefix="atomic-index-") as directory:
            environment["GIT_INDEX_FILE"] = str(Path(directory) / "index")

            def run(args, data=None, check=True):
                result = subprocess.run([self.git, "--git-dir", str(self.remote), *args], input=data,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        env=environment, check=False)
                if check and result.returncode:
                    raise bridge.ApiPublicationError("local atomic test operation was rejected")
                return result

            run(["read-tree", expected])
            blob = run(["hash-object", "-w", "--stdin"], data=content).stdout.decode().strip()
            run(["update-index", "--add", "--cacheinfo", "100644", blob, path])
            tree = run(["write-tree"]).stdout.decode().strip()
            commit = run(["commit-tree", tree, "-p", expected, "-m", "synthetic atomic result"]).stdout.decode().strip()
            barrier = os.environ.get("PINGPONG_TEST_CAS_BARRIER")
            if barrier:
                folder = Path(barrier)
                (folder / Path(path).name).write_bytes(b"1")
                deadline = time.monotonic() + 10
                while len(list(folder.iterdir())) < 2 and time.monotonic() < deadline:
                    time.sleep(.02)
                if len(list(folder.iterdir())) < 2:
                    raise bridge.ApiPublicationError("local atomic test barrier timed out")
            run(["update-ref", "refs/heads/" + branch, commit, expected])
        marker = os.environ.get("PINGPONG_TEST_LOST_ACK")
        if marker and Path(marker).exists():
            Path(marker).unlink()
            raise bridge.ApiPublicationError("synthetic acknowledgement loss")
        return commit

    def close(self) -> None:
        return None


if __name__ == "__main__":
    with mock.patch.object(bridge, "GitHubAtomicTransport", LocalAtomicTransport):
        raise SystemExit(bridge.main())
