from datetime import datetime
from typing import Annotated, Final

from sqlalchemy import DateTime, MetaData, String
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION: Final = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

Sha256Hex = Annotated[str, "sha256_hex"]


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {
        datetime: DateTime(timezone=True),
        Sha256Hex: Annotated[str, String(64)],
    }
