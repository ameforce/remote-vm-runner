from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure src layout import
BASE_DIR = Path(__file__).parent
SRC_DIR = BASE_DIR / "src"
if SRC_DIR.is_dir():
    sys.path.insert(0, str(SRC_DIR))


def run_server() -> int:
    from rvmrunner.api import create_app
    import uvicorn

    app = create_app()
    uvicorn.run(app, host="0.0.0.0", port=495, workers=1)
    return 0


def run_client() -> int:
    from rvmrunner.cli import VMClient

    client = VMClient("http://192.168.0.6:495")
    # 최소 UX: VM 목록만 출력
    for name in client.get_vm_list():
        print(name)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="remote-vm-runner entrypoint")
    parser.add_argument(
        "mode",
        choices=["server", "client"],
        help="Run server (FastAPI) or client (simple list)",
    )
    args = parser.parse_args()
    if args.mode == "server":
        return run_server()
    return run_client()


if __name__ == "__main__":
    raise SystemExit(main())


