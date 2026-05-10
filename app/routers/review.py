import json
from app.templates import templates
from difflib import SequenceMatcher
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy import select, and_
from sqlalchemy.orm import selectinload
import httpx

from app.auth import get_current_user
from app.database import SessionLocal
from app.models import (
    AddedLabel, AddedLabelDecision, GroupAssignment,
    MandatoryClassification, Session as ReviewSession,
    StoryRejection, StoryRelevance, TaxonomyLabel, UserStory
)

router = APIRouter()

SPARQL_ENDPOINT = "https://publications.europa.eu/webapi/rdf/sparql"

MANDATORY_CATEGORIES = {"Target user in story", "User Story Concept"}


async def get_active_session(db) -> ReviewSession | None:
    result = await db.execute(
        select(ReviewSession)
        .where(ReviewSession.ended_at.is_(None))
        .order_by(ReviewSession.started_at.desc())
    )
    return result.scalar_one_or_none()


@router.get("/review", response_class=HTMLResponse)
async def review(request: Request, pos: int = 0):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not user.group_id:
        return templates.TemplateResponse(request, "no_group.html", {"user": user})

    async with SessionLocal() as db:
        rev_session = await get_active_session(db)
        if not rev_session:
            return templates.TemplateResponse(request, "waiting.html", {"user": user})

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

        story = await db.get(UserStory, assignment.story_id)

        # All labels added by anyone in this group for this story/session
        from app.models import User as UserModel
        added_result = await db.execute(
            select(AddedLabel)
            .join(UserModel, AddedLabel.user_id == UserModel.id)
            .where(
                AddedLabel.session_id == rev_session.id,
                AddedLabel.story_id == story.id,
                UserModel.group_id == user.group_id,
            )
            .options(selectinload(AddedLabel.taxonomy_label),
                     selectinload(AddedLabel.decisions))
        )
        all_added = added_result.scalars().all()

        # Separate: own labels vs teammate labels
        my_labels = [a for a in all_added if a.user_id == user.id]
        teammate_labels = [a for a in all_added if a.user_id != user.id]

        # My decisions on teammate labels
        my_decisions = {}
        for dec in [d for a in teammate_labels for d in a.decisions if d.user_id == user.id]:
            my_decisions[dec.added_label_id] = dec.decision

        # User-created added labels (for separate display)
        uc_ids_result = await db.execute(
            select(TaxonomyLabel.id).where(TaxonomyLabel.is_user_created == True)
        )
        user_created_ids = set(uc_ids_result.scalars().all())
        my_added = [a for a in my_labels if a.taxonomy_label_id not in user_created_ids]
        my_created = [a for a in my_labels if a.taxonomy_label_id in user_created_ids]

        # Mandatory classification for this group/story/session
        mc_result = await db.execute(
            select(MandatoryClassification).where(
                MandatoryClassification.session_id == rev_session.id,
                MandatoryClassification.group_id == user.group_id,
                MandatoryClassification.story_id == story.id,
            )
        )
        mandatory = mc_result.scalar_one_or_none()

        # Story rejection
        rej_result = await db.execute(
            select(StoryRejection).where(
                StoryRejection.session_id == rev_session.id,
                StoryRejection.group_id == user.group_id,
                StoryRejection.story_id == story.id,
            )
        )
        rejection = rej_result.scalar_one_or_none()

        # Story relevance
        rel_result = await db.execute(
            select(StoryRelevance).where(
                StoryRelevance.session_id == rev_session.id,
                StoryRelevance.group_id == user.group_id,
                StoryRelevance.story_id == story.id,
            )
        )
        relevance = rel_result.scalar_one_or_none()

        # Taxonomy — split mandatory from regular
        tax_result = await db.execute(select(TaxonomyLabel).order_by(
            TaxonomyLabel.label, TaxonomyLabel.sublabel
        ))
        taxonomy = tax_result.scalars().all()

        tax_tree: dict[str, list] = {}
        tax_json: dict[str, dict] = {}
        target_user_sublabels: list[str] = []
        concept_sublabels: list[str] = []
        tax_categories: list[str] = []

        for t in taxonomy:
            if not t.sublabel:
                continue
            if t.label in MANDATORY_CATEGORIES:
                if t.label == "Target user in story":
                    target_user_sublabels.append(t.sublabel)
                else:
                    concept_sublabels.append(t.sublabel)
                continue
            tax_tree.setdefault(t.label, [])
            tax_tree[t.label].append(t)
            tax_json[str(t.id)] = {
                "label": t.label,
                "sublabel": t.sublabel or "",
                "description": t.description or "",
            }

        tax_categories = [cat for cat, subs in tax_tree.items() if subs]

        return templates.TemplateResponse(request, "review.html", {
            "user": user,
            "story": story,
            "pos": pos,
            "total": total,
            "session_id": rev_session.id,
            "my_added": my_added,
            "my_created": my_created,
            "teammate_labels": teammate_labels,
            "my_decisions": my_decisions,
            "mandatory": mandatory,
            "rejection": rejection,
            "relevance": relevance,
            "target_user_sublabels": target_user_sublabels,
            "concept_sublabels": concept_sublabels,
            "tax_json": json.dumps(tax_json),
            "tax_tree": tax_tree,
            "tax_categories": tax_categories,
        })


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
        rev_session = await get_active_session(db)
        if not rev_session:
            return RedirectResponse(url="/review", status_code=302)
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


