#!/usr/bin/env python3
"""Tests for the pull-request authorization backstop.

Run directly (`python3 scripts/ci/test_dragback_ci_check.py`) or through
`scripts/ci/verify.sh --self-test`. Standard library only, so the same command
works on a developer machine and on a runner with nothing installed.

The cases that matter most are the two asymmetries:

* an unreachable service **fails** here, where the hook allows;
* a scope-preserved sibling **passes** here, because invalidation is
  scope-sensitive and its snapshot is intentionally behind.
"""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dragback_ci_check as check  # noqa: E402,I001


NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def grant(
    snapshot: str = "graph-v17",
    verdict: str = "ALLOW",
    expires_at: datetime = None,
    issued_at: datetime = None,
) -> dict:
    return {
        "authorization_id": "AUTH-1",
        "run_id": "LIVE-WS-1-RUN",
        "task_id": "TICKET-9",
        "decision_snapshot": snapshot,
        "plan_hash": "sha256:" + "0" * 64,
        "verdict": verdict,
        "issued_at": iso(issued_at or (NOW - timedelta(minutes=5))),
        "expires_at": iso(expires_at or (NOW + timedelta(hours=1))),
    }


def assignment(
    task_id: str = "TASK-102",
    state: str = "running",
    snapshot: str = "graph-v17",
    provider: str = "claude-code",
    execution_mode: str = "live",
    scopes=None,
    interrupt_reason: str = None,
    redirect_instruction: str = None,
    provenance_path=None,
) -> dict:
    return {
        "id": "ASSIGNMENT-" + task_id,
        "task_id": task_id,
        "task_title": "Task " + task_id,
        "agent_name": "agent",
        "runtime_provider": provider,
        "execution_mode": execution_mode,
        "run_id": "LIVE-WS-1-RUN-" + task_id,
        "state": state,
        "scopes": scopes if scopes is not None else ["pricing"],
        "action_ids": [],
        "authorized_actions": [],
        "plan_id": "PLAN-1",
        "decision_snapshot": snapshot,
        "interrupt_reason": interrupt_reason,
        "redirect_instruction": redirect_instruction,
        "provenance_path": provenance_path or [],
        "interrupt_enforced": False,
    }


def workspace(
    workspace_id: str = "WS-1",
    graph_version: str = "graph-v17",
    assignments=None,
    report=None,
    authorizations=None,
    approved_mutations=None,
) -> dict:
    payload = {
        "id": workspace_id,
        "graph_version": graph_version,
        "supervisor": {
            "id": "SUP-1",
            "execution_mode": "live",
            "adapter": "claude-code-hook-runtime",
            "assignments": assignments if assignments is not None else [assignment()],
        },
        "invalidation_report": report,
        "approved_mutations": approved_mutations or [],
    }
    payload.update(authorizations or {})
    return payload


def invalidation_report() -> dict:
    return {
        "graph_version": "graph-v17",
        "changed_decision_id": "DEC-018",
        "superseded_decision_id": "DEC-004",
        "affected_scopes": ["pricing"],
        "affected_artifact_ids": ["SPEC-009"],
        "preserved_task_ids": ["TASK-101"],
        "invalidated_task_ids": ["TASK-102"],
        "preserved_artifact_ids": ["SPEC-011"],
        "paths": [{"artifact_id": "TASK-102", "node_ids": ["DEC-018", "SPEC-009"]}],
        "evidence_refs": ["evidence://slack/C123/p1700000000"],
    }


def approved_mutation() -> dict:
    return {
        "mutation": {
            "decision": {
                "id": "DEC-018",
                "kind": "Decision",
                "title": "Annual plans move to net-30 terms",
                "text": "Net-30 replaces prepay for annual plans.",
            },
            "supersedes_id": "DEC-004",
            "affected_scopes": ["pricing"],
        },
        "actor_role": "approve_compliance",
        "approval_evidence": {
            "workspace_id": "WS-1",
            "decision_id": "DEC-018",
            "approver_user_id": "U123",
            "permission_id": "approve_compliance",
            "channel": "slack",
            "evidence_ref": "evidence://slack/C123/p1700000000",
            "approved_at": iso(NOW - timedelta(minutes=10)),
            "confirmed_proposal_fingerprint": "sha256:" + "a" * 64,
            "confirmed_proposal_instance_id": "WS-1:DEC-018:1",
        },
    }


