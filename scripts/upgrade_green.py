#!/usr/bin/env python3
"""Stage a verified Windows service update and restart after its job is published."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

COMMIT = re.compile(r"^[0-9a-f]{40}$")
IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")


class UpgradeError(RuntimeError):
    """The update could not be staged or safely activated."""


def read_config(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpgradeError("Cannot read service configuration") from exc
    if not isinstance(value, dict):
        raise UpgradeError("Invalid service configuration")
    return value


def normalized_hash(data: bytes) -> str:
    try:
        text = data.decode("utf-8-sig").replace("\r\n", "\n")
    except UnicodeDecodeError as exc:
        raise UpgradeError("Service source is not UTF-8") from exc
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def git_output(config: Dict[str, Any], arguments: List[str]) -> bytes:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "Never"
    if config.get("git_config_global"):
        env["GIT_CONFIG_GLOBAL"] = str(config["git_config_global"])
    argv = [str(config.get("git_executable") or "git"), "--git-dir", str(config["git_dir"]), *arguments]
    try:
        process = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 env=env, timeout=90, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpgradeError("Git update operation could not complete") from exc
    if process.returncode:
        raise UpgradeError("Git update operation failed: " + arguments[0])
    return process.stdout


def powershell(config: Dict[str, Any], script: str) -> None:
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    argv = [str(config.get("powershell_executable") or "powershell.exe"), "-NoLogo", "-NoProfile",
            "-NonInteractive", "-EncodedCommand", encoded]
    try:
        process = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpgradeError("Scheduled-task operation could not complete") from exc
    if process.returncode:
        raise UpgradeError("Scheduled-task operation failed")


def ps_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def watcher_registration(config: Dict[str, Any], helper: Path, config_path: Path,
                         new_commit: str, job_id: str, task_name: str) -> str:
    for path in (str(helper), str(config_path)):
        if '"' in path:
            raise UpgradeError("Double quotes in update paths are unsupported")
    argument = ('"%s" --config "%s" --new-commit %s --job-id %s --watch' %
                (helper, config_path, new_commit, job_id))
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        "$name = " + ps_literal(task_name),
        "if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) { throw 'TASK_EXISTS' }",
        "$action = New-ScheduledTaskAction -Execute " + ps_literal(sys.executable) + " -Argument " + ps_literal(argument),
        "$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1)",
        "$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name",
        "$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited",
        "$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -StartWhenAvailable",
        "Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null",
        "Start-ScheduledTask -TaskName $name",
    ])


def service_start_script(config_path: Path, service_path: Path) -> str:
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        "$service = " + ps_literal(str(service_path)),
        "$config = " + ps_literal(str(config_path)),
        "Start-ScheduledTask -TaskName 'BlueGreenPingpong'",
        "$deadline = (Get-Date).AddSeconds(45)",
        "do { Start-Sleep -Seconds 2; $task = Get-ScheduledTask -TaskName 'BlueGreenPingpong'; "
        "$newProcess = @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and "
        "$_.CommandLine.IndexOf($service,[StringComparison]::OrdinalIgnoreCase) -ge 0 -and "
        "$_.CommandLine.IndexOf($config,[StringComparison]::OrdinalIgnoreCase) -ge 0 }); "
        "if ($task.State -eq 'Running' -and $newProcess.Count -eq 1) { exit 0 } } while ((Get-Date) -lt $deadline)",
        "throw 'SERVICE_DID_NOT_START'",
    ])


def service_health_script(config_path: Path, service_path: Path) -> str:
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        "$service = " + ps_literal(str(service_path)),
        "$config = " + ps_literal(str(config_path)),
        "Start-Sleep -Seconds 5",
        "$task = Get-ScheduledTask -TaskName 'BlueGreenPingpong'",
        "$processes = @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and "
        "$_.CommandLine.IndexOf($service,[StringComparison]::OrdinalIgnoreCase) -ge 0 -and "
        "$_.CommandLine.IndexOf($config,[StringComparison]::OrdinalIgnoreCase) -ge 0 })",
        "if ($task.State -ne 'Running' -or $processes.Count -ne 1) { throw 'SERVICE_UNHEALTHY' }",
    ])


def service_stop_script(config_path: Path, service_path: Path) -> str:
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        "$service = " + ps_literal(str(service_path)),
        "$config = " + ps_literal(str(config_path)),
        "$serviceProcesses = @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and "
        "$_.CommandLine.IndexOf($service,[StringComparison]::OrdinalIgnoreCase) -ge 0 -and "
        "$_.CommandLine.IndexOf($config,[StringComparison]::OrdinalIgnoreCase) -ge 0 })",
        "if ($serviceProcesses.Count -gt 1) { throw 'SERVICE_PROCESS_AMBIGUOUS' }",
        "$old = if ($serviceProcesses.Count -eq 1) { $serviceProcesses[0] } else { $null }",
        "$task = Get-ScheduledTask -TaskName 'BlueGreenPingpong'",
        "if ($task.State -eq 'Running') { Stop-ScheduledTask -TaskName 'BlueGreenPingpong' }",
        "Start-Sleep -Seconds 3",
        "if ($old) { $remaining = Get-CimInstance Win32_Process -Filter (\"ProcessId = {0}\" -f $old.ProcessId) "
        "-ErrorAction SilentlyContinue; if ($remaining -and $remaining.CreationDate -eq $old.CreationDate -and "
        "$remaining.CommandLine -and $remaining.CommandLine.IndexOf($service,[StringComparison]::OrdinalIgnoreCase) -ge 0 "
        "-and $remaining.CommandLine.IndexOf($config,[StringComparison]::OrdinalIgnoreCase) -ge 0) "
        "{ Stop-Process -Id ([int]$old.ProcessId) -Force -ErrorAction Stop } }",
    ])


def replace_service(service: Path, source: bytes) -> None:
    fd, name = tempfile.mkstemp(prefix=".pingpong-upgrade-", suffix=".tmp", dir=str(service.parent))
    try:
        with os.fdopen(fd, "wb") as target:
            target.write(source)
        os.replace(name, service)
    except OSError as exc:
        raise UpgradeError("Could not replace service source") from exc


def stage(config: Dict[str, Any], config_path: Path, base_commit: str, new_commit: str, job_id: str) -> None:
    if sys.platform != "win32":
        raise UpgradeError("Automatic service upgrade requires Windows")
    adapter = Path(config["adapter_argv"][1])
    service = adapter.parent / "green_bridge.py"
    if not service.is_file():
        raise UpgradeError("Service script is not next to the configured adapter")
    remote = str(config.get("remote") or "origin")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", remote):
        raise UpgradeError("Invalid Git remote name")
    git_output(config, ["fetch", "--no-tags", remote, "refs/heads/main"])
    old_source = git_output(config, ["show", base_commit + ":scripts/green_bridge.py"])
    new_source = git_output(config, ["show", new_commit + ":scripts/green_bridge.py"])
    helper_source = git_output(config, ["show", new_commit + ":scripts/upgrade_green.py"])
    previous_source = service.read_bytes()
    if normalized_hash(previous_source) != normalized_hash(old_source):
        raise UpgradeError("Installed service does not match the expected baseline")
    try:
        compile(new_source, str(service), "exec")
        compile(helper_source, "upgrade_green.py", "exec")
    except SyntaxError as exc:
        raise UpgradeError("New service or updater has invalid Python syntax") from exc
    directory = Path(config["state_dir"]) / "upgrades" / job_id
    directory.mkdir(parents=True, exist_ok=False)
    helper = directory / "upgrade_green.py"
    helper.write_bytes(helper_source)
    (directory / "previous_service.py").write_bytes(previous_source)
    task_name = "PingpongUpgrade-" + job_id
    powershell(config, watcher_registration(config, helper, config_path, new_commit, job_id, task_name))
    replaced = False
    try:
        replace_service(service, new_source)
        replaced = True
        (directory / "ready.json").write_text(json.dumps({"commit": new_commit, "sha256": normalized_hash(new_source),
                                                           "previous_sha256": normalized_hash(previous_source)}),
                                              encoding="utf-8")
    except (UpgradeError, OSError) as exc:
        if replaced:
            try:
                replace_service(service, previous_source)
            except UpgradeError as restore_exc:
                raise UpgradeError("Staging failed and previous service could not be restored") from restore_exc
        raise UpgradeError("Could not complete service staging") from exc
    print("UPGRADE_STAGED=" + new_commit)


def job_ready(database: Path, job_id: str) -> Optional[bool]:
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=3)
    try:
        row = connection.execute("SELECT state, published_state FROM jobs WHERE id = ?", (job_id,)).fetchone()
        active = connection.execute("SELECT count(*) FROM jobs WHERE state IN ('queued', 'accepted', 'running')").fetchone()[0]
        pending = connection.execute("SELECT count(*) FROM jobs WHERE published_state != state").fetchone()[0]
    finally:
        connection.close()
    if row is None:
        return None
    if row[0] in {"failed", "needs_input", "unknown"}:
        return False
    if row[0] == "completed" and row[1] == "completed" and active == 0 and pending == 0:
        return True
    return None


def when_quiescent(database: Path, job_id: str, action: Callable[[], None]) -> bool:
    """Prevent a new job from starting while the service is being restarted."""
    connection = sqlite3.connect(database, timeout=3)
    try:
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower():
                return False  # A busy service will be checked again.
            raise
        row = connection.execute("SELECT state, published_state FROM jobs WHERE id = ?", (job_id,)).fetchone()
        active = connection.execute("SELECT count(*) FROM jobs WHERE state IN ('queued', 'accepted', 'running')").fetchone()[0]
        pending = connection.execute("SELECT count(*) FROM jobs WHERE published_state != state").fetchone()[0]
        if row is None or row[0] != "completed" or row[1] != "completed" or active or pending:
            return False
        action()
        return True
    finally:
        connection.close()


def watch(config: Dict[str, Any], config_path: Path, new_commit: str, job_id: str) -> bool:
    directory = Path(config["state_dir"]) / "upgrades" / job_id
    marker = directory / "ready.json"
    log = directory / "watch.log"
    deadline = time.monotonic() + 720
    service = Path(config["adapter_argv"][1]).parent / "green_bridge.py"
    restart_started = False
    recorded: Optional[Dict[str, Any]] = None
    try:
        while time.monotonic() < deadline:
            ready = job_ready(Path(config["state_dir"]) / "state.sqlite3", job_id)
            if ready is False:
                raise UpgradeError("Upgrade job did not finish successfully")
            if ready and marker.is_file():
                recorded = json.loads(marker.read_text(encoding="utf-8"))
                if recorded.get("commit") != new_commit or recorded.get("sha256") != normalized_hash(service.read_bytes()):
                    raise UpgradeError("Staged service verification failed")
                def stop() -> None:
                    nonlocal restart_started
                    restart_started = True
                    powershell(config, service_stop_script(config_path, service))

                if not when_quiescent(Path(config["state_dir"]) / "state.sqlite3", job_id, stop):
                    time.sleep(2)
                    continue
                powershell(config, service_start_script(config_path, service))
                powershell(config, service_health_script(config_path, service))
                log.write_text("SERVICE_RESTARTED\n", encoding="ascii")
                return True
            time.sleep(2)
        raise UpgradeError("Timed out waiting for the published upgrade result")
    except (UpgradeError, OSError, ValueError, sqlite3.Error) as exc:
        log.write_text("UPGRADE_FAILED: " + str(exc) + "\n", encoding="utf-8")
        if restart_started:
            try:
                previous = (directory / "previous_service.py").read_bytes()
                if (recorded is None or recorded.get("previous_sha256") != normalized_hash(previous) or
                        recorded.get("sha256") != normalized_hash(service.read_bytes())):
                    raise UpgradeError("Previous service verification failed")

                def rollback() -> None:
                    powershell(config, service_stop_script(config_path, service))
                    replace_service(service, previous)

                if not when_quiescent(Path(config["state_dir"]) / "state.sqlite3", job_id, rollback):
                    raise UpgradeError("Active jobs prevent automatic rollback")
                powershell(config, service_start_script(config_path, service))
                powershell(config, service_health_script(config_path, service))
                with log.open("a", encoding="utf-8") as stream:
                    stream.write("PREVIOUS_SERVICE_RESTORED\n")
            except (UpgradeError, OSError, sqlite3.Error):
                with log.open("a", encoding="utf-8") as stream:
                    stream.write("Automatic rollback was not completed.\n")
                try:
                    powershell(config, "$task = Get-ScheduledTask -TaskName 'BlueGreenPingpong'; "
                               "if ($task.State -ne 'Running') { Start-ScheduledTask -TaskName 'BlueGreenPingpong' }")
                except UpgradeError:
                    with log.open("a", encoding="utf-8") as stream:
                        stream.write("Could not request a fallback service start.\n")
        return False
    finally:
        name = "PingpongUpgrade-" + job_id
        try:
            powershell(config, "Unregister-ScheduledTask -TaskName " + ps_literal(name) + " -Confirm:$false")
        except UpgradeError:
            with log.open("a", encoding="utf-8") as stream:
                stream.write("Could not remove one-time update task.\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely activate a staged Windows service update")
    parser.add_argument("--config", required=True)
    parser.add_argument("--base-commit")
    parser.add_argument("--new-commit", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    if not COMMIT.fullmatch(args.new_commit) or not IDENTIFIER.fullmatch(args.job_id):
        raise UpgradeError("Invalid update commit or job ID")
    if not args.watch and (not args.base_commit or not COMMIT.fullmatch(args.base_commit)):
        raise UpgradeError("Invalid base commit")
    config_path = Path(args.config).resolve()
    config = read_config(config_path)
    if args.watch:
        return 0 if watch(config, config_path, args.new_commit, args.job_id) else 1
    else:
        stage(config, config_path, args.base_commit, args.new_commit, args.job_id)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (UpgradeError, KeyError, IndexError, OSError, sqlite3.Error) as exc:
        sys.stderr.write("UPGRADE_ERROR: " + str(exc) + "\n")
        raise SystemExit(1)
