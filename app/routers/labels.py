from app.templates import templates
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select, case

from app.auth import get_current_user
from app.database import SessionLocal
from app.models import TaxonomyLabel

router = APIRouter()


@router.get("/labels", response_class=HTMLResponse)
async def labels_page(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    async with SessionLocal() as db:
        result = await db.execute(
            select(TaxonomyLabel)
            .where(TaxonomyLabel.sublabel.isnot(None))
            .order_by(
                case((TaxonomyLabel.is_user_created == True, 0), else_=1),
                TaxonomyLabel.label,
                TaxonomyLabel.sublabel,
            )
        )
        all_labels = result.scalars().all()

        grouped: dict[str, list] = {}
        for lbl in all_labels:
            grouped.setdefault(lbl.label, []).append({
                "sublabel": lbl.sublabel,
                "description": lbl.description or "",
                "is_user_created": lbl.is_user_created,
            })

    return templates.TemplateResponse(request, "labels.html", {
        "user": user,
        "grouped": grouped,
    })
