from __future__ import annotations

try:
    import winreg  # type: ignore
except Exception:  # ImportError on non-Windows
    winreg = None  # type: ignore


if winreg is not None:
    _REG_KEY = winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\\QA_VM_API\\Durations")

    def _load_samples(name: str) -> list[float]:
        try:
            data, _ = winreg.QueryValueEx(_REG_KEY, name)
            return [float(x) for x in data.split(',') if x]
        except FileNotFoundError:
            return []
        except OSError:
            return []

    def record_duration(name: str, secs: float, limit: int = 10) -> None:
        samples = _load_samples(name)
        samples.append(secs)
        samples = samples[-limit:]
        winreg.SetValueEx(_REG_KEY, name, 0, winreg.REG_SZ, ','.join(f"{s:.1f}" for s in samples))

    def average_duration(name: str) -> float | None:
        samples = _load_samples(name)
        if not samples:
            return None
        return sum(samples) / len(samples)
else:
    def record_duration(name: str, secs: float, limit: int = 10) -> None:  # type: ignore[no-redef]
        return None

    def average_duration(name: str) -> float | None:  # type: ignore[no-redef]
        return None
