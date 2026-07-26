"""Approver-side CLI; every apply uses the shared Hexclave-checked path."""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import SecretStr

from writai.domain import (
    ApprovalStatus,
    Artifact,
    ArtifactKind,
    DecisionMutation,
    ValidityStatus,
)
from writai.intake.approval import (
    ApprovalAttemptRequest,
    ApprovalChannel,
    PendingApproval,
    actual_partition_from_workspace,
    pending_from_workspace,
)
from writai.intake.decisions import (
    DecisionDraftError,
    select_seeded_supersession_root,
    select_supersession_target,
)
from writai.llm.extractor import evidence_span_error

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime cycle
    from writai.cli import Route

GROUP_NAME = "approve"

AddRuntimeOptions = Callable[..., None]

_AMBER = "\x1b[38;5;214m"
_GREEN = "\x1b[32m"
_RESET = "\x1b[0m"


class ApprovalClient(Protocol):
    def request(
        self,
        route: Route,
        *,
        workspace_id: str | None = None,
        decision_id: str | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


def register(
    subparsers: Any,
    add_runtime_options: AddRuntimeOptions,
) -> argparse.ArgumentParser:
    """Register `writai approve ...`. Called once from `cli.build_parser`."""

    group = subparsers.add_parser(
        GROUP_NAME,
        help="Review and approve pending decision changes.",
    )
    add_runtime_options(group, defaults=False)
    group.add_argument(
        "--text",
        help="Decision sentence to extract, preview, and approve.",
    )
    group.add_argument(
        "--workspace",
        dest="intake_workspace",
        help="Workspace receiving --text (auto-selected when unambiguous).",
    )
    group.add_argument(
        "--scope",
        help="Explicit affected scope; with --was/--now, bypasses extraction.",
    )
    group.add_argument(
        "--was",
        help="Explicit current value; requires --scope and --now.",
    )
    group.add_argument(
        "--now",
        help="Explicit replacement value; requires --scope and --was.",
    )
    commands = group.add_subparsers(dest="command")

    def command_parser(name: str, help_text: str) -> argparse.ArgumentParser:
        child = commands.add_parser(name, help=help_text)
        add_runtime_options(child, defaults=False)
        return child

    pending = command_parser(
        "pending",
        "List pending decision changes and their blast radius.",
    )
    pending.add_argument("--workspace", help="Limit to one workspace.")

    change = command_parser(
        "change",
        "Approve one pending decision change as an authoritative role.",
    )
    change.add_argument("workspace_id")
    change.add_argument("decision_id")

    return group


def _list_workspaces(client: ApprovalClient) -> list[dict[str, Any]]:
    from writai.cli import Route

    payload = client.request(Route("GET", "/live-workspaces"))
    raw_items = payload.get("workspaces", [])
    if not isinstance(raw_items, list):
        return []
    return [item for item in raw_items if isinstance(item, dict)]


def _show_workspace(
    client: ApprovalClient,
    workspace_id: str,
) -> dict[str, Any]:
    from writai.cli import Route

    return client.request(
        Route("GET", "/live-workspaces/{workspace_id}"),
        workspace_id=workspace_id,
    )


def _preview_change(
    client: ApprovalClient,
    *,
    workspace_id: str,
    decision_id: str,
) -> dict[str, Any]:
    from writai.cli import Route

    return client.request(
        Route(
            "GET",
            "/live-workspaces/{workspace_id}/decisions/{decision_id}/preview",
        ),
        workspace_id=workspace_id,
        decision_id=decision_id,
    )


def _assignment_labels(
    workspace: dict[str, Any],
) -> dict[str, str]:
    supervisor = workspace.get("supervisor")
    if not isinstance(supervisor, dict):
        return {}
    assignments = supervisor.get("assignments")
    if not isinstance(assignments, list):
        return {}
    labels: dict[str, str] = {}
    for assignment in assignments:
        if not isinstance(assignment, dict):
            continue
        assignment_id = assignment.get("id")
        if not isinstance(assignment_id, str) or not assignment_id:
            continue
        agent_name = assignment.get("agent_name")
        task_title = assignment.get("task_title")
        parts = [
            value
            for value in (agent_name, task_title)
            if isinstance(value, str) and value.strip()
        ]
        labels[assignment_id] = " · ".join(parts) or assignment_id
    return labels


def _approved_decisions(workspace: Mapping[str, Any]) -> list[Artifact]:
    decisions: list[Artifact] = []
    baseline = workspace.get("baseline_decision")
    if isinstance(baseline, Mapping):
        decisions.append(Artifact.model_validate(baseline))
    approved = workspace.get("approved_mutations")
    if isinstance(approved, list):
        for item in approved:
            if not isinstance(item, Mapping):
                continue
            mutation = item.get("mutation")
            if isinstance(mutation, Mapping):
                decisions.append(
                    DecisionMutation.model_validate(mutation).decision
                )
    return [
        item
        for item in decisions
        if item.approval_status is ApprovalStatus.APPROVED
    ]


def _baseline_decision_id(workspace: Mapping[str, Any]) -> str:
    from writai.cli import CliError

    baseline = workspace.get("baseline_decision")
    decision_id = baseline.get("id") if isinstance(baseline, Mapping) else None
    if not isinstance(decision_id, str) or not decision_id.strip():
        raise CliError(
            "The workspace has no seeded baseline Decision.",
            code="BASELINE_DECISION_MISSING",
        )
    return decision_id.strip()


def _permission_for_scopes(
    workspace: Mapping[str, Any],
    scopes: set[str],
) -> str:
    from writai.cli import CliError

    policy = workspace.get("authority_policy")
    if not isinstance(policy, Mapping):
        raise CliError(
            "The workspace has no authority policy.",
            code="AUTHORITY_POLICY_MISSING",
        )
    permissions: set[str] | None = None
    for scope in scopes:
        raw = policy.get(scope)
        if not isinstance(raw, (list, set, tuple)):
            raise CliError(
                f"The workspace has no authority permission for {scope!r}.",
                code="SCOPE_NOT_GOVERNED",
            )
        current = {
            item for item in raw if isinstance(item, str) and item.strip()
        }
        permissions = (
            current if permissions is None else permissions & current
        )
    if not permissions:
        raise CliError(
            "No single authority permission governs every affected scope.",
            code="AUTHORITY_POLICY_AMBIGUOUS",
        )
    return sorted(permissions)[0]


def _source_reference(workspace_id: str, now: datetime) -> str:
    return (
        f"cli://{workspace_id}/"
        f"{now.strftime('%Y%m%dT%H%M%S%fZ')}"
    )


def _proposal_from_extraction(
    *,
    workspace: Mapping[str, Any],
    raw_text: str,
) -> dict[str, Any]:
    from writai.cli import CliError
    from writai.llm.provider import (
        LLMProviderConfigurationError,
        build_decision_extractor,
    )

    try:
        extractor = build_decision_extractor()
    except LLMProviderConfigurationError as exc:
        raise CliError(str(exc), code="EXTRACTION_NOT_CONFIGURED") from exc
    if extractor is None:
        raise CliError(
            "LLM_PROVIDER must be gemini or venice for --text extraction; "
            "use --scope/--was/--now to bypass extraction.",
            code="EXTRACTION_NOT_CONFIGURED",
        )
    try:
        candidate = extractor.extract(raw_text)
    except Exception as exc:
        raise CliError(
            f"Decision extraction failed: {exc}",
            code="DECISION_EXTRACTION_FAILED",
        ) from exc
    span_error = evidence_span_error(raw_text, candidate.evidence_spans)
    if span_error is not None:
        raise CliError(span_error, code="INVALID_EXTRACTION_EVIDENCE")

    affected_scopes = set(candidate.mutation.affected_scopes)
    try:
        superseded = select_seeded_supersession_root(
            decisions=_approved_decisions(workspace),
            affected_scopes=affected_scopes,
            root_decision_id=_baseline_decision_id(workspace),
        )
    except DecisionDraftError as exc:
        raise CliError(str(exc), code="SCOPE_NOT_GOVERNED") from exc
    requirements = candidate.mutation.decision.attributes.get(
        "requirements"
    )
    if (
        not isinstance(requirements, Mapping)
        or set(requirements) != affected_scopes
        or any(not isinstance(value, Mapping) for value in requirements.values())
    ):
        raise CliError(
            "Extraction requirements must exactly match the affected scopes.",
            code="INVALID_EXTRACTED_DECISION",
        )

    now = datetime.now(UTC)
    workspace_id = str(workspace["id"])
    decision = candidate.mutation.decision.model_copy(deep=True)
    decision.id = f"DEC-CLI-{now.strftime('%Y%m%d%H%M%S%f')}"
    decision.kind = ArtifactKind.DECISION
    decision.title = decision.title or raw_text
    decision.text = raw_text
    decision.scopes = affected_scopes
    decision.validity = ValidityStatus.VALID
    decision.invalidated_scopes = set()
    decision.approval_status = ApprovalStatus.PROPOSAL
    decision.authority_role = _permission_for_scopes(
        workspace, affected_scopes
    )
    decision.effective_at = now
    decision.source_ref = _source_reference(workspace_id, now)
    decision.attributes = {
        **decision.attributes,
        "requirements": {
            scope: dict(requirements[scope])
            for scope in sorted(affected_scopes)
        },
        "extraction": {
            "source": "cli",
            "delivered_at": now.isoformat(),
            "extraction_confidence": decision.confidence,
            "human_reviewed": False,
            "review_required": True,
            "validated_evidence_spans": [
                {
                    "source_ref": decision.source_ref,
                    **span.model_dump(mode="json"),
                }
                for span in candidate.evidence_spans
            ],
        },
    }
    return {
        "decision": decision.model_dump(mode="json"),
        "supersedes_id": superseded.id,
        "affected_scopes": sorted(affected_scopes),
    }


def _argument_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip().casefold().replace("-", "_").replace(" ", "_")


def _proposal_from_explicit_delta(
    *,
    workspace: Mapping[str, Any],
    raw_text: str,
    scope: str,
    was: str,
    now_value: str,
) -> dict[str, Any]:
    from writai.cli import CliError

    affected_scopes = {scope}
    try:
        decisions = _approved_decisions(workspace)
        superseded = select_seeded_supersession_root(
            decisions=decisions,
            affected_scopes=affected_scopes,
            root_decision_id=_baseline_decision_id(workspace),
        )
        current_decision = select_supersession_target(
            decisions=decisions,
            affected_scopes=affected_scopes,
        )
    except DecisionDraftError as exc:
        raise CliError(str(exc), code="SCOPE_NOT_GOVERNED") from exc
    requirements = current_decision.attributes.get("requirements")
    current = (
        requirements.get(scope)
        if isinstance(requirements, Mapping)
        else None
    )
    if not isinstance(current, Mapping) or not current:
        raise CliError(
            f"The current Decision has no requirement for {scope!r}.",
            code="CURRENT_REQUIREMENT_MISSING",
        )

    parsed_was = _argument_value(was)
    parsed_now = _argument_value(now_value)
    if isinstance(parsed_was, Mapping) and isinstance(parsed_now, Mapping):
        old_requirement = dict(parsed_was)
        new_requirement = dict(parsed_now)
    elif len(current) == 1:
        key = next(iter(current))
        old_requirement = {key: parsed_was}
        new_requirement = {key: parsed_now}
    else:
        raise CliError(
            "--was and --now must be JSON objects when the scope has "
            "multiple requirement fields.",
            code="AMBIGUOUS_REQUIREMENT_DELTA",
        )
    if old_requirement != dict(current):
        raise CliError(
            "--was does not match the workspace's current requirement: "
            + json.dumps(dict(current), sort_keys=True),
            code="STALE_EXPLICIT_DELTA",
        )

    created_at = datetime.now(UTC)
    workspace_id = str(workspace["id"])
    decision = Artifact(
        id=f"DEC-CLI-{created_at.strftime('%Y%m%d%H%M%S%f')}",
        kind=ArtifactKind.DECISION,
        title=raw_text,
        text=raw_text,
        scopes=affected_scopes,
        approval_status=ApprovalStatus.PROPOSAL,
        authority_role=_permission_for_scopes(workspace, affected_scopes),
        confidence=1.0,
        effective_at=created_at,
        source_ref=_source_reference(workspace_id, created_at),
        attributes={
            "requirements": {scope: new_requirement},
            "extraction": {
                "source": "cli-explicit",
                "delivered_at": created_at.isoformat(),
                "human_reviewed": False,
                "review_required": True,
            },
        },
    )
    return {
        "decision": decision.model_dump(mode="json"),
        "supersedes_id": superseded.id,
        "affected_scopes": [scope],
    }


def _preview_payload(payload: dict[str, Any]) -> dict[str, Any]:
    nested = payload.get("preview")
    return nested if isinstance(nested, dict) else payload


def _pending_from_preview(
    payload: dict[str, Any],
    *,
    expected: PendingApproval,
) -> PendingApproval:
    preview = _preview_payload(payload)
    raw_pending = preview.get("pending")
    if not isinstance(raw_pending, dict):
        raise ValueError("approval preview is missing server field pending")
    pending = PendingApproval.model_validate(raw_pending)
    if (
        pending.workspace_id != expected.workspace_id
        or pending.decision_id != expected.decision_id
    ):
        raise ValueError("approval preview does not match the requested Decision")
    return pending


def _preview_integer(preview: dict[str, Any], key: str) -> int:
    value = preview.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"approval preview is missing server field {key}")
    return value


