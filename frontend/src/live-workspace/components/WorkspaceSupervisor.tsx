import type {
  LiveWorkspaceView,
  WorkspaceAgentRuntimeProvider,
  WorkspaceSubagentAssignment,
  WorkspaceSubagentState,
} from "../model";

type AssignmentTone = "neutral" | "positive" | "attention" | "redirected";

interface AssignmentPresentation {
  label: string;
  signal: string;
  tone: AssignmentTone;
  detail: string;
}

const STATE_LABELS: Record<WorkspaceSubagentState, string> = {
  queued: "Waiting for authorization",
  running: "Running",
  continuing: "Continuing",
  interrupted: "Interrupted",
  redirected: "Redirected",
  resumed: "Resumed",
  completed: "Completed",
};

function humanize(value: string): string {
  return value
    .split(/[-_]/)
    .filter(Boolean)
    .map((part) => `${part[0]?.toUpperCase() ?? ""}${part.slice(1)}`)
    .join(" ");
}

function runtimeLabel(provider: WorkspaceAgentRuntimeProvider): string {
  if (provider === "claude-code") return "Claude Code";
  if (provider === "codex") return "Codex";
  return "Generic coding agent";
}

function isLive(assignment: WorkspaceSubagentAssignment): boolean {
  return assignment.executionMode === "live";
}

function presentationFor(
  assignment: WorkspaceSubagentAssignment,
): AssignmentPresentation {
  const live = isLive(assignment);
  switch (assignment.state) {
    case "running":
      return {
        label: STATE_LABELS.running,
        signal: "Start request issued",
        tone: "positive",
        detail: live
          ? `Registered at ${assignment.decisionSnapshot}. A live Claude Code session is bound to this assignment and every tool call is checked.`
          : `The assignment may start from ${assignment.decisionSnapshot}; this simulated adapter does not record process acknowledgement.`,
      };
    case "continuing":
      return {
        label: STATE_LABELS.continuing,
        signal: "No interrupt requested",
        tone: "positive",
        detail: live
          ? "Its task scope did not change. This session was never interrupted and kept working."
          : "Its task scope did not change, so Dragback preserves the run.",
      };
    case "interrupted":
      return {
        label: STATE_LABELS.interrupted,
        signal: "Interrupt request issued",
        tone: "attention",
        detail: live
          ? assignment.interruptEnforced
            ? "The next tool call this session makes is denied once and handed the correction."
            : "Marked interrupted. The deny reaches the session on its next tool call."
          : assignment.interruptEnforced
            ? "The executor rejected the old grant, so protected tools remain blocked."
            : "The assignment is marked interrupted while the executor checks the old grant.",
      };
    case "redirected":
      return {
        label: STATE_LABELS.redirected,
        signal: "Replacement assignment issued",
        tone: "redirected",
        detail: live
          ? "The correction was delivered to the session, which is running again. Delivery is not proof the agent complied."
          : "The corrected assignment is ready but cannot run until authority approves it.",
      };
    case "resumed":
      return {
        label: STATE_LABELS.resumed,
        signal: "Replacement run requested",
        tone: "positive",
        detail: `The corrected assignment is authorized on ${assignment.decisionSnapshot}; this view does not claim the provider process acknowledged it.`,
      };
    case "completed":
      return {
        label: STATE_LABELS.completed,
        signal: "Executor accepted",
        tone: "positive",
        detail: "The authorized assignment finished its protected execution.",
      };
    default:
      return {
        label: STATE_LABELS.queued,
        signal: "No process started",
        tone: "neutral",
        detail: "Dragback created the work, but no worker runs before authorization.",
      };
  }
}

function cliCommand(
  workspaceId: string,
  assignment: WorkspaceSubagentAssignment,
): string | null {
  if (assignment.runtimeProvider === "generic") return null;
  return `dragback agent run ${workspaceId} --task ${assignment.taskId} --provider ${assignment.runtimeProvider}`;
}

