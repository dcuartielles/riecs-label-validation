import io
import re
from collections import defaultdict
from copy import copy
from pathlib import Path

import openpyxl
from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.auth import get_current_user
from app.database import SessionLocal
from app.models import AddedLabel, Group, LabelDecision, StoryLabel, TaxonomyLabel, User, UserStory

router = APIRouter()

from app.config import find_latest_dataset
DATASET_PATH = find_latest_dataset()
LABELBOOK_PATH = Path("labelbook/Revised labelbook proposal for Oulu.xlsx")

# ── Fill colours ──────────────────────────────────────────────────────────────
FILL_YELLOW      = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")
FILL_GREEN       = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
FILL_RED         = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
FILL_LT_GREEN    = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid")
FILL_LT_RED      = PatternFill(start_color="FCE4D6", end_color="FCE4D6", fill_type="solid")
FILL_HDR         = PatternFill(start_color="70AD47", end_color="70AD47", fill_type="solid")
FILL_HDR_NEW     = PatternFill(start_color="375623", end_color="375623", fill_type="solid")
FILL_STAT_HDR    = PatternFill(start_color="2C324C", end_color="2C324C", fill_type="solid")
FILL_CONFLICT    = PatternFill(start_color="FCE4D6", end_color="FCE4D6", fill_type="solid")

FONT_WHITE_BOLD  = Font(bold=True, color="FFFFFF")


def copy_cell_style(src, dst):
    if src.has_style:
        dst.font      = copy(src.font)
        dst.alignment = copy(src.alignment)
        dst.border    = copy(src.border)


def build_label_col_map(headers: list) -> dict[int, tuple[str, int]]:
    """Map 1-based column index → (source, label_index) for Human/AI label columns."""
    result = {}
    for i, h in enumerate(headers):
        if not h:
            continue
        m = re.match(r'^(Human|AI) label (\d+)$', str(h).strip(), re.IGNORECASE)
        if m:
            source = "Human" if m.group(1).lower() == "human" else "AI"
            result[i + 1] = (source, int(m.group(2)))
    return result


# ── Overview sheet (Sheet 1) ──────────────────────────────────────────────────

def build_overview_sheet(ws_src, all_story_decisions: dict, story_added: dict, wb_out):
    """
    Sheet 1: full dataset.
    Row colours:
      - light green  → reviewed by 2+ groups, all decisions agree
      - light red    → reviewed by 2+ groups, at least one conflict
      - yellow       → reviewed by exactly 1 group
      - no fill      → not reviewed
    """
    ws = wb_out.create_sheet(title="Overview")
    headers = [cell.value for cell in ws_src[1]]
    label_col_map = build_label_col_map(headers)

    max_added = max((len(v) for v in story_added.values()), default=0)

    for col_idx, val in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx, value=val)
        cell.font = Font(bold=True)
        copy_cell_style(ws_src.cell(row=1, column=col_idx), cell)

    for i in range(max_added):
        cell = ws.cell(row=1, column=len(headers) + i + 1, value=f"Added label {i+1}")
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = FILL_HDR

    for src_row in ws_src.iter_rows(min_row=2):
        story_id = str(src_row[0].value or "")
        if not story_id:
            continue
        row_num = src_row[0].row

        group_decisions = all_story_decisions.get(story_id, {})
        n_groups = len(group_decisions)

        if n_groups == 0:
            row_fill = None
        elif n_groups == 1:
            row_fill = FILL_YELLOW
        else:
            conflict = False
            all_keys = set()
            for gd in group_decisions.values():
                all_keys |= gd.keys()
            for key in all_keys:
                decisions_for_key = {gd[key] for gd in group_decisions.values() if key in gd}
                if len(decisions_for_key) > 1:
                    conflict = True
                    break
            row_fill = FILL_LT_RED if conflict else FILL_LT_GREEN

        for src_cell in src_row:
            dst = ws.cell(row=row_num, column=src_cell.column, value=src_cell.value)
            copy_cell_style(src_cell, dst)
            if row_fill:
                dst.fill = row_fill

        for i, lbl in enumerate(story_added.get(story_id, [])):
            text = lbl["label"]
            if lbl.get("sublabel"):
                text += f" > {lbl['sublabel']}"
            if lbl.get("note"):
                text += f" ({lbl['note']})"
            cell = ws.cell(row=row_num, column=len(headers) + i + 1, value=text)
            cell.fill = FILL_GREEN

    for col in ws.columns:
        max_len = max((len(str(c.value or "")) for c in col), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 50)

    return ws