def _preview_ids(preview: dict[str, Any], key: str) -> list[str]:
    value = preview.get(key)
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ValueError(f"approval preview is missing server field {key}")
    return value


def _format_delta_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, separators=(", ", ": "))


def _requirement_delta_lines(
    workspace: dict[str, Any],
    pending: PendingApproval,
) -> list[str]:
    decisions = _approved_decisions(workspace)
    lines: list[str] = []
    for scope in sorted(pending.requirements):
        new_requirement = pending.requirements[scope]
        try:
            superseded = select_supersession_target(
                decisions=decisions,
                affected_scopes={scope},
            )
        except DecisionDraftError:
            lines.append(
                f"  {scope}: "
                + json.dumps(new_requirement, sort_keys=True)
            )
            continue
        requirements = superseded.attributes.get("requirements")
        old_requirement = (
            requirements.get(scope)
            if isinstance(requirements, Mapping)
            else None
        )
        if (
            isinstance(old_requirement, Mapping)
            and len(old_requirement) == 1
            and old_requirement.keys() == new_requirement.keys()
        ):
            key = next(iter(old_requirement))
            lines.append(
                f"  {scope}.{key}: "
                f"{_format_delta_value(old_requirement[key])} → "
                f"{_format_delta_value(new_requirement[key])}"
            )
        else:
            lines.append(
                f"  {scope}: "
                f"{json.dumps(old_requirement, sort_keys=True)} → "
                f"{json.dumps(new_requirement, sort_keys=True)}"
            )
    return lines


