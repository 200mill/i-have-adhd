import argparse
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_scenario_eval  # noqa: E402


SCENARIO = ROOT / "evals" / "scenarios" / "persistence-topic-switch-stop"


class ScenarioEvaluationTest(unittest.TestCase):
    def test_budget_is_rounded_down(self):
        self.assertEqual("0.123456", run_scenario_eval.budget_value(0.1234569))

    def test_scenario_is_valid(self):
        self.assertEqual([], run_scenario_eval.validate_scenario(SCENARIO))
        turns, criteria = run_scenario_eval.load_scenario(SCENARIO)
        self.assertEqual(6, len(turns))
        self.assertIn(
            "leads with an action",
            " ".join(criteria),
        )

    def test_checks_accept_expected_transcript(self):
        result = self._check(self._passing_rows())
        self.assertEqual(0, result.returncode, result.stderr)

    def test_checks_reject_missing_debug_response(self):
        rows = self._passing_rows()
        rows[-2]["response"] = ""

        result = self._check(rows)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("failure-3: response is missing", result.stderr)

    @mock.patch("run_scenario_eval.subprocess.run")
    def test_claude_call_uses_isolated_environment(self, subprocess_run):
        subprocess_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"result": "done", "total_cost_usd": 0.01}),
            stderr="",
        )

        run_scenario_eval.call_claude(
            "task",
            session_id="session",
            resume=False,
            remaining_budget=1.0,
            model="fixture",
            workdir=ROOT,
        )

        command = subprocess_run.call_args.args[0]
        self.assertIn("--safe-mode", command)
        self.assertIn("--strict-mcp-config", command)
        self.assertNotIn("--setting-sources", command)

    @mock.patch("run_scenario_eval.subprocess.run")
    def test_invalid_response_preserves_reported_cost(self, subprocess_run):
        subprocess_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"total_cost_usd": 0.12}),
            stderr="",
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "cumulative reported cost: \\$0.220000",
        ):
            run_scenario_eval.invoke_with_cost(
                run_scenario_eval.call_claude,
                "task",
                prior_cost=0.1,
                session_id="session",
                resume=False,
                remaining_budget=0.1,
                model="fixture",
                workdir=ROOT,
            )

    @mock.patch("run_scenario_eval.subprocess.run")
    def test_judge_is_fresh_and_structured(self, subprocess_run):
        subprocess_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "structured_output": {
                        "pass": True,
                        "results": [
                            {
                                "id": "C1",
                                "pass": True,
                                "evidence": "response",
                            }
                        ],
                    },
                    "total_cost_usd": 0.01,
                }
            ),
            stderr="",
        )

        run_scenario_eval.call_judge(
            "judge",
            remaining_budget=1.0,
            model="fixture",
            workdir=ROOT,
        )

        command = subprocess_run.call_args.args[0]
        self.assertIn("--safe-mode", command)
        self.assertIn("--strict-mcp-config", command)
        self.assertIn("--no-session-persistence", command)
        self.assertIn("--json-schema", command)
        self.assertIn("--system-prompt", command)
        self.assertNotIn("--session-id", command)
        self.assertNotIn("--resume", command)

    def test_children_cannot_read_parent_input(self):
        probe = "import sys; print(repr(sys.stdin.read()))"
        wrapper = (
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "import run_scenario_eval as runner; "
            "print(runner.run_claude_command([sys.executable, '-c', sys.argv[2]], "
            "workdir=runner.pathlib.Path.cwd(), label='probe'))"
        )
        # The fake CLI includes its stdin in otherwise valid provider output.
        cli = (
            "import json, sys; print(json.dumps({"
            "'result': sys.stdin.read(), 'total_cost_usd': 0}))"
        )
        result = subprocess.run(
            [sys.executable, "-c", wrapper, str(ROOT / "scripts"), cli],
            input="unrelated piped input",
            text=True,
            capture_output=True,
            timeout=5,
            check=True,
        )
        self.assertIn("'result': ''", result.stdout)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "checks.py").write_text(probe, encoding="utf-8")
            result = run_scenario_eval.run_checks(root, root / "unused", root)
        self.assertEqual("''", result.stdout.strip())

    def test_children_time_out(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "checks.py").write_text(
                "import time; time.sleep(5)", encoding="utf-8"
            )
            with mock.patch.object(run_scenario_eval, "CHECK_TIMEOUT_SECONDS", 0.1):
                with self.assertRaisesRegex(RuntimeError, "checks timed out"):
                    run_scenario_eval.run_checks(root, root / "unused", root)
            with mock.patch.object(run_scenario_eval, "CALL_TIMEOUT_SECONDS", 0.1):
                with self.assertRaisesRegex(
                    run_scenario_eval.ClaudeCallError, "timed out; call cost unavailable"
                ):
                    run_scenario_eval.run_claude_command(
                        [sys.executable, str(root / "checks.py")],
                        workdir=root,
                        label="probe",
                    )

    @mock.patch("run_scenario_eval.subprocess.run")
    def test_invalid_costs_fail_closed(self, subprocess_run):
        for cost in (True, False, float("nan"), float("inf"), -1, None, "0.1"):
            with self.subTest(cost=cost):
                subprocess_run.return_value = subprocess.CompletedProcess(
                    [], 0, json.dumps({"result": "done", "total_cost_usd": cost}), ""
                )
                with self.assertRaisesRegex(
                    run_scenario_eval.ClaudeCallError, "invalid total_cost_usd"
                ):
                    run_scenario_eval.run_claude_command([], workdir=ROOT, label="probe")

    @mock.patch("run_scenario_eval.subprocess.run")
    def test_malformed_failure_payload_does_not_crash(self, subprocess_run):
        for payload in ([], None, "error"):
            with self.subTest(payload=payload):
                subprocess_run.return_value = subprocess.CompletedProcess(
                    [], 1, json.dumps(payload), "failed"
                )
                with self.assertRaisesRegex(
                    run_scenario_eval.ClaudeCallError, "call cost unavailable"
                ):
                    run_scenario_eval.run_claude_command([], workdir=ROOT, label="probe")

    @mock.patch("run_scenario_eval.subprocess.run")
    def test_provider_error_is_not_a_successful_response(self, subprocess_run):
        subprocess_run.return_value = subprocess.CompletedProcess(
            [], 0, json.dumps({
                "is_error": True, "result": "budget exhausted", "total_cost_usd": 0.12
            }), ""
        )
        with self.assertRaises(run_scenario_eval.ClaudeCallError) as caught:
            run_scenario_eval.run_claude_command([], workdir=ROOT, label="probe")
        self.assertEqual(0.12, caught.exception.cost_usd)

    @mock.patch("run_scenario_eval.subprocess.run")
    def test_invalid_judge_result_preserves_cost(self, subprocess_run):
        subprocess_run.return_value = subprocess.CompletedProcess(
            [], 0, json.dumps({
                "structured_output": {
                    "pass": True,
                    "results": [{"id": "C1", "pass": "yes", "evidence": "claim"}],
                },
                "total_cost_usd": 0.12,
            }), ""
        )
        with self.assertRaisesRegex(RuntimeError, r"cumulative reported cost: \$0.220000"):
            run_scenario_eval.invoke_with_cost(
                run_scenario_eval.call_judge,
                "judge",
                prior_cost=0.1,
                remaining_budget=0.9,
                model="fixture",
                workdir=ROOT,
            )

    def test_runner_uses_one_session_and_blind_judge(self):
        calls = []
        judge_calls = []

        def fake_model(prompt, **options):
            calls.append((prompt, options))
            if "New topic:" in prompt:
                response = "Open the browser Network tab and reload the page."
            elif "replaced `fetch`" in prompt:
                response = (
                    "Stop changing code. The assumption that the request reaches "
                    "the API may be wrong. What exact Request URL and status appear "
                    "in the Network tab?"
                )
            elif "Still broken" in prompt:
                response = "Check whether the API received the request."
            elif prompt == "normal mode":
                response = "Normal mode restored."
            else:
                response = "Open the failing auth test first."
            return response, 0.01

        def fake_judge(prompt, **options):
            judge_calls.append((prompt, options))
            return {
                "pass": True,
                "results": [
                    {"id": f"C{index}", "pass": True, "evidence": "response"}
                    for index, _criterion in enumerate(
                        run_scenario_eval.load_scenario(SCENARIO)[1],
                        start=1,
                    )
                ],
            }, 0.02

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "transcript.jsonl"
            args = argparse.Namespace(
                scenario=SCENARIO,
                condition="candidate",
                condition_skill=ROOT / "skills" / "i-have-adhd" / "SKILL.md",
                model="fixture",
                budget_usd=1.0,
                output=output,
            )
            with contextlib.redirect_stdout(io.StringIO()):
                status = run_scenario_eval.run_scenario(
                    args,
                    model_call=fake_model,
                    judge_call=fake_judge,
                )
            rows = self._read_rows(output)

        self.assertEqual(0, status)
        self.assertEqual(
            [False, True, True, True, True, True],
            [call[1]["resume"] for call in calls],
        )
        self.assertEqual(1, len({call[1]["session_id"] for call in calls}))
        self.assertIn("<response_style>", calls[0][0])
        self.assertNotIn("<response_style>", calls[1][0])
        self.assertEqual(6, len(rows))
        self.assertEqual(1, len(judge_calls))
        self.assertNotIn("candidate", judge_calls[0][0])
        self.assertNotIn("response_style", judge_calls[0][0])
        self.assertNotIn(calls[0][1]["session_id"], judge_calls[0][0])
        self.assertAlmostEqual(0.94, judge_calls[0][1]["remaining_budget"])
        self.assertNotEqual(
            calls[0][1]["workdir"],
            judge_calls[0][1]["workdir"],
        )

    def test_semantic_failure_fails_the_run(self):
        def fake_model(_prompt, **_options):
            return "One line.", 0.01

        def fake_judge(_prompt, **_options):
            results = [
                {"id": f"C{index}", "pass": True, "evidence": "response"}
                for index, _criterion in enumerate(
                    run_scenario_eval.load_scenario(SCENARIO)[1],
                    start=1,
                )
            ]
            results[3]["pass"] = False
            results[3]["evidence"] = "No question was asked."
            return {
                "pass": False,
                "results": results,
            }, 0.01

        with tempfile.TemporaryDirectory() as temporary:
            args = argparse.Namespace(
                scenario=SCENARIO,
                condition="baseline",
                condition_skill=None,
                model="fixture",
                budget_usd=1.0,
                output=Path(temporary) / "transcript.jsonl",
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                status = run_scenario_eval.run_scenario(
                    args,
                    model_call=fake_model,
                    judge_call=fake_judge,
                )

        self.assertEqual(1, status)
        self.assertFalse(json.loads(stdout.getvalue())["semantic_passed"])

    def _check(self, rows):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        transcript = root / "transcript.jsonl"
        transcript.write_text(
            "".join(json.dumps(row) + "\n" for row in rows),
            encoding="utf-8",
        )
        return run_scenario_eval.run_checks(SCENARIO, transcript, root)

    @staticmethod
    def _read_rows(path):
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    @staticmethod
    def _passing_rows():
        return [
            {"turn_id": "initial-task", "response": "Open the auth test first."},
            {
                "turn_id": "debug-start",
                "response": "Open the browser Network tab and reload the page.",
            },
            {
                "turn_id": "failure-1",
                "response": "Check whether the API received the request.",
            },
            {
                "turn_id": "failure-2",
                "response": "Inspect the browser console for the exact error.",
            },
            {
                "turn_id": "failure-3",
                "response": (
                    "Stop changing code. The assumption that the request reaches "
                    "the API may be wrong. What exact Request URL and status appear "
                    "in the Network tab?"
                ),
            },
            {"turn_id": "stop", "response": "Normal mode restored."},
        ]


if __name__ == "__main__":
    unittest.main()