# ── Per-group sheet ───────────────────────────────────────────────────────────

def build_group_sheet(ws_src, story_decisions: dict, story_added: dict, group_name: str, wb_out):
    """One sheet per group with the original colour scheme."""
    ws = wb_out.create_sheet(title=group_name)
    headers = [cell.value for cell in ws_src[1]]
    label_col_map = build_label_col_map(headers)
    max_added = max((len(v) for v in story_added.values()), default=0)

    for col_idx, val in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx, value=val)
        cell.font = Font(bold=True)
        copy_cell_style(ws_src.cell(row=1, column=col_idx), cell)

    for i in range(max_added):
        col_idx = len(headers) + i + 1
        cell = ws.cell(row=1, column=col_idx, value=f"Added label {i+1}")
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = FILL_HDR

    for src_row in ws_src.iter_rows(min_row=2):
        story_id = str(src_row[0].value or "")
        if not story_id:
            continue
        row_num = src_row[0].row
        decisions = story_decisions.get(story_id, {})
        was_reviewed = bool(decisions)

        for src_cell in src_row:
            dst = ws.cell(row=row_num, column=src_cell.column, value=src_cell.value)
            copy_cell_style(src_cell, dst)
            col = src_cell.column
            info = label_col_map.get(col)
            if was_reviewed and not info:
                dst.fill = FILL_YELLOW
            elif info:
                decision = decisions.get(info)
                if decision == "confirm":
                    dst.fill = FILL_GREEN
                elif decision == "reject":
                    dst.fill = FILL_RED
                elif was_reviewed:
                    dst.fill = FILL_YELLOW

        added = story_added.get(story_id, [])
        for i, lbl in enumerate(added):
            text = lbl["label"]
            if lbl.get("sublabel"):
                text += f" > {lbl['sublabel']}"
            if lbl.get("note"):
                text += f" ({lbl['note']})"
            cell = ws.cell(row=row_num, column=len(headers) + i + 1, value=text)
            cell.fill = FILL_GREEN

    for col in ws.columns:
        max_len = max((len(str(c.value or "")) for c in col), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 50)

    return ws


# ── Statistics & conflicts sheet ──────────────────────────────────────────────

def build_stats_sheet(wb_out, groups, group_stats, conflict_rows):
    ws = wb_out.create_sheet(title="Statistics & Conflicts")

    def hdr(row, col, val, fill=None):
        cell = ws.cell(row=row, column=col, value=val)
        cell.font = FONT_WHITE_BOLD
        if fill:
            cell.fill = fill
        return cell

    # ── Summary block ──────────────────────────────────────────────────────
    hdr(1, 1, "Group", FILL_STAT_HDR)
    hdr(1, 2, "Assigned", FILL_STAT_HDR)
    hdr(1, 3, "Reviewed", FILL_STAT_HDR)
    hdr(1, 4, "Confirmed", FILL_STAT_HDR)
    hdr(1, 5, "Rejected", FILL_STAT_HDR)
    hdr(1, 6, "New labels", FILL_STAT_HDR)

    for r, gs in enumerate(group_stats, start=2):
        ws.cell(row=r, column=1, value=gs["group"].name)
        ws.cell(row=r, column=2, value=gs["total_assigned"])
        ws.cell(row=r, column=3, value=gs["stories_reviewed"])
        ws.cell(row=r, column=4, value=gs["confirmed"]).fill = FILL_GREEN
        ws.cell(row=r, column=5, value=gs["rejected"]).fill = FILL_RED
        ws.cell(row=r, column=6, value=gs["new_labels"])

    conflict_start = len(group_stats) + 4

    # ── Conflicts block ────────────────────────────────────────────────────
    ws.cell(row=conflict_start - 1, column=1, value="Conflicts — stories with differing group decisions").font = Font(bold=True, size=12)

    col_headers = ["Story ID", "Label"] + [g.name for g in groups]
    for c, val in enumerate(col_headers, 1):
        hdr(conflict_start, c, val, FILL_STAT_HDR)

    for r, row in enumerate(conflict_rows, start=conflict_start + 1):
        ws.cell(row=r, column=1, value=row["story_id"])
        ws.cell(row=r, column=2, value=row["label"])
        for c, group in enumerate(groups, start=3):
            dec = row["decisions"].get(group.name, "—")
            cell = ws.cell(row=r, column=c, value=dec)
            if dec == "confirm":
                cell.fill = FILL_GREEN
            elif dec == "reject":
                cell.fill = FILL_RED

    for col in ws.columns:
        max_len = max((len(str(c.value or "")) for c in col), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 30)

    return ws


