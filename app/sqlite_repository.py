import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .cache import TTLCache
from .domain import ListingPage, Property
from .repository import LISTING_SORTS

RENT_EXPRESSION = (
    "CASE WHEN monthly_rent > 0 THEN monthly_rent ELSE rent END"
)
AVAILABLE_WHERE = (
    "lower(COALESCE(status, 'Available')) IN ('available', 'vacant') "
    "AND COALESCE(trim(id), '') <> '' "
    "AND COALESCE(NULLIF(trim(name), ''), NULLIF(trim(title), ''), "
    "NULLIF(trim(unit_number), '')) IS NOT NULL "
    f"AND {RENT_EXPRESSION} > 0"
)
SORT_ORDERS = {
    "newest": "updated_at DESC, id ASC",
    "rent_low": f"{RENT_EXPRESSION} ASC, id ASC",
    "rent_high": f"{RENT_EXPRESSION} DESC, id ASC",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS properties (
    id TEXT PRIMARY KEY,
    title TEXT,
    name TEXT,
    unit_number TEXT,
    location TEXT,
    rent REAL,
    monthly_rent REAL,
    description TEXT,
    area REAL,
    area_sqm REAL,
    bedrooms INTEGER,
    bathrooms INTEGER,
    amenities TEXT NOT NULL DEFAULT '[]',
    inclusions TEXT NOT NULL DEFAULT '[]',
    images TEXT NOT NULL DEFAULT '[]',
    image_url TEXT,
    property_type TEXT,
    furnishing_status TEXT,
    security_deposit REAL,
    advance_rent_months INTEGER,
    minimum_lease_months INTEGER,
    updated_at TEXT,
    status TEXT NOT NULL DEFAULT 'Available'
);

CREATE TABLE IF NOT EXISTS inquiries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT NOT NULL,
    email TEXT NOT NULL,
    phone TEXT NOT NULL,
    message TEXT NOT NULL,
    unit_id TEXT,
    unit_name TEXT,
    extra_data TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
"""


class SQLitePropertyRepository:
    def __init__(self, database_path: Path, cache: TTLCache) -> None:
        self._database_path = Path(database_path)
        self._cache = cache
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self._database_path)) as connection:
            connection.executescript(SCHEMA)

    def fetch_available_units(
        self,
        *,
        page: int = 1,
        page_size: int = 12,
        search: str = "",
        sort: str = "newest",
        force_refresh: bool = False,
        property_type: str | None = None,
        furnishing_status: str | None = None,
        minimum_rent: float | None = None,
        maximum_rent: float | None = None,
        bedrooms: int | None = None,
    ) -> ListingPage:
        if sort not in LISTING_SORTS:
            raise ValueError("Unsupported listing sort option.")
        filters = (
            page,
            page_size,
            search,
            sort,
            property_type,
            furnishing_status,
            minimum_rent,
            maximum_rent,
            bedrooms,
        )
        return self._cache.get_or_load(
            f"sqlite-listings:{filters!r}",
            lambda: self._fetch_available_units(
                page=page,
                page_size=page_size,
                search=search,
                sort=sort,
                property_type=property_type,
                furnishing_status=furnishing_status,
                minimum_rent=minimum_rent,
                maximum_rent=maximum_rent,
                bedrooms=bedrooms,
            ),
            force_refresh=force_refresh,
        )

    def _fetch_available_units(
        self,
        *,
        page: int,
        page_size: int,
        search: str,
        sort: str,
        property_type: str | None,
        furnishing_status: str | None,
        minimum_rent: float | None,
        maximum_rent: float | None,
        bedrooms: int | None,
    ) -> ListingPage:
        conditions = [AVAILABLE_WHERE]
        parameters: list[Any] = []
        if search:
            pattern = f"%{search}%"
            conditions.append(
                "("
                "COALESCE(name, '') LIKE ? COLLATE NOCASE OR "
                "COALESCE(title, '') LIKE ? COLLATE NOCASE OR "
                "COALESCE(unit_number, '') LIKE ? COLLATE NOCASE OR "
                "COALESCE(location, '') LIKE ? COLLATE NOCASE"
                ")"
            )
            parameters.extend([pattern] * 4)
        if property_type:
            conditions.append("property_type = ?")
            parameters.append(property_type)
        if furnishing_status:
            conditions.append("furnishing_status = ?")
            parameters.append(furnishing_status)
        if minimum_rent is not None:
            conditions.append(f"{RENT_EXPRESSION} >= ?")
            parameters.append(minimum_rent)
        if maximum_rent is not None:
            conditions.append(f"{RENT_EXPRESSION} <= ?")
            parameters.append(maximum_rent)
        if bedrooms is not None:
            conditions.append("bedrooms >= ?" if bedrooms >= 4 else "bedrooms = ?")
            parameters.append(bedrooms)

        statement = (
            "SELECT * FROM properties WHERE "
            + " AND ".join(conditions)
            + f" ORDER BY {SORT_ORDERS[sort]} LIMIT ? OFFSET ?"
        )
        parameters.extend([page_size + 1, (page - 1) * page_size])
        with closing(sqlite3.connect(self._database_path)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(statement, parameters).fetchall()
        properties = [self._property_from_row(row) for row in rows[:page_size]]
        return ListingPage(properties, has_next_page=len(rows) > page_size)

    def fetch_property_types(self, *, force_refresh: bool = False) -> list[str]:
        return self._fetch_distinct_listing_values(
            "property_type", force_refresh=force_refresh
        )

    def fetch_furnishing_statuses(
        self, *, force_refresh: bool = False
    ) -> list[str]:
        return self._fetch_distinct_listing_values(
            "furnishing_status", force_refresh=force_refresh
        )

    def _fetch_distinct_listing_values(
        self, column: str, *, force_refresh: bool = False
    ) -> list[str]:
        if column not in {"property_type", "furnishing_status"}:
            raise ValueError("Unsupported listing category.")
        return self._cache.get_or_load(
            f"sqlite-listing-values:{column}",
            lambda: self._distinct_listing_values(column),
            force_refresh=force_refresh,
        )

    def _distinct_listing_values(self, column: str) -> list[str]:
        with closing(sqlite3.connect(self._database_path)) as connection:
            rows = connection.execute(
                f"SELECT DISTINCT trim({column}) FROM properties "
                f"WHERE {AVAILABLE_WHERE} AND trim(COALESCE({column}, '')) <> '' "
                f"ORDER BY trim({column})"
            ).fetchall()
        return [row[0] for row in rows]

    def fetch_property(self, property_id: str) -> Property | None:
        return self._cache.get_or_load(
            f"sqlite-listing:{property_id}",
            lambda: self._fetch_property(property_id),
        )

    def _fetch_property(self, property_id: str) -> Property | None:
        with closing(sqlite3.connect(self._database_path)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                f"SELECT * FROM properties WHERE id = ? AND {AVAILABLE_WHERE}",
                (property_id,),
            ).fetchone()
        return self._property_from_row(row) if row is not None else None

    def submit_inquiry(
        self,
        *,
        name: str,
        email: str,
        phone: str,
        message: str,
        unit_id: str | None = None,
        unit_name: str | None = None,
        extra_data: dict[str, Any] | None = None,
    ) -> None:
        with closing(sqlite3.connect(self._database_path)) as connection, connection:
            connection.execute(
                """
                INSERT INTO inquiries (
                    full_name, email, phone, message, unit_id, unit_name,
                    extra_data, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name.strip(),
                    email.strip().lower(),
                    phone.strip(),
                    message.strip(),
                    unit_id,
                    unit_name,
                    json.dumps(extra_data or {}, ensure_ascii=False),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    @staticmethod
    def _property_from_row(row: sqlite3.Row) -> Property:
        values = dict(row)
        for field in ("amenities", "inclusions", "images"):
            value = values.get(field)
            if isinstance(value, str):
                values[field] = json.loads(value)
        return Property.from_row(values)
