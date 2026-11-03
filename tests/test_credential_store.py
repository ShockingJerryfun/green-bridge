"""Exercise the scoped Git Credential Manager optimization with synthetic credentials."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import green_bridge as bridge


@unittest.skipIf(os.name == "nt", "the synthetic credential manager uses a POSIX shell")
class CredentialStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="pingpong-credential-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git_dir = self.root / "control.git"
        subprocess.run(["git", "init", "--bare", str(self.git_dir)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.log = self.root / "helper.log"
        helper = self.root / "git-credential-manager"
        helper.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$1\" >> \"$PINGPONG_TEST_HELPER_LOG\"\n"
            "if [ \"$1\" = get ] && [ \"$PINGPONG_TEST_MISSING\" != 1 ]; then\n"
            " printf 'username=synthetic\\npassword=%s\\n' \"${PINGPONG_TEST_PASSWORD:-test-only}\"\n"
            "fi\n",
            encoding="utf-8")
        helper.chmod(0o755)
        self.git_config = self.root / "gitconfig"
        self.git_config.write_text("[credential]\n\thelper = manager\n", encoding="utf-8")
        self.config = self.root / "config.json"
        self.environment = mock.patch.dict(os.environ, {
            "PATH": str(self.root) + os.pathsep + os.environ.get("PATH", ""),
            "PINGPONG_TEST_HELPER_LOG": str(self.log),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def transport(self, skip_store: bool) -> bridge.GitTransport:
        self.config.write_text(json.dumps({"git_dir": str(self.git_dir), "state_dir": str(self.root / "state"),
                                           "git_config_global": str(self.git_config),
                                           "gcm_skip_store": skip_store}), encoding="utf-8")
        return bridge.GitTransport(bridge.load_settings(self.config))

    def operations(self, skip_store: bool) -> list:
        transport = self.transport(skip_store)
        context = b"protocol=https\nhost=example.invalid\n\n"
        credential = transport.run(["credential", "fill"], data=context)
        self.assertIn(b"username=synthetic\n", credential)
        self.assertIn(b"password=test-only\n", credential)
        transport.run(["credential", "approve"], data=credential)
        transport.run(["credential", "reject"], data=credential)
        return self.log.read_text(encoding="utf-8").splitlines()

    def test_default_credential_manager_keeps_normal_behavior(self) -> None:
        self.assertEqual(self.operations(False), ["get", "store", "erase"])

    def test_optional_optimization_preserves_get_and_erase(self) -> None:
        self.assertEqual(self.operations(True), ["get", "erase"])

    def test_missing_credential_fails_without_fabricating_one(self) -> None:
        transport = self.transport(True)
        with mock.patch.dict(os.environ, {"PINGPONG_TEST_MISSING": "1"}):
            with self.assertRaises(bridge.GitOperationError) as error:
                transport.run(["credential", "fill"], data=b"protocol=https\nhost=example.invalid\n\n")
        self.assertEqual(error.exception.kind, "authentication")
        self.assertEqual(self.log.read_text(encoding="utf-8").splitlines(), ["get"])

    def test_replaced_credential_is_read_on_the_next_operation(self) -> None:
        transport = self.transport(True)
        context = b"protocol=https\nhost=example.invalid\n\n"
        self.assertIn(b"password=test-only\n", transport.run(["credential", "fill"], data=context))
        with mock.patch.dict(os.environ, {"PINGPONG_TEST_PASSWORD": "replacement"}):
            self.assertIn(b"password=replacement\n", transport.run(["credential", "fill"], data=context))


if __name__ == "__main__":
    unittest.main()
