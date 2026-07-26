# writ.ai Claude Code hooks

Three small, standard-library-only command hooks — `writai_session_start.py`,
`writai_pre_tool_use.py` and `writai_session_end.py` — over one shared
`writai_hook_lib.py`.

Install all four files at an organisation-controlled path such as
`/opt/writai/hooks/`, then install `managed-settings.example.json` through
Claude Code's managed-settings mechanism. The example sets
`allowManagedHooksOnly: true`, so project and user settings cannot replace or
remove the managed hooks. Change the command paths if your managed installation
uses another location.

The entry scripts import `writai_hook_lib` from their own directory, so keep
the four files together.

The hook reads these environment variables:

- `WRITAI_HOOK_ENDPOINT` — defaults to
  `http://localhost:8002/supervisor/sessions`.
- `WRITAI_HOOK_API_KEY` — required. The hook sends this per-developer token
  only in the `X-writ.ai-Hook-API-Key` header; it never writes it to the
  verdict cache or JSON payload.
- `WRITAI_HOOK_TIMEOUT_SECONDS` — defaults to 3 seconds and must be at most
  30 seconds.
- `WRITAI_HOOK_CACHE_PATH` — defaults to
  `.writai/hook-verdict-cache.json`, resolved against the session cwd.

Privacy is enforced in code: `PreToolUse` sends only `session_id`, `tool_name`,
and a UTC timestamp. It does not send `tool_input`, `cwd`, transcript paths, or
file contents.

Claude Code hooks fail open if the hook process is killed or never runs. This
script catches its own parsing, HTTP, timeout, response, and cache errors and
emits an explicit `deny`. During a service outage it reuses a cached deny when
one exists; a cached allow never authorizes work while the service is
unreachable. Protected-branch or PR verification remains the backstop for
process crashes and for proving that the redirected plan was actually obeyed.

---

## What still fails open — read this first

**Claude Code hooks fail open by design.** If a hook times out, crashes, is killed, or
returns nothing, the tool call *proceeds*. Only an explicit `deny` blocks it.

Closed by these scripts: supervisor unreachable, a slow supervisor (explicit short
timeout), a garbage verdict, and any bug inside the scripts — all emit `deny`.

Still open, honestly: a hook process that never starts or is killed is invisible to
Claude Code and the call proceeds; `allowManagedHooksOnly` stops a developer *removing*
the hook, not the process dying. Work already written before the interrupt arrived is not
rolled back. **The backstop is the PR check** — it re-evaluates finished work against the
current decision graph with no dependency on a hook having run. Treat a green hook as a
strong signal, never as proof.

---

## Install — `settings.local.json` only

Merge `settings.example.json` into **`.claude/settings.local.json`** inside the working
directory you want supervised.

- **Never install this into the user-level `~/.claude/settings.json`.** That points every
  project on the machine at a service that is only running for the demo, and every
  unrelated session then depends on it.
- **Use `.claude/settings.local.json`, not `.claude/settings.json`.** The `.local` file is
  personal and untracked, so wiring up a demo cannot change a teammate's checkout.
  `.gitignore` excludes it.
- **Do not commit hook config to `.claude/settings.json` until after the demo.** Promoting
  it enforces for everyone who pulls — a deliberate decision to take once rehearsed.

The one legitimate machine-wide install is the organisation-managed
`managed-settings.json` carrying `"allowManagedHooksOnly": true`.