# ── Main export endpoint ──────────────────────────────────────────────────────

@router.get("/export")
async def export_all(request: Request, session_id: int | None = None):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    async with SessionLocal() as db:
        groups = (await db.execute(select(Group))).scalars().all()
        if not user.is_admin:
            groups = [g for g in groups if g.id == user.group_id]

        wb_src = openpyxl.load_workbook(DATASET_PATH)
        ws_src = wb_src.active
        wb_out = openpyxl.Workbook()
        wb_out.remove(wb_out.active)

        # Collect per-group decision and added-label maps
        group_story_decisions: dict[int, dict[str, dict]] = {}   # group_id → story_id → {(src,idx): decision}
        group_story_added:     dict[int, dict[str, list]] = {}   # group_id → story_id → [entries]
        group_stats_list = []

        for group in groups:
            dec_where = [User.group_id == group.id]
            if session_id:
                dec_where.append(LabelDecision.session_id == session_id)

            dec_result = await db.execute(
                select(LabelDecision, StoryLabel, UserStory)
                .join(StoryLabel, StoryLabel.id == LabelDecision.story_label_id)
                .join(UserStory, UserStory.id == LabelDecision.story_id)
                .join(User, User.id == LabelDecision.user_id)
                .where(*dec_where)
            )

            raw: dict[str, dict[tuple, list[str]]] = {}
            for dec, lbl, story in dec_result.all():
                key = (lbl.source, lbl.label_index)
                raw.setdefault(story.story_id, {}).setdefault(key, []).append(dec.decision)

            story_decisions: dict[str, dict[tuple, str]] = {}
            for sid, label_map in raw.items():
                story_decisions[sid] = {}
                for key, votes in label_map.items():
                    if "reject" in votes:
                        story_decisions[sid][key] = "reject"
                    elif all(v == "confirm" for v in votes):
                        story_decisions[sid][key] = "confirm"
                    else:
                        story_decisions[sid][key] = "abstain"

            added_where = [User.group_id == group.id]
            if session_id:
                added_where.append(AddedLabel.session_id == session_id)

            added_result = await db.execute(
                select(AddedLabel, UserStory)
                .join(UserStory, UserStory.id == AddedLabel.story_id)
                .join(User, User.id == AddedLabel.user_id)
                .outerjoin(TaxonomyLabel, TaxonomyLabel.id == AddedLabel.taxonomy_label_id)
                .where(*added_where)
                .options(selectinload(AddedLabel.taxonomy_label))
            )
            story_added: dict[str, list] = {}
            for added, story in added_result.all():
                entry = {
                    "label":    added.taxonomy_label.label    if added.taxonomy_label else (added.free_text or ""),
                    "sublabel": added.taxonomy_label.sublabel if added.taxonomy_label else "",
                    "note":     added.note or "",
                }
                story_added.setdefault(story.story_id, []).append(entry)

            group_story_decisions[group.id] = story_decisions
            group_story_added[group.id]     = story_added

            # Collect stats
            from sqlalchemy import func, and_
            from app.models import GroupAssignment
            total_assigned = (await db.execute(
                select(func.count(GroupAssignment.id)).where(GroupAssignment.group_id == group.id)
            )).scalar()
            group_stats_list.append({
                "group": group,
                "total_assigned": total_assigned,
                "stories_reviewed": len(story_decisions),
                "confirmed": sum(1 for sd in story_decisions.values() for d in sd.values() if d == "confirm"),
                "rejected":  sum(1 for sd in story_decisions.values() for d in sd.values() if d == "reject"),
                "new_labels": sum(len(v) for v in story_added.values()),
            })

        # Build combined story decisions {story_id: {group_id: {key: decision}}}
        all_story_decisions: dict[str, dict] = {}
        for group in groups:
            for story_id, sd in group_story_decisions[group.id].items():
                all_story_decisions.setdefault(story_id, {})[group.id] = sd

        # Merge added labels across all groups (deduplicate by label text)
        all_story_added: dict[str, list] = {}
        for gid, sadded in group_story_added.items():
            for story_id, entries in sadded.items():
                seen = {e["label"] + (e.get("sublabel") or "") for e in all_story_added.get(story_id, [])}
                for entry in entries:
                    key = entry["label"] + (entry.get("sublabel") or "")
                    if key not in seen:
                        all_story_added.setdefault(story_id, []).append(entry)
                        seen.add(key)

        # Build conflict rows for stats sheet
        conflict_rows = []
        for story_id, gd in all_story_decisions.items():
            if len(gd) < 2:
                continue
            all_keys = set()
            for d in gd.values():
                all_keys |= d.keys()
            for key in all_keys:
                group_decisions = {g.name: gd[g.id][key] for g in groups if g.id in gd and key in gd[g.id]}
                unique_decisions = set(group_decisions.values())
                if len(unique_decisions) > 1:
                    label_text = f"{key[0]} label {key[1]}"
                    conflict_rows.append({
                        "story_id": story_id,
                        "label": label_text,
                        "decisions": group_decisions,
                    })
        conflict_rows.sort(key=lambda r: r["story_id"])

        # Sheet 1: Overview
        build_overview_sheet(ws_src, all_story_decisions, all_story_added, wb_out)

        # Per-group sheets
        for group in groups:
            build_group_sheet(
                ws_src,
                group_story_decisions[group.id],
                group_story_added[group.id],
                group.name,
                wb_out,
            )

        # Final: Statistics & Conflicts
        build_stats_sheet(wb_out, groups, group_stats_list, conflict_rows)

    buf = io.BytesIO()
    wb_out.save(buf)
    buf.seek(0)
    filename = "label_validation_results.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ── Revised labelbook export ──────────────────────────────────────────────────

