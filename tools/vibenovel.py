#!/usr/bin/env python3
"""Run the v3 source checkout without installation, from any working directory."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vibenovel.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
