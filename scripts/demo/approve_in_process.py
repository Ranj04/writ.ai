#!/usr/bin/env python3
"""Approve the workspace baseline in process, for the demo launcher only.

`writai workspace approve-baseline` is DISABLED on purpose (`cli.py`
`_request_for_command`): Lane B routed every approval through an
`ApprovalAttemptEnvelope` carrying a Hexclave-resolvable `approval_token`, so
that the approver's permission is actually verified. Its stated replacement is
"the authenticated Workspace approval UI", which needs a browser and a token
that no local demo has.

That left `scripts/demo/up.sh` unable to arm the stage at all after the lanes
merged. This is the seam that unblocks it, and it is the same one
`scripts/demo/seed.py` already uses.

**What this bypasses:** the *channel* authentication in front of the approval —
nobody proves who the approver is.

**What it does NOT bypass:** any authority decision. Role, scope, confidence,
the three-way requirement match and Lane B's proposal-binding check (fingerprint
plus instance id must match the exact stored proposal) all run unchanged inside
`approve_baseline`. Nothing in the service is modified, patched or monkeyed:
this is a second caller of the same orchestrator method the route calls.

It is gated on the same explicit opt-in as the seeder, and it appears on the
real-vs-simulated panel.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))

OPT_IN_ENV = "WRITAI_DEMO_UNAUTHENTICATED_APPROVAL"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REFUSED = 3
EXIT_FAILED = 4


def _refuse(message: str) -> int:
    sys.stderr.write(f"writai-demo: {message}\n")
    return EXIT_REFUSED


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        sys.stderr.write(
            "usage: approve_in_process.py WORKSPACE_ID ROLE [DECISION_ID]\n"
            "\n"
            "With two arguments, approves the workspace BASELINE.\n"
            "With three, approves the pending decision CHANGE.\n"
            "\n"
            "Both call the orchestrator directly, because every authenticated\n"
            "approval path needs a Hexclave-resolvable token that no local demo\n"
            "has. Neither bypasses an authority check.\n"
        )
        return EXIT_USAGE

    workspace_id, role = argv[0], argv[1]
    decision_id = argv[2] if len(argv) == 3 else None

    if (os.environ.get(OPT_IN_ENV) or "").strip() != "1":
        return _refuse(
            "refusing to approve without channel authentication.\n\n"
            "  This approves the baseline by calling the orchestrator directly,\n"
            "  so nobody proves who the approver is. Every authority check still\n"
            "  runs -- role, scope, confidence, the three-way requirement match\n"
            "  and the proposal binding.\n\n"
            "  That is fine on a demo machine and nowhere else, so say so:\n\n"
            f"      export {OPT_IN_ENV}=1\n"
        )

    # Imported after the gate so a refusal costs nothing and touches no state.
    from writai.domain import utc_now
    from writai.hashing import stable_hash
    from writai.intake.approval import ApprovalChannel, ApprovalEvidence
    from writai.services import agent_api
    from writai.workspaces.models import WorkspaceApprovalRequest

    repository = agent_api.workspace_repository
    orchestrator = agent_api.workspace_orchestrator

    try:
        record = repository.get(workspace_id)
    except Exception as exc:  # noqa: BLE001 - report, never traceback at the operator
        sys.stderr.write(f"writai-demo: cannot read workspace {workspace_id}: {exc}\n")
        return EXIT_FAILED

    if decision_id is None:
        decision = record.definition.baseline_decision
        instance_id = record.baseline_proposal_instance_id
        confirmed_decision_id = decision.id
        missing = "no baseline proposal instance id; import it first"
    else:
        # A change is fingerprinted over the WHOLE mutation, not just its
        # decision: the supersession target and the affected scopes are part of
        # what the human confirmed.
        decision = record.pending_mutation
        instance_id = record.pending_proposal_instance_id
        confirmed_decision_id = decision_id
        missing = "no pending decision change to approve; propose one first"
    if decision is None or instance_id is None:
        sys.stderr.write(f"writai-demo: the workspace carries {missing}.\n")
        return EXIT_FAILED

    # Bind to the exact proposal on disk right now. Lane B requires the
    # fingerprint and instance id to match so an approval cannot land on a
    # proposal that changed after the approver saw it, and this honours that
    # rather than routing around it.
    fingerprint = stable_hash(decision)
    request = WorkspaceApprovalRequest(
        actor_role=role,
        proposal_fingerprint=fingerprint,
        proposal_instance_id=instance_id,
        approval_evidence=ApprovalEvidence(
            workspace_id=workspace_id,
            decision_id=confirmed_decision_id,
            approver_user_id=f"demo-launcher:{role}",
            permission_id=role,
            channel=ApprovalChannel.CLI,
            evidence_ref=f"demo://{workspace_id}/{confirmed_decision_id}",
            approved_at=utc_now(),
            confirmed_proposal_fingerprint=fingerprint,
            confirmed_proposal_instance_id=instance_id,
        ),
    )

    what = "baseline" if decision_id is None else f"change {decision_id}"
    try:
        if decision_id is None:
            orchestrator.approve_baseline(workspace_id, request)
        else:
            orchestrator.approve_decision(workspace_id, decision_id, request)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"writai-demo: {what} approval refused: {exc}\n")
        return EXIT_FAILED

    print(f"{what} approved as {role} (channel authentication bypassed, demo only)")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
