"""Pydantic request/response schemas, split by domain.

Everything is re-exported from ``app.schemas.schemas`` for backward
compatibility, so both ``from app.schemas import X`` and
``from app.schemas.schemas import X`` keep working.
"""

from app.schemas.schemas import *  # noqa: F401,F403
from app.schemas.schemas import __all__  # noqa: F401