def _print_assignment_group(
    *,
    heading: str,
    count: int,
    assignment_ids: list[str],
    labels: dict[str, str],
    color: str = "",
) -> None:
    prefix = color
    suffix = _RESET if color else ""
    print(f"  {prefix}{heading} ({count}){suffix}")
    if not assignment_ids:
        print(f"  {prefix}None{suffix}")
        return
    for assignment_id in assignment_ids:
        print(f"  {prefix}{labels.get(assignment_id, assignment_id)}{suffix}")


def _print_change_preview(
    *,
    workspace: dict[str, Any],
    pending: PendingApproval,
    payload: dict[str, Any],
) -> None:
    preview = _preview_payload(payload)
    stopping_count = _preview_integer(preview, "interrupted_count")
    continuing_count = _preview_integer(preview, "preserved_count")
    total_count = _preview_integer(preview, "total_assignment_count")
    stopping_ids = _preview_ids(preview, "interrupted_assignment_ids")
    continuing_ids = _preview_ids(preview, "preserved_assignment_ids")
    labels = _assignment_labels(workspace)

    print(f"⏺ Source: {pending.source_ref}")
    print(f"  Decision: {pending.text}")
    print("⏺ Decision delta")
    for line in _requirement_delta_lines(workspace, pending):
        print(line)
    print(
        "⏺ Blast radius: "
        f"{stopping_count} of {total_count} active sessions will be interrupted"
    )
    print(
        "  "
        f"{_AMBER}{stopping_count} of {total_count} stopping{_RESET}; "
        f"{_GREEN}{continuing_count} continuing{_RESET}"
    )
    _print_assignment_group(
        heading="Stopping",
        count=stopping_count,
        assignment_ids=stopping_ids,
        labels=labels,
    )
    _print_assignment_group(
        heading="Continuing",
        count=continuing_count,
        assignment_ids=continuing_ids,
        labels=labels,
        color=_GREEN,
    )


