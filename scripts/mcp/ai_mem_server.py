from __future__ import annotations

import sys
from pathlib import Path


def _bootstrap_package_path() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    package_src = repo_root / "client" / "src"
    if package_src.exists():
        sys.path.insert(0, str(package_src))


_bootstrap_package_path()

from ai_mem.mcp_server import main  # noqa: E402


if __name__ == "__main__":
    main()
