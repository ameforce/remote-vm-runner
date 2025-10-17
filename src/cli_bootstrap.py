from __future__ import annotations

import logging
import sys
from pathlib import Path

from .envutils import ensure_path_contains


def _detect_batch_dir(argv: list[str]) -> str:
    if len(argv) >= 2 and argv[1]:
        return str(Path(argv[1]).resolve())
    here = Path(__file__).resolve()
    repo_root = here.parents[1]
    return str(repo_root)


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv
    logging.getLogger("src.cli_bootstrap").setLevel(logging.INFO)

    batch_dir = _detect_batch_dir(argv)
    try:
        scope = ensure_path_contains(batch_dir)
        print(f"PATH 등록 확인: {batch_dir} (scope={scope})")
    except Exception as exc:
        print(f"경로 등록 실패(무시): {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
