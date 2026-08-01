"""
Admin catalog management. Every route here depends on `require_admin`
(app/auth/dependencies.py) — that single dependency is the entire
authorization boundary; nothing in the route bodies below re-checks
role. A non-admin hitting any of these gets a 403 before the route body
ever runs (Stage 2's NotAuthorized handler in main.py).

Create/update do manual field-by-field validation and re-render the
form with errors rather than relying on Pydantic request models — this
matches the pattern already established in app/auth/routes.py and keeps
error messages tied to the specific business rule (e.g. "duration must
be positive") rather than a generic schema-validation message.
"""

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth.dependencies import require_admin
from app.core.csrf import issue_csrf_token, set_csrf_cookie, verify_csrf
from app.db.models.user import User
from app.db.session import get_db
from app.products import service
from app.retrieval import sync as vector_sync
from app.templating import templates

router = APIRouter(prefix="/admin/products")


def _parse_and_validate(
    *, title: str, price_raw: str, level: str, duration_raw: str
) -> tuple[Decimal | None, int | None, list[str]]:
    errors: list[str] = []

    if not title.strip():
        errors.append("Title is required.")

    price = None
    try:
        price = Decimal(price_raw)
        if price < 0:
            errors.append("Price cannot be negative.")
    except InvalidOperation:
        errors.append("Price must be a valid number.")

    if level not in service.VALID_LEVELS:
        errors.append("Level must be beginner, intermediate, or advanced.")

    duration_minutes = None
    try:
        duration_minutes = int(duration_raw)
        if duration_minutes <= 0:
            errors.append("Duration must be a positive number of minutes.")
    except ValueError:
        errors.append("Duration must be a whole number of minutes.")

    return price, duration_minutes, errors


@router.get("")
async def list_products(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    products = service.list_products(db, include_archived=True)
    needs_sync = sum(1 for p in products if p.status == "active" and p.vector_sync_status != "synced")
    token = issue_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "admin/products/index.html",
        {"products": products, "current_user": admin, "csrf_token": token, "needs_sync": needs_sync},
    )
    set_csrf_cookie(response, token)
    return response


