import json
import random
from app.templates import templates
from datetime import datetime

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select, func, delete
from sqlalchemy.orm import selectinload

from app.auth import get_current_user
from app.database import SessionLocal
from app.models import (
    AddedLabel, AddedLabelDecision, Group, GroupAssignment,
    MandatoryClassification, Session as ReviewSession,
    StoryRejection, StoryRelevance, User, UserStory
)

router = APIRouter()

RANDOM_SEED = 42


async def require_admin(request: Request):
    user = await get_current_user(request)
    if not user or not user.is_admin:
        return None
    return user


async def get_active_session(db) -> ReviewSession | None:
    result = await db.execute(
        select(ReviewSession)
        .where(ReviewSession.ended_at.is_(None))
        .order_by(ReviewSession.started_at.desc())
    )
    return result.scalar_one_or_none()


def _assign_stories(story_ids: list[int], group_count: int,
                    overlap_pct: float, seed: int) -> dict[int, list[int]]:
    rng = random.Random(seed)
    ids = story_ids[:]
    rng.shuffle(ids)
    n = len(ids)
    n_shared = round(n * overlap_pct)
    shared = ids[:n_shared]
    unique_pool = ids[n_shared:]
    unique_per_group = max(1, (len(unique_pool) + group_count - 1) // group_count)
    assignments: dict[int, list[int]] = {}
    for g in range(group_count):
        start = g * unique_per_group
        end = min(start + unique_per_group, len(unique_pool))
        assignments[g] = shared + unique_pool[start:end]
    return assignments


@router.get("/admin", response_class=HTMLResponse)
async def admin(request: Request):
    user = await require_admin(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    async with SessionLocal() as db:
        users = (await db.execute(select(User).order_by(User.email))).scalars().all()
        groups = (await db.execute(select(Group))).scalars().all()
        active_session = await get_active_session(db)

        # Progress per group — stories with at least one added label in the active session
        progress = {}
        for group in groups:
            total_q = await db.execute(
                select(func.count(GroupAssignment.id))
                .where(GroupAssignment.group_id == group.id)
            )
            if active_session:
                labelled_q = await db.execute(
                    select(func.count(func.distinct(AddedLabel.story_id)))
                    .join(User, AddedLabel.user_id == User.id)
                    .where(
                        User.group_id == group.id,
                        AddedLabel.session_id == active_session.id,
                    )
                )
                labelled = labelled_q.scalar()
            else:
                labelled = 0
            progress[group.id] = {
                "total": total_q.scalar(),
                "labelled": labelled,
            }

        # Cross-group comparison — stories labelled by 2+ groups
        comparison = []
        if active_session:
            overlap_result = await db.execute(
                select(
                    UserStory.id,
                    UserStory.story_id,
                    func.count(func.distinct(User.group_id)).label("group_count"),
                )
                .join(AddedLabel, AddedLabel.story_id == UserStory.id)
                .join(User, AddedLabel.user_id == User.id)
                .where(
                    User.group_id.isnot(None),
                    AddedLabel.session_id == active_session.id,
                )
                .group_by(UserStory.id, UserStory.story_id)
                .having(func.count(func.distinct(User.group_id)) > 1)
                .order_by(UserStory.story_id)
            )
            overlapping = overlap_result.all()

            for row in overlapping:
                story_detail = []
                for group in groups:
                    count_q = await db.execute(
                        select(func.count(AddedLabel.id))
                        .join(User, AddedLabel.user_id == User.id)
                        .where(
                            AddedLabel.story_id == row.id,
                            AddedLabel.session_id == active_session.id,
                            User.group_id == group.id,
                        )
                    )
                    n = count_q.scalar()
                    if n:
                        story_detail.append({"group": group.name, "labels": n})
                if story_detail:
                    comparison.append({
                        "story_id": row.story_id,
                        "groups": story_detail,
                    })

    return templates.TemplateResponse(request, "admin.html", {
        "user": user,
        "users": users,
        "groups": groups,
        "progress": progress,
        "comparison": comparison,
        "active_session": active_session,
        "add_user_error": None,
    })


@router.post("/session/start")
async def start_session(
    request: Request,
    overlap_pct: float = Form(0.0),
    chart_refresh_secs: int = Form(300),
):
    user = await require_admin(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    overlap_pct = max(0.0, min(1.0, overlap_pct))
    chart_refresh_secs = max(60, chart_refresh_secs)

    async with SessionLocal() as db:
        existing = await get_active_session(db)
        if existing:
            return RedirectResponse(url="/admin", status_code=302)

        groups = (await db.execute(select(Group))).scalars().all()
        all_stories = (await db.execute(select(UserStory))).scalars().all()
        story_ids = [s.id for s in all_stories]
        n = len(story_ids)
        g = len(groups)
        stories_per_group = round(n / g) + round(n * overlap_pct / g) if g else 0

        # Regenerate group assignments
        await db.execute(delete(GroupAssignment))
        assignments = _assign_stories(story_ids, g, overlap_pct, RANDOM_SEED)
        for g_idx, group in enumerate(groups):
            for position, sid in enumerate(assignments[g_idx]):
                db.add(GroupAssignment(
                    group_id=group.id,
                    story_id=sid,
                    position=position,
                ))

        db.add(ReviewSession(
            started_by=user.id,
            overlap_pct=overlap_pct,
            stories_per_group=stories_per_group,
            chart_refresh_secs=chart_refresh_secs,
        ))
        await db.commit()

    return RedirectResponse(url="/admin", status_code=302)


@router.post("/session/end")
async def end_session(request: Request):
    user = await require_admin(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    async with SessionLocal() as db:
        session = await get_active_session(db)
        if session:
            session.ended_at = datetime.utcnow()
            await db.commit()

    return RedirectResponse(url="/admin", status_code=302)


@router.post("/admin/add-user")
async def add_user(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    group_id: int = Form(0),
):
    user = await require_admin(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    email = email.strip().lower()
    async with SessionLocal() as db:
        existing = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if not existing:
            db.add(User(
                name=name.strip(),
                email=email,
                group_id=group_id if group_id != 0 else None,
            ))
            await db.commit()

    return RedirectResponse(url="/admin", status_code=302)


@router.post("/admin/remove-user")
async def remove_user(
    request: Request,
    user_id: int = Form(...),
):
    user = await require_admin(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    async with SessionLocal() as db:
        target = await db.get(User, user_id)
        if target and target.id != user.id:
            await db.delete(target)
            await db.commit()

    return RedirectResponse(url="/admin", status_code=302)


@router.post("/admin/assign-group")
async def assign_group(
    request: Request,
    user_id: int = Form(...),
    group_id: int = Form(...),
):
    user = await require_admin(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    async with SessionLocal() as db:
        target = await db.get(User, user_id)
        if target:
            target.group_id = group_id if group_id != 0 else None
            await db.commit()

    return RedirectResponse(url="/admin", status_code=302)


@router.post("/admin/set-admin")
async def set_admin(
    request: Request,
    user_id: int = Form(...),
    is_admin: bool = Form(False),
):
    user = await require_admin(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    async with SessionLocal() as db:
        target = await db.get(User, user_id)
        if target:
            target.is_admin = is_admin
            await db.commit()

    return RedirectResponse(url="/admin", status_code=302)
