"""
Log free disk space to `sync_log` so a filling disk reaches warehouse_health.

A full disk is the quietest way to lose a pipeline: SQLite cannot even write the
`sync_log` row that would say so, so runs just stop appearing. This probe runs
while there is still room to write, and logs one row (platform `disk_space`).

Status (always one row, never a silent zero):
  ok        free space >= DISK_PROBE_DEGRADED_GB
  degraded  free space >= DISK_PROBE_ERROR_GB but under the degraded threshold:
            time to look at backup retention and large files
  error     free space < DISK_PROBE_ERROR_GB

It always exits 0: a full disk must never fail the pipeline step itself; the
signal is the sync_log row, which `warehouse_health.py` reads.

Configuration (.env, all optional):
  DISK_PROBE_PATH           file or folder on the volume to watch. Default: the
                            folder holding the warehouse database.
  DISK_PROBE_DEGRADED_GB    default 20
  DISK_PROBE_ERROR_GB       default 10

Run:  python disk_probe.py
Wire it into the last wave of the daily pipeline, before warehouse_health.py
(see pipeline.example.json).
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv

from warehouse import db as wdb

load_dotenv()

PLATFORM = "disk_space"
GB = 1024 ** 3


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        raise SystemExit(f"{name} must be a number, got {raw!r}")


def watched_path() -> Path:
    raw = os.environ.get("DISK_PROBE_PATH", "").strip()
    if raw:
        p = Path(raw)
    else:
        p = Path(os.environ.get("WAREHOUSE_DB", "warehouse.db"))
        if not p.is_absolute():
            p = Path(__file__).resolve().parent / p
    # disk_usage needs an existing path; walk up to the nearest one.
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def classify(free_gb: float, degraded_gb: float, error_gb: float) -> str:
    if free_gb < error_gb:
        return "error"
    if free_gb < degraded_gb:
        return "degraded"
    return "ok"


def probe(path: Path | None = None) -> tuple[str, str]:
    path = path or watched_path()
    degraded_gb = _float_env("DISK_PROBE_DEGRADED_GB", 20.0)
    error_gb = _float_env("DISK_PROBE_ERROR_GB", 10.0)
    free_gb = shutil.disk_usage(path).free / GB
    return (classify(free_gb, degraded_gb, error_gb),
            f"{free_gb:.1f} GB free (degraded under {degraded_gb:g}, "
            f"error under {error_gb:g})")


def main() -> int:
    started = wdb.now()
    try:
        status, message = probe()
    except OSError as exc:
        status, message = "error", f"could not read disk usage: {exc}"
    wdb.log_sync(PLATFORM, started, 1 if status == "ok" else 0, status, message)
    print(f"{PLATFORM}: {status} - {message}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
