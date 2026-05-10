from app.templates import templates
from collections import defaultdict
from itertools import combinations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.auth import get_current_user
from app.database import SessionLocal
from app.models import (
    AddedLabel, AddedLabelDecision, Group, MandatoryClassification,
    Session as ReviewSession, TaxonomyLabel, User, UserStory,
)

router = APIRouter()

MANDATORY_CATS = {"Target user in story", "User Story Concept"}

CAT_COLOURS = {
    "Architecture & Modularity":            "#4472C4",
    "Scalability & Performance":            "#ED7D31",
    "Data and Software Standards":          "#A9D18E",
    "Infrastructure and connectivity":      "#FFC000",
    "Hardware and sensor technology":       "#5B9BD5",
    "Software and application development": "#70AD47",
    "Data quality and management":          "#264478",
    "Human capacity and training":          "#9E480E",
    "Workflows and user experience":        "#636363",
    "To Be Classified":                     "#997300",
    "Governance":                           "#255E91",
    "Data governance":                      "#43682B",
    "Inclusivity and participation":        "#BE4B48",
    "Scientific principles and practices":  "#698ED0",
    "Domain focus":                         "#F1975A",
    "Ethics":                               "#B7DE8F",
    "User Story Concept":                   "#C00000",
    "Target user in story":                 "#7030A0",
    "AI and advanced analytics":            "#00B0F0",
}
_FALLBACK = ["#376782","#85ab86","#648a9e","#a07060","#6a8a6a","#8a6a9e","#9a8060","#5a7090"]


def _col(cat: str) -> str:
    return CAT_COLOURS.get(cat, _FALLBACK[abs(hash(cat)) % len(_FALLBACK)])