def _opener(payload: str):
    """A urlopen stand-in returning one fixed body."""

    def opener(_request, timeout=None):  # noqa: ANN001 - stdlib shape
        response = mock.MagicMock()
        response.read.return_value = payload.encode("utf-8")
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        return response

    return opener


def config(branch: str = "feature/TASK-102-net-30", repo_root: Path = None, **kwargs):
    return check.Config(
        agent_url="http://agent.test",
        repo_root=repo_root or Path("/nonexistent-repo-root"),
        branch=branch,
        timeout_seconds=5.0,
        **kwargs
    )


def bind(workspaces, cfg) -> check.Binding:
    return check.resolve_binding(
        cfg.branch, cfg.repo_root, check.live_claude_candidates(workspaces)
    )


def run(workspaces, cfg) -> check.Outcome:
    return check.evaluate(bind(workspaces, cfg), cfg, now=NOW)


class BranchBindingTests(unittest.TestCase):
    def test_exact_token_match_binds(self):
        outcome = run([workspace()], config())
        self.assertEqual(outcome.binding.source, check.SOURCE_BRANCH)
        self.assertEqual(outcome.binding.candidate.task_id, "TASK-102")

    def test_prefix_of_a_longer_task_id_does_not_bind(self):
        # A branch named for TASK-10 must never bind TASK-102, and vice versa.
        self.assertFalse(check.branch_mentions_task("feature/TASK-102", "TASK-10"))
        self.assertTrue(check.branch_mentions_task("feature/TASK-102", "TASK-102"))
        self.assertTrue(check.branch_mentions_task("TASK-102", "TASK-102"))
        self.assertFalse(check.branch_mentions_task("feature/xTASK-102", "TASK-102"))

    def test_two_matching_assignments_resolve_to_unbound(self):
        other = workspace(
            workspace_id="WS-2", assignments=[assignment(task_id="TASK-102")]
        )
        outcome = run([workspace(), other], config())
        self.assertEqual(outcome.binding.source, check.SOURCE_UNBOUND)
        self.assertTrue(outcome.ok)

    def test_unbound_passes_by_default_and_fails_under_require_binding(self):
        lenient = run([workspace()], config(branch="chore/docs"))
        self.assertTrue(lenient.ok)
        self.assertEqual(lenient.code, "UNBOUND")

        strict = run([workspace()], config(branch="chore/docs", require_binding=True))
        self.assertFalse(strict.ok)
        self.assertEqual(strict.exit_code, check.EXIT_UNAUTHORIZED)


class CandidateFilterTests(unittest.TestCase):
    def test_simulated_codex_and_completed_assignments_are_not_bindable(self):
        cases = [
            assignment(task_id="TASK-201", execution_mode="simulated"),
            assignment(task_id="TASK-202", provider="codex"),
            assignment(task_id="TASK-203", state="completed"),
        ]
        candidates = check.live_claude_candidates([workspace(assignments=cases)])
        self.assertEqual(candidates, [])

    def test_simulated_supervisor_contributes_nothing(self):
        payload = workspace()
        payload["supervisor"]["execution_mode"] = "simulated"
        self.assertEqual(check.live_claude_candidates([payload]), [])


