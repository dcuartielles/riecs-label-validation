import io
from pathlib import Path
from copy import copy

import openpyxl
from openpyxl.styles import PatternFill, Font, Alignment
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.auth import get_current_user
from app.database import SessionLocal
from app.models import AddedLabel, Group, LabelDecision, StoryLabel, TaxonomyLabel, User, UserStory

router = APIRouter()

DATASET_PATH = Path("input_data/unified_outcomes_v004_labelled_v002_UItest_v001.xlsx")

# ── Fill colours ──────────────────────────────────────────────────────────────
FILL_YELLOW = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")  # reviewed row
FILL_GREEN  = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")  # confirmed / added
FILL_RED    = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")  # rejected
FILL_HDR    = PatternFill(start_color="70AD47", end_color="70AD47", fill_type="solid")  # added-label header


def label_col_index(source: str, label_index: int) -> int:
    """Return 1-based column number for a label cell in the original spreadsheet."""
    # Human label N → col 9 + 2*(N-1)   (9,11,13,15,17)
    # AI label N    → col 10 + 2*(N-1)  (10,12,14,16,18)
    base = 9 if source == "Human" else 10
    return base + 2 * (label_index - 1)


def copy_cell_style(src, dst):
    if src.has_style:
        dst.font      = copy(src.font)
        dst.alignment = copy(src.alignment)
        dst.border    = copy(src.border)


async def build_sheet(ws_src, story_decisions: dict, story_added: dict, group_name: str, wb_out):
    """
    Clone ws_src into a new sheet in wb_out, applying colour highlights.

    story_decisions: {story_id_str: {(source, label_index): 'confirm'|'reject'|'abstain'}}
    story_added:     {story_id_str: [{'label': ..., 'sublabel': ..., 'note': ...}]}
    """
    ws = wb_out.create_sheet(title=group_name)

    # Find maximum number of added labels across any story (for extra columns)
    max_added = max((len(v) for v in story_added.values()), default=0)

    # ── Copy header row ────────────────────────────────────────────────────────
    headers = [cell.value for cell in ws_src[1]]
    for col_idx, val in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx, value=val)
        cell.font = Font(bold=True)
        copy_cell_style(ws_src.cell(row=1, column=col_idx), cell)

    # Added-label header columns
    for i in range(max_added):
        col_idx = len(headers) + i + 1
        cell = ws.cell(row=1, column=col_idx, value=f"Added label {i+1}")
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = FILL_HDR

    # ── Copy data rows ─────────────────────────────────────────────────────────
    for src_row in ws_src.iter_rows(min_row=2):
        story_id = src_row[0].value
        if not story_id:
            continue

        row_num = src_row[0].row
        decisions = story_decisions.get(str(story_id), {})
        was_reviewed = bool(decisions)

        for src_cell in src_row:
            dst = ws.cell(row=row_num, column=src_cell.column, value=src_cell.value)
            copy_cell_style(src_cell, dst)

            col = src_cell.column

            if was_reviewed and col <= 8:
                # Info columns — yellow to mark row as reviewed
                dst.fill = FILL_YELLOW
                continue

            # Label columns (9–18): colour by decision
            if col >= 9:
                # Work out which (source, index) this column corresponds to
                offset = col - 9         # 0-17 within label columns
                label_index = offset // 2 + 1
                source = "Human" if offset % 2 == 0 else "AI"
                decision = decisions.get((source, label_index))
                if decision == "confirm":
                    dst.fill = FILL_GREEN
                elif decision == "reject":
                    dst.fill = FILL_RED
                elif was_reviewed:
                    dst.fill = FILL_YELLOW  # reviewed but abstained / no decision on this cell

        # Write added labels in extra columns
        added = story_added.get(str(story_id), [])
        for i, lbl in enumerate(added):
            col_idx = len(headers) + i + 1
            text = lbl["label"]
            if lbl.get("sublabel"):
                text += f" > {lbl['sublabel']}"
            if lbl.get("note"):
                text += f" ({lbl['note']})"
            cell = ws.cell(row=row_num, column=col_idx, value=text)
            cell.fill = FILL_GREEN

    # Column widths — approximate
    for col in ws.columns:
        max_len = max((len(str(c.value or "")) for c in col), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 50)

    return ws


@router.get("/export")
async def export_all(request: Request, session_id: int | None = None):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    async with SessionLocal() as db:
        if user.is_admin:
            groups = (await db.execute(select(Group))).scalars().all()
        else:
            groups = (await db.execute(
                select(Group).where(Group.id == user.group_id)
            )).scalars().all()

        # Load source workbook
        wb_src = openpyxl.load_workbook(DATASET_PATH)
        ws_src = wb_src.active

        wb_out = openpyxl.Workbook()
        wb_out.remove(wb_out.active)  # remove default empty sheet

        for group in groups:
            # ── Decisions for this group (optionally filtered by session) ─────
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
            # Aggregate: if any group member rejected → reject; if all confirmed → confirm
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

            # ── Added labels for this group ───────────────────────────────────
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
                    "label": added.taxonomy_label.label if added.taxonomy_label else (added.free_text or ""),
                    "sublabel": added.taxonomy_label.sublabel if added.taxonomy_label else "",
                    "note": added.note or "",
                }
                story_added.setdefault(story.story_id, []).append(entry)

            await build_sheet(ws_src, story_decisions, story_added, group.name, wb_out)

    # Save to buffer
    buf = io.BytesIO()
    wb_out.save(buf)
    buf.seek(0)

    scope = "all_groups" if user.is_admin else f"{groups[0].name.replace(' ', '_')}"
    filename = f"label_validation_{scope}.xlsx"

    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
