# Live Workspace CLI

The Dragback CLI turns the Live Workspace API into an enforceable terminal and CI
workflow. It imports user-owned YAML or JSON, requests real snapshot-bound
authorizations, and exits nonzero when the executor rejects a stored grant.

## Install and connect

Install the project in editable mode:

```bash
python -m pip install -e .
```

The CLI uses `http://127.0.0.1:8002` by default. Point it at another agent service
with either option:

```bash
export DRAGBACK_AGENT_URL=https://dragback-agent.example.com
dragback workspace list

dragback --agent-url https://dragback-agent.example.com workspace list
```

`--agent-url`, `--timeout`, and `--json` may appear before or after a workspace
command.

## Run Codex or Claude Code under Dragback

The agent wrapper connects an ordinary developer CLI to one task assignment:

```bash
dragback agent run --workspace voyagr-reservation \
  --task TASK-CALL-GUEST \
  --provider codex \
  --cwd ./voyagr

dragback agent run --workspace voyagr-reservation \
  --task TASK-CALL-GUEST \
  --provider claude-code \
  --cwd ./voyagr
```

Dragback first reads the workspace's `supervisor.assignments`, selects the exact
task, and verifies its provider binding. An assignment whose provider is
`generic` may be run with either supported CLI. The wrapper starts child
processes directly, without a shell. The provider inherits the caller's terminal
so interactive Codex and Claude Code sessions continue to work:

```text
codex -C WORKING_DIRECTORY --no-alt-screen ASSIGNMENT_PROMPT
claude --name RUN_ID ASSIGNMENT_PROMPT
```

Provider options may be passed only after `--`:

```bash
dragback agent run --workspace voyagr-reservation \
  --task TASK-CALL-GUEST \
  --provider codex \
  --cwd ./voyagr \
  -- --model gpt-5
```

Options that would override Dragback's working directory, run identity, terminal
control, or provider safety controls are rejected.

After launch, the wrapper listens to
`GET /live-workspaces/{workspace_id}/events`. The stream sends an immediate
redacted workspace snapshot and then the complete redacted workspace view after
each mutation. Streams are isolated by workspace.

The process lifecycle is deterministic:

1. `running`, `continuing`, or `resumed` may launch.
2. `queued`, `redirected`, `interrupted`, and `completed` never launch.
3. An `interrupted` ping for the selected task sends `SIGINT` to that task's
   child only.
4. If the child remains alive for `--interrupt-timeout` seconds, the wrapper
   sends terminate, waits again, then uses a bounded kill fallback.
5. A redirected task launches again only after the supervisor emits `resumed`
   with a new `run_id` linked through `redirected_from_run_id`. The service
   reaches `resumed` only after the corrected plan receives replacement
   authorization.

Sibling-task pings are ignored. A missing task, duplicate task assignment, or
concrete provider mismatch fails closed. Grant tokens are neither included in
the assignment prompt nor printed.

Use a one-shot dry run to inspect what would launch:

```bash
dragback agent run --workspace voyagr-reservation \
  --task TASK-CALL-GUEST \
  --provider codex \
  --cwd ./voyagr \
  --dry-run
```

Fixture-backed assignments are explicitly printed as
`[SIMULATED SUPERVISOR DATA]`. `--dry-run` validates and displays the redacted
assignment command and prompt, starts no provider process, and does not open the
event stream.

The non-dry wrapper subscribes before launch and uses the stream's immediate
snapshot, closing the fetch-to-subscribe race. It is a long-running supervisor:
keep it open until Dragback reports the assignment or supervisor `completed`, or
stop it with Ctrl-C. A clean stream disconnect is treated as an error and the
direct provider child is stopped. Automatic provider-exit acknowledgement and
descendant-process-tree cleanup require a future PTY proxy and are explicitly
outside this demo cut.

## Run the practical refund example

Start the three Dragback services, then run:

```bash
# 1. Import user-owned decisions, work, provenance, and an agent plan.
dragback workspace import examples/dragback-workspace.yaml

# 2. An authoritative role approves the proposed baseline, creating graph-v17.
dragback workspace approve-baseline refund-operations --role finance-admin

# 3. Authorize the initial plan against graph-v17.
dragback workspace authorize refund-operations

# 4. Propose and approve a new upstream decision, creating graph-v18.
dragback workspace propose-change refund-operations examples/dragback-change.yaml
dragback workspace approve-change \
  refund-operations DEC-REFUND-002 --role finance-admin

# 5. The old graph-v17 grant is now rejected. This command exits 1.
dragback workspace verify refund-operations --grant initial

# 6. Store the corrected plan and request its replacement authorization.
dragback workspace update-plan \
  refund-operations examples/dragback-corrected-plan.json

# 7. The executor accepts the graph-v18 replacement grant.
dragback workspace verify refund-operations --grant replacement
```

The decision change never names `PAY-104`. Dragback reaches it through the
decision → specification → ticket provenance chain. The calculation task remains
valid because its scope does not intersect the changed execution policy; the
automatic issue-refund task is invalidated.

`update-plan` intentionally performs two API operations: it stores the plan, then
requests the replacement authorization. Deterministic authority code still decides
whether that replacement is allowed.

## Commands

