#!/usr/bin/env python3
"""Create or update a PBKDF2 Basic Auth file for Studio campus mode.

The file is intentionally a tiny, line-oriented format understood by the
Studio boundary: ``username:pbkdf2-sha256$iterations$salt$digest``.  Passwords
are read from the terminal and never appear in command arguments or logs.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import os
import secrets
import stat
import tempfile
from pathlib import Path


ITERATIONS = 310_000


def _hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    encode = lambda value: base64.b64encode(value).decode("ascii")
    return f"pbkdf2-sha256${ITERATIONS}${encode(salt)}${encode(digest)}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, required=True, help="凭据文件路径")
    parser.add_argument("--username", required=True, help="账号名（不含冒号）")
    args = parser.parse_args(argv)
    username = args.username.strip()
    if not username or ":" in username or "\n" in username or "\r" in username:
        parser.error("用户名不能为空且不能包含冒号或换行")
    password = getpass.getpass("密码: ")
    if not password:
        parser.error("密码不能为空")
    if password != getpass.getpass("再次输入密码: "):
        parser.error("两次密码不一致")
    target = args.file.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if target.exists():
        try:
            lines = target.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            parser.error(f"无法读取凭据文件: {exc}")
    replacement = f"{username}:{_hash(password)}"
    found = False
    output = []
    for line in lines:
        if line.strip() and not line.lstrip().startswith("#") and ":" in line:
            existing = line.split(":", 1)[0]
            if existing == username:
                if not found:
                    output.append(replacement)
                    found = True
                continue
        output.append(line)
    if not found:
        output.append(replacement)
    content = "\n".join(output).rstrip("\n") + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent), text=True)
    try:
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    if os.name != "nt":
        os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
    print(f"已更新 Basic Auth 凭据文件: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
