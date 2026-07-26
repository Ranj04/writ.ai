# Terminal output spec

For most of the demo the terminal **is** the interface. These formats are the spec — paste the relevant section into the Lane A and Lane B sessions as a follow-up instruction.

The web approval screen and the CLI show the same five blocks in the same order: **where it came from → what changed → the chain → who's affected → the action.** That sequence is what makes them feel like one product.

## Rules for all terminal output

- Two-space left indent on everything. Dense left-aligned text reads as noise on a projector.
- One idea per line, blank line between blocks.
- Make numbers big by **isolation**, not decoration — put the count alone on its own line.
- Colour: dim grey for labels, one amber for what's stopping, green for what's preserved. Nothing else.
- No box-drawing characters, no ASCII tables, no spinners. They wrap badly, record badly, and look dated.
- Degrade cleanly with `NO_COLOR` set and when piped to a file.

---

## 1. The deny message — Lane A (highest value in the product)

This is the most-read text in the demo, and it is read by **both the developer and the model**, so it has to be human-scannable and instruction-clear at once. Budget ~400 characters of the 10,000 available; the cap is not a target.

```
  ⏹  WRITAI — the decision behind this task changed

     Approved by Dana Kaur (Compliance) 2 minutes ago:
     "exports must be admin-only, effective immediately"

     Still valid    CSV generation, download endpoint
     No longer      exposing the export to all users
     Now required   admin-only check, unauthorized-access test

     Why  DEC-018 → SPEC-101 → TICKET-101 → TASK-102 → this session
          writai why
```

Lead with what survived, not what died. "Still valid" first is the difference between an agent that adapts and an agent that starts over.

---

## 2. `writai approve` — Lane B

```
  Extracted from #compliance · Dana Kaur · 2:41 PM

    "Approved — exports must be admin-only, effective immediately"

  Decision   DEC-018 supersedes DEC-004
  Scope      export.authorization
  Was        all users
  Now        admins only

  This will interrupt 3 of 5 active sessions.

    stopping      Priya Raman     TASK-102
                  Marcus Obi      TASK-102
                  Dan Levy        TASK-102

    continuing    Ana Silva       TASK-101
                  Jonas Tan       TASK-101

  Approve?  [y/N]
```

After confirmation, one line and nothing more:

```
  ✓  Applied · graph-v18 · 3 sessions redirected, 2 preserved
```

The blast radius must come from the server's `preview()`. Never compute it in the CLI.

---

## 3. `writai why` — Lane A

```
  Your agent changed direction 40 seconds ago.

  Because        "exports must be admin-only, effective immediately"
                 Dana Kaur · #compliance · 2:41 PM

  Which became   DEC-018, approved, supersedes DEC-004
                 scope: export.authorization

  Which reached  SPEC-101  export specification
                 TICKET-101  implementation ticket
                 TASK-102  expose export to all users     invalidated
                 TASK-101  generate CSV files             still valid

  Preserved      CSV generation, download endpoint
  Changed        admin-only check, unauthorized-access test
```

The two-column ending — invalidated beside still-valid — is the product. Do not collapse it into one list.

---

## 4. `writai status` — Lane A

```
  5 sessions · graph-v18

    ●  Priya Raman     TASK-102   redirected    2m ago
    ●  Marcus Obi      TASK-102   redirected    2m ago
    ●  Dan Levy        TASK-102   redirected    2m ago
    ○  Ana Silva       TASK-101   running
    ○  Jonas Tan       TASK-101   running
```

An unbound session shows `—  <session-id>  unbound` so the gap is visible rather than silent.
