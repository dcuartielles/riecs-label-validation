import json
from datetime import datetime
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, and_
from sqlalchemy.orm import selectinload
from app.auth import get_current_user
from app.database import SessionLocal
from app.models import (
    GroupAssignment, LabelDecision, AddedLabel,
    Session as ReviewSession, StoryLabel, TaxonomyLabel, UserStory
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


async def get_or_create_session(db, group_id: int) -> ReviewSession:
    result = await db.execute(
        select(ReviewSession)
        .where(and_(ReviewSession.group_id == group_id,
                    ReviewSession.ended_at.is_(None)))
        .order_by(ReviewSession.started_at.desc())
    )
    session = result.scalar_one_or_none()
    if not session:
        session = ReviewSession(group_id=group_id)
        db.add(session)
        await db.flush()
    return session


@router.get("/review", response_class=HTMLResponse)
async def review(request: Request, pos: int = 0):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not user.group_id:
        return templates.TemplateResponse(request, "no_group.html", {"user": user})

    async with SessionLocal() as db:
        # Load this group's ordered subset
        result = await db.execute(
            select(GroupAssignment)
            .where(GroupAssignment.group_id == user.group_id)
            .order_by(GroupAssignment.position)
        )
        assignments = result.scalars().all()
        total = len(assignments)

        if total == 0:
            return templates.TemplateResponse(request, "no_stories.html", {"user": user})

        pos = max(0, min(pos, total - 1))
        assignment = assignments[pos]

        # Load story with its labels
        story = await db.get(UserStory, assignment.story_id)
        label_result = await db.execute(
            select(StoryLabel).where(StoryLabel.story_id == story.id)
        )
        labels = label_result.scalars().all()

        # Load existing decisions for this story in the current session
        rev_session = await get_or_create_session(db, user.group_id)
        await db.commit()

        decisions_result = await db.execute(
            select(LabelDecision).where(
                and_(
                    LabelDecision.session_id == rev_session.id,
                    LabelDecision.story_id == story.id,
                    LabelDecision.user_id == user.id,
                )
            )
        )
        decisions = {d.story_label_id: d.decision for d in decisions_result.scalars()}

        # Load added labels for this story in session
        added_result = await db.execute(
            select(AddedLabel).where(
                and_(
                    AddedLabel.session_id == rev_session.id,
                    AddedLabel.story_id == story.id,
                    AddedLabel.user_id == user.id,
                )
            ).options(selectinload(AddedLabel.taxonomy_label))
        )
        added = added_result.scalars().all()

        # Load taxonomy for the add-label panel
        tax_result = await db.execute(select(TaxonomyLabel).order_by(
            TaxonomyLabel.label, TaxonomyLabel.sublabel
        ))
        taxonomy = tax_result.scalars().all()

        # Group taxonomy by top-level label
        tax_tree: dict[str, list] = {}
        tax_json: dict[str, dict] = {}
        tax_desc: dict[str, str] = {}  # normalized text → description
        for t in taxonomy:
            tax_tree.setdefault(t.label, [])
            if t.sublabel:
                tax_tree[t.label].append(t)
                tax_json[str(t.id)] = {
                    "label": t.label,
                    "sublabel": t.sublabel or "",
                    "description": t.description or "",
                }
            if t.description:
                if t.sublabel:
                    tax_desc[t.sublabel.strip().lower()] = t.description
                tax_desc[t.label.strip().lower()] = t.description

        # Map story_label.id → taxonomy description (for hover tooltips)
        label_descriptions: dict[int, str] = {}
        for lbl in labels:
            desc = tax_desc.get(lbl.label_text.strip().lower(), "")
            if desc:
                label_descriptions[lbl.id] = desc

        return templates.TemplateResponse(request, "review.html", {
            "user": user,
            "story": story,
            "labels": labels,
            "decisions": decisions,
            "added": added,
            "pos": pos,
            "total": total,
            "session_id": rev_session.id,
            "tax_json": json.dumps(tax_json),
            "tax_tree": tax_tree,
            "label_descriptions": label_descriptions,
        })


@router.post("/review/decide")
async def decide(
    request: Request,
    story_id: int = Form(...),
    story_label_id: int = Form(...),
    decision: str = Form(...),
    pos: int = Form(0),
):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    if decision not in ("confirm", "reject", "abstain"):
        return JSONResponse({"error": "invalid decision"}, status_code=400)

    async with SessionLocal() as db:
        rev_session = await get_or_create_session(db, user.group_id)

        result = await db.execute(
            select(LabelDecision).where(
                and_(
                    LabelDecision.session_id == rev_session.id,
                    LabelDecision.user_id == user.id,
                    LabelDecision.story_label_id == story_label_id,
                )
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.decision = decision
        else:
            db.add(LabelDecision(
                session_id=rev_session.id,
                user_id=user.id,
                story_id=story_id,
                story_label_id=story_label_id,
                decision=decision,
            ))
        await db.commit()

    return RedirectResponse(url=f"/review?pos={pos}", status_code=302)


@router.post("/review/add-label")
async def add_label(
    request: Request,
    story_id: int = Form(...),
    taxonomy_label_id: int = Form(None),
    free_text: str = Form(None),
    note: str = Form(None),
    pos: int = Form(0),
):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    async with SessionLocal() as db:
        rev_session = await get_or_create_session(db, user.group_id)
        db.add(AddedLabel(
            session_id=rev_session.id,
            user_id=user.id,
            story_id=story_id,
            taxonomy_label_id=taxonomy_label_id or None,
            free_text=free_text or None,
            note=note or None,
        ))
        await db.commit()

    return RedirectResponse(url=f"/review?pos={pos}", status_code=302)


@router.post("/review/remove-added-label")
async def remove_added_label(
    request: Request,
    added_label_id: int = Form(...),
    pos: int = Form(0),
):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    async with SessionLocal() as db:
        label = await db.get(AddedLabel, added_label_id)
        if label and label.user_id == user.id:
            await db.delete(label)
            await db.commit()

    return RedirectResponse(url=f"/review?pos={pos}", status_code=302)


@router.post("/session/end")
async def end_session(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    async with SessionLocal() as db:
        result = await db.execute(
            select(ReviewSession)
            .where(and_(
                ReviewSession.group_id == user.group_id,
                ReviewSession.ended_at.is_(None),
            ))
            .order_by(ReviewSession.started_at.desc())
        )
        session = result.scalar_one_or_none()
        if session:
            session.ended_at = datetime.utcnow()
            await db.commit()

    return RedirectResponse(url="/stats", status_code=302)
