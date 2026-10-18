#!/usr/bin/env python3
"""Reuse this workspace's private GitHub credentials without echoing them."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "Asilences/Vibe-Novel"


def settings():
    path = ROOT / "config" / "github.local.json"
    with path.open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("配置超过上限")
    data = json.loads(raw.decode("utf-8-sig"))
    token = data["token"]
    if (not isinstance(token, str) or not token or not token.isascii()
            or any(ord(char) <= 32 or ord(char) == 127 for char in token)
            or data["repository"] != REPOSITORY
            or data["proxy_url"] != "http://127.0.0.1:7890"):
        raise ValueError("配置字段无效")
    return data


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main(args):
    try:
        data = settings()
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        print("无法读取本机 GitHub 配置，请检查 config/github.local.json；凭据未输出。", file=sys.stderr)
        return 2
    if args == ["check"]:
        request = urllib.request.Request("https://api.github.com/user", headers={
            "Authorization": "Bearer " + data["token"], "Accept": "application/vnd.github+json",
            "User-Agent": "VibeNovel-local-workflow"})
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": data["proxy_url"], "https": data["proxy_url"]}), NoRedirect())
        try:
            with opener.open(request, timeout=20) as response:
                result = json.loads(response.read(1048576).decode("utf-8"))
                print(json.dumps({"authenticated": True, "account": result.get("login"),
                                  "scopes": response.headers.get("X-OAuth-Scopes", "unknown")}, ensure_ascii=False))
            return 0
        except urllib.error.HTTPError as exc:
            print("GitHub 认证检查失败（HTTP %s），凭据未输出。" % exc.code, file=sys.stderr)
            exc.close()
            return 1
        except (OSError, ValueError):
            print("GitHub 认证检查连接失败或响应无效，请确认本机7890代理。", file=sys.stderr)
            return 1
    if not args or args[0] not in ("fetch", "push", "ls-remote"):
        print("用法：github_local.py check | fetch origin | push -u origin <分支> | ls-remote origin", file=sys.stderr)
        return 2
    allowed_flags = {"push": {"-u", "--set-upstream", "--dry-run"}, "fetch": set(),
                     "ls-remote": {"--exit-code", "--heads", "--tags"}}
    positional = [argument for argument in args[1:] if not argument.startswith("-")]
    if (any(argument not in allowed_flags[args[0]] for argument in args[1:] if argument.startswith("-"))
            or not 1 <= len(positional) <= 2 or positional[0] != "origin"
            or any(":" in argument or "\\" in argument or argument.startswith("/") for argument in positional[1:])
            or (args[0] == "push" and len(positional) != 2)):
        print("仅允许对 origin 执行普通 fetch、分支 push 或 ls-remote；不接受其他地址或强制/删除操作。", file=sys.stderr)
        return 2
    # Restrict inherited remotes and scoped authentication to this repository.
    try:
        remote = subprocess.check_output(["git", "remote", "get-url", "origin"], cwd=ROOT,
                                         stderr=subprocess.DEVNULL, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        print("未找到本仓库的 origin。", file=sys.stderr)
        return 2
    if remote not in ("https://github.com/" + REPOSITORY + ".git", "https://github.com/" + REPOSITORY):
        print("origin 与工作文档中的仓库不一致，未使用凭据。", file=sys.stderr)
        return 2
    # Git config environment applies to the child only; avoid command-line
    # credentials, persistent helpers, prompts and inherited HTTP trace logs.
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("GIT_CONFIG_", "GIT_TRACE")) and key != "GIT_CURL_VERBOSE"}
    encoded = base64.b64encode(("x-access-token:" + data["token"]).encode("ascii")).decode("ascii")
    config = [("http.proxy", data["proxy_url"]), ("http.https://github.com/.extraheader", "Authorization: Basic " + encoded),
              ("credential.helper", ""), ("http.followRedirects", "false")]
    environment["GIT_CONFIG_COUNT"] = str(len(config))
    for index, (key, value) in enumerate(config):
        environment["GIT_CONFIG_KEY_%d" % index] = key
        environment["GIT_CONFIG_VALUE_%d" % index] = value
    environment.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")
    try:
        return subprocess.run(["git", *args], cwd=ROOT, env=environment, check=False).returncode
    except OSError:
        print("无法启动 Git。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
