"""
Stage 3's critical path: catalog CRUD, slug generation/uniqueness,
search/filtering, soft-delete (archive) behavior, and — importantly —
that admin routes are actually gated by role, not just "logged in."
"""

from decimal import Decimal

import pytest

from app.auth import service as auth_service
from app.products import service as product_service


def _make_product(db, **overrides):
    defaults = dict(
        title="Retrieval-Augmented Generation (RAG) in Production",
        description="Chunking, embeddings, vector search, and grounding.",
        category="Generative AI",
        subcategory="RAG",
        price=Decimal("6999"),
        level="advanced",
        tags=["rag", "embeddings"],
        instructor="Rahul Chatterjee",
        duration_minutes=1800,
        image_url=None,
    )
    defaults.update(overrides)
    return product_service.create_product(db, **defaults)


# ---------------------------------------------------------------------------
# app.products.service — unit level
# ---------------------------------------------------------------------------

def test_slugify_basic():
    assert product_service.slugify("Deep Learning End-to-End") == "deep-learning-end-to-end"


def test_create_product_generates_unique_slug(db_session):
    p1 = _make_product(db_session, title="RAG in Production")
    p2 = _make_product(db_session, title="RAG in Production")  # same title again
    assert p1.slug == "rag-in-production"
    assert p2.slug == "rag-in-production-2"


def test_update_product_keeps_slug_if_title_unchanged(db_session):
    product = _make_product(db_session)
    original_slug = product.slug
    product_service.update_product(
        db_session,
        product,
        title=product.title,
        description="Updated description.",
        category=product.category,
        subcategory=product.subcategory,
        price=Decimal("7499"),
        level=product.level,
        tags=product.tags,
        instructor=product.instructor,
        duration_minutes=product.duration_minutes,
        image_url=product.image_url,
    )
    assert product.slug == original_slug
    assert product.description == "Updated description."
    assert product.price == Decimal("7499")


def test_update_product_regenerates_slug_if_title_changed(db_session):
    product = _make_product(db_session)
    product_service.update_product(
        db_session,
        product,
        title="RAG in Production (Advanced)",
        description=product.description,
        category=product.category,
        subcategory=product.subcategory,
        price=product.price,
        level=product.level,
        tags=product.tags,
        instructor=product.instructor,
        duration_minutes=product.duration_minutes,
        image_url=product.image_url,
    )
    assert product.slug == "rag-in-production-advanced"


def test_archive_and_restore(db_session):
    product = _make_product(db_session)
    product_service.archive_product(db_session, product)
    assert product.status == "archived"

    with pytest.raises(product_service.ProductNotFound):
        product_service.get_product_by_slug(db_session, product.slug)  # excluded by default

    found = product_service.get_product_by_slug(db_session, product.slug, include_archived=True)
    assert found.id == product.id

    product_service.restore_product(db_session, product)
    assert product.status == "active"
    assert product_service.get_product_by_slug(db_session, product.slug).id == product.id


def test_list_products_excludes_archived_by_default(db_session):
    active = _make_product(db_session, title="Active Course")
    archived = _make_product(db_session, title="Archived Course")
    product_service.archive_product(db_session, archived)

    results = product_service.list_products(db_session)
    ids = [p.id for p in results]
    assert active.id in ids
    assert archived.id not in ids

    all_results = product_service.list_products(db_session, include_archived=True)
    assert archived.id in [p.id for p in all_results]


def test_list_products_filters_by_category_and_level(db_session):
    _make_product(db_session, title="RAG Course", category="Generative AI", level="advanced")
    _make_product(db_session, title="Python Basics", category="Foundations", level="beginner")

    results = product_service.list_products(db_session, category="Foundations")
    assert len(results) == 1
    assert results[0].title == "Python Basics"

    results = product_service.list_products(db_session, level="advanced")
    assert len(results) == 1
    assert results[0].title == "RAG Course"


def test_list_products_search_matches_title_description_and_tags(db_session):
    _make_product(
        db_session,
        title="Agentic AI Systems",
        description="Design LLM agents with tool use and planning.",
        tags=["agentic-ai", "agents", "planning"],
    )
    _make_product(db_session, title="Data Analyst Career Track", description="SQL and dashboards.", tags=["sql"])

    assert len(product_service.list_products(db_session, q="agentic")) == 1
    assert len(product_service.list_products(db_session, q="planning")) == 1  # matches description
    assert len(product_service.list_products(db_session, q="dashboards")) == 1
    assert len(product_service.list_products(db_session, q="nonexistent-topic")) == 0


