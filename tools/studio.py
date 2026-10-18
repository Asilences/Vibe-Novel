#!/usr/bin/env python3
"""Launch Studio from a source checkout with its optional dependencies installed."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

if __name__ == "__main__":
    try:
        from vibenovel.studio.server import main
    except ModuleNotFoundError as exc:
        if exc.name in {"fastapi", "uvicorn", "pydantic"}:
            raise SystemExit('请先安装 Studio 依赖: python -m pip install -e ".[studio]"')
        raise
    main()