function AssignmentCard({
  workspaceId,
  assignment,
}: {
  workspaceId: string;
  assignment: WorkspaceSubagentAssignment;
}) {
  const presentation = presentationFor(assignment);
  const developerCommand = cliCommand(workspaceId, assignment);

  return (
    <article
      className={`lw-supervisor-worker lw-supervisor-worker--${presentation.tone}`}
    >
      <header>
        <div>
          <span>{assignment.taskId}</span>
          <h3>{assignment.agentName}</h3>
          <p>{assignment.taskTitle}</p>
        </div>
        <strong>{presentation.label}</strong>
      </header>

      <div className="lw-supervisor-worker__signal">
        <span aria-hidden="true">
          {presentation.tone === "attention"
            ? "!"
            : presentation.tone === "redirected"
              ? "→"
              : "✓"}
        </span>
        <div>
          <strong>{presentation.signal}</strong>
          <p>{presentation.detail}</p>
        </div>
      </div>

      {assignment.interruptReason ? (
        <div className="lw-supervisor-worker__interrupt">
          <span>Interrupt reason</span>
          <p>{assignment.interruptReason}</p>
        </div>
      ) : null}

      <dl>
        <div>
          <dt>Runtime</dt>
          <dd>
            {runtimeLabel(assignment.runtimeProvider)} ·{" "}
            {humanize(assignment.executionMode)}
          </dd>
        </div>
        <div>
          <dt>Decision snapshot</dt>
          <dd>
            <code>{assignment.decisionSnapshot}</code>
          </dd>
        </div>
        <div>
          <dt>Run</dt>
          <dd>
            <code>{assignment.runId}</code>
          </dd>
        </div>
        <div>
          <dt>Scopes</dt>
          <dd>{assignment.scopes.join(", ") || "No scopes supplied"}</dd>
        </div>
      </dl>

      {assignment.redirectedFromRunId ? (
        <div className="lw-supervisor-worker__redirect">
          <span>Redirected run</span>
          <code>{assignment.redirectedFromRunId}</code>
          <b aria-hidden="true">→</b>
          <code>{assignment.runId}</code>
          {assignment.redirectInstruction ? (
            <p>{assignment.redirectInstruction}</p>
          ) : null}
        </div>
      ) : null}

      {assignment.provenancePath.length > 0 ? (
        <details>
          <summary>Why this worker changed</summary>
          <code>{assignment.provenancePath.join(" → ")}</code>
        </details>
      ) : null}

      {developerCommand ? (
        <div className="lw-supervisor-worker__cli">
          <span>Developer entry point</span>
          <code>{developerCommand}</code>
        </div>
      ) : null}
    </article>
  );
}

