"""
Public catalog HTTP layer: browse, filter, search, and view a single
course. No auth required — the catalog is public; personalization
(Stage 6+) layers on top of this once a user is logged in, it doesn't
gate access to it.
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.db.session import get_db
from app.products import service
from app.templating import templates

router = APIRouter()


@router.get("/courses")
async def browse_courses(
    request: Request,
    q: str | None = None,
    category: str | None = None,
    level: str | None = None,
    db: Session = Depends(get_db),
):
    products = service.list_products(db, category=category, level=level, q=q)
    categories = service.list_categories(db)
    current_user = get_current_user(request, db)
    return templates.TemplateResponse(
        request,
        "courses/index.html",
        {
            "products": products,
            "categories": categories,
            "filters": {"q": q or "", "category": category or "", "level": level or ""},
            "current_user": current_user,
        },
    )


@router.get("/courses/{slug}")
async def course_detail(request: Request, slug: str, db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    try:
        product = service.get_product_by_slug(db, slug)
    except service.ProductNotFound:
        return templates.TemplateResponse(
            request,
            "courses/not_found.html",
            {"slug": slug, "current_user": current_user},
            status_code=404,
        )
    return templates.TemplateResponse(
        request, "courses/detail.html", {"product": product, "current_user": current_user}
    )