def _print_pending(
    workspaces: list[dict[str, Any]],
    *,
    json_output: bool,
) -> None:
    pending = []
    for workspace in workspaces:
        item = pending_from_workspace(workspace)
        if item is not None:
            pending.append(item)
    if json_output:
        print(
            json.dumps(
                [item.model_dump(mode="json") for item in pending],
                indent=2,
                sort_keys=True,
            )
        )
        return
    if not pending:
        print("No pending decision changes.")
        return
    print("WORKSPACE\tDECISION\tSCOPES\tREQUIRED PERMISSION")
    for item in pending:
        print(
            "\t".join(
                (
                    item.workspace_id,
                    item.decision_id,
                    ",".join(sorted(item.affected_scopes)),
                    item.permission_id,
                )
            )
        )
        print(f"  Title: {item.title}")
        print(f"  Effective at: {item.effective_at.isoformat()}")
        print(f"  Source: {item.source_ref}")
        print(f"  Proposal fingerprint: {item.proposal_fingerprint}")
        print(f"  Proposal instance: {item.proposal_instance_id}")
        print("  Confirmed requirements:")
        for scope in sorted(item.requirements):
            print(
                f"    {scope}: "
                + json.dumps(item.requirements[scope], sort_keys=True)
            )
        if item.evidence_refs:
            print("  Evidence:")
            for reference in item.evidence_refs:
                print(f"    {reference}")


