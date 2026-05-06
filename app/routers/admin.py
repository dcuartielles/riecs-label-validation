from app.templates import templates
from datetime import datetime

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from app.auth import get_current_user
from app.database import SessionLocal
from app.models import (
    Group, GroupAssignment, LabelDecision,
    Session as ReviewSession, User, UserStory
)

router = APIRouter()


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


@router.get("/admin", response_class=HTMLResponse)
async def admin(request: Request):
    user = await require_admin(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    async with SessionLocal() as db:
        users = (await db.execute(select(User).order_by(User.email))).scalars().all()
        groups = (await db.execute(select(Group))).scalars().all()
        active_session = await get_active_session(db)

        # Progress per group — scoped to the active session
        progress = {}
        for group in groups:
            total_q = await db.execute(
                select(func.count(GroupAssignment.id))
                .where(GroupAssignment.group_id == group.id)
            )
            decided_where = [User.group_id == group.id]
            if active_session:
                decided_where.append(LabelDecision.session_id == active_session.id)
            decided_q = await db.execute(
                select(func.count(func.distinct(LabelDecision.story_id)))
                .join(User, LabelDecision.user_id == User.id)
                .where(*decided_where)
            )
            progress[group.id] = {
                "total": total_q.scalar(),
                "decided": decided_q.scalar(),
            }

        # Cross-group comparison
        overlap_result = await db.execute(
            select(
                UserStory.id,
                UserStory.story_id,
                func.count(func.distinct(User.group_id)).label("group_count"),
            )
            .join(LabelDecision, LabelDecision.story_id == UserStory.id)
            .join(User, LabelDecision.user_id == User.id)
            .where(User.group_id.isnot(None))
            .group_by(UserStory.id, UserStory.story_id)
            .having(func.count(func.distinct(User.group_id)) > 1)
            .order_by(UserStory.story_id)
        )
        overlapping_stories = overlap_result.all()

        comparison = []
        for row in overlapping_stories:
            story_detail = []
            for group in groups:
                counts_q = await db.execute(
                    select(
                        LabelDecision.decision,
                        func.count(LabelDecision.id).label("n"),
                    )
                    .join(User, LabelDecision.user_id == User.id)
                    .where(
                        LabelDecision.story_id == row.id,
                        User.group_id == group.id,
                    )
                    .group_by(LabelDecision.decision)
                )
                counts = {r.decision: r.n for r in counts_q.all()}
                if counts:
                    story_detail.append({
                        "group": group.name,
                        "confirm": counts.get("confirm", 0),
                        "reject": counts.get("reject", 0),
                        "abstain": counts.get("abstain", 0),
                    })
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
async def start_session(request: Request):
    user = await require_admin(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    async with SessionLocal() as db:
        existing = await get_active_session(db)
        if not existing:
            db.add(ReviewSession(started_by=user.id))
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
        if target and target.id != user.id:  # can't remove yourself
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
