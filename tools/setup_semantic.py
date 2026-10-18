#!/usr/bin/env python3
"""Explicit installation of pinned public model assets. Never reads novel text.

Inference uses only local files. Downloads do not use API/GitHub credentials.
"""
from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vibenovel.core.files import atomic_write, digest, safe_path  # noqa: E402
from vibenovel.core.semantic import (MODEL, REVISION, MODEL_SHA256, TOKENIZER_SHA256,
                                     MODEL_DIRECTORY)  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="下载并校验本地语义模型（约24MB）")
    parser.add_argument("--proxy", help="可选 HTTP(S) 代理，例如 http://127.0.0.1:7890")
    args = parser.parse_args()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(
        {"http": args.proxy, "https": args.proxy}) if args.proxy else urllib.request.ProxyHandler())
    directory = safe_path(ROOT, MODEL_DIRECTORY)
    directory.mkdir(parents=True, exist_ok=True)
    for remote, name, expected, maximum in (
            ("onnx/model_quantized.onnx", "model_quantized.onnx", MODEL_SHA256, 25 * 1024 * 1024),
            ("tokenizer.json", "tokenizer.json", TOKENIZER_SHA256, 1024 * 1024)):
        destination = safe_path(directory, name)
        if destination.is_file() and digest(destination.read_bytes()) == expected:
            print(name + ": verified")
            continue
        url = f"https://huggingface.co/{MODEL}/resolve/{REVISION}/{remote}"
        with opener.open(url, timeout=60) as response:
            raw = response.read(maximum + 1)
        if len(raw) > maximum or digest(raw) != expected:
            raise ValueError("模型文件超限或SHA256校验失败，未安装: " + name)
        atomic_write(destination, raw, overwrite=destination.exists())
        print(name + ": installed and verified")
    print("Local semantic model ready. Inference requires .[semantic] optional dependencies.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("模型安装失败；请检查网络/代理。没有读取或上传作品，不影响字面检索。", file=sys.stderr)
        sys.exit(1)
