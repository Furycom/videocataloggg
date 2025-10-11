from __future__ import annotations

import json
import time

from core.paths import get_logs_dir, resolve_working_dir


class MainThreadWatchdog:
    def __init__(self, tk_root, *, interval_ms: int = 200) -> None:
        self._root = tk_root
        self._interval_ms = max(50, int(interval_ms))
        self._last = time.perf_counter()
        self._max_block_ms = 0.0
        self._logs_path = get_logs_dir(resolve_working_dir()) / "gui_watchdog.json"
        self._schedule()

    def _schedule(self) -> None:
        self._root.after(self._interval_ms, self._tick)

    def _tick(self) -> None:
        now = time.perf_counter()
        elapsed = (now - self._last) * 1000.0
        delay = max(0.0, elapsed - self._interval_ms)
        if delay > self._max_block_ms:
            self._max_block_ms = delay
            self._persist()
        self._last = now
        self._schedule()

    def _persist(self) -> None:
        payload = {
            "ts": time.time(),
            "max_block_ms": round(self._max_block_ms, 2),
            "where": "ui/watchdog.py",
        }
        self._logs_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self._logs_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
        except OSError:
            return

    @property
    def max_block_ms(self) -> float:
        return self._max_block_ms