class MarkerFileTests(unittest.TestCase):
    def test_task_file_binds_when_the_branch_does_not(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".dragback").mkdir()
            (root / ".dragback" / "task").write_text("TASK-102\n", encoding="utf-8")
            outcome = run(
                [workspace()], config(branch="chore/no-task-id", repo_root=root)
            )
            self.assertEqual(outcome.binding.source, check.SOURCE_TASK_FILE)

    def test_attach_file_takes_precedence_over_the_branch(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".dragback").mkdir()
            (root / ".dragback" / "attach").write_text(
                "ASSIGNMENT-TASK-101\n", encoding="utf-8"
            )
            payload = workspace(
                assignments=[assignment(), assignment(task_id="TASK-101")]
            )
            outcome = run([payload], config(repo_root=root))
            self.assertEqual(outcome.binding.source, check.SOURCE_EXPLICIT)
            self.assertEqual(outcome.binding.candidate.task_id, "TASK-101")

    def test_a_symlinked_marker_is_refused(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".dragback").mkdir()
            target = root / "elsewhere"
            target.write_text("TASK-102\n", encoding="utf-8")
            os.symlink(str(target), str(root / ".dragback" / "task"))
            self.assertIsNone(check.read_marker_file(root, "task"))

    def test_a_multi_line_marker_is_refused(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".dragback").mkdir()
            (root / ".dragback" / "task").write_text(
                "TASK-102\nTASK-103\n", encoding="utf-8"
            )
            self.assertIsNone(check.read_marker_file(root, "task"))


class VerdictTests(unittest.TestCase):
    def test_interrupted_assignment_fails_with_the_full_explanation(self):
        payload = workspace(
            assignments=[
                assignment(
                    state="interrupted",
                    interrupt_reason=(
                        "Approved decision DEC-018 changed pricing at graph-v17."
                    ),
                    redirect_instruction=(
                        "Stop Task TASK-102. Request a corrected plan for pricing."
                    ),
                    provenance_path=["DEC-018", "DEC-004", "SPEC-009", "TASK-102"],
                )
            ],
            report=invalidation_report(),
            approved_mutations=[approved_mutation()],
        )
        outcome = run([payload], config())
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, "ASSIGNMENT_INTERRUPTED")
        self.assertEqual(outcome.exit_code, check.EXIT_UNAUTHORIZED)

        rendered = check.render(outcome, config())
        # The same three things the hook tells the model, plus the path.
        self.assertIn("Still valid", rendered)
        self.assertIn("TASK-101", rendered)
        self.assertIn("No longer", rendered)
        self.assertIn("TASK-102", rendered)
        self.assertIn("Now required", rendered)
        self.assertIn("Request a corrected plan", rendered)
        self.assertIn("DEC-018 → DEC-004 → SPEC-009 → TASK-102 → this branch", rendered)
        self.assertIn("evidence://slack/C123/p1700000000", rendered)
        self.assertIn("Approved by approve_compliance", rendered)
        self.assertIn("Annual plans move to net-30 terms", rendered)
        self.assertIn("pricing", rendered)

    def test_preserved_sibling_passes_even_though_its_snapshot_is_behind(self):
        # Invalidation is scope-sensitive: an out-of-scope sibling survives, and
        # `_apply_supervisor_invalidation` leaves its snapshot behind on purpose.
        payload = workspace(
            graph_version="graph-v17",
            assignments=[
                assignment(
                    task_id="TASK-101",
                    state="continuing",
                    snapshot="graph-v16",
                    scopes=["billing"],
                )
            ],
            report=invalidation_report(),
        )
        outcome = run([payload], config(branch="feature/TASK-101-invoices"))
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.code, "PRESERVED")

    def test_stale_assignment_snapshot_fails(self):
        payload = workspace(
            graph_version="graph-v17",
            assignments=[assignment(state="redirected", snapshot="graph-v16")],
            report=invalidation_report(),
        )
        outcome = run([payload], config())
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, "STALE_ASSIGNMENT_SNAPSHOT")
        self.assertIn("graph-v16", outcome.detail)
        self.assertIn("graph-v17", outcome.detail)

    def test_missing_assignment_state_change_still_passes_when_current(self):
        payload = workspace(
            authorizations={"initial_authorization": {"grant": grant()}},
        )
        outcome = run([payload], config())
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.code, "AUTHORIZED")


