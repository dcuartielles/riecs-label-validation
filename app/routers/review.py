import json
from difflib import SequenceMatcher
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, and_
from sqlalchemy.orm import selectinload
import httpx

from app.auth import get_current_user
from app.database import SessionLocal
from app.models import (
    GroupAssignment, LabelDecision, AddedLabel,
    Session as ReviewSession, StoryLabel, TaxonomyLabel, UserStory
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

SPARQL_ENDPOINT = "https://publications.europa.eu/webapi/rdf/sparql"


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
        label_result = await db.execute(
            select(StoryLabel).where(StoryLabel.story_id == story.id)
        )
        labels = label_result.scalars().all()

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

        tax_result = await db.execute(select(TaxonomyLabel).order_by(
            TaxonomyLabel.label, TaxonomyLabel.sublabel
        ))
        taxonomy = tax_result.scalars().all()

        tax_tree: dict[str, list] = {}
        tax_json: dict[str, dict] = {}
        tax_desc: dict[str, str] = {}
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
        rev_session = await get_active_session(db)
        if not rev_session:
            return RedirectResponse(url="/review", status_code=302)

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


@router.get("/api/eurovoc")
async def eurovoc_lookup(request: Request, term: str = ""):
    """Query EuroVoc via EU Publications SPARQL endpoint."""
    user = await get_current_user(request)
    if not user or not term.strip():
        return JSONResponse({"matches": [], "duplicates": []})

    term = term.strip()

    # EuroVoc SPARQL lookup
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

    # Fuzzy duplicate detection against existing taxonomy
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