def _select_intake_workspace(
    client: ApprovalClient,
    requested_id: str | None,
) -> dict[str, Any]:
    from writai.cli import CliError

    workspaces = _list_workspaces(client)
    selected_id = (
        (requested_id or "").strip()
        or os.getenv("WRITAI_WORKSPACE_ID", "").strip()
    )
    if selected_id:
        if not any(item.get("id") == selected_id for item in workspaces):
            raise CliError(
                f"Workspace {selected_id!r} was not found.",
                code="WORKSPACE_NOT_FOUND",
            )
        return _show_workspace(client, selected_id)
    if len(workspaces) == 1:
        workspace_id = workspaces[0].get("id")
        if isinstance(workspace_id, str):
            return _show_workspace(client, workspace_id)
    for preferred_id in ("stage-exports-e2e", "csv-exports"):
        preferred = next(
            (item for item in workspaces if item.get("id") == preferred_id),
            None,
        )
        if preferred is not None:
            return _show_workspace(client, preferred_id)
    ids = sorted(
        item["id"]
        for item in workspaces
        if isinstance(item.get("id"), str)
    )
    raise CliError(
        "Select a workspace with --workspace. Available: "
        + (", ".join(ids) if ids else "none"),
        code="WORKSPACE_REQUIRED",
    )


def _propose_text_decision(
    *,
    client: ApprovalClient,
    args: argparse.Namespace,
) -> tuple[dict[str, Any], PendingApproval]:
    from writai.cli import CliError, Route

    raw_text = str(getattr(args, "text", "") or "").strip()
    if not raw_text:
        raise CliError(
            "writai approve requires --text, or a pending/change subcommand.",
            code="DECISION_TEXT_REQUIRED",
        )
    explicit = {
        "scope": getattr(args, "scope", None),
        "was": getattr(args, "was", None),
        "now": getattr(args, "now", None),
    }
    supplied = {key for key, value in explicit.items() if value is not None}
    if supplied and supplied != set(explicit):
        raise CliError(
            "--scope, --was, and --now must be supplied together.",
            code="INCOMPLETE_EXPLICIT_DELTA",
        )

    workspace = _select_intake_workspace(
        client,
        getattr(args, "intake_workspace", None),
    )
    if workspace.get("pending_mutation") is not None:
        raise CliError(
            "The workspace already has a pending Decision.",
            code="PENDING_DECISION_EXISTS",
        )
    if supplied:
        body = _proposal_from_explicit_delta(
            workspace=workspace,
            raw_text=raw_text,
            scope=str(explicit["scope"]),
            was=str(explicit["was"]),
            now_value=str(explicit["now"]),
        )
    else:
        body = _proposal_from_extraction(
            workspace=workspace,
            raw_text=raw_text,
        )
    workspace_id = str(workspace["id"])
    proposed = client.request(
        Route(
            "POST",
            "/live-workspaces/{workspace_id}/decisions/propose",
        ),
        workspace_id=workspace_id,
        body=body,
    )
    pending = pending_from_workspace(proposed)
    if pending is None:
        raise CliError(
            "The service did not return the pending Decision.",
            code="PENDING_DECISION_NOT_FOUND",
        )
    return proposed, pending