class GrantTests(unittest.TestCase):
    def test_stale_grant_snapshot_fails_even_when_the_assignment_is_current(self):
        payload = workspace(
            graph_version="graph-v17",
            assignments=[assignment(state="redirected", snapshot="graph-v17")],
            authorizations={
                "initial_authorization": {"grant": grant(snapshot="graph-v16")}
            },
            report=invalidation_report(),
        )
        outcome = run([payload], config())
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, "STALE_GRANT_SNAPSHOT")
        self.assertEqual(outcome.exit_code, check.EXIT_UNAUTHORIZED)

    def test_expired_grant_fails(self):
        payload = workspace(
            authorizations={
                "initial_authorization": {
                    "grant": grant(expires_at=NOW - timedelta(seconds=1))
                }
            }
        )
        outcome = run([payload], config())
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, "GRANT_EXPIRED")

    def test_non_allow_grant_fails(self):
        payload = workspace(
            authorizations={"initial_authorization": {"grant": grant(verdict="REPLAN")}}
        )
        outcome = run([payload], config())
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, "GRANT_NOT_ALLOW")

    def test_unreadable_expiry_fails_closed(self):
        broken = grant()
        broken["expires_at"] = "not-a-timestamp"
        payload = workspace(authorizations={"initial_authorization": {"grant": broken}})
        outcome = run([payload], config())
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, "GRANT_UNREADABLE")

    def test_replacement_grant_answers_over_the_initial_one(self):
        payload = workspace(
            authorizations={
                "initial_authorization": {
                    "grant": grant(
                        snapshot="graph-v16", issued_at=NOW - timedelta(hours=2)
                    )
                },
                "replacement_authorization": {
                    "grant": grant(
                        snapshot="graph-v17", issued_at=NOW - timedelta(minutes=1)
                    )
                },
            }
        )
        outcome = run([payload], config())
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.code, "AUTHORIZED")

    def test_a_bound_branch_with_no_grant_fails_by_default(self):
        """`--require-grant` is now the default, scoped by where it is applied."""

        strict = run([workspace()], config())
        self.assertFalse(strict.ok)
        self.assertEqual(strict.code, "NO_GRANT")
        self.assertEqual(strict.exit_code, check.EXIT_UNAUTHORIZED)

        lenient = run([workspace()], config(require_grant=False))
        self.assertTrue(lenient.ok)

    def test_an_unbound_branch_still_passes_with_no_grant(self):
        """The scoping that keeps a docs PR green.

        An unbound branch returns from `evaluate` at step 1 and never reaches
        the grant check, so turning the grant requirement on by default cannot
        fail a pull request that touches no governed task.
        """

        outcome = run([workspace()], config(branch="chore/update-readme"))
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.code, "UNBOUND")
        self.assertEqual(outcome.exit_code, check.EXIT_OK)

    def test_allow_missing_grant_flag_turns_the_default_back_off(self):
        parser = check.build_parser()
        default = check.build_config(
            parser.parse_args(["--branch", "feature/TASK-102-net-30"]),
            {"DRAGBACK_AGENT_URL": "http://agent.test"},
        )
        self.assertTrue(default.require_grant)
        self.assertFalse(default.require_binding)

        relaxed = check.build_config(
            parser.parse_args(
                ["--branch", "feature/TASK-102-net-30", "--allow-missing-grant"]
            ),
            {"DRAGBACK_AGENT_URL": "http://agent.test"},
        )
        self.assertFalse(relaxed.require_grant)