@router.get("/export/labelbook")
async def export_labelbook(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    async with SessionLocal() as db:
        new_labels = (await db.execute(
            select(TaxonomyLabel)
            .where(TaxonomyLabel.is_user_created == True)
            .order_by(TaxonomyLabel.label, TaxonomyLabel.sublabel)
        )).scalars().all()

    wb = openpyxl.load_workbook(LABELBOOK_PATH)
    ws = wb.active

    if new_labels:
        # Find the last used row and append new labels with a green section
        last_row = ws.max_row + 2
        header_cell = ws.cell(row=last_row, column=3, value="NEW LABELS (added during sessions)")
        header_cell.font = FONT_WHITE_BOLD
        header_cell.fill = FILL_HDR_NEW
        ws.merge_cells(start_row=last_row, start_column=3, end_row=last_row, end_column=6)

        for i, t in enumerate(new_labels, start=1):
            r = last_row + i
            ws.cell(row=r, column=3, value=t.label).fill  = FILL_GREEN
            ws.cell(row=r, column=4, value=t.sublabel).fill = FILL_GREEN
            ws.cell(row=r, column=5, value=t.source or "").fill = FILL_GREEN
            ws.cell(row=r, column=6, value=t.description or "").fill = FILL_GREEN

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=labelbook_revised.xlsx"},
    )