def test_parse_tags():
    assert product_service.parse_tags("RAG, Embeddings,  vector-search ,") == ["rag", "embeddings", "vector-search"]


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

def test_browse_courses_returns_active_products(client, db_session):
    _make_product(db_session, title="Visible Course")
    response = client.get("/courses")
    assert response.status_code == 200
    assert "Visible Course" in response.text


def test_course_detail_returns_404_for_unknown_slug(client):
    response = client.get("/courses/does-not-exist")
    assert response.status_code == 404


def test_course_detail_renders_for_active_product(client, db_session):
    product = _make_product(db_session, title="Findable Course")
    response = client.get(f"/courses/{product.slug}")
    assert response.status_code == 200
    assert "Findable Course" in response.text


def test_admin_products_redirects_anonymous_to_login(client):
    response = client.get("/admin/products", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_admin_products_forbidden_for_regular_user(client, db_session):
    csrf = client.get("/register").cookies.get("csrf_token")
    client.post(
        "/register",
        data={
            "email": "learner@example.com",
            "password": "password123",
            "confirm_password": "password123",
            "display_name": "Learner",
            "csrf_token": csrf,
        },
    )
    response = client.get("/admin/products")
    assert response.status_code == 403


def test_admin_can_create_edit_and_archive_product(client, db_session):
    from app.auth import service as auth_svc

    admin = auth_svc.register_user(
        db_session, email="admin@example.com", password="password123", display_name="Admin", role="admin"
    )
    session = auth_svc.create_session(db_session, admin)
    client.cookies.set("session_token", session.token)

    form_page = client.get("/admin/products/new")
    assert form_page.status_code == 200
    csrf = form_page.cookies.get("csrf_token")

    create_response = client.post(
        "/admin/products/new",
        data={
            "title": "New Course From Admin",
            "description": "A brand new course.",
            "category": "Generative AI",
            "subcategory": "RAG",
            "price": "4999",
            "level": "advanced",
            "tags": "rag, retrieval",
            "instructor": "Test Instructor",
            "duration_minutes": "1200",
            "image_url": "",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert create_response.status_code == 303
    assert create_response.headers["location"] == "/admin/products"

    product = product_service.get_product_by_slug(db_session, "new-course-from-admin")
    assert product.title == "New Course From Admin"
    assert product.price == Decimal("4999")

    edit_page = client.get(f"/admin/products/{product.id}/edit")
    assert edit_page.status_code == 200
    edit_csrf = edit_page.cookies.get("csrf_token")

    client.post(
        f"/admin/products/{product.id}/edit",
        data={
            "title": product.title,
            "description": "Updated description via edit form.",
            "category": product.category,
            "subcategory": product.subcategory,
            "price": "5499",
            "level": product.level,
            "tags": "rag, retrieval, updated",
            "instructor": product.instructor,
            "duration_minutes": str(product.duration_minutes),
            "image_url": "",
            "csrf_token": edit_csrf,
        },
    )
    db_session.refresh(product)
    assert product.description == "Updated description via edit form."
    assert product.price == Decimal("5499")

    archive_csrf = client.get("/admin/products").cookies.get("csrf_token")
    client.post(f"/admin/products/{product.id}/archive", data={"csrf_token": archive_csrf})
    db_session.refresh(product)
    assert product.status == "archived"

    # Archived products disappear from the public catalog...
    public_detail = client.get(f"/courses/{product.slug}")
    assert public_detail.status_code == 404


def test_create_product_rejects_invalid_price(client, db_session):
    from app.auth import service as auth_svc

    admin = auth_svc.register_user(
        db_session, email="admin2@example.com", password="password123", display_name="Admin", role="admin"
    )
    session = auth_svc.create_session(db_session, admin)
    client.cookies.set("session_token", session.token)

    form_page = client.get("/admin/products/new")
    csrf = form_page.cookies.get("csrf_token")

    response = client.post(
        "/admin/products/new",
        data={
            "title": "Bad Price Course",
            "description": "desc",
            "category": "Generative AI",
            "subcategory": "",
            "price": "not-a-number",
            "level": "advanced",
            "tags": "",
            "instructor": "",
            "duration_minutes": "60",
            "image_url": "",
            "csrf_token": csrf,
        },
    )
    assert response.status_code == 422
    assert "valid number" in response.text