@router.get("/infograph", response_class=HTMLResponse)
async def infograph(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    async with SessionLocal() as db:
        res = await db.execute(
            select(ReviewSession).order_by(ReviewSession.started_at.desc())
        )
        session = res.scalars().first()

    refresh_secs = (session.chart_refresh_secs or 300) if session else 300

    return templates.TemplateResponse(
        request, "infograph.html",
        {"user": user, "chart_refresh_ms": refresh_secs * 1000},
    )


@router.get("/infograph/data")
async def infograph_data(request: Request, session_id: int | None = None):
    user = await get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    async with SessionLocal() as db:
        taxonomy = (await db.execute(select(TaxonomyLabel))).scalars().all()
        sub_to_main: dict[str, str] = {
            t.sublabel.strip(): t.label for t in taxonomy if t.sublabel
        }

        if session_id:
            session = await db.get(ReviewSession, session_id)
        else:
            res = await db.execute(
                select(ReviewSession).order_by(ReviewSession.started_at.desc())
            )
            session = res.scalars().first()

        af = [AddedLabel.session_id == session.id] if session else []

        rows = (await db.execute(
            select(AddedLabel)
            .where(*af)
            .options(
                selectinload(AddedLabel.taxonomy_label),
                selectinload(AddedLabel.user),
            )
        )).scalars().all()

        groups = (await db.execute(select(Group))).scalars().all()
        gid_name = {g.id: g.name for g in groups}

        mc_f = [MandatoryClassification.session_id == session.id] if session else []
        mc_rows = (await db.execute(select(MandatoryClassification).where(*mc_f))).scalars().all()

        dec_f = [AddedLabelDecision.session_id == session.id] if session else []
        dec_rows = (await db.execute(select(AddedLabelDecision).where(*dec_f))).scalars().all()

        stories = (await db.execute(select(UserStory))).scalars().all()
        sid_utype = {s.id: (s.user_type or "Unknown").strip() for s in stories}

    # ── Aggregation ────────────────────────────────────────────────────────
    freq:      dict[str, int] = defaultdict(int)
    tech_freq: dict[str, int] = defaultdict(int)
    s_all:     dict[int, set] = defaultdict(set)
    s_tech:    dict[int, set] = defaultdict(set)
    partner_main: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    ut_tech:      dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for al in rows:
        if not al.taxonomy_label or not al.taxonomy_label.sublabel:
            continue
        sub  = al.taxonomy_label.sublabel.strip()
        main = sub_to_main.get(sub, al.taxonomy_label.label or "")
        is_tech = main not in MANDATORY_CATS

        freq[sub] += 1
        s_all[al.story_id].add(sub)
        if is_tech:
            tech_freq[sub] += 1
            s_tech[al.story_id].add(sub)
            if al.user and al.user.group_id:
                partner_main[gid_name.get(al.user.group_id, "?")][main] += 1
            ut = sid_utype.get(al.story_id, "Unknown")
            ut_tech[ut][sub] += 1

    # Co-occurrence
    cooc: dict = defaultdict(int)
    t_cooc: dict = defaultdict(int)
    for lbls in s_all.values():
        for a, b in combinations(sorted(lbls), 2):
            cooc[(a, b)] += 1
    for lbls in s_tech.values():
        for a, b in combinations(sorted(lbls), 2):
            t_cooc[(a, b)] += 1

    # Mandatory classifications
    tu_freq:  dict[str, int] = defaultdict(int)
    con_freq: dict[str, int] = defaultdict(int)
    sid_cons: dict[int, list] = {}
    for mc in mc_rows:
        if mc.target_user:
            tu_freq[mc.target_user.strip()] += 1
        for c in mc.concepts:
            con_freq[c.strip()] += 1
        sid_cons[mc.story_id] = mc.concepts

    # Concept → tech label mapping
    con_tech: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for al in rows:
        if not al.taxonomy_label or not al.taxonomy_label.sublabel:
            continue
        sub  = al.taxonomy_label.sublabel.strip()
        main = sub_to_main.get(sub, "")
        if main in MANDATORY_CATS:
            continue
        for c in sid_cons.get(al.story_id, []):
            con_tech[c.strip()][sub] += 1

    # ── Serialise ──────────────────────────────────────────────────────────
    def items(d, n=100):
        return [
            {"sublabel": s, "main_category": sub_to_main.get(s, ""),
             "color": _col(sub_to_main.get(s, "")), "count": c}
            for s, c in sorted(d.items(), key=lambda x: -x[1])[:n]
        ]

    def links(d, top_n, min_c=2):
        top_set = set(top_n[:40])
        return [
            {"source": a, "target": b, "count": c}
            for (a, b), c in sorted(d.items(), key=lambda x: -x[1])
            if a in top_set and b in top_set and c >= min_c
        ][:300]

    top50      = [e["sublabel"] for e in items(freq, 50)]
    top50_tech = [e["sublabel"] for e in items(tech_freq, 50)]

    pm_serial = {}
    for p, mf in partner_main.items():
        tot = sum(mf.values()) or 1
        pm_serial[p] = [
            {"main_category": m, "count": c, "pct": round(c/tot*100,1), "color": _col(m)}
            for m, c in sorted(mf.items(), key=lambda x: -x[1])
        ]

    ut_serial = {
        ut: [{"sublabel": s, "count": c, "main_category": sub_to_main.get(s,""), "color": _col(sub_to_main.get(s,""))}
             for s, c in sorted(ld.items(), key=lambda x: -x[1])[:10]]
        for ut, ld in sorted(ut_tech.items(), key=lambda x: -sum(x[1].values()))[:10]
    }

    con_serial = {
        co: [{"sublabel": s, "count": c, "main_category": sub_to_main.get(s,""), "color": _col(sub_to_main.get(s,""))}
             for s, c in sorted(ld.items(), key=lambda x: -x[1])[:10]]
        for co, ld in sorted(con_tech.items(), key=lambda x: -sum(x[1].values()))[:10]
    }

    dec_c = sum(1 for d in dec_rows if d.decision == "confirm")
    dec_r = sum(1 for d in dec_rows if d.decision == "reject")

    return JSONResponse({
        "label_freq":         items(freq, 100),
        "tech_label_freq":    items(tech_freq, 100),
        "cooccurrence":       links(cooc, top50),
        "tech_cooccurrence":  links(t_cooc, top50_tech),
        "target_user_freq":   [{"sublabel": k, "count": v} for k, v in sorted(tu_freq.items(), key=lambda x: -x[1])],
        "concept_freq":       [{"concept": k, "count": v} for k, v in sorted(con_freq.items(), key=lambda x: -x[1])],
        "partner_main_freq":  pm_serial,
        "user_type_tech_freq": ut_serial,
        "concept_tech_freq":  con_serial,
        "peer_review":        {"confirm": dec_c, "reject": dec_r},
        "categories":         [{"name": k, "color": v} for k, v in CAT_COLOURS.items()],
        "total_stories":      len(s_all),
        "total_labels":       sum(freq.values()),
    })
