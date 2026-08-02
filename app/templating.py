"""
One shared Jinja2Templates instance. Every route module (main.py,
auth/routes.py, and every routes.py added in later stages) imports
`templates` from here rather than constructing its own — with a single
instance, template globals/filters we add later (e.g. a `format_price`
filter in Stage 3) are automatically available everywhere instead of
needing to be registered N times.
"""

from fastapi.templating import Jinja2Templates

from app.core.time import utcnow

templates = Jinja2Templates(directory="app/templates")

# Registered as a Jinja2 global (not a context variable a route has to
# pass in) so every template — including base.html's footer — can call
# it without every route handler's context dict needing a `current_year`
# key. Uses the same utcnow() the rest of the app uses as its one clock,
# rather than a bare datetime.now() call, for the same reason Stage 0
# centralized time in the first place: one source of truth, easy to
# freeze/mock in tests if that's ever needed.
templates.env.globals["current_year"] = lambda: utcnow().year
