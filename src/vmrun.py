from __future__ import annotations

import subprocess
from typing import Iterable
import logging
import time

from .config import VMRUN


def run_vmrun(args: Iterable[str], capture: bool = True, timeout: int = 120) -> str:
    logger = logging.getLogger("src.vmrun")
    cmd = [str(VMRUN), "-T", "ws", *args]
    masked: list[str] = []
    it = iter(cmd)
    prev_flag = None
    for tok in it:
        if prev_flag in {"-gu", "-gp"}:
            masked.append("***")
            prev_flag = None
            continue
        if tok in {"-gu", "-gp"}:
            masked.append(tok)
            prev_flag = tok
            continue
        masked.append(tok)
    cmd_str = " ".join(masked)
    t0 = time.perf_counter()
    logger.debug("vmrun begin: %s timeout=%ss capture=%s", cmd_str, timeout, capture)
    try:
            completed = subprocess.run(
                cmd,
                capture_output=capture,
                text=True,
                check=True,
                encoding="utf-8",
                timeout=timeout,
            )
            out = completed.stdout.strip()
            try:
                action = (args and list(args)[0]) or ""
                action_low = str(action).lower()
                if action_low in {"start", "stop", "reset", "suspend", "reverttosnapshot", "listsnapshots", "checkpoint", "checktoolsstate", "runprograminguest"}:
                    logger.info("vmrun success: %s elapsed=%.2fs", cmd_str, time.perf_counter() - t0)
                else:
                    logger.debug("vmrun success: %s elapsed=%.2fs", cmd_str, time.perf_counter() - t0)
            except Exception:
                pass
            return out
    except subprocess.TimeoutExpired as exc:
        try:
            if exc.process:
                exc.process.kill()
        except Exception:
            pass
            try:
                logger.warning("vmrun timeout: %s elapsed=%.2fs", cmd_str, time.perf_counter() - t0)
            except Exception:
                pass
        return ""
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or "").strip()
        out = (exc.stdout or "").strip()
        msg = err or out or "unknown error"
        code = getattr(exc, "returncode", None)
        detail = f"{msg}" if code is None else f"{msg} (code={code})"
        try:
            logger.warning("vmrun failed: %s elapsed=%.2fs detail=%s", cmd_str, time.perf_counter() - t0, detail)
        except Exception:
            pass
        raise RuntimeError(f"vmrun failed: {detail}") from exc
