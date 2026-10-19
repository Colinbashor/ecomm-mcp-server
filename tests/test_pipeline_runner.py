"""pipeline_runner: ordered child-process steps, tolerated pauses, tree kills.

Hermetic: steps are tiny throwaway scripts in a temp dir (or a mocked Popen),
the database is a temp file, and nothing touches the network.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

import pipeline_runner
from warehouse import db


def _proc(pid: int = 1, wait_result: int | Exception = 0) -> Mock:
    """A stand-in for the Popen a step runs under."""
    process = Mock(pid=pid)
    if isinstance(wait_result, Exception):
        process.wait.side_effect = wait_result
    else:
        process.wait.return_value = wait_result
    return process


class RunStepsTests(unittest.TestCase):
    def test_run_steps_continues_and_collects_failures(self) -> None:
        results = [_proc(wait_result=0), _proc(wait_result=7),
                   _proc(wait_result=0)]
        waves = (
            (pipeline_runner.Step("one.py"),),
            (pipeline_runner.Step("two.py", ("--flag",)),),
            (pipeline_runner.Step("three.py"),),
        )
        with patch.object(pipeline_runner.subprocess, "Popen",
                          side_effect=results) as popen:
            failures = pipeline_runner.run_steps(waves)

        self.assertEqual(
            failures,
            [pipeline_runner.StepOutcome("two.py --flag", 7, tolerated=False)],
        )
        self.assertEqual(popen.call_count, 3)

    def test_command_uses_this_interpreter_and_no_shell(self) -> None:
        step = pipeline_runner.Step("run_sync.py", ("--days", "3"))
        self.assertEqual(step.command[0], sys.executable)
        self.assertEqual(step.command[2:], ["--days", "3"])
        self.assertEqual(Path(step.command[1]).name, "run_sync.py")
        with patch.object(pipeline_runner.subprocess, "Popen",
                          return_value=_proc()) as popen:
            pipeline_runner.run_step(step)
        self.assertNotIn("shell", popen.call_args.kwargs)

    def test_hung_step_is_killed_and_the_pipeline_carries_on(self) -> None:
        """A wedged step must not hold the lock and block every later run."""
        hung = _proc(pid=4242, wait_result=subprocess.TimeoutExpired(
            cmd=["hung.py"], timeout=1))
        waves = ((pipeline_runner.Step("hung.py"),),
                 (pipeline_runner.Step("after.py"),))
        with (
            patch.object(pipeline_runner.subprocess, "Popen",
                         side_effect=[hung, _proc(wait_result=0)]) as popen,
            patch.object(pipeline_runner, "kill_tree") as kill_tree,
        ):
            failures = pipeline_runner.run_steps(waves)

        kill_tree.assert_called_once_with(hung)
        self.assertEqual(len(failures), 1)
        outcome = failures[0]
        self.assertTrue(outcome.label.startswith("hung.py (timeout after "))
        self.assertEqual(outcome.exit_code, pipeline_runner.TIMEOUT_EXIT)
        self.assertFalse(outcome.tolerated)
        self.assertEqual(popen.call_count, 2, "the step after the hang ran")

    def test_a_timeout_is_never_tolerated_even_when_pauses_are(self) -> None:
        step = pipeline_runner.Step(
            "pausy.py", tolerated_exits=(pipeline_runner.GRACEFUL_PAUSE_EXIT,))
        hung = _proc(wait_result=subprocess.TimeoutExpired(cmd=["x"], timeout=1))
        with (
            patch.object(pipeline_runner.subprocess, "Popen", return_value=hung),
            patch.object(pipeline_runner, "kill_tree"),
        ):
            outcome = pipeline_runner.run_step(step)
        self.assertFalse(outcome.tolerated)

    def _slow_popen(self, running: list, peak: list, delay: float):
        def slow_popen(*_a, **_kw):
            running.append(1)
            peak[0] = max(peak[0], len(running))
            proc = Mock(pid=1)

            def wait(timeout=None):
                time.sleep(delay)
                running.pop()
                return 0

            proc.wait.side_effect = wait
            return proc
        return slow_popen

    def test_a_wave_runs_its_steps_concurrently(self) -> None:
        running, peak = [], [0]
        wave = tuple(pipeline_runner.Step(f"s{i}.py") for i in range(4))
        with patch.object(pipeline_runner.subprocess, "Popen",
                          self._slow_popen(running, peak, 0.05)):
            failures = pipeline_runner.run_steps(
                (wave,), launch_stagger_seconds=0)
        self.assertEqual(failures, [])
        self.assertGreater(peak[0], 1, "steps in a wave did not overlap")

    def test_concurrency_is_capped(self) -> None:
        running, peak = [], [0]
        wave = tuple(pipeline_runner.Step(f"s{i}.py") for i in range(12))
        with patch.object(pipeline_runner.subprocess, "Popen",
                          self._slow_popen(running, peak, 0.02)):
            pipeline_runner.run_steps((wave,), max_concurrent=3,
                                      launch_stagger_seconds=0)
        self.assertLessEqual(peak[0], 3)


class ToleratedPauseTests(unittest.TestCase):
    """Exit 75 from a resumable step is degraded, not failed -- per step."""

    def _run(self, step):
        with patch.object(pipeline_runner.subprocess, "Popen",
                          return_value=_proc(
                              wait_result=pipeline_runner.GRACEFUL_PAUSE_EXIT)):
            return pipeline_runner.run_step(step)

    def test_graceful_pause_does_not_fail_the_pipeline(self) -> None:
        outcome = self._run(pipeline_runner.Step(
            "flexport_orders_sync.py",
            tolerated_exits=(pipeline_runner.GRACEFUL_PAUSE_EXIT,)))
        self.assertTrue(outcome.tolerated)

    def test_same_exit_from_an_undeclared_step_is_a_failure(self) -> None:
        """Exit 75 from something with no checkpoint-and-resume design is a
        real failure."""
        self.assertFalse(self._run(pipeline_runner.Step("run_sync.py")).tolerated)

    def test_summary_partitions_outcomes(self) -> None:
        Outcome = pipeline_runner.StepOutcome
        self.assertEqual(pipeline_runner.summarize([])[0], "ok")
        self.assertEqual(pipeline_runner.summarize(
            [Outcome("a.py", 75, True)])[0], "degraded")
        status, message = pipeline_runner.summarize(
            [Outcome("a.py", 75, True), Outcome("b.py", 1, False)])
        self.assertEqual(status, "error")
        self.assertIn("a.py exit 75 (tolerated pause)", message)
        self.assertIn("b.py exit 1", message)


class ConfigTests(unittest.TestCase):
    def _cfg(self, steps, **pipeline):
        return {"pipelines": {"p": {"waves": [steps], **pipeline}}}

    def test_example_config_loads_and_every_script_exists(self) -> None:
        """The checked-in example may only reference scripts in this repo."""
        pipelines = pipeline_runner.load_config(pipeline_runner.EXAMPLE_CONFIG)
        self.assertIn("daily", pipelines)
        for pipeline in pipelines.values():
            for step in pipeline.steps:
                with self.subTest(pipeline=pipeline.name, step=step.label):
                    self.assertTrue(step.path.is_file())
                    self.assertFalse(Path(step.script).is_absolute())

    def test_only_resumable_example_steps_tolerate_a_pause(self) -> None:
        """Keep the list closed so tolerance cannot spread by copy-paste:
        each of these checkpoints its progress and exits 75 by design."""
        allowed = {"flexport_orders_sync.py", "flexport_returns_sync.py",
                   "flexport_inbounds_sync.py", "merchant_center_sync.py",
                   "tiktok_affiliate_sync.py"}
        for pipeline in pipeline_runner.load_config(
                pipeline_runner.EXAMPLE_CONFIG).values():
            for step in pipeline.steps:
                if step.tolerated_exits:
                    with self.subTest(step=step.label):
                        self.assertIn(step.script, allowed)
                        self.assertEqual(step.tolerated_exits,
                                         (pipeline_runner.GRACEFUL_PAUSE_EXIT,))

    def test_every_step_gets_a_positive_timeout(self) -> None:
        pipelines = pipeline_runner.parse_config(
            self._cfg([{"script": "a.py"}, {"script": "b.py",
                                            "timeout_minutes": 2}]),
            check_scripts=False)
        a, b = pipelines["p"].steps
        self.assertEqual(a.timeout_seconds,
                         pipeline_runner.DEFAULT_STEP_TIMEOUT_MINUTES * 60)
        self.assertEqual(b.timeout_seconds, 120)

    def test_bad_configs_are_rejected_loudly(self) -> None:
        bad = {
            "typo key": [{"script": "a.py", "timeout_minute": 5}],
            "args as one string": [{"script": "a.py", "args": "--days 3"}],
            "non-python": [{"script": "a.sh"}],
            "zero timeout": [{"script": "a.py", "timeout_minutes": 0}],
            "duplicate invocation": [{"script": "a.py"}, {"script": "a.py"}],
            "duplicate name": [{"script": "a.py", "name": "x"},
                               {"script": "b.py", "name": "x"}],
            "tolerate not bool": [{"script": "a.py", "tolerate_pause": 75}],
            "empty wave": [],
        }
        for label, steps in bad.items():
            with self.subTest(label), self.assertRaises(
                    pipeline_runner.ConfigError):
                pipeline_runner.parse_config(self._cfg(steps),
                                             check_scripts=False)

    def test_same_script_with_different_args_is_allowed(self) -> None:
        pipeline_runner.parse_config(self._cfg([
            {"script": "run_sync.py", "args": ["--days", "3"]},
            {"script": "run_sync.py", "args": ["--only", "google", "--days", "60"]},
        ]), check_scripts=False)

    def test_missing_script_is_rejected(self) -> None:
        with self.assertRaises(pipeline_runner.ConfigError):
            pipeline_runner.parse_config(
                self._cfg([{"script": "definitely_not_here.py"}]))

    def test_wave_may_be_an_object_with_a_description(self) -> None:
        cfg = {"pipelines": {"p": {"waves": [
            {"description": "why", "steps": [{"script": "a.py"}]}]}}}
        self.assertEqual(len(pipeline_runner.parse_config(
            cfg, check_scripts=False)["p"].steps), 1)

    def test_main_reports_a_config_error_with_exit_2(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "p.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(pipeline_runner.main(["daily", "--config",
                                                   str(path)]),
                             pipeline_runner.CONFIG_ERROR_EXIT)

    def test_dry_run_runs_nothing(self) -> None:
        with patch.object(pipeline_runner.subprocess, "Popen") as popen:
            code = pipeline_runner.main(
                ["daily", "--dry-run", "--config",
                 str(pipeline_runner.EXAMPLE_CONFIG)])
        self.assertEqual(code, 0)
        popen.assert_not_called()


class LockTests(unittest.TestCase):
    """A real OS lock (msvcrt on Windows, flock elsewhere), no mocks."""

    def test_second_holder_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "pipeline.lock"
            with pipeline_runner.pipeline_lock(path):
                with self.assertRaises(pipeline_runner.PipelineBusy):
                    with pipeline_runner.pipeline_lock(path):
                        self.fail("contended lock should not be entered")

    def test_lock_is_released_on_exit(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "pipeline.lock"
            with pipeline_runner.pipeline_lock(path):
                pass
            with pipeline_runner.pipeline_lock(path):
                pass

    def test_bounded_wait_retries_then_acquires(self) -> None:
        calls = []
        real = pipeline_runner._try_lock

        def flaky(handle):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("busy")
            real(handle)

        with tempfile.TemporaryDirectory() as folder, \
                patch.object(pipeline_runner, "_try_lock", flaky), \
                patch.object(pipeline_runner.time, "sleep") as sleep:
            with pipeline_runner.pipeline_lock(Path(folder) / "l",
                                               wait_seconds=60,
                                               poll_seconds=1):
                pass
        sleep.assert_called_once()
        self.assertEqual(len(calls), 2)

    def test_lock_lives_beside_the_database(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(db, "DB_PATH", Path(folder) / "w.db"):
                self.assertEqual(pipeline_runner.lock_path(),
                                 Path(folder) / "w.db.pipeline.lock")


def _write(folder: Path, name: str, body: str) -> Path:
    path = folder / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


class RealProcessTests(unittest.TestCase):
    """End to end against real child processes and a temp database."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        self._db = patch.object(db, "DB_PATH", self.folder / "warehouse.db")
        self._db.start()
        db.init_db()

    def tearDown(self) -> None:
        self._db.stop()
        self._tmp.cleanup()

    def _rows(self):
        conn = sqlite3.connect(db.DB_PATH)
        try:
            return conn.execute(
                "SELECT platform, status, message FROM sync_log ORDER BY id"
            ).fetchall()
        finally:
            conn.close()

    def _config(self, steps) -> Path:
        path = self.folder / "pipeline.json"
        path.write_text(json.dumps({"pipelines": {"nightly": {
            "launch_stagger_seconds": 0, "waves": steps}}}), encoding="utf-8")
        return path

    def test_step_and_run_rows_are_written(self) -> None:
        ok = _write(self.folder, "ok.py", "raise SystemExit(0)\n")
        pause = _write(self.folder, "pause.py", "raise SystemExit(75)\n")
        config = self._config([[
            {"script": str(ok), "name": "ok"},
            {"script": str(pause), "name": "pause", "tolerate_pause": True},
        ]])

        code = pipeline_runner.main(["nightly", "--config", str(config)])

        self.assertEqual(code, 0, "a tolerated pause alone must not exit 1")
        rows = {platform: (status, message)
                for platform, status, message in self._rows()}
        self.assertEqual(rows["nightly_pipeline:ok"][0], "ok")
        self.assertEqual(rows["nightly_pipeline:pause"][0], "degraded")
        self.assertEqual(rows["nightly_pipeline"][0], "degraded")
        self.assertIn("tolerated pause", rows["nightly_pipeline"][1])

    def test_a_real_failure_fails_the_run_but_later_waves_still_run(self) -> None:
        bad = _write(self.folder, "bad.py", "raise SystemExit(3)\n")
        marker = self.folder / "after.txt"
        after = _write(self.folder, "after.py",
                       f"open({str(marker)!r}, 'w').write('ran')\n")
        config = self._config([[{"script": str(bad), "name": "bad"}],
                               [{"script": str(after), "name": "after"}]])

        code = pipeline_runner.main(["nightly", "--config", str(config)])

        self.assertEqual(code, 1)
        self.assertTrue(marker.exists())
        statuses = {p: s for p, s, _ in self._rows()}
        self.assertEqual(statuses["nightly_pipeline:bad"], "error")
        self.assertEqual(statuses["nightly_pipeline:after"], "ok")
        self.assertEqual(statuses["nightly_pipeline"], "error")

    def test_busy_lock_logs_degraded_and_exits_75(self) -> None:
        ok = _write(self.folder, "ok.py", "raise SystemExit(0)\n")
        config = self._config([[{"script": str(ok)}]])
        with pipeline_runner.pipeline_lock():
            code = pipeline_runner.main(["nightly", "--config", str(config)])
        self.assertEqual(code, pipeline_runner.GRACEFUL_PAUSE_EXIT)
        self.assertEqual(self._rows()[-1][:2], ("nightly_pipeline", "degraded"))

    def test_timeout_kills_the_grandchild_too(self) -> None:
        """The step spawns a worker and sleeps; killing only the direct child
        would leave the worker running. Its heartbeat must stop."""
        beat = self.folder / "beat.txt"
        worker = _write(self.folder, "worker.py", f"""
            import time
            for i in range(300):          # self-terminates after ~30s anyway
                with open({str(beat)!r}, "w") as fh:
                    fh.write(str(i))
                time.sleep(0.1)
            """)
        parent = _write(self.folder, "parent.py", f"""
            import subprocess, sys, time
            subprocess.Popen([sys.executable, {str(worker)!r}])
            time.sleep(60)
            """)
        step = pipeline_runner.Step(str(parent), timeout_seconds=2.0)

        with patch.object(pipeline_runner, "KILL_GRACE_SECONDS", 1):
            outcome = pipeline_runner.run_step(step)

        self.assertEqual(outcome.exit_code, pipeline_runner.TIMEOUT_EXIT)
        self.assertTrue(beat.exists(), "worker never started")

        def read() -> str:
            for _ in range(20):
                try:
                    text = beat.read_text()
                    if text:
                        return text
                except OSError:
                    pass
                time.sleep(0.05)
            return ""

        time.sleep(0.5)      # let any in-flight write land
        first = read()
        time.sleep(1.0)
        self.assertEqual(read(), first, "grandchild survived the timeout kill")

    def test_main_logs_and_returns_child_failure(self) -> None:
        pipeline = pipeline_runner.Pipeline(
            "daily", ((pipeline_runner.Step("broken.py"),),))
        with (
            patch.object(pipeline_runner, "pipeline_lock",
                         return_value=nullcontext()),
            patch.object(pipeline_runner, "run_steps", return_value=[
                pipeline_runner.StepOutcome("broken.py", 4, tolerated=False)]),
            patch.object(pipeline_runner.db, "log_sync") as log_sync_mock,
        ):
            result = pipeline_runner.run_pipeline(pipeline)

        self.assertEqual(result, 1)
        self.assertEqual(log_sync_mock.call_args.args[0], "daily_pipeline")
        self.assertEqual(log_sync_mock.call_args.args[3], "error")


if __name__ == "__main__":
    unittest.main()
