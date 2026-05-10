from app.templates import templates
from collections import defaultdict
from itertools import combinations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.auth import get_current_user
from app.database import SessionLocal
from app.models import AddedLabel, TaxonomyLabel, User

router = APIRouter()

_PALETTE = [
    "#376782", "#85ab86", "#648a9e", "#a07060",
    "#6a8a6a", "#8a6a9e", "#9a8060", "#5a7090",
    "#6a9a8a", "#2c324c",
]


@router.get("/infograph", response_class=HTMLResponse)
async def infograph(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(request, "infograph.html", {"user": user})


@router.get("/infograph/data")
async def infograph_data(request: Request, filter: str = "all", session_id: int | None = None):
    user = await get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    async with SessionLocal() as db:
        # Taxonomy: build sublabel → (category, color) lookup
        taxonomy = (await db.execute(select(TaxonomyLabel))).scalars().all()

        cat_color: dict[str, str] = {}
        sublabel_to_cat: dict[str, str] = {}
        categories = []
        for t in taxonomy:
            if t.label not in cat_color:
                cat_color[t.label] = _PALETTE[len(cat_color) % len(_PALETTE)]
                categories.append({"name": t.label, "color": cat_color[t.label]})
            if t.sublabel:
                sublabel_to_cat[t.sublabel.strip().lower()] = t.label

        # Collect all added labels with taxonomy info
        added_filter = []
        if session_id:
            added_filter.append(AddedLabel.session_id == session_id)

        added_rows = (await db.execute(
            select(AddedLabel)
            .where(*added_filter)
            .options(selectinload(AddedLabel.taxonomy_label))
        )).scalars().all()

        # Build story → label text mapping
        story_labels: dict[int, set[str]] = defaultdict(set)
        for al in added_rows:
            if al.taxonomy_label and al.taxonomy_label.sublabel:
                text = al.taxonomy_label.sublabel.strip()
            elif al.taxonomy_label and al.taxonomy_label.label:
                text = al.taxonomy_label.label.strip()
            elif al.free_text:
                text = al.free_text.strip()
            else:
                continue
            story_labels[al.story_id].add(text)

        # Frequency and co-occurrence
        freq: dict[str, int] = defaultdict(int)
        cooc: dict[tuple[str, str], int] = defaultdict(int)

        for label_set in story_labels.values():
            lst = sorted(label_set)
            for lbl in lst:
                freq[lbl] += 1
            for a, b in combinations(lst, 2):
                cooc[(min(a, b), max(a, b))] += 1

        nodes = [
            {
                "id": lbl,
                "label": lbl,
                "frequency": count,
                "category": sublabel_to_cat.get(lbl.lower(), ""),
                "color": cat_color.get(sublabel_to_cat.get(lbl.lower(), ""), "#648a9e"),
            }
            for lbl, count in sorted(freq.items(), key=lambda x: -x[1])
        ]

        links = [
            {"source": a, "target": b, "weight": w}
            for (a, b), w in sorted(cooc.items(), key=lambda x: -x[1])
        ]

    return JSONResponse({"nodes": nodes, "links": links, "categories": categories})
