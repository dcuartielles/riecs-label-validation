from app.templates import templates
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select, func, and_
from sqlalchemy.orm import selectinload
from app.auth import get_current_user
from app.database import SessionLocal
from app.models import (
    AddedLabel, Group, GroupAssignment, LabelDecision,
    Session as ReviewSession, TaxonomyLabel, User, UserStory
)

router = APIRouter()


@router.get("/stats", response_class=HTMLResponse)
async def stats(request: Request, session_id: int | None = None):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    async with SessionLocal() as db:
        groups = (await db.execute(select(Group))).scalars().all()

        # All sessions (global, no group filter)
        all_sessions = (await db.execute(
            select(ReviewSession)
            .order_by(ReviewSession.started_at.desc())
        )).scalars().all()

        selected_session = None
        if session_id:
            for s in all_sessions:
                if s.id == session_id:
                    selected_session = s
                    break
        if not selected_session and all_sessions:
            selected_session = all_sessions[0]

        group_stats = []
        for group in groups:
            if not user.is_admin and group.id != user.group_id:
                continue

            total_assigned = (await db.execute(
                select(func.count(GroupAssignment.id))
                .where(GroupAssignment.group_id == group.id)
            )).scalar()

            session_filter = and_(
                User.group_id == group.id,
                LabelDecision.session_id == selected_session.id,
            ) if selected_session else and_(User.group_id == group.id)

            stories_reviewed = (await db.execute(
                select(func.count(func.distinct(LabelDecision.story_id)))
                .join(User, LabelDecision.user_id == User.id)
                .where(session_filter)
            )).scalar()

            confirmed = (await db.execute(
                select(func.count(LabelDecision.id))
                .join(User, LabelDecision.user_id == User.id)
                .where(and_(session_filter, LabelDecision.decision == "confirm"))
            )).scalar()

            rejected = (await db.execute(
                select(func.count(LabelDecision.id))
                .join(User, LabelDecision.user_id == User.id)
                .where(and_(session_filter, LabelDecision.decision == "reject"))
            )).scalar()

            added_filter = and_(
                User.group_id == group.id,
                AddedLabel.session_id == selected_session.id,
            ) if selected_session else and_(User.group_id == group.id)

            new_labels = (await db.execute(
                select(func.count(AddedLabel.id))
                .join(User, AddedLabel.user_id == User.id)
                .where(added_filter)
            )).scalar()

            created_labels = (await db.execute(
                select(func.count(func.distinct(AddedLabel.taxonomy_label_id)))
                .join(User, AddedLabel.user_id == User.id)
                .join(TaxonomyLabel, AddedLabel.taxonomy_label_id == TaxonomyLabel.id)
                .where(and_(added_filter, TaxonomyLabel.is_user_created == True))
            )).scalar()

            group_stats.append({
                "group": group,
                "total_assigned": total_assigned,
                "stories_reviewed": stories_reviewed,
                "confirmed": confirmed,
                "rejected": rejected,
                "new_labels": new_labels,
                "created_labels": created_labels,
            })

        # Cross-group overlap (admin only)
        overlap_data = []
        if user.is_admin and len(groups) > 1 and selected_session:
            overlap_result = await db.execute(
                select(
                    UserStory.story_id,
                    func.count(func.distinct(User.group_id)).label("group_count"),
                )
                .join(LabelDecision, LabelDecision.story_id == UserStory.id)
                .join(User, LabelDecision.user_id == User.id)
                .where(and_(
                    User.group_id.isnot(None),
                    LabelDecision.session_id == selected_session.id,
                ))
                .group_by(UserStory.story_id)
                .having(func.count(func.distinct(User.group_id)) > 1)
            )
            overlap_data = overlap_result.all()

    return templates.TemplateResponse(request, "stats.html", {
        "user": user,
        "group_stats": group_stats,
        "overlap_count": len(overlap_data),
        "all_sessions": all_sessions,
        "selected_session": selected_session,
    })
