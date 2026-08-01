"""
One shared Jinja2Templates instance. Every route module (main.py,
auth/routes.py, and every routes.py added in later stages) imports
`templates` from here rather than constructing its own — with a single
instance, template globals/filters we add later (e.g. a `format_price`
filter in Stage 3) are automatically available everywhere instead of
needing to be registered N times.
"""

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="app/templates")
