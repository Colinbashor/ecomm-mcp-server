"""
Log free disk space to `sync_log` so a filling disk reaches warehouse_health.

A full disk is the quietest way to lose a pipeline: SQLite cannot even write the
`sync_log` row that would say so, so runs just stop appearing. This probe runs
while there is still room to write, and logs one row (platform `disk_space`).

THRESHOLDS FOLLOW THE BACKUP'S NEED when backup_db.py writes to the watched
volume, because on a warehouse machine the thing that fills the disk is usually
the backup, and what it needs grows with the database. A fixed number of GB
that was generous for a small database reads `ok` on a night the backup of a
larger one can no longer fit. Both multipliers come from backup_db.py
(`FREE_MARGIN`, `ZIP_SIZE_FRACTION`) and the size from `backup_db.db_bytes()`
(database + live -wal), so the probe and the backup cannot disagree about what
"enough" means.

Status (always one row, never a silent zero):
  ok        free space covers the next backup, including the weekly compressed
            copy if that is enabled
  degraded  free space covers a plain nightly copy but not a zip night (the
            next one will drop a full copy to make room), or is under
            DISK_PROBE_DEGRADED_GB: time to look at backup retention and
            large files
  error     free space is under what one backup needs (it must sacrifice a
            kept copy or refuse to run), or under DISK_PROBE_ERROR_GB

The two DISK_PROBE_*_GB values are FLOORS: a small database never relaxes the
probe below them. They are the only thresholds when the backup folder does not
exist (backups not in use), is on a different volume from the watched path, or
the database size cannot be read; the message says which.

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


def backup_need(path: Path) -> tuple[float, float, str]:
    """(GB one nightly backup needs, GB a zip-night backup needs, note).

    Both are 0.0 when the backup's need does not apply to `path`'s volume;
    the note then says why, so a reader can tell "fixed thresholds by design"
    from "could not work it out". Never raises: the probe must always log."""
    try:
        import backup_db  # here, not at the top: a bad backup setting must not stop the probe

        if not backup_db.BACKUP_DIR.exists():
            return 0.0, 0.0, "no backup folder, fixed thresholds only"
        if os.stat(backup_db.BACKUP_DIR).st_dev != os.stat(path).st_dev:
            return 0.0, 0.0, "backups are on another volume, fixed thresholds only"
        size = backup_db.db_bytes()
        plain = size * backup_db.FREE_MARGIN / GB
        zip_night = plain
        if backup_db.zips_enabled():
            zip_night += size * backup_db.ZIP_SIZE_FRACTION / GB
    except Exception as exc:  # noqa: BLE001
        return 0.0, 0.0, (f"backup need unreadable ({type(exc).__name__}: {exc}), "
                          f"fixed thresholds only")
    return plain, zip_night, f"database + WAL {size / GB:.1f} GB"


def thresholds(path: Path) -> tuple[float, float, str]:
    """(error_gb, degraded_gb, note): the larger of the configured floor and
    what the backup needs on this volume."""
    plain, zip_night, note = backup_need(path)
    error_gb = max(_float_env("DISK_PROBE_ERROR_GB", 10.0), plain)
    degraded_gb = max(_float_env("DISK_PROBE_DEGRADED_GB", 20.0), zip_night)
    return error_gb, degraded_gb, note


def probe(path: Path | None = None) -> tuple[str, str]:
    path = path or watched_path()
    error_gb, degraded_gb, note = thresholds(path)
    free_gb = shutil.disk_usage(path).free / GB
    return (classify(free_gb, degraded_gb, error_gb),
            f"{free_gb:.1f} GB free (degraded under {degraded_gb:.1f}, "
            f"error under {error_gb:.1f}; {note})")


def main() -> int:
    started = wdb.now()
    try:
        status, message = probe()
    except Exception as exc:  # noqa: BLE001 - never a silent zero, never a failed step
        status, message = "error", f"probe failed: {type(exc).__name__}: {exc}"
    wdb.log_sync(PLATFORM, started, 1 if status == "ok" else 0, status, message)
    print(f"{PLATFORM}: {status} - {message}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