@router.post("/review/decide-added-label")
async def decide_added_label(
    request: Request,
    added_label_id: int = Form(...),
    decision: str = Form(...),
    pos: int = Form(0),
):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if decision not in ("confirm", "reject"):
        return RedirectResponse(url=f"/review?pos={pos}", status_code=302)

    async with SessionLocal() as db:
        rev_session = await get_active_session(db)
        if not rev_session:
            return RedirectResponse(url="/review", status_code=302)

        existing = (await db.execute(
            select(AddedLabelDecision).where(
                AddedLabelDecision.session_id == rev_session.id,
                AddedLabelDecision.user_id == user.id,
                AddedLabelDecision.added_label_id == added_label_id,
            )
        )).scalar_one_or_none()

        if existing:
            existing.decision = decision
        else:
            db.add(AddedLabelDecision(
                session_id=rev_session.id,
                user_id=user.id,
                added_label_id=added_label_id,
                decision=decision,
            ))
        await db.commit()

    return RedirectResponse(url=f"/review?pos={pos}", status_code=302)


@router.post("/review/mandatory")
async def save_mandatory(
    request: Request,
    story_id: int = Form(...),
    target_user: str = Form(""),
    pos: int = Form(0),
):
    """Save target user selection. Concepts saved separately via /review/mandatory-concepts."""
    user = await get_current_user(request)
    if not user or not user.group_id:
        return RedirectResponse(url="/login", status_code=302)

    async with SessionLocal() as db:
        rev_session = await get_active_session(db)
        if not rev_session:
            return RedirectResponse(url="/review", status_code=302)

        mc = (await db.execute(
            select(MandatoryClassification).where(
                MandatoryClassification.session_id == rev_session.id,
                MandatoryClassification.group_id == user.group_id,
                MandatoryClassification.story_id == story_id,
            )
        )).scalar_one_or_none()

        if mc:
            mc.target_user = target_user.strip() or None
            mc.user_id = user.id
            mc.updated_at = __import__("datetime").datetime.utcnow()
        else:
            db.add(MandatoryClassification(
                session_id=rev_session.id,
                group_id=user.group_id,
                story_id=story_id,
                target_user=target_user.strip() or None,
                concepts_json=json.dumps([]),
                user_id=user.id,
            ))
        await db.commit()

    return RedirectResponse(url=f"/review?pos={pos}", status_code=302)