class RedirectedBranchTests(unittest.TestCase):
    """The branch that was invalidated, redirected, re-authorized, and must pass again.

    This is the case a naive implementation fails forever. `_mark_artifact` only
    ever downgrades validity and nothing clears `invalidated_scopes`, so
    `invalidated_task_ids` keeps naming TASK-102 for the life of the workspace.
    A gate that consults that list can never be satisfied: the developer does
    exactly what the redirect instruction told them to do, gets re-authorized
    against the new snapshot, and the check still fails. The gate is snapshot
    equality; the report feeds the explanation only.
    """

    def _redirected_workspace(self, **overrides):
        payload = workspace(
            graph_version="graph-v18",
            assignments=[
                assignment(
                    state="redirected",
                    snapshot="graph-v18",
                    interrupt_reason="Annual plans move to net-30 terms.",
                    redirect_instruction="Re-plan against net-30.",
                    provenance_path=["DEC-018", "SPEC-009", "TASK-102"],
                )
            ],
            report=invalidation_report(),
            approved_mutations=[approved_mutation()],
            authorizations={
                "initial_authorization": {
                    "grant": grant(
                        snapshot="graph-v17", issued_at=NOW - timedelta(hours=2)
                    )
                },
                "replacement_authorization": {
                    "grant": grant(
                        snapshot="graph-v18", issued_at=NOW - timedelta(minutes=1)
                    )
                },
            },
        )
        payload.update(overrides)
        return payload

    def test_redirected_then_re_authorized_passes_again(self):
        payload = self._redirected_workspace()

        # The trap, stated: this task is still named as invalidated, and always
        # will be. Passing while that is true is the point of the test.
        self.assertIn("TASK-102", payload["invalidation_report"]["invalidated_task_ids"])

        outcome = run([payload], config())
        self.assertTrue(outcome.ok, outcome.detail)
        self.assertEqual(outcome.code, "AUTHORIZED")
        self.assertEqual(outcome.exit_code, check.EXIT_OK)

    def test_the_same_branch_fails_before_it_is_re_authorized(self):
        """The other half: the pass above is earned, not the check going blind."""

        interrupted = self._redirected_workspace()
        interrupted["supervisor"]["assignments"] = [
            assignment(
                state="interrupted",
                snapshot="graph-v17",
                interrupt_reason="Annual plans move to net-30 terms.",
                redirect_instruction="Re-plan against net-30.",
                provenance_path=["DEC-018", "SPEC-009", "TASK-102"],
            )
        ]
        outcome = run([interrupted], config())
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, "ASSIGNMENT_INTERRUPTED")

        # And again for the assignment that complied but was never re-granted:
        # redirected onto graph-v18 while the only grant still names graph-v17.
        stale_grant = self._redirected_workspace(
            initial_authorization={"grant": grant(snapshot="graph-v17")},
            replacement_authorization=None,
        )
        stale_grant.pop("replacement_authorization")
        outcome = run([stale_grant], config())
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, "STALE_GRANT_SNAPSHOT")


class FailClosedTests(unittest.TestCase):
    """The whole reason this check exists: the hook allows here, we must not."""

    def _main(self, opener_side_effect=None, payload=None, argv=None):
        environment = {"DRAGBACK_AGENT_URL": "http://agent.test"}
        if opener_side_effect is not None:
            patched = mock.patch.object(
                check.urllib.request, "urlopen", side_effect=opener_side_effect
            )
        else:
            response = mock.MagicMock()
            response.read.return_value = json.dumps(payload).encode("utf-8")
            response.__enter__.return_value = response
            response.__exit__.return_value = False
            patched = mock.patch.object(
                check.urllib.request, "urlopen", return_value=response
            )
        stdout = io.StringIO()
        with patched, mock.patch.object(sys, "stdout", stdout):
            code = check.main(
                argv or ["--branch", "feature/TASK-102-net-30"], env=environment
            )
        return code, stdout.getvalue()

    def test_unreachable_service_fails_the_check(self):
        code, output = self._main(opener_side_effect=OSError("connection refused"))
        self.assertEqual(code, check.EXIT_UNREACHABLE)
        self.assertIn("failing closed", output)
        self.assertIn("no cached-verdict", output)

    def test_non_json_response_fails_the_check(self):
        response = mock.MagicMock()
        response.read.return_value = b"<html>502</html>"
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        stdout = io.StringIO()
        with mock.patch.object(
            check.urllib.request, "urlopen", return_value=response
        ), mock.patch.object(sys, "stdout", stdout):
            code = check.main(
                ["--branch", "feature/TASK-102-net-30"],
                env={"DRAGBACK_AGENT_URL": "http://agent.test"},
            )
        self.assertEqual(code, check.EXIT_UNREACHABLE)
        self.assertIn("failing closed", stdout.getvalue())

    def test_response_without_a_workspaces_list_fails_the_check(self):
        code, output = self._main(payload={"correlation_id": "abc"})
        self.assertEqual(code, check.EXIT_UNREACHABLE)
        self.assertIn("failing closed", output)

    def test_missing_agent_url_on_a_runner_is_a_failure_not_a_skip(self):
        stderr = io.StringIO()
        with mock.patch.object(sys, "stderr", stderr):
            code = check.main(
                ["--branch", "feature/TASK-102-net-30"],
                env={"GITHUB_ACTIONS": "true"},
            )
        self.assertEqual(code, check.EXIT_USAGE)
        self.assertIn("Failing closed", stderr.getvalue())

    def test_interrupted_assignment_exits_non_zero_through_main(self):
        payload = {
            "workspaces": [
                workspace(
                    assignments=[
                        assignment(
                            state="interrupted",
                            interrupt_reason="Approved decision DEC-018 changed pricing.",
                            redirect_instruction="Stop and request a corrected plan.",
                            provenance_path=["DEC-018", "TASK-102"],
                        )
                    ],
                    report=invalidation_report(),
                )
            ]
        }
        code, output = self._main(payload=payload)
        self.assertEqual(code, check.EXIT_UNAUTHORIZED)
        self.assertIn("DRAGBACK", output)
        self.assertIn("Now required", output)

    def test_authorized_branch_exits_zero_through_main(self):
        # `main` uses the real clock, so this grant is anchored to it rather
        # than to the frozen NOW the unit-level tests use.
        live_now = datetime.now(timezone.utc)
        payload = {
            "workspaces": [
                workspace(
                    authorizations={
                        "initial_authorization": {
                            "grant": grant(
                                issued_at=live_now - timedelta(minutes=1),
                                expires_at=live_now + timedelta(hours=1),
                            )
                        }
                    }
                )
            ]
        }
        code, output = self._main(payload=payload)
        self.assertEqual(code, check.EXIT_OK)
        self.assertIn("current approved decision snapshot", output)


