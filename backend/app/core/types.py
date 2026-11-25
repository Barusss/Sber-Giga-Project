"""Version-stable database types; migration history imports this module."""
from enum import Enum

from sqlalchemy.types import UserDefinedType


class Vector384(UserDefinedType):
    """pgvector(384); SQLite accepts its declaration but cannot rank vectors."""

    cache_ok = True

    def get_col_spec(self, **kw):
        return "vector(384)"


class RequirementStatus(str, Enum):
    fulfilled = "fulfilled"
    missing = "missing"
    needs_review = "needs_review"


class RequirementCategory(str, Enum):
    power = "power"
    reliability = "reliability"
    voltage = "voltage"
    connection_point = "connection_point"
    metering = "metering"
    protection = "protection"
    equipment = "equipment"
    other = "other"
