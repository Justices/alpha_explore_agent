"""Run local CLI model tools and normalize their decision envelopes."""

import json
import math
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from uuid import UUID

from alpha_research.state.store import atomic_write_json


CLI_COMMANDS = {
    "codex_cli": "codex",
    "agy_cli": "agy",
    "claude_cli": "claude",
}
_CLI_ENVIRONMENT = {
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR",
    "HOME", "HOMEDRIVE", "HOMEPATH", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    "CODEX_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
    "XDG_STATE_HOME", "LANG", "LC_ALL",
}


class CliModelError(RuntimeError):
    pass


def _command(provider, executable, workspace, prompt, *, persistent=False, thread_id=None):
    if provider == "agy_cli":
        settings = workspace / "agy-settings.json"
        settings.write_text(json.dumps({
            "toolPermission": "strict",
            "permissions": {"deny": [
                "command(*)", "read_file(*)", "write_file(*)",
                "read_url(*)", "execute_url(*)", "mcp(*)",
            ]},
        }), encoding="utf-8")
        return [executable, "--print=", "--input-format=stream-json",
                "--output-format=stream-json", "--sandbox", "--disable-slash-commands",
                f"--settings={settings}"], json.dumps({
                    "event": "user", "message": {"content": prompt},
                }, ensure_ascii=False) + "\n"
    if provider == "claude_cli":
        return [executable, "--print", "--output-format", "json",
                "--no-session-persistence", "--setting-sources", "",
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--tools", ""], prompt
    command = [executable, "-a", "never", "exec", "--sandbox", "read-only"]
    if thread_id:
        command.append("resume")
    command.extend(["--json", "--ignore-user-config", "--skip-git-repo-check"])
    if not persistent:
        command.append("--ephemeral")
    return command, prompt


def _bound_thread(path):
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise CliModelError("Codex thread binding is unreadable") from exc
    if isinstance(record, dict) and record.get("status") == "starting":
        raise CliModelError("Codex thread creation is unresolved for this research")
    try:
        thread_id = record["thread_id"]
        if record["status"] != "bound" or str(UUID(thread_id)) != thread_id:
            raise ValueError("invalid thread binding")
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise CliModelError("Codex thread binding is invalid") from exc
    return thread_id


def _thread_from_output(stdout):
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", errors="replace")
    for line in (stdout or "").splitlines():
        try:
            event = json.loads(line)
            if event.get("type") == "thread.started":
                thread_id = event["thread_id"]
                if str(UUID(thread_id)) == thread_id:
                    return thread_id
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    return None


def _verify_thread(path, expected, stdout):
    found = _thread_from_output(stdout)
    if expected is not None and found != expected:
        raise CliModelError("Codex did not confirm the bound research thread")
    if found is None:
        raise CliModelError("Codex did not return a research thread ID")
    if expected is None:
        atomic_write_json(path, {"status": "bound", "thread_id": found})
    return found


def _response(provider, stdout):
    if provider == "claude_cli":
        result = json.loads(stdout)
        if result["type"] != "result" or result["subtype"] != "success" or result.get("is_error"):
            raise CliModelError("Claude CLI did not complete a decision")
        return result["result"], result.get("usage"), result.get("model")
    events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    if provider == "agy_cli":
        result, = [event["result"] for event in events if event.get("event") == "result"]
        if result["status"] != "SUCCESS":
            raise CliModelError("AGY CLI did not complete a decision")
        return result["response"], result.get("usage"), result.get("model")
    completed, = [event for event in events if event.get("type") == "turn.completed"]
    messages = [event["item"]["text"] for event in events
                if event.get("type") == "item.completed"
                and event["item"].get("type") == "agent_message"]
    return messages[-1], completed.get("usage"), completed.get("model")


class LocalCliModelTool:
    def __init__(self, config, environ):
        self.config = config
        self.environ = environ

    def invoke(self, prompt, *, thread_path=None):
        provider = self.config["provider"]
        name = self.config.get("executable") or CLI_COMMANDS[provider]
        if not isinstance(name, str):
            raise CliModelError("local model executable must be a path or program name")
        timeout = self.config.get("timeout_seconds", 180)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise CliModelError("local model timeout must be positive")
        model = self.config.get("model")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            raise CliModelError("local model name must be nonempty")
        env = {key: value for key, value in self.environ.items()
               if key.upper() in _CLI_ENVIRONMENT}
        executable = shutil.which(name + ".exe" if os.name == "nt" and not Path(name).suffix else name,
                                  path=env.get("PATH"))
        if not executable:
            raise CliModelError(f"{provider} executable unavailable")
        if os.name == "nt" and Path(executable).suffix.lower() != ".exe":
            raise CliModelError(f"{provider} requires a native .exe on Windows")
        thread_id = _bound_thread(thread_path) if thread_path else None
        try:
            with TemporaryDirectory(prefix="alpha-model-") as directory:
                workspace = Path(directory)
                if thread_path:
                    workspace = thread_path.parent / "workspace"
                    workspace.mkdir(parents=True, exist_ok=True)
                args, stdin = _command(provider, executable, workspace, prompt,
                                       persistent=thread_path is not None, thread_id=thread_id)
                if model:
                    args.extend(["--model", model])
                if provider == "codex_cli":
                    if thread_id:
                        args.append(thread_id)
                    args.append("-")
                if thread_path and thread_id is None:
                    with thread_path.open("x", encoding="utf-8") as pending:
                        json.dump({"status": "starting"}, pending)
                process = subprocess.run(
                    args, input=stdin, cwd=workspace, env=env, encoding="utf-8",
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            if thread_path and _thread_from_output(exc.stdout):
                _verify_thread(thread_path, thread_id, exc.stdout)
            raise CliModelError(f"{provider} timed out") from exc
        except (OSError, UnicodeError) as exc:
            raise CliModelError(f"{provider} could not execute") from exc
        if thread_path and (not process.returncode or _thread_from_output(process.stdout)):
            thread_id = _verify_thread(thread_path, thread_id, process.stdout)
        if process.returncode:
            raise CliModelError(f"{provider} exited with code {process.returncode}")
        try:
            message, usage, reported_model = _response(provider, process.stdout)
            return message, usage, reported_model, thread_id
        except (AttributeError, KeyError, IndexError, ValueError, TypeError) as exc:
            raise CliModelError(f"{provider} returned an invalid decision envelope") from exc
