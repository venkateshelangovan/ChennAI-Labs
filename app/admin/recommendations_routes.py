"""
Stage 14: the "behavior & recommendations" view Journey 3 (Stage 0,
Section 2) describes — "admin can also open a 'behavior & recommendations'
view for a given user ... for support/debugging purposes." Read-only,
same as app/admin/events_routes.py, and for the same reason kept as its
own router rather than folded into app/admin/routes.py: this is
observability, not catalog management, even though both sit under
`/admin` and share `require_admin`.

Everything rendered here comes directly off a `RecommendationSnapshot`
row (app/db/models/recommendation_snapshot.py) — no recomputation, no
calling back into app/recommendations/cache.py. That's deliberate:
Section 17 asks for the actual trace of what happened, not a fresh
answer to "what would happen now" that could differ from what the user
actually saw.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth.dependencies import require_admin
from app.db.models.product import Product
from app.db.models.recommendation_snapshot import RecommendationSnapshot
from app.db.models.user import User
from app.db.session import get_db
from app.events import service as events_service
from app.templating import templates

router = APIRouter(prefix="/admin/recommendations")

RECENT_EVENTS_LIMIT = 30


@router.get("")
async def list_recommendations(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    rows = (
        db.query(RecommendationSnapshot, User)
        .join(User, User.id == RecommendationSnapshot.user_id)
        .order_by(RecommendationSnapshot.generated_at.desc())
        .all()
    )
    return templates.TemplateResponse(
        request,
        "admin/recommendations/index.html",
        {"rows": rows, "current_user": admin},
    )


@router.get("/{user_id}")
async def recommendation_detail(
    request: Request, user_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)
):
    user = db.query(User).filter(User.id == user_id).one_or_none()
    if user is None:
        return RedirectResponse(url="/admin/recommendations", status_code=303)

    snapshot = db.query(RecommendationSnapshot).filter(RecommendationSnapshot.user_id == user_id).one_or_none()

    # The snapshot only stores product IDs + the ranking decision (Stage
    # 12) — resolve real Product rows here purely for display, same
    # drift-safety pattern as app/recommendations/cache.py: a product
    # referenced in an old trace may since have been archived or
    # deleted, and that's worth SHOWING an admin (it explains why a
    # recommendation might look stale), not hiding.
    recommendations = []
    if snapshot:
        product_ids = [row["product_id"] for row in snapshot.recommendations]
        products = {p.id: p for p in db.query(Product).filter(Product.id.in_(product_ids)).all()}
        for row in snapshot.recommendations:
            product = products.get(row["product_id"])
            recommendations.append(
                {
                    "product_id": row["product_id"],
                    "reason": row["reason"],
                    "similarity": row["similarity"],
                    "product": product,  # None if archived/deleted since the snapshot was taken
                }
            )

    recent_events = events_service.list_recent_events(db, user_id=user_id, limit=RECENT_EVENTS_LIMIT)

    return templates.TemplateResponse(
        request,
        "admin/recommendations/detail.html",
        {
            "target_user": user,
            "snapshot": snapshot,
            "recommendations": recommendations,
            "recent_events": recent_events,
            "current_user": admin,
        },
    )
