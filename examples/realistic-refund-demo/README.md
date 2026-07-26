# Realistic refund demo source pack

These files simulate records exported from company tools. They are realistic demo
fixtures, not records retrieved from live Slack, Jira, or coding-agent accounts.

## Files

- `slack-finance-decision.json` — a Slack-style thread approving the original policy.
- `jira-pay-104.json` — a Jira-style engineering ticket with three subtasks.
- `refund-processing-spec.md` — the product and finance specification behind the ticket.
- `agent-plan.json` — the coding agent's plan for implementing the ticket.
- `slack-policy-change.json` — a later Slack-style decision requiring human approval.
- `writai-workspace.yaml` — the same information normalized into writ.ai's import format.
- `writai-policy-change.json` — the later decision normalized for the change editor.

## Demo flow

1. Open writ.ai's Workspace page.
2. Upload `writai-workspace.yaml`.
3. Approve the baseline as `finance-admin`.
4. Authorize the initial plan.
5. Paste `writai-policy-change.json` into the decision proposal editor when the
   interface asks for a change. (The starter refund change shown by the UI is also
   compatible.)
6. Approve it as `finance-admin`.
7. Verify that the old authorization is rejected.
8. Update and reauthorize the plan.

The individual source files make the story feel like a real company workflow. The
combined YAML is necessary because this prototype does not yet connect to live Slack
or Jira accounts.