def run(*, client: ApprovalClient, args: argparse.Namespace) -> int:
    """Execute the fallback terminal approval path."""

    json_output = bool(getattr(args, "json", False))
    if args.command == "pending":
        workspaces = _list_workspaces(client)
        if args.workspace:
            workspaces = [
                item
                for item in workspaces
                if item.get("id") == args.workspace
            ]
        _print_pending(workspaces, json_output=json_output)
        return 0

    if args.command is None:
        workspace, pending = _propose_text_decision(
            client=client,
            args=args,
        )
    else:
        workspace = _show_workspace(client, str(args.workspace_id))
        workspace_pending = pending_from_workspace(workspace)
        if (
            workspace_pending is None
            or workspace_pending.decision_id != args.decision_id
        ):
            print(
                "writai: the requested Decision is not awaiting approval. "
                "[PENDING_DECISION_NOT_FOUND]",
                file=sys.stderr,
            )
            return 2
        pending = workspace_pending
    approval_token = os.getenv("HEXCLAVE_APPROVER_USER_API_KEY", "").strip()
    if not approval_token:
        print(
            "writai: HEXCLAVE_APPROVER_USER_API_KEY is required for authenticated "
            "approval. [APPROVAL_AUTHENTICATION_REQUIRED]",
            file=sys.stderr,
        )
        return 2
    preview = _preview_change(
        client,
        workspace_id=pending.workspace_id,
        decision_id=pending.decision_id,
    )
    pending = _pending_from_preview(preview, expected=pending)
    if not json_output:
        _print_change_preview(
            workspace=workspace,
            pending=pending,
            payload=preview,
        )
    print("Approve this decision? [y/N] ", end="", flush=True)
    if input().strip().casefold() != "y":
        print("Approval cancelled.")
        return 0
    from writai.cli import Route

    attempt = ApprovalAttemptRequest(
        approval_token=SecretStr(approval_token),
        channel=ApprovalChannel.CLI,
        evidence_ref=(
            f"cli://approval/{pending.workspace_id}/{pending.decision_id}/"
            f"{pending.proposal_fingerprint}"
        ),
        confirmed_proposal_fingerprint=pending.proposal_fingerprint,
        confirmed_proposal_instance_id=pending.proposal_instance_id,
    )
    attempt_body = attempt.model_dump(mode="json", exclude={"approval_token"})
    attempt_body["approval_token"] = attempt.approval_token.get_secret_value()
    result = client.request(
        Route(
            "POST",
            "/live-workspaces/{workspace_id}/decisions/{decision_id}/approve",
        ),
        workspace_id=pending.workspace_id,
        decision_id=pending.decision_id,
        body=attempt_body,
    )
    partition = actual_partition_from_workspace(result)

    output = {
        "workspace_id": pending.workspace_id,
        "decision_id": pending.decision_id,
        "permission_id": pending.permission_id,
        "proposal_fingerprint": pending.proposal_fingerprint,
        "proposal_instance_id": pending.proposal_instance_id,
        "approval": "APPROVED",
        "interrupted_assignment_ids": list(
            partition.interrupted_assignment_ids
        ),
        "preserved_assignment_ids": list(
            partition.preserved_assignment_ids
        ),
    }
    if json_output:
        print(json.dumps(output, indent=2, sort_keys=True))
    else:
        print("Decision change approved")
        print(f"Workspace: {pending.workspace_id}")
        print(f"Decision: {pending.decision_id}")
        print(f"Permission: {pending.permission_id}")
    return 0