@router.post("/sync")
async def sync_now(
    request: Request,
    csrf_token: str = Form(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Manual trigger for app/retrieval/sync.py's reconciliation sweep —
    the repair mechanism from Stage 0 Section 9. Retries every active
    product not currently marked 'synced' and removes any orphaned
    vector-store entries. Not run on a schedule (see Stage 15).
    """
    if verify_csrf(request, csrf_token):
        vector_sync.reconcile(db)
    return RedirectResponse(url="/admin/products", status_code=303)


@router.get("/new")
async def new_product_form(
    request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)
):
    token = issue_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "admin/products/form.html",
        {
            "mode": "create",
            "product": None,
            "errors": [],
            "form": {},
            "csrf_token": token,
            "current_user": admin,
            "levels": service.VALID_LEVELS,
        },
    )
    set_csrf_cookie(response, token)
    return response


@router.post("/new")
async def create_product_submit(
    request: Request,
    title: str = Form(...),
    description: str = Form(...),
    category: str = Form(...),
    subcategory: str = Form(""),
    price: str = Form(...),
    level: str = Form(...),
    tags: str = Form(""),
    instructor: str = Form(""),
    duration_minutes: str = Form(...),
    image_url: str = Form(""),
    csrf_token: str = Form(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    errors: list[str] = []
    if not verify_csrf(request, csrf_token):
        errors.append("Your form session expired. Please try again.")
    if not description.strip():
        errors.append("Description is required.")
    if not category.strip():
        errors.append("Category is required.")

    parsed_price, parsed_duration, validation_errors = _parse_and_validate(
        title=title, price_raw=price, level=level, duration_raw=duration_minutes
    )
    errors.extend(validation_errors)

    if not errors:
        service.create_product(
            db,
            title=title,
            description=description,
            category=category,
            subcategory=subcategory,
            price=parsed_price,
            level=level,
            tags=service.parse_tags(tags),
            instructor=instructor,
            duration_minutes=parsed_duration,
            image_url=image_url,
        )
        return RedirectResponse(url="/admin/products", status_code=303)

    token = issue_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "admin/products/form.html",
        {
            "mode": "create",
            "product": None,
            "errors": errors,
            "form": {
                "title": title,
                "description": description,
                "category": category,
                "subcategory": subcategory,
                "price": price,
                "level": level,
                "tags": tags,
                "instructor": instructor,
                "duration_minutes": duration_minutes,
                "image_url": image_url,
            },
            "csrf_token": token,
            "current_user": admin,
            "levels": service.VALID_LEVELS,
        },
        status_code=422,
    )
    set_csrf_cookie(response, token)
    return response


@router.get("/{product_id}/edit")
async def edit_product_form(
    request: Request, product_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)
):
    product = service.get_product_by_id(db, product_id)
    token = issue_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "admin/products/form.html",
        {
            "mode": "edit",
            "product": product,
            "errors": [],
            "form": {
                "title": product.title,
                "description": product.description,
                "category": product.category,
                "subcategory": product.subcategory or "",
                "price": str(product.price),
                "level": product.level,
                "tags": ", ".join(product.tags or []),
                "instructor": product.instructor or "",
                "duration_minutes": str(product.duration_minutes),
                "image_url": product.image_url or "",
            },
            "csrf_token": token,
            "current_user": admin,
            "levels": service.VALID_LEVELS,
        },
    )
    set_csrf_cookie(response, token)
    return response


@router.post("/{product_id}/edit")
async def update_product_submit(
    request: Request,
    product_id: int,
    title: str = Form(...),
    description: str = Form(...),
    category: str = Form(...),
    subcategory: str = Form(""),
    price: str = Form(...),
    level: str = Form(...),
    tags: str = Form(""),
    instructor: str = Form(""),
    duration_minutes: str = Form(...),
    image_url: str = Form(""),
    csrf_token: str = Form(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    product = service.get_product_by_id(db, product_id)

    errors: list[str] = []
    if not verify_csrf(request, csrf_token):
        errors.append("Your form session expired. Please try again.")
    if not description.strip():
        errors.append("Description is required.")
    if not category.strip():
        errors.append("Category is required.")

    parsed_price, parsed_duration, validation_errors = _parse_and_validate(
        title=title, price_raw=price, level=level, duration_raw=duration_minutes
    )
    errors.extend(validation_errors)

    if not errors:
        service.update_product(
            db,
            product,
            title=title,
            description=description,
            category=category,
            subcategory=subcategory,
            price=parsed_price,
            level=level,
            tags=service.parse_tags(tags),
            instructor=instructor,
            duration_minutes=parsed_duration,
            image_url=image_url,
        )
        return RedirectResponse(url="/admin/products", status_code=303)

    token = issue_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "admin/products/form.html",
        {
            "mode": "edit",
            "product": product,
            "errors": errors,
            "form": {
                "title": title,
                "description": description,
                "category": category,
                "subcategory": subcategory,
                "price": price,
                "level": level,
                "tags": tags,
                "instructor": instructor,
                "duration_minutes": duration_minutes,
                "image_url": image_url,
            },
            "csrf_token": token,
            "current_user": admin,
            "levels": service.VALID_LEVELS,
        },
        status_code=422,
    )
    set_csrf_cookie(response, token)
    return response


@router.post("/{product_id}/archive")
async def archive_product(
    request: Request,
    product_id: int,
    csrf_token: str = Form(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if verify_csrf(request, csrf_token):
        product = service.get_product_by_id(db, product_id)
        service.archive_product(db, product)
    return RedirectResponse(url="/admin/products", status_code=303)


@router.post("/{product_id}/restore")
async def restore_product(
    request: Request,
    product_id: int,
    csrf_token: str = Form(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if verify_csrf(request, csrf_token):
        product = service.get_product_by_id(db, product_id)
        service.restore_product(db, product)
    return RedirectResponse(url="/admin/products", status_code=303)
