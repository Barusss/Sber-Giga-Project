#!/usr/bin/env python3
"""GitHub Actions -> GREF INDEX. Execute commands via authenticated SSH."""

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

COMMANDS_PATH = Path(".gref/commands")


def ssh_options():
    host = os.environ.get("GREF_SSH_HOST", "")
    user = os.environ.get("GREF_SSH_USER", "")
    port = os.environ.get("GREF_SSH_PORT", "22") or "22"
    script = os.environ.get("GREF_SSH_SCRIPT_PATH", "")
    key = Path.home() / ".ssh" / "gref_actions"
    known_hosts = Path.home() / ".ssh" / "known_hosts"
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]{0,253}", host):
        raise RuntimeError("Set valid GREF_SSH_HOST")
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]{0,31}", user):
        raise RuntimeError("Set valid GREF_SSH_USER")
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise RuntimeError("Invalid SSH port")
    if (not script or not script.endswith("/private/gref_cli.php")
            or ".." in Path(script).parts):
        raise RuntimeError("Set GREF_SSH_SCRIPT_PATH to actual private/gref_cli.php path")
    if not key.is_file() or not known_hosts.is_file():
        raise RuntimeError("Missing SSH key or trusted host fingerprint")
    return host, user, port, script, key, known_hosts


def api_call(action: str, payload=None) -> dict:
    host, user, port, script, key, known_hosts = ssh_options()
    remote = "php8.2 " + shlex.quote(script)
    if payload is None:
        remote += " --check"
    request = [
        "ssh", "-T", "-p", port, "-i", str(key),
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "UserKnownHostsFile=" + str(known_hosts),
        "-o", "ConnectTimeout=20",
        user + "@" + host, remote
    ]
    data = None
    if payload is not None:
        if action == "task":
            task_fields = payload["task"]
            command = {"action": action, **task_fields, "requestId": payload["requestId"]}
        else:
            command = {"action": action, **payload}
        data = json.dumps(command, ensure_ascii=False).encode("utf-8")
    completed = subprocess.run(
        request, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=55, check=False
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError("SSH/PHP failed: " + detail[:500])
    try:
        answer = json.loads(completed.stdout.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise RuntimeError("SSH/PHP did not return JSON") from None
    if not isinstance(answer, dict) or answer.get("ok") is not True:
        raise RuntimeError("GREF CLI rejected the operation")
    return answer


def check_connection():
    check = api_call("check")
    if check.get("mode") != "check":
        raise RuntimeError("Beget is missing the updated gref_cli.php")
    print("GREF INDEX: SSH, PHP and MySQL connection OK.")


def check_command(path: Path) -> tuple[str, dict]:
    raw = path.read_text(encoding="utf-8")
    if len(raw.encode("utf-8")) > 6000:
        raise RuntimeError(f"Command file too large: {path}")
    obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise RuntimeError(f"Not a JSON object: {path}")
    kind = obj.get("type")
    if kind == "rating":
        if set(obj) != {"type", "member", "delta", "reason"}:
            raise RuntimeError("Rating command needs exactly type/member/delta/reason")
        if obj["member"] not in ("mate1", "mate2"):
            raise RuntimeError("Rating is editable only for Stepan/Danil")
        delta = obj["delta"]
        if type(delta) is not int or not -500 <= delta <= 500 or delta == 0:
            raise RuntimeError("Invalid rating delta")
        reason = obj["reason"]
        if not isinstance(reason, str) or not 3 <= len(reason) <= 200:
            raise RuntimeError("Invalid reason")
        return "rating", {"member": obj["member"], "delta": delta, "reason": reason}
    if kind == "task":
        if set(obj) != {"type", "task"} or not isinstance(obj["task"], dict):
            raise RuntimeError("Task command needs type/task")
        task = obj["task"]
        op = task.get("operation")
        if op == "delete":
            if set(task) != {"operation", "id"} or type(task["id"]) is not int or task["id"] <= 0:
                raise RuntimeError("Invalid delete task command")
            return "task", task
        expected = {"operation", "title", "description", "assignee", "deadline", "priority", "status", "githubUrl"}
        if op != "save" or not set(task).issubset(expected | {"id"}) or not expected.issubset(task):
            raise RuntimeError("Invalid task save fields")
        if task["assignee"] not in ("owner", "mate1", "mate2"):
            raise RuntimeError("Invalid assignee")
        if task["priority"] not in ("normal", "high", "critical"):
            raise RuntimeError("Invalid priority")
        if task["status"] not in ("todo", "progress", "done"):
            raise RuntimeError("Invalid status")
        if not isinstance(task["title"], str) or not 1 <= len(task["title"]) <= 150:
            raise RuntimeError("Invalid task title")
        if not isinstance(task["description"], str) or len(task["description"]) > 1500:
            raise RuntimeError("Invalid task description")
        if not isinstance(task["githubUrl"], str) or len(task["githubUrl"]) > 350:
            raise RuntimeError("Invalid GitHub URL")
        if not isinstance(task["deadline"], str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", task["deadline"]):
            raise RuntimeError("Invalid deadline")
        if "id" in task and (type(task["id"]) is not int or task["id"] <= 0):
            raise RuntimeError("Invalid task ID")
        return "task", task
    raise RuntimeError("Unknown command type")


def new_command_paths() -> list[Path]:
    before = os.environ["BEFORE_SHA"]
    after = os.environ["AFTER_SHA"]
    if not re.fullmatch(r"[0-9a-f]{40}", before) or not re.fullmatch(r"[0-9a-f]{40}", after):
        raise RuntimeError("Invalid Git SHA from workflow")
    if before == "0" * 40:
        output = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", after, "--", str(COMMANDS_PATH)])
    else:
        output = subprocess.check_output(["git", "diff", "--diff-filter=A", "--name-only", before, after, "--", str(COMMANDS_PATH)])
    paths = [Path(line) for line in output.decode().splitlines() if line.endswith(".json")]
    for path in paths:
        if path.parent != COMMANDS_PATH or not path.is_file() or path.is_symlink():
            raise RuntimeError(f"Unexpected command file: {path}")
    return sorted(paths)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--apply-new", action="store_true")
    args = parser.parse_args()
    check_connection()
    if args.check and not args.apply_new:
        return
    if not args.apply_new:
        parser.error("Choose --check or --apply-new")
    paths = new_command_paths()
    print(f"New GREF commands: {len(paths)}")
    for path in paths:
        action, payload = check_command(path)
        request_id = "github-" + hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:48]
        payload = {**payload, "requestId": request_id}
        result = api_call(action, payload)
        if result.get("requestId") != request_id:
            raise RuntimeError("GREF INDEX did not return the request identifier; aborting")
        if result.get("duplicate") is True:
            print(f"SKIPPED (already applied): {path}")
        elif result.get("applied") is True:
            print(f"APPLIED: {path}")
        else:
            raise RuntimeError("GREF API returned an unexpected operation result")
    print("GREF INDEX commands completed successfully.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, json.JSONDecodeError) as error:
        print("ERROR: " + str(error), file=sys.stderr)
        sys.exit(1)