```text
dragback workspace import FILE
dragback workspace list
dragback workspace show WORKSPACE_ID
dragback workspace approve-baseline WORKSPACE_ID --role ROLE
dragback workspace authorize WORKSPACE_ID
dragback workspace propose-change WORKSPACE_ID FILE
dragback workspace approve-change WORKSPACE_ID DECISION_ID --role ROLE
dragback workspace cancel-change WORKSPACE_ID
dragback workspace verify WORKSPACE_ID [--grant initial|replacement]
dragback workspace update-plan WORKSPACE_ID FILE
dragback agent run --workspace WORKSPACE_ID --task TASK_ID \
  --provider {codex,claude-code} [--cwd DIRECTORY] [--dry-run] \
  [--interrupt-timeout SECONDS] [-- PROVIDER_ARGS...]
```

Use `-` instead of a filename to read YAML or JSON from standard input. Add
`--json` for scripting. Signed grant tokens are never printed; if an upstream
response accidentally contains a token field, the CLI replaces it with
`"[REDACTED]"`.

If a proposal is wrong or no longer needed, `cancel-change` deletes only the
pending proposal. It does not mutate the authority graph, invalidate work, or
replace the existing initial authorization.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Request succeeded; for `verify`, the code is `VALID` and execution was applied. |
| `1` | Verification deterministically rejected the grant or did not apply execution. |
| `2` | Usage, input, transport, or API/protocol error. |

This makes verification usable as a CI gate:

```bash
dragback workspace verify refund-operations --grant initial
```

## GitHub Actions

The repository includes a composite action:

```yaml
- name: Verify Dragback authorization
  uses: Eman-Gon/lookback/.github/actions/dragback-verify@main
  with:
    agent-url: ${{ secrets.DRAGBACK_AGENT_URL }}
    workspace-id: refund-operations
    grant: initial
```

The runner must be able to reach the configured agent service. The action installs
the CLI from the referenced repository version and lets the CLI exit code decide
the job result. It never accepts or prints a signed grant token.

## Security boundary

The CLI is a client, not an authority:

- User input may propose graph structure and decision changes.
- Only the service validates role authority, approval, scope, confidence, graph
  traversal, plan requirements, and grants.
- The CLI does not mint, decode, persist, or locally trust signed grants.
- The agent wrapper treats prompts as a final argv value and never invokes a
  shell. Only allowlisted assignment fields enter the prompt.
- A CLI interrupt stops local execution; deterministic service state and
  snapshot-bound authorization decide whether a replacement run may resume.
- `--role` represents the prototype's explicit approval actor. Production
  deployments should derive that role from authenticated identity instead.

## `dragback dev` — the developer's side

The `dev` group is what a teammate running Claude Code uses. It binds a working
directory to one assignment, shows what the supervisor believes about every
registered session, and explains an interrupt as a path rather than a badge. It
decides nothing: the agent service does.

```bash
# Bind this repository checkout to one assignment. Local file only, no HTTP.
dragback dev attach ASSIGNMENT-TASK-102 --workspace refund-operations

# Every registered session, including the ones Dragback could not bind.
dragback dev status [--workspace refund-operations]

# Why this session was interrupted: the path, the scopes, the evidence.
dragback dev why [SESSION_ID] [--workspace refund-operations]

# Acknowledge the decision that is denying this session outright.
dragback dev ack SESSION_ID

# Follow supervisor transitions for a workspace as they happen.
dragback dev watch refund-operations
```

`attach` writes the assignment id to `.dragback/attach` in the current directory,
creating `.dragback/` if needed, and prints the path. The `SessionStart` hook
reports that file; the service, not the CLI, resolves the binding from it.

`status` prints `SESSION`, `TASK`, `SOURCE`, `STATE`. A session Dragback could
not bind is listed with `unbound` in its task and source columns and counted in a
closing line — it is registered, allowed everything, and unenforced, and hiding it
would turn a visible gap into a silent one. `--workspace` narrows the bound rows
and still shows unbound sessions, because an unbound session belongs to no
workspace. The state column is read from the session's assignment; if that
workspace cannot be read the row still prints, with `—` for the state.

`why` renders the interrupt as `DEC-REFUND-002 → SPEC-REFUND → TICKET-PAY-104 →
TASK-102`, followed by the affected scopes, the assignment's own scopes when they
differ, the interrupt reason, the corrected instruction, and the evidence ref.
With no `SESSION_ID` it explains the only bound session and refuses to guess when
there is more than one.

`ack` sends `{"decision_id": …}` for the decision the service reports as
currently blocking that session. It never derives that id from workspace state:
acknowledging a decision before it interrupts you is a bypass, not a shortcut, so
a session with nothing outstanding exits 2 rather than acknowledging something.

`watch` streams `GET /live-workspaces/{id}/events` and prints one line per
supervisor or assignment transition, with the interrupt reason and corrected
instruction indented under it. It is long-running; stop it with Ctrl-C.

`--json` works on every `dev` command and is redacted by the same rule as the
rest of the CLI — signed grant tokens are never printed. `watch --json` emits one
JSON object per line so it can be piped into a follower.

Exit codes follow the table above: `0` on success and `2` for usage, transport,
or API errors. No `dev` command returns `1`; that code belongs to the
verification gate.