class BranchResolutionTests(unittest.TestCase):
    def test_pull_request_head_ref_wins_over_the_detached_ref_name(self):
        env = {"GITHUB_HEAD_REF": "feature/TASK-102", "GITHUB_REF_NAME": "42/merge"}
        self.assertEqual(
            check.resolve_branch(env, Path(".")), "feature/TASK-102"
        )

    def test_a_literal_head_is_not_treated_as_a_branch(self):
        with TemporaryDirectory() as directory:
            resolved = check.resolve_branch(
                {"GITHUB_REF_NAME": "HEAD"}, Path(directory)
            )
            self.assertEqual(resolved, "")


class RenderingTests(unittest.TestCase):
    def test_provenance_is_a_path_not_a_badge(self):
        rendered = check.compact_provenance(["DEC-018", "SPEC-009", "TASK-102"])
        self.assertEqual(rendered, "DEC-018 → SPEC-009 → TASK-102")

    def test_long_provenance_is_truncated_and_says_so(self):
        nodes = ["N%d" % index for index in range(40)]
        rendered = check.compact_provenance(nodes)
        self.assertIn("more truncated", rendered)
        self.assertIn("N39", rendered)

    def test_attribution_is_omitted_rather_than_invented(self):
        attribution, decision_text = check.approval_attribution(workspace())
        self.assertEqual(attribution, "")
        self.assertEqual(decision_text, "")

    def test_json_output_carries_the_machine_readable_verdict(self):
        payload = workspace(
            assignments=[assignment(state="interrupted")], report=invalidation_report()
        )
        outcome = run([payload], config())
        as_dict = outcome.as_dict()
        self.assertFalse(as_dict["ok"])
        self.assertEqual(as_dict["task_id"], "TASK-102")
        self.assertEqual(as_dict["code"], "ASSIGNMENT_INTERRUPTED")
        json.dumps(as_dict)  # must stay serialisable


