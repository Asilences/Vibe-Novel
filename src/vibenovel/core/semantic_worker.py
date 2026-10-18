"""Private bounded stdin/stdout worker for optional native semantic inference."""
import json
import sys
from pathlib import Path

from .semantic import MAX_CHUNKS, _NativeEmbedding


def main():
    raw = sys.stdin.buffer.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        return 2
    request = json.loads(raw)
    texts = request["texts"]
    if (not isinstance(texts, list) or len(texts) > MAX_CHUNKS
            or any(not isinstance(item, str) or len(item) > 250 for item in texts)):
        return 2
    result = _NativeEmbedding(Path(request["workspace"])).embed(texts, query=request["query"] is True)
    sys.stdout.buffer.write(json.dumps(result, separators=(",", ":"), allow_nan=False).encode("utf-8"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Never print body, source paths, inputs, environment, or native logs.
        sys.exit(2)
