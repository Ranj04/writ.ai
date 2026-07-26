# Writ — pitch pack

Read the narration out loud twice before you go up. Everything else is for the Q&A and the judges' table.

---

## 1. The three-minute narration

**0:00 — the hook.** Don't introduce the product. Introduce the problem.

> "Right now, five engineers on this team are building the wrong thing. Four of them don't know it yet."

**0:20 — the setup.** Gesture at the terminals.

> "Five Claude Code sessions, same feature. Two are building the CSV export. Three are building who's allowed to download it. Normal Tuesday."

**0:40 — the trigger.** Your teammate posts it in Slack, from their phone, held up.

> "And their compliance lead posts this." *(read it out)* "Exports must be admin-only, effective immediately. No ticket number. Nobody tagged. Just how people actually talk."

**1:10 — the approval.** Read the blast radius off the screen.

> "Writ pulls that out of Slack and asks exactly one person to confirm — the one who's actually allowed to. And before they confirm, it tells them who they're about to interrupt. Three of five. Priya, Marcus, Dan." *(confirm)* "That's the only human action in this entire demo."

**1:40 — the correction. Then stop talking for three seconds.**

Three panes halt. Two keep scrolling. Let them watch it.

> "Three agents stopped on their next tool call. Each got what changed, what it can keep, and what to add — and carried on in the new direction. Two never noticed, because their work wasn't affected. Nobody typed into any of those terminals."

**2:20 — the receipt.**

> "And any of them can ask why." *(run `writ why`)* "Straight back to the sentence Dana wrote, with the exact quote."

**2:45 — the close.**

> "Tests prove the code works. Writ proves the work is still wanted — for everyone at once."

---

## 2. Lines to have ready

**If the live run fails**, said flat, not apologetic — then hit the recording:

> "That's the live one being slow. Here's the same run from earlier."

**The stub disclosure**, said once, early, confidently. Never wait to be asked:

> "The Slack message reaches us through Composio. Turning it into a structured decision is the piece we're still hardening, so tonight we're transcribing it. Everything after that is live."

**If someone asks what's real:** the graph, the authority rules, the interrupt, the permission check, the hook, and the pull-request backstop. What's replayed: the CrustData signal, and the extraction step.

---

## 3. Q&A — the seven you'll actually get

**"What if the AI misreads the message?"**
This is your strongest answer, so don't be defensive.

> "It does. We ran a live model five times against the same message. It hallucinated an evidence span four times, invented scopes that don't exist in that workspace, and never once produced valid requirements. All five were refused deterministically. Zero bad writes. The model proposes structure — it never issues a verdict, and a human with the right permission confirms before anything applies."

**"Can a developer just turn it off?"**

> "Not locally. The hook is enforced through organisation-managed settings, which project and user settings can't override. And I'll volunteer the weakness: hooks fail open — if ours crashes or times out, the tool call proceeds. That's why there's a pull-request check behind it that fails closed. We'd rather tell you where the hole is than have you find it."

**"Does it work with Cursor, Codex, anything else?"**

> "Claude Code today, because it's the only runtime with a real synchronous veto — we can stop the agent mid-work rather than warn it. Everything else gets caught at the pull request. That's later, but it's still before it ships."

**"Isn't this just a kill switch?"**
The most important one to answer well.

> "A kill switch stops everything, which is why teams turn them off. Two of those five sessions kept working. The product is stopping exactly the right ones — we intersect the changed decision's scope with each task's scope, so out-of-scope work survives. That's the whole thing."

**"How is this different from Linear, Jira, Notion?"**

> "They record decisions. None of them can reach into a running agent and correct it. We're not a system of record, we're a permission layer."

**"Who buys it?"**

> "Platform Engineering owns it, because they already own the agent tooling and the CI. Compliance funds it, because they're the ones who get asked why an agent shipped something that violated a policy nobody told it about."

**"What's your moat?"**

> "Being in the kill path, and the graph. Anyone can post a Slack alert. Very few things can deny a tool call synchronously with an explanation the model acts on — and almost nothing can work out which three of five people to interrupt and leave the other two alone."

---

## 4. The fundability argument

**The one sentence:**

> Every company just handed their engineers autonomous agents. Nobody gave them a way to change their mind.

**The problem in their language.** Agents don't fail by writing bad code — they pass every test. They fail by executing a correct plan against an objective the company has already abandoned. That failure is invisible until review, it compounds across everyone running an agent, and it costs the same whether one person or five people are working from the stale objective.

**What to measure in a pilot** — and take your own numbers, don't borrow anyone's:

| Metric | Why it matters |
|---|---|
| Sessions corrected per decision change | The reach. One acknowledgement, N agents inherited it. |
| Sessions *preserved* per decision change | The precision. This is the differentiator, not the interrupts. |
| False interrupts | The only number that kills adoption. Target zero; report it honestly. |
| Time from decision posted → agents corrected | Today: humans, hours to never. Writ: seconds. |
| Corrections landing before the PR is opened | Rework prevented rather than caught. |

**One input is yours to supply, and don't let anyone invent it:** what an hour of engineering work against an obsolete objective costs you. Everything above converts to money through that number, and a made-up one is worse than none.

**The 30-day pilot.** One team. One high-consequence decision domain — export authorization, data residency, or production access. Success is three things: at least one real correction that would otherwise have shipped, zero false interrupts, and the team leaving it switched on after day 30. That last one is the real test.

**Why now.** Coding agents crossed from suggestion to autonomous execution in the last year. Governance tooling is still built for humans, who read Slack. Agents don't.

---

## 5. What you'd do with money

Three things, in order: coverage of other agent runtimes so the claim isn't Claude-Code-only; hardening the extraction path so decisions come out of Slack without a human transcribing them; and durable multi-machine infrastructure, since the event broker is process-local today.

Say that plainly. A team that names its three weaknesses in order reads as one that knows what it built.

---

## 6. If you only remember one thing

Say the number, then stop talking and let them watch three terminals stop and two keep going. That's the pitch. Everything in this document is support for those three seconds.