class UnresolvedAttachmentTests(unittest.TestCase):
    """`.dragback/attach` must not be an opt-out from a required check.

    Raised by the cross-model review of the integration. An attachment that is
    read successfully but names no live assignment used to resolve to UNBOUND,
    and UNBOUND passes by default — so committing one junk marker file made a
    bound branch merge without its authorization ever being checked.
    """

    def _with_attach(self, contents: str, workspaces):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".dragback").mkdir()
            (root / ".dragback" / "attach").write_text(contents, encoding="utf-8")
            return run(workspaces, config(repo_root=root))

    def test_an_attachment_naming_nothing_fails_rather_than_passing(self):
        outcome = self._with_attach("ASSIGNMENT-DOES-NOT-EXIST\n", [workspace()])
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, "UNRESOLVED_ATTACHMENT")
        self.assertEqual(outcome.exit_code, check.EXIT_UNAUTHORIZED)

    def test_an_ambiguous_attachment_fails(self):
        other = workspace(workspace_id="WS-2")
        outcome = self._with_attach("ASSIGNMENT-TASK-102\n", [workspace(), other])
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, "UNRESOLVED_ATTACHMENT")

    def test_it_fails_even_without_require_binding(self):
        """The whole point: --require-binding is off by default and this still fails."""

        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".dragback").mkdir()
            (root / ".dragback" / "attach").write_text("NOPE\n", encoding="utf-8")
            cfg = config(repo_root=root)
            self.assertFalse(cfg.require_binding)
            self.assertFalse(run([workspace()], cfg).ok)

    def test_a_genuinely_unbound_branch_still_passes(self):
        """Absence of binding information is still permissive. Only a failed
        explicit request is a failure."""

        outcome = run([workspace()], config(branch="chore/update-readme"))
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.code, "UNBOUND")

    def test_an_unreadable_marker_still_falls_through(self):
        """A symlinked or multi-line marker is ignored, matching the service."""

        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".dragback").mkdir()
            (root / ".dragback" / "attach").write_text("A\nB\n", encoding="utf-8")
            outcome = run(
                [workspace()],
                config(branch="feature/TASK-102-net-30", repo_root=root),
            )
            self.assertEqual(outcome.binding.source, check.SOURCE_BRANCH)