export function WorkspaceSupervisor({
  workspace,
  titleId,
}: {
  workspace: LiveWorkspaceView;
  titleId: string;
}) {
  const supervisor = workspace.supervisor;
  if (!supervisor) return null;

  const supervisorIsLive = supervisor.executionMode === "live";
  const interrupted = supervisor.assignments.filter(
    (assignment) => assignment.state === "interrupted",
  ).length;
  const continuing = supervisor.assignments.filter(
    (assignment) => assignment.state === "continuing",
  ).length;

  return (
    <section className="lw-supervisor" aria-labelledby={titleId}>
      <header className="lw-supervisor__heading">
        <div>
          <span>Agent control plane</span>
          <h2 id={titleId}>{supervisor.name}</h2>
          <p>
            Creates scoped worker assignments, listens for approved changes,
            and routes cancellation or replacement work to the affected local
            CLI process.
          </p>
        </div>
        <div className="lw-supervisor__identity">
          <strong>{humanize(supervisor.state)}</strong>
          <span>
            {humanize(supervisor.executionMode)} adapter ·{" "}
            {humanize(supervisor.adapter)}
          </span>
        </div>
      </header>

      <div
        className={`lw-supervisor__honesty lw-supervisor__honesty--${
          supervisorIsLive ? "live" : "simulated"
        }`}
        aria-label="What is real and what is simulated"
      >
        <strong>{supervisorIsLive ? "Live enforcement" : "Simulated"}</strong>
        {supervisorIsLive ? (
          <p>
            A real Claude Code <code>PreToolUse</code> hook gates every tool call
            in each bound session. Graph traversal, scope intersection, grant
            signing and stale-grant rejection are real.{" "}
            <b>
              Hooks fail open: if the hook process crashes, is killed, or never
              starts, the tool call proceeds.
            </b>{" "}
            Organisation-managed settings stop a developer removing the hook,
            not the process dying — the PR check is the backstop, because it
            re-evaluates finished work against the current decision graph with
            no dependency on a hook having run.
          </p>
        ) : (
          <p>
            No provider process is controlled. Graph traversal, scope
            intersection, grant signing and stale-grant rejection are real; the
            worker lifecycle below is recorded state, not an agent being
            stopped. Nothing here is enforcing anything on a running session.
          </p>
        )}

        {/*
          Standing facts about this build, not per-workspace state. Each one is
          something a viewer would otherwise reasonably assume is live. They are
          listed whether the supervisor is live or simulated, because they are
          true either way.
        */}
        <ul className="lw-supervisor__caveats">
          <li>
            <b>The PR check is a required status check on protected main.</b>{" "}
            <i>Branch authorization is current</i> now blocks a merge when the
            hook&apos;s fail-open path leaves branch authorization stale.
          </li>
          <li>
            <b>Slack extraction reaches a real graph write, and its wording
            is not reproducible.</b>{" "}
            Measured over 14 live runs on the same message: 14/14 produced a
            valid proposal, and 6/6 driven through approval applied to the graph
            with the correct blast radius — three stopped, two preserved. But
            all 14 invented a different requirement <i>shape</i>, and none used
            the workspace&apos;s own <code>audience</code> key, so the wording a
            redirected agent receives varies run to run. The scope-level verdict
            is stable; the requirement text is not. No real Composio webhook or
            Hexclave-authenticated approval has exercised the route, and the
            staged demo still fires the seeded fixture via{" "}
            <code>--scope/--was/--now</code>.
          </li>
          <li>
            <b>The CrustData person watcher never fires live here.</b> Its
            watcher has a one-hour minimum interval, so the approver
            role-change and departure path replays a stored payload on demand.
            That payload is <i>documentation-reconstructed, not captured</i> —
            no CrustData delivery has been recorded against this build.
          </li>
          <li>
            <b>The demo seeder approves without channel authentication.</b> It
            calls the orchestrator directly, so no approval token is resolved
            to a person. Every authority check still runs — role, scope,
            confidence, the three-way requirement match and the proposal
            binding. It refuses to run unless{" "}
            <code>DRAGBACK_DEMO_UNAUTHENTICATED_APPROVAL=1</code> is set.
          </li>
          <li>
            <b>Events do not survive a restart or reach a second machine.</b>{" "}
            The event broker is process-local with a hundred-event history, and
            the session registry is in memory — after a service restart every
            open session reads as unregistered until it registers again.
          </li>
        </ul>
      </div>

      <div className="lw-supervisor__route" aria-label="Supervisor routing summary">
        <div>
          <span>1</span>
          <strong>Approved change</strong>
          <small>Slack, policy, or ticket event</small>
        </div>
        <b aria-hidden="true">→</b>
        <div>
          <span>2</span>
          <strong>Deterministic authority</strong>
          <small>Graph path + scope intersection</small>
        </div>
        <b aria-hidden="true">→</b>
        <div>
          <span>3</span>
          <strong>CLI control signal</strong>
          <small>Preserve, interrupt, or redirect</small>
        </div>
      </div>

      {interrupted > 0 || continuing > 0 ? (
        <p className="lw-supervisor__outcome" role="status">
          <strong>{interrupted} interrupted</strong>
          <span>{continuing} continuing without an interrupt</span>
        </p>
      ) : null}

      <div className="lw-supervisor__workers">
        {supervisor.assignments.map((assignment) => (
          <AssignmentCard
            key={assignment.id}
            workspaceId={workspace.id}
            assignment={assignment}
          />
        ))}
      </div>

      <footer>
        <strong>Authority boundary</strong>
        <p>
          The supervisor may create, cancel, and redirect agent runs. It cannot
          approve its own work; the independent authority and executor still
          decide whether protected actions can proceed.
        </p>
      </footer>
    </section>
  );
}
