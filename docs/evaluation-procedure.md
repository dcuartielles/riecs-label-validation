# RIECS Label of User Stories — Simulation & Evaluation Procedure

Version: 1.0 — 2026-05-10  
Applies to: app v007 and later

---

## Purpose

This procedure describes how to run a controlled simulation of a labelling session to:
- Verify the infographic dashboard produces meaningful outputs before a real workshop
- Generate realistic data for README screenshots and stakeholder demonstrations
- Validate the export spreadsheet structure
- Catch issues with group assignments, mandatory classifications, and conflict detection

The procedure can be repeated before any workshop. The simulation script is
`scripts/simulate_session.py`.

---

## Prerequisites

1. Database initialised with real stories, taxonomy, and groups:
   ```
   python scripts/import_data.py --reset
   ```
2. Real participant users registered (or at least the admin account present).
3. Server running (`python run.py`).
4. No active session in the database.

---

## Phase 0 — Backup real users

Before creating any fake data, save the current user list so it can be
restored later.

```bash
python scripts/simulate_session.py --save-users
```

Output: `scripts/real_users_backup.json`

This file records every user's id, name, email, group_id, and is_admin flag.

---

## Phase 1 — Create fake users, session, and 50% labels

```bash
python scripts/simulate_session.py --phase 1
```

This command performs four steps in sequence:

1. **Insert fake users** — 3 per institution (13 institutions = 39 users total),
   each with a `@simulation.fake` email so they can be identified and removed.
   Fake users are assigned to their institution's group.

2. **Create session** — overlap = 10%, chart refresh = 60 s.
   Story assignments are generated with `RANDOM_SEED = 42` (same seed as the
   admin panel) so the simulation matches what the server would produce.

3. **Generate labels** — For each group, 50% of assigned stories receive:
   - 1 mandatory target-user classification
   - 1–3 mandatory story-concept classifications
   - 2–5 technical taxonomy labels  
     (selected by keyword analysis of task + goal + notes text)
   - ~7% chance of story rejection with a reason
   - ~12% chance of High or Very High relevance with a reason

4. **Print summary** — total labels added, stories covered, time taken.

---

## Phase 1 evaluation — Infograph review

After phase 1 completes, open the app in a browser and review each
infographic tab. Verify:

| Tab | What to check |
|-----|--------------|
| C01/C10 · Frequency | Top labels visible, category colours correct, bars clickable |
| C02/C03 · Distributions | Pie slices with realistic target-user and concept split |
| C04/C11 · Networks | Graph renders, nodes draggable, edge weights visible |
| C05 · Heatmap | ~20×20 matrix with gradient colours |
| C06/C12 · Treemaps | Tiles grouped by category, hover tooltips work |
| C07 · Partners | All 13 partner bars visible in stacked chart |
| C08/C13 · Pareto | 80% annotation visible on the cumulative line |
| C14/C15 · Breakdown | Rows per user type / concept, cells with counts |
| Filter bar | Click a bar → amber filter bar appears → Clear all resets |

If any tab is empty or broken, report the issue before continuing to phase 2.

Screenshots of all tabs at this point should be saved to `docs/screenshots/`
(use the browser's full-page screenshot tool or Ctrl+Shift+S in Firefox).

---

## Phase 2 — Complete remaining labels

```bash
python scripts/simulate_session.py --phase 2
```

Labels the remaining ~50% of story-group pairs using the same keyword
analysis as phase 1.

After phase 2 completes:
- Re-screenshot all infograph tabs (these are the final screenshots for the README)
- Download the export spreadsheet from `/export` and verify:
  - Summary sheet has colour-coded rows
  - Rejections & Relevance sheet lists flagged stories
  - Statistics & Conflicts sheet shows per-partner counts

---

## Phase 3 — Export and document

1. Navigate to `/export` → download the results spreadsheet.
2. Verify sheet names: one per partner (13), Summary, Rejections & Relevance,
   Statistics & Conflicts.
3. Save the file to `docs/` for reference.
4. Update `README.md` with the screenshots taken after phase 2.

---

## Phase 4 — Reset

```bash
python scripts/simulate_session.py --reset
```

This command:
1. Deletes all session data (added_labels, mandatory_classifications,
   story_rejections, story_relevances, added_label_decisions, sessions)
2. Removes all fake users (identified by `@simulation.fake` email)
3. Restores the real user list from `scripts/real_users_backup.json`
   (re-inserts any users who were deleted, restores group assignments)

After reset, verify the DB is clean:
```bash
python scripts/simulate_session.py --status
```

---

## Timing estimates

| Phase | Stories labelled | Approximate duration |
|-------|-----------------|---------------------|
| Phase 1 | ~412 | 30–60 s |
| Phase 2 | ~412 | 30–60 s |
| Total | ~823 | < 2 min |

---

## Notes on label quality

The simulation uses keyword analysis of story text fields (task, goal,
additional_notes, user_type) to choose labels. Accuracy is intentionally
representative rather than perfect — the goal is to produce a realistic
distribution across all taxonomy categories, not to perfectly classify
every story. Expect:

- ~5–10% of labels may be debatable
- Co-occurrence patterns will emerge for related concepts (e.g. Data Privacy +
  Security commonly appear together)
- Partner distributions (C07) will differ because different groups review
  different story subsets with different domain distributions
- The conflict detection (Statistics & Conflicts sheet) will flag stories in
  the 10% overlap set where both groups assigned different labels

---

## Repeating the procedure

The simulation is deterministic (fixed seeds) so phases 1 and 2 can be
re-run after a reset to produce identical data. Change `SIM_RNG_SEED`
at the top of `simulate_session.py` to produce a different randomisation.
