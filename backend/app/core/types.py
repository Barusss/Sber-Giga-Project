"""Version-stable database types; migration history imports this module."""
from sqlalchemy.types import UserDefinedType


class Vector384(UserDefinedType):
    """pgvector(384); SQLite accepts its declaration but cannot rank vectors."""

    cache_ok = True

    def get_col_spec(self, **kw):
        return "vector(384)"
