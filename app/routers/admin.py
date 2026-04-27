from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from app.auth import get_current_user
from app.database import SessionLocal
from app.models import Group, GroupAssignment, LabelDecision, StoryLabel, User, UserStory

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


async def require_admin(request: Request):
    user = await get_current_user(request)
    if not user or not user.is_admin:
        return None
    return user


@router.get("/admin", response_class=HTMLResponse)
async def admin(request: Request):
    user = await require_admin(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    async with SessionLocal() as db:
        users = (await db.execute(select(User).order_by(User.email))).scalars().all()
        groups = (await db.execute(select(Group))).scalars().all()

        # Progress per group
        progress = {}
        for group in groups:
            total_q = await db.execute(
                select(func.count(GroupAssignment.id))
                .where(GroupAssignment.group_id == group.id)
            )
            decided_q = await db.execute(
                select(func.count(func.distinct(LabelDecision.story_id)))
                .join(User, LabelDecision.user_id == User.id)
                .where(User.group_id == group.id)
            )
            progress[group.id] = {
                "total": total_q.scalar(),
                "decided": decided_q.scalar(),
            }

        # Cross-group comparison: stories reviewed by 2+ groups
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

        # For each overlapping story, get per-group confirm/reject rates
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
    })


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
