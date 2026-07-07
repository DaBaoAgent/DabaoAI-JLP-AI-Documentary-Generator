#!/usr/bin/env python3
"""Check the local runtime required by DabaoAI-JLP."""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
REQUIRED_PACKAGES = ["fastapi", "uvicorn", "pydantic", "dashscope", "PIL", "cryptography", "numpy", "psutil"]
API_KEYS = ["DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "SILICONFLOW_API_KEY", "SEEDREAM_API_KEY"]


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text("utf-8-sig").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "OK" if ok else "MISSING"
    suffix = f" - {detail}" if detail else ""
    print(f"[{mark}] {name}{suffix}")


def main() -> int:
    env = {**load_env(ROOT / ".env"), **os.environ}
    print("DabaoAI-JLP runtime check")
    print("=" * 32)

    check("Python", sys.version_info >= (3, 10), sys.version.split()[0])
    check("Node.js/npm", shutil.which("npm") is not None, "required for the first WebUI build")
    check("FFmpeg", shutil.which("ffmpeg") is not None, "install FFmpeg or add ffmpeg/bin to PATH")
    check("FFprobe", shutil.which("ffprobe") is not None, "usually included with FFmpeg")

    for package in REQUIRED_PACKAGES:
        check(f"python:{package}", importlib.util.find_spec(package) is not None)

    for key in API_KEYS:
        value = env.get(key, "")
        check(key, bool(value), "set in .env or system environment")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
