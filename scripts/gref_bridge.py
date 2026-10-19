#!/usr/bin/env python3
"""GitHub Actions -> GREF INDEX. HTTP writes are performed only from CI."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

SITE_URL = "https://ttvvgg5r.beget.tech/api.php"
COMMANDS_PATH = Path(".gref/commands")


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


http = build_opener(NoRedirects())


def api_call(action: str, payload=None) -> dict:
    url = SITE_URL + "?action=" + action
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
        headers["X-API-Key"] = os.environ["GREF_API_KEY"]
    request = Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        with http.open(request, timeout=20) as response:
            content = response.read(20000).decode("utf-8")
            if response.status != 200:
                raise RuntimeError("GREF API: HTTP " + str(response.status))
    except HTTPError as exc:
        raise RuntimeError(f"GREF API returned HTTP {exc.code} ({action})") from None
    except URLError as exc:
        raise RuntimeError(f"Cannot connect to GREF INDEX over HTTPS: {exc.reason}") from None
    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        raise RuntimeError("GREF API returned non-JSON response") from None
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("GREF API rejected request: " + str(result.get("error", "unknown error")))
    return result


def check_connection():
    if not os.environ.get("GREF_API_KEY"):
        raise RuntimeError("Missing GitHub Actions secret GREF_API_KEY")
    state = api_call("state")
    if not isinstance(state.get("capabilities"), dict) or not state["capabilities"].get("requestId"):
        raise RuntimeError("Old api.php on Beget: upload updated api.php before running commands")
    print("GREF INDEX: HTTPS OK, API version supports safe retries.")


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
