"""Run an ordered list of sync steps as child processes, one pipeline at a time.

This is the thing you schedule (Task Scheduler, cron, launchd) instead of a
pile of independent jobs. It reads the step list from a JSON config
(`pipeline.json`, falling back to the checked-in `pipeline.example.json`),
runs each step as a child process, and records the outcome in `sync_log`:
one row per step and one row for the whole run.

    python pipeline_runner.py daily                 # run the "daily" pipeline
    python pipeline_runner.py daily --dry-run       # print the plan, run nothing
    python pipeline_runner.py --list                # list configured pipelines
    python pipeline_runner.py daily --config my_pipeline.json

THREE OUTCOMES, NOT TWO. Exit 75 is this repo's convention for a connector
DELIBERATELY pausing on an upstream fault (a third-party 5xx window that
outlasted its retries, a report the platform has not published yet): state is
checkpointed and the next run resumes. Treating that like a crash is how a
pipeline ends up logging `error` on every run it ever makes -- and a light that
is always red is not a light; a real outage then sits behind it unnoticed. So a
step may declare `"tolerate_pause": true` and the run resolves as:

    ok        every step exited 0                    -> exit 0, logged `ok`
    degraded  the only non-zero exits were pauses     -> exit 0, logged `degraded`
    error     at least one real failure or timeout    -> exit 1, logged `error`

This is CLASSIFICATION, NOT SUPPRESSION: `warehouse_health.py` puts an aging
clock on `degraded` and turns it into an error once a pause stops being
transient, so nothing can hide here indefinitely.

Tolerance is PER STEP on purpose. Exit 75 from a script that has no
checkpoint-and-resume design is a real failure, and tolerating it globally
would let that spread by copy-paste.

WAVES. A pipeline is a list of waves; steps inside one wave run concurrently,
waves run in order. Put a step in a later wave when it READS something an
earlier step writes. Concurrency is safe because SQLite queues writers behind
`db.BUSY_TIMEOUT_SECONDS`, not because the tables are disjoint -- SQLite's
write lock is database-wide, so two writers always contend however unrelated
their tables. Keep waves small if your steps write heavily.

TIMEOUTS KILL THE WHOLE TREE. A step that hangs instead of failing would
otherwise hold the pipeline lock forever and every later scheduled run would
burn its wait budget and fail. Each step has a budget; on expiry the step's
entire process tree is killed -- not just the direct child. That matters: a
virtualenv's `python.exe` on Windows is a launcher stub whose real interpreter
is a CHILD process, so killing only the direct child leaves the wedged worker
alive and still holding its sockets and database handles. Size budgets from the
step's slowest healthy run (look at `sync_log`), never from its typical one --
a budget set from the median kills a healthy slow day. A timeout is never
tolerated, whatever the step declares: a hang is not a pause.

ONE PIPELINE AT A TIME. A file lock beside the database stops two scheduled
pipelines from writing concurrently. A pipeline waits up to its
`lock_wait_minutes` for the active one to finish, then gives up with exit 75
and a `degraded` row saying it could not run.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent import futures
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

from warehouse import db

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "pipeline.json"
EXAMPLE_CONFIG = HERE / "pipeline.example.json"

# Generous on purpose: a budget exists to catch a HANG, not a slow day.
DEFAULT_STEP_TIMEOUT_MINUTES = 180
# GNU `timeout` convention, so a killed step is recognisable in sync_log next
# to real child exit codes.
TIMEOUT_EXIT = 124
# A deliberate pause on an upstream fault (see module docstring). Same value
# every connector in this repo uses.
GRACEFUL_PAUSE_EXIT = 75
# A config the runner cannot use. argparse's own usage-error code.
CONFIG_ERROR_EXIT = 2

DEFAULT_MAX_CONCURRENT = 4
# Several API clients launching at the same instant can trip a platform's
# connection shedding; a few seconds apart is negligible against a long wave.
DEFAULT_LAUNCH_STAGGER_SECONDS = 5
DEFAULT_LOCK_WAIT_MINUTES = 0
# POSIX only: how long a timed-out step gets after SIGTERM before SIGKILL.
KILL_GRACE_SECONDS = 10


class ConfigError(ValueError):
    """The pipeline config cannot be used as written."""


class PipelineBusy(RuntimeError):
    """Another pipeline owns the writer lock."""


@dataclass(frozen=True)
class Step:
    script: str
    args: tuple[str, ...] = ()
    timeout_seconds: float = DEFAULT_STEP_TIMEOUT_MINUTES * 60
    # Exit codes this step may return without failing the pipeline. Only ever
    # GRACEFUL_PAUSE_EXIT (set via "tolerate_pause" in the config), and only for
    # a step that checkpoints and resumes. NEVER TIMEOUT_EXIT: a hang is not a
    # pause, and load_config refuses it.
    tolerated_exits: tuple[int, ...] = ()
    name: str | None = None

    @property
    def label(self) -> str:
        """Identity is script + ARGS: one script may legitimately run twice
        with different windows, but never as the same invocation twice."""
        return " ".join((self.script, *self.args))

    @property
    def display_name(self) -> str:
        return self.name or self.label

    @property
    def path(self) -> Path:
        path = Path(self.script)
        return path if path.is_absolute() else HERE / path

    @property
    def command(self) -> list[str]:
        # sys.executable, never a shell: no quoting rules to get wrong, and the
        # same interpreter (and venv) the runner itself was started with.
        return [sys.executable, str(self.path), *self.args]


@dataclass(frozen=True)
class StepOutcome:
    """A step that did not exit 0. `tolerated` splits degraded from failed."""
    label: str
    exit_code: int
    tolerated: bool


@dataclass(frozen=True)
class Pipeline:
    name: str
    waves: tuple[tuple[Step, ...], ...]
    lock_wait_seconds: float = DEFAULT_LOCK_WAIT_MINUTES * 60
    max_concurrent: int = DEFAULT_MAX_CONCURRENT
    launch_stagger_seconds: float = DEFAULT_LAUNCH_STAGGER_SECONDS
    description: str = ""

    @property
    def steps(self) -> tuple[Step, ...]:
        return tuple(step for wave in self.waves for step in wave)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

_COMMENT_KEYS = {"description", "_comment"}
_TOP_KEYS = {"pipelines"} | _COMMENT_KEYS
_PIPELINE_KEYS = {"waves", "lock_wait_minutes", "max_concurrent",
                  "launch_stagger_seconds"} | _COMMENT_KEYS
_WAVE_KEYS = {"steps"} | _COMMENT_KEYS
_STEP_KEYS = {"script", "args", "timeout_minutes", "tolerate_pause",
              "name"} | _COMMENT_KEYS


def _reject_unknown(obj: dict, allowed: set[str], where: str) -> None:
    # Unknown keys are an error, not ignored: "timeout_minute" silently falling
    # back to the default budget is exactly the plausible-but-wrong outcome
    # this project keeps paying for.
    unknown = sorted(set(obj) - allowed)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {unknown}; allowed: "
                          f"{sorted(allowed)}")


def _positive_number(value: Any, where: str, allow_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}: must be a number, got {value!r}")
    if value < 0 or (value == 0 and not allow_zero):
        raise ConfigError(f"{where}: must be {'>= 0' if allow_zero else '> 0'}, "
                          f"got {value!r}")
    return float(value)


def _parse_step(raw: Any, where: str, check_scripts: bool) -> Step:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: a step must be an object, got {raw!r}")
    _reject_unknown(raw, _STEP_KEYS, where)
    script = raw.get("script")
    if not isinstance(script, str) or not script.strip():
        raise ConfigError(f"{where}: 'script' is required")
    if not script.endswith(".py"):
        raise ConfigError(f"{where}: 'script' must be a .py file run with this "
                          f"interpreter, got {script!r}")
    args = raw.get("args", [])
    # A single string is the classic mistake ("--days 3" would arrive as ONE
    # argv element and argparse would reject it, or worse, accept it).
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise ConfigError(f"{where}: 'args' must be a list of strings, "
                          f"e.g. [\"--days\", \"3\"], got {args!r}")
    timeout = _positive_number(
        raw.get("timeout_minutes", DEFAULT_STEP_TIMEOUT_MINUTES),
        f"{where}.timeout_minutes") * 60
    tolerate = raw.get("tolerate_pause", False)
    if not isinstance(tolerate, bool):
        raise ConfigError(f"{where}: 'tolerate_pause' must be true or false")
    name = raw.get("name")
    if name is not None and (not isinstance(name, str) or not name.strip()):
        raise ConfigError(f"{where}: 'name' must be a non-empty string")
    step = Step(
        script=script,
        args=tuple(args),
        timeout_seconds=timeout,
        tolerated_exits=(GRACEFUL_PAUSE_EXIT,) if tolerate else (),
        name=name,
    )
    if check_scripts and not step.path.is_file():
        raise ConfigError(f"{where}: script not found: {step.path}")
    return step


def _parse_pipeline(name: str, raw: Any, check_scripts: bool) -> Pipeline:
    where = f"pipelines.{name}"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: must be an object")
    _reject_unknown(raw, _PIPELINE_KEYS, where)
    raw_waves = raw.get("waves")
    if not isinstance(raw_waves, list) or not raw_waves:
        raise ConfigError(f"{where}: 'waves' must be a non-empty list")
    waves: list[tuple[Step, ...]] = []
    for w, raw_wave in enumerate(raw_waves):
        wave_where = f"{where}.waves[{w}]"
        # A wave is either a bare list of steps or {"description", "steps"} --
        # the object form exists so a wave can say WHY it is ordered there.
        if isinstance(raw_wave, dict):
            _reject_unknown(raw_wave, _WAVE_KEYS, wave_where)
            raw_steps = raw_wave.get("steps")
        else:
            raw_steps = raw_wave
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ConfigError(f"{wave_where}: must be a non-empty list of steps")
        waves.append(tuple(
            _parse_step(s, f"{wave_where}.steps[{i}]", check_scripts)
            for i, s in enumerate(raw_steps)
        ))
    pipeline = Pipeline(
        name=name,
        waves=tuple(waves),
        lock_wait_seconds=_positive_number(
            raw.get("lock_wait_minutes", DEFAULT_LOCK_WAIT_MINUTES),
            f"{where}.lock_wait_minutes", allow_zero=True) * 60,
        max_concurrent=int(_positive_number(
            raw.get("max_concurrent", DEFAULT_MAX_CONCURRENT),
            f"{where}.max_concurrent")),
        launch_stagger_seconds=_positive_number(
            raw.get("launch_stagger_seconds", DEFAULT_LAUNCH_STAGGER_SECONDS),
            f"{where}.launch_stagger_seconds", allow_zero=True),
        description=str(raw.get("description", "")),
    )
    # The same invocation twice is always a mistake, and a duplicate NAME would
    # make two steps share one sync_log platform and hide each other.
    seen: set[str] = set()
    for step in pipeline.steps:
        for key in {step.label, step.display_name}:
            if key in seen:
                raise ConfigError(f"{where}: step {key!r} is scheduled twice")
            seen.add(key)
    return pipeline


def parse_config(data: Any, check_scripts: bool = True) -> dict[str, Pipeline]:
    if not isinstance(data, dict):
        raise ConfigError("config must be a JSON object")
    _reject_unknown(data, _TOP_KEYS, "config")
    raw = data.get("pipelines")
    if not isinstance(raw, dict) or not raw:
        raise ConfigError("config: 'pipelines' must be a non-empty object")
    return {name: _parse_pipeline(name, body, check_scripts)
            for name, body in raw.items()}


def default_config_path() -> Path:
    """Your own pipeline.json if you made one, else the checked-in example."""
    return DEFAULT_CONFIG if DEFAULT_CONFIG.exists() else EXAMPLE_CONFIG


def load_config(path: Path | None = None,
                check_scripts: bool = True) -> dict[str, Pipeline]:
    path = Path(path) if path else default_config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path}: invalid JSON: {exc}") from exc
    return parse_config(data, check_scripts=check_scripts)


# --------------------------------------------------------------------------
# Lock
# --------------------------------------------------------------------------

if os.name == "nt":
    import msvcrt

    def _try_lock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def lock_path() -> Path:
    """Beside the DATABASE, not the code: two checkouts pointed at one
    warehouse.db must still serialise against each other."""
    return db.DB_PATH.parent / f"{db.DB_PATH.name}.pipeline.lock"


@contextmanager
def pipeline_lock(path: Path | None = None, wait_seconds: float = 0,
                  poll_seconds: float = 30) -> Iterator[None]:
    """Hold the one-byte pipeline lock, or raise PipelineBusy after waiting.

    An OS-level lock rather than a pidfile, so a crashed runner can never leave
    a stale lock behind: the OS releases it when the process dies.
    """
    path = Path(path) if path else lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if path.stat().st_size == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + wait_seconds
        announced = False
        while True:
            try:
                _try_lock(handle)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise PipelineBusy(str(path)) from exc
                if not announced:
                    print("pipeline lock is busy; waiting for the active "
                          "pipeline to finish", flush=True)
                    announced = True
                time.sleep(min(poll_seconds,
                               max(0.0, deadline - time.monotonic())))
        try:
            yield
        finally:
            _unlock(handle)
    finally:
        handle.close()


# --------------------------------------------------------------------------
# Running steps
# --------------------------------------------------------------------------

# Every child currently running, so Ctrl-C can take their trees down too. On
# POSIX each step runs in its own session (so a timeout can kill its whole
# process group), which also means the terminal's SIGINT no longer reaches it.
_LIVE: set[subprocess.Popen] = set()
_LIVE_LOCK = threading.Lock()


def _popen_kwargs() -> dict:
    if os.name == "nt":
        return {}
    # New session => new process group whose id is the child's pid, so
    # killpg() reaches every descendant that did not deliberately escape.
    return {"start_new_session": True}


def kill_tree(process: subprocess.Popen) -> None:
    """Terminate a step and every process under it. Best-effort: a step that
    already exited is not an error here."""
    if os.name == "nt":
        # /T walks the tree, /F because console programs ignore the polite
        # close request. Needed even for a plain `python script.py`: a venv's
        # python.exe is a launcher stub and the real worker is its child.
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)],
                       capture_output=True, check=False)
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        process.wait(timeout=KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    # The direct child may have exited on SIGTERM while a grandchild ignored
    # it; SIGKILL the group regardless.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _step_platform(pipeline_name: str, step: Step) -> str:
    return f"{pipeline_name}_pipeline:{step.display_name}"


def _log_step(pipeline_name: str | None, step: Step, started: str,
              status: str, message: str) -> None:
    if not pipeline_name:
        return
    try:
        db.log_sync(_step_platform(pipeline_name, step), started, 0, status,
                    message)
    except Exception as exc:  # noqa: BLE001 - never lose the run over a log row
        print(f"    (could not write sync_log row for {step.display_name}: "
              f"{exc})", flush=True)


def run_step(step: Step, pipeline_name: str | None = None) -> StepOutcome | None:
    """Run one step to completion. Returns a StepOutcome unless it exited 0.

    With `pipeline_name`, also writes the step's own sync_log row.
    """
    print(f">>> {step.display_name}", flush=True)
    started_at = db.now()
    started = time.monotonic()
    try:
        # stdout/stderr are INHERITED, not piped: reading a pipe only after
        # wait() deadlocks once a chatty child outgrows the pipe buffer.
        process = subprocess.Popen(step.command, cwd=HERE, **_popen_kwargs())
    except OSError as exc:
        print(f"<<< {step.display_name} FAILED to start: {exc}", flush=True)
        _log_step(pipeline_name, step, started_at, "error",
                  f"failed to start: {exc}")
        return StepOutcome(step.label, -1, tolerated=False)
    with _LIVE_LOCK:
        _LIVE.add(process)
    try:
        try:
            returncode = process.wait(timeout=step.timeout_seconds)
        except subprocess.TimeoutExpired:
            # A hang, not a failure of the code path. Kill the TREE and keep
            # going so the remaining feeds still refresh and the lock is freed.
            kill_tree(process)
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
            minutes = (time.monotonic() - started) / 60
            message = (f"timed out after {minutes:.0f} min (budget "
                       f"{step.timeout_seconds / 60:.0f} min); process tree "
                       f"killed")
            print(f"<<< {step.display_name} TIMED OUT - {message}, continuing",
                  flush=True)
            _log_step(pipeline_name, step, started_at, "error", message)
            # Never tolerated, whatever the step declares.
            return StepOutcome(f"{step.label} (timeout after {minutes:.0f} min)",
                               TIMEOUT_EXIT, tolerated=False)
        except KeyboardInterrupt:
            # On POSIX the child is in its own session and never saw the ^C.
            kill_tree(process)
            raise
    finally:
        with _LIVE_LOCK:
            _LIVE.discard(process)

    minutes = (time.monotonic() - started) / 60
    if returncode:
        tolerated = returncode in step.tolerated_exits
        word = "PAUSED" if tolerated else "FAILED"
        print(f"<<< {step.display_name} {word} (exit {returncode}) after "
              f"{minutes:.1f} min - continuing", flush=True)
        _log_step(pipeline_name, step, started_at,
                  "degraded" if tolerated else "error",
                  f"exit {returncode} after {minutes:.1f} min"
                  + (" (tolerated pause)" if tolerated else ""))
        return StepOutcome(step.label, returncode, tolerated=tolerated)
    print(f"<<< {step.display_name} ok ({minutes:.1f} min)", flush=True)
    _log_step(pipeline_name, step, started_at, "ok",
              f"exit 0 after {minutes:.1f} min")
    return None


def run_steps(waves: Sequence[Sequence[Step]],
              pipeline_name: str | None = None,
              max_concurrent: int = DEFAULT_MAX_CONCURRENT,
              launch_stagger_seconds: float = DEFAULT_LAUNCH_STAGGER_SECONDS,
              ) -> list[StepOutcome]:
    """Run each wave's steps CONCURRENTLY, waves in order.

    Output INTERLEAVES within a wave; the `>>> name` / `<<< name` markers are
    what keep the log navigable, so keep them.

    Failures never abort the run: independent feeds should still refresh when
    one is broken. The returned list mixes real failures and tolerated pauses;
    the caller partitions them.
    """
    outcomes: list[StepOutcome] = []
    for n, wave in enumerate(waves, 1):
        if len(waves) > 1:
            print(f"=== wave {n}/{len(waves)}: {len(wave)} step(s)", flush=True)
        if len(wave) == 1:
            outcome = run_step(wave[0], pipeline_name)
            if outcome:
                outcomes.append(outcome)
            continue
        workers = max(1, min(max_concurrent, len(wave)))
        with futures.ThreadPoolExecutor(max_workers=workers) as pool:
            pending = []
            try:
                for i, step in enumerate(wave):
                    if i and launch_stagger_seconds:
                        time.sleep(launch_stagger_seconds)
                    pending.append(pool.submit(run_step, step, pipeline_name))
                for future in pending:
                    outcome = future.result()
                    if outcome:
                        outcomes.append(outcome)
            except KeyboardInterrupt:
                # The pool's exit waits for its threads, and each thread waits
                # for its child -- so the children must die first or ^C hangs.
                for future in pending:
                    future.cancel()
                _kill_live_children()
                raise
    return outcomes


def _kill_live_children() -> None:
    with _LIVE_LOCK:
        live = list(_LIVE)
    for process in live:
        kill_tree(process)


def summarize(outcomes: Sequence[StepOutcome]) -> tuple[str, str]:
    """(status, message) for the run-level sync_log row."""
    failures = [o for o in outcomes if not o.tolerated]
    paused = [o for o in outcomes if o.tolerated]
    message = "; ".join(
        f"{o.label} exit {o.exit_code}"
        + (" (tolerated pause)" if o.tolerated else "")
        for o in outcomes
    )
    status = "error" if failures else ("degraded" if paused else "ok")
    return status, message


def run_pipeline(pipeline: Pipeline) -> int:
    """Lock, run, log. Returns the process exit code."""
    platform = f"{pipeline.name}_pipeline"
    started = db.now()
    try:
        with pipeline_lock(wait_seconds=pipeline.lock_wait_seconds):
            outcomes = run_steps(
                pipeline.waves,
                pipeline_name=pipeline.name,
                max_concurrent=pipeline.max_concurrent,
                launch_stagger_seconds=pipeline.launch_stagger_seconds,
            )
            status, message = summarize(outcomes)
            if status == "ok":
                message = f"{len(pipeline.steps)} step(s) ok"
            db.log_sync(platform, started, 0, status, message)
    except PipelineBusy:
        # A run that could not make progress must say so, not vanish: a
        # `degraded` row here is what lets warehouse_health see a pipeline that
        # keeps losing the lock to a wedged predecessor.
        message = "skipped: another pipeline holds the writer lock"
        print(message, flush=True)
        db.log_sync(platform, started, 0, "degraded", message)
        return GRACEFUL_PAUSE_EXIT

    if status == "error":
        print(f"PIPELINE FAILED: {message}", flush=True)
        return 1
    if status == "degraded":
        # Exit 0 deliberately: the scheduler's "last result" is the only
        # consumer of this code, and red for an expected upstream pause is what
        # makes a status unreadable. warehouse_health holds the aging clock.
        print(f"PIPELINE DEGRADED: {message}", flush=True)
        return 0
    print(f"PIPELINE OK: {pipeline.name}", flush=True)
    return 0


def describe(pipeline: Pipeline) -> str:
    lines = [f"pipeline {pipeline.name!r}: {len(pipeline.waves)} wave(s), "
             f"{len(pipeline.steps)} step(s), max {pipeline.max_concurrent} "
             f"concurrent, lock wait {pipeline.lock_wait_seconds / 60:.0f} min"]
    if pipeline.description:
        lines.append(f"  {pipeline.description}")
    for n, wave in enumerate(pipeline.waves, 1):
        lines.append(f"  wave {n}:")
        for step in wave:
            extra = [f"timeout {step.timeout_seconds / 60:.0f} min"]
            if step.tolerated_exits:
                extra.append("exit 75 = tolerated pause")
            name = f" [{step.name}]" if step.name else ""
            lines.append(f"    - {step.label}{name} ({', '.join(extra)})")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a configured pipeline of sync steps.")
    parser.add_argument("pipeline", nargs="?",
                        help="pipeline name from the config (e.g. daily)")
    parser.add_argument("--config", type=Path,
                        help="pipeline config JSON (default: pipeline.json if "
                             "present, else pipeline.example.json)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan and exit without running anything")
    parser.add_argument("--list", action="store_true",
                        help="list the configured pipelines and exit")
    args = parser.parse_args(argv)

    config_path = args.config or default_config_path()
    try:
        pipelines = load_config(config_path)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr, flush=True)
        return CONFIG_ERROR_EXIT
    print(f"config: {config_path}", flush=True)

    if args.list:
        for pipeline in pipelines.values():
            print(describe(pipeline))
        return 0
    if not args.pipeline:
        parser.error(f"choose a pipeline: {', '.join(sorted(pipelines))}")
    pipeline = pipelines.get(args.pipeline)
    if pipeline is None:
        print(f"unknown pipeline {args.pipeline!r}; configured: "
              f"{', '.join(sorted(pipelines))}", file=sys.stderr, flush=True)
        return CONFIG_ERROR_EXIT
    if args.dry_run:
        print(describe(pipeline))
        return 0

    db.init_db()
    try:
        return run_pipeline(pipeline)
    except KeyboardInterrupt:
        print("interrupted - killing running steps", flush=True)
        _kill_live_children()
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
