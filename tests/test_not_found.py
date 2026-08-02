"""
Stage 18: an undefined route used to fall through to FastAPI's default
handler — a bare `{"detail":"Not Found"}` JSON body, no styling, no way
back into the app. app/main.py's `handle_http_exception` now renders
the same branded empty-state template every other "nothing here" page
in this app uses (e.g. courses/not_found.html for an archived/missing
course slug).
"""


def test_unknown_route_renders_branded_html_404(client):
    response = client.get("/this-route-does-not-exist")

    assert response.status_code == 404
    assert "text/html" in response.headers["content-type"]
    assert "We couldn't find that page" in response.text
    # Still a real page, not just a bare error string — nav/header/footer
    # from base.html are present, and there's a way back into the app.
    assert "ChennAI Labs" in response.text
    assert 'href="/courses"' in response.text


def test_course_specific_404_is_unaffected_by_the_global_handler(client):
    """
    courses/not_found.html (an archived/missing *course slug*, a 404
    the app raises deliberately with its own copy) must keep rendering
    its own narrower message, not get swallowed by the new global
    handler — the global handler only fires for routes FastAPI itself
    can't match at all.
    """
    response = client.get("/courses/this-slug-does-not-exist")

    assert response.status_code == 404
    assert "We couldn't find that course" in response.text