class MalformedResponseTests(unittest.TestCase):
    """INT-1. A clean answer with nothing in it passes; a broken answer fails.

    Silently discarding a malformed workspace or assignment can empty the
    candidate set, resolve the branch to UNBOUND — which passes by default — and
    merge work whose authorization was never evaluated. So the rule is the one
    the `.dragback/attach` marker settled: absence of binding information is
    permissive, failure to OBTAIN it is not.
    """

    def test_clean_answers_with_no_candidates_still_pass(self):
        simulated_supervisor = workspace()
        simulated_supervisor["supervisor"]["execution_mode"] = "simulated"
        filtered_assignments = workspace(
            assignments=[
                assignment(task_id="TASK-201", execution_mode="simulated"),
                assignment(task_id="TASK-202", provider="codex"),
                assignment(task_id="TASK-203", state="completed"),
            ]
        )
        cases = {
            "empty": [],
            "null supervisor": [
                {"id": "WS-IDLE", "graph_version": "graph-v17", "supervisor": None}
            ],
            "simulated supervisor": [simulated_supervisor],
            "non-bindable assignments": [filtered_assignments],
        }
        for name, workspaces in cases.items():
            with self.subTest(name=name):
                candidates = check.live_claude_candidates(workspaces)
                self.assertEqual(candidates, [])
                outcome = check.evaluate(
                    check.resolve_binding(
                        "feature/TASK-102-net-30",
                        Path("/nope"),
                        candidates,
                    ),
                    config(),
                    now=NOW,
                )
                self.assertTrue(outcome.ok)
                self.assertEqual(outcome.code, "UNBOUND")

    def test_a_workspace_that_is_not_an_object_fails(self):
        payload = json.dumps({"workspaces": [workspace(), "not-a-workspace"]})
        with self.assertRaises(check.MalformedServiceResponse):
            check.fetch_workspaces(config(), opener=_opener(payload))

    def test_a_supervisor_that_is_not_an_object_fails(self):
        payload = workspace()
        payload["supervisor"] = "live"
        with self.assertRaises(check.MalformedServiceResponse):
            check.live_claude_candidates([payload])

    def test_assignments_that_are_not_a_list_fail(self):
        payload = workspace()
        payload["supervisor"]["assignments"] = {"0": assignment()}
        with self.assertRaises(check.MalformedServiceResponse):
            check.live_claude_candidates([payload])

    def test_an_assignment_that_is_not_an_object_fails(self):
        payload = workspace(assignments=[assignment(), "ASSIGNMENT-TASK-999"])
        with self.assertRaises(check.MalformedServiceResponse):
            check.live_claude_candidates([payload])

    def test_missing_supervisor_and_assignments_fail(self):
        no_supervisor = workspace()
        no_supervisor.pop("supervisor")
        no_assignments = workspace()
        no_assignments["supervisor"].pop("assignments")
        for name, payload in {
            "supervisor": no_supervisor,
            "assignments": no_assignments,
        }.items():
            with self.subTest(name=name):
                with self.assertRaises(check.MalformedServiceResponse):
                    check.live_claude_candidates([payload])

    def test_missing_or_unknown_supervisor_mode_fails(self):
        missing = workspace()
        missing["supervisor"].pop("execution_mode")
        unknown = workspace()
        unknown["supervisor"]["execution_mode"] = "future"
        for name, payload in {"missing": missing, "unknown": unknown}.items():
            with self.subTest(name=name):
                with self.assertRaises(check.MalformedServiceResponse):
                    check.live_claude_candidates([payload])

    def test_missing_or_unknown_candidate_discriminators_fail(self):
        cases = {}
        for field in ("execution_mode", "runtime_provider", "state"):
            missing = assignment()
            missing.pop(field)
            cases["missing " + field] = missing
        cases["unknown execution_mode"] = assignment(execution_mode="future")
        cases["unknown runtime_provider"] = assignment(provider="future")
        cases["unknown state"] = assignment(state="hibernating")
        for name, malformed in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(check.MalformedServiceResponse):
                    check.live_claude_candidates(
                        [workspace(assignments=[malformed])]
                    )

    def test_malformed_bindable_candidate_fields_fail(self):
        cases = {}
        for field in ("id", "task_id", "decision_snapshot"):
            malformed = assignment()
            malformed.pop(field)
            cases["assignment " + field] = workspace(assignments=[malformed])
        malformed_scopes = assignment()
        malformed_scopes["scopes"] = ["pricing", None]
        cases["assignment scopes"] = workspace(assignments=[malformed_scopes])
        for field in ("id", "graph_version"):
            malformed_workspace = workspace()
            malformed_workspace.pop(field)
            cases["workspace " + field] = malformed_workspace
        for name, payload in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(check.MalformedServiceResponse):
                    check.live_claude_candidates([payload])

    def test_one_malformed_candidate_poisons_the_whole_response(self):
        malformed = assignment(task_id="TASK-103")
        malformed["scopes"] = {"pricing": True}
        payload = workspace(assignments=[assignment(), malformed])
        with self.assertRaises(check.MalformedServiceResponse):
            check.live_claude_candidates([payload])

    def test_unknown_extra_fields_are_harmless_schema_drift(self):
        drifted = dict(assignment(), unexpected_field="ignored")
        candidates = check.live_claude_candidates(
            [workspace(assignments=[drifted])]
        )
        self.assertEqual([item.task_id for item in candidates], ["TASK-102"])

    def test_the_malformed_verdict_is_reported_as_itself(self):
        payload = json.dumps({"workspaces": ["broken"]})
        stdout = io.StringIO()
        with mock.patch.object(
            check.urllib.request, "urlopen", _opener(payload)
        ), mock.patch.object(sys, "stdout", stdout):
            code = check.main(
                ["--branch", "feature/TASK-102-net-30"],
                env={"DRAGBACK_AGENT_URL": "http://agent.test"},
            )
        self.assertEqual(code, check.EXIT_UNREACHABLE)
        self.assertIn("cannot trust", stdout.getvalue())
        # "Could not be reached" would be wrong: it answered.
        self.assertNotIn("could not be reached", stdout.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