@router.post("/review/mandatory-concepts")
async def save_mandatory_concepts(request: Request, pos: int = Form(0)):
    """Save concept checkboxes (multi-value form field)."""
    user = await get_current_user(request)
    if not user or not user.group_id:
        return RedirectResponse(url="/login", status_code=302)

    form = await request.form()
    story_id = int(form.get("story_id", 0))
    concepts = form.getlist("concepts")

    async with SessionLocal() as db:
        rev_session = await get_active_session(db)
        if not rev_session:
            return RedirectResponse(url="/review", status_code=302)

        mc = (await db.execute(
            select(MandatoryClassification).where(
                MandatoryClassification.session_id == rev_session.id,
                MandatoryClassification.group_id == user.group_id,
                MandatoryClassification.story_id == story_id,
            )
        )).scalar_one_or_none()

        import datetime as dt
        if mc:
            mc.concepts = concepts
            mc.user_id = user.id
            mc.updated_at = dt.datetime.utcnow()
        else:
            new_mc = MandatoryClassification(
                session_id=rev_session.id,
                group_id=user.group_id,
                story_id=story_id,
                concepts_json=json.dumps(concepts),
                user_id=user.id,
            )
            db.add(new_mc)
        await db.commit()

    return RedirectResponse(url=f"/review?pos={pos}", status_code=302)


@router.post("/review/rejection")
async def save_rejection(
    request: Request,
    story_id: int = Form(...),
    rejected: str = Form("off"),
    reason: str = Form(""),
    pos: int = Form(0),
):
    user = await get_current_user(request)
    if not user or not user.group_id:
        return RedirectResponse(url="/login", status_code=302)

    is_rejected = rejected in ("on", "true", "1", "yes")

    async with SessionLocal() as db:
        rev_session = await get_active_session(db)
        if not rev_session:
            return RedirectResponse(url="/review", status_code=302)

        import datetime as dt
        rej = (await db.execute(
            select(StoryRejection).where(
                StoryRejection.session_id == rev_session.id,
                StoryRejection.group_id == user.group_id,
                StoryRejection.story_id == story_id,
            )
        )).scalar_one_or_none()

        if rej:
            rej.rejected = is_rejected
            rej.reason = reason.strip() or None
            rej.user_id = user.id
            rej.updated_at = dt.datetime.utcnow()
        else:
            db.add(StoryRejection(
                session_id=rev_session.id,
                group_id=user.group_id,
                story_id=story_id,
                rejected=is_rejected,
                reason=reason.strip() or None,
                user_id=user.id,
            ))
        await db.commit()

    return RedirectResponse(url=f"/review?pos={pos}", status_code=302)


@router.post("/review/relevance")
async def save_relevance(
    request: Request,
    story_id: int = Form(...),
    score: str = Form("Normal"),
    reason: str = Form(""),
    pos: int = Form(0),
):
    user = await get_current_user(request)
    if not user or not user.group_id:
        return RedirectResponse(url="/login", status_code=302)

    if score not in ("Normal", "High", "VeryHigh"):
        score = "Normal"

    async with SessionLocal() as db:
        rev_session = await get_active_session(db)
        if not rev_session:
            return RedirectResponse(url="/review", status_code=302)

        import datetime as dt
        rel = (await db.execute(
            select(StoryRelevance).where(
                StoryRelevance.session_id == rev_session.id,
                StoryRelevance.group_id == user.group_id,
                StoryRelevance.story_id == story_id,
            )
        )).scalar_one_or_none()

        if rel:
            rel.score = score
            rel.reason = reason.strip() or None
            rel.user_id = user.id
            rel.updated_at = dt.datetime.utcnow()
        else:
            db.add(StoryRelevance(
                session_id=rev_session.id,
                group_id=user.group_id,
                story_id=story_id,
                score=score,
                reason=reason.strip() or None,
                user_id=user.id,
            ))
        await db.commit()

    return RedirectResponse(url=f"/review?pos={pos}", status_code=302)


