"""Docker HEALTHCHECK helper.

Runs inside the Orion container and asks the local health endpoint whether the
bot is ready. No secrets are used or logged.
"""
from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    host = os.getenv("HEALTH_HOST", "127.0.0.1")
    port = os.getenv("HEALTH_PORT", "8080")
    url = f"http://{host}:{port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return 0 if response.status == 200 else 1
    except (OSError, urllib.error.URLError) as exc:
        print(f"orion healthcheck failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
