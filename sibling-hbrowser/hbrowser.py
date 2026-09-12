#!/usr/bin/env python3
"""hbrowser.py - minimal Python wrapper around the hbrowser Mojo binary.

Usage as a library:

    from hbrowser import fetch
    res = fetch("https://example.com", links=True)
    print(res["title"], res["elapsed_ms"])

Usage from the shell:

    python3 hbrowser.py https://example.com
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any


def _find_binary() -> str:
    env = os.environ.get("HBROWSER")
    if env:
        return env
    found = shutil.which("hbrowser")
    if found:
        return found
    raise FileNotFoundError(
        "hbrowser binary not found; set HBROWSER=/path/to/hbrowser "
        "or copy it onto PATH"
    )


def fetch(
    url: str,
    *,
    timeout: int = 15,
    max_bytes: int = 2 * 1024 * 1024,
    headers: list[str] | None = None,
    user_agent: str | None = None,
    links: bool = False,
    binary: str | None = None,
) -> dict[str, Any]:
    """Fetch a URL through hbrowser and return the parsed JSON dict.

    Raises RuntimeError on network errors (hbrowser exit code 2/3) and
    FileNotFoundError when the binary is missing.
    """
    cmd = [binary or _find_binary(), url, f"--timeout={timeout}", f"--max={max_bytes}"]
    for h in headers or []:
        cmd.append(f"--header={h}")
    if user_agent:
        cmd.append(f"--ua={user_agent}")
    if links:
        cmd.append("--links")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"hbrowser failed ({proc.returncode}): {proc.stderr.strip()}")
    return json.loads(proc.stdout)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("usage: hbrowser.py <url>", file=sys.stderr)
        sys.exit(1)
    result = fetch(sys.argv[1], links="--links" in sys.argv)
    print(json.dumps(result, indent=2, ensure_ascii=False))