@router.post("/review/create-taxonomy-label")
async def create_taxonomy_label(
    request: Request,
    story_id: int = Form(...),
    top_label: str = Form(...),
    sublabel: str = Form(...),
    description: str = Form(""),
    note: str = Form(""),
    pos: int = Form(0),
):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    top_label = top_label.strip()
    sublabel = sublabel.strip()
    if not top_label or not sublabel:
        return RedirectResponse(url=f"/review?pos={pos}", status_code=302)

    async with SessionLocal() as db:
        rev_session = await get_active_session(db)
        if not rev_session:
            return RedirectResponse(url="/review", status_code=302)

        new_tax = TaxonomyLabel(
            label=top_label,
            sublabel=sublabel,
            description=description.strip() or None,
            is_user_created=True,
        )
        db.add(new_tax)
        await db.flush()

        db.add(AddedLabel(
            session_id=rev_session.id,
            user_id=user.id,
            story_id=story_id,
            taxonomy_label_id=new_tax.id,
            note=note.strip() or None,
        ))
        await db.commit()

    return RedirectResponse(url=f"/review?pos={pos}", status_code=302)


@router.get("/api/taxonomy")
async def taxonomy_list(request: Request):
    user = await get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    async with SessionLocal() as db:
        all_tax = (await db.execute(
            select(TaxonomyLabel).order_by(TaxonomyLabel.label, TaxonomyLabel.sublabel)
        )).scalars().all()

    tree: dict[str, list] = {}
    flat: dict[str, dict] = {}
    for t in all_tax:
        if not t.sublabel:
            continue
        if t.label in MANDATORY_CATEGORIES:
            continue
        tree.setdefault(t.label, [])
        tree[t.label].append({
            "id": t.id,
            "sublabel": t.sublabel,
            "description": t.description or "",
        })
        flat[str(t.id)] = {
            "label": t.label,
            "sublabel": t.sublabel,
            "description": t.description or "",
        }

    categories = [cat for cat, subs in tree.items() if subs]
    return JSONResponse({"flat": flat, "tree": tree, "categories": categories})


@router.get("/api/eurovoc")
async def eurovoc_lookup(request: Request, term: str = ""):
    user = await get_current_user(request)
    if not user or not term.strip():
        return JSONResponse({"matches": [], "duplicates": []})

    term = term.strip()

    sparql = f"""
    PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
    SELECT DISTINCT ?prefLabel WHERE {{
      ?concept a skos:Concept ;
               skos:prefLabel ?prefLabel .
      FILTER(lang(?prefLabel) = "en")
      FILTER(strstarts(str(?concept), "http://eurovoc.europa.eu/"))
      FILTER(contains(lcase(str(?prefLabel)), lcase("{term}")))
    }}
    LIMIT 6
    """
    eurovoc_matches = []
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(SPARQL_ENDPOINT, params={
                "query": sparql,
                "format": "application/sparql-results+json",
            }, headers={"Accept": "application/sparql-results+json"})
            if resp.status_code == 200:
                data = resp.json()
                eurovoc_matches = [
                    b["prefLabel"]["value"]
                    for b in data["results"]["bindings"]
                ]
    except Exception:
        pass

    async with SessionLocal() as db:
        all_tax = (await db.execute(select(TaxonomyLabel))).scalars().all()

    term_lower = term.lower()
    duplicates = []
    for t in all_tax:
        candidates = [t.sublabel or "", t.label or ""]
        for c in candidates:
            ratio = SequenceMatcher(None, term_lower, c.lower()).ratio()
            if ratio >= 0.75 and c:
                duplicates.append({
                    "label": t.label,
                    "sublabel": t.sublabel or "",
                    "similarity": round(ratio * 100),
                })
                break
    duplicates.sort(key=lambda x: -x["similarity"])

    return JSONResponse({"matches": eurovoc_matches, "duplicates": duplicates[:5]})
