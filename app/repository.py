import re
from typing import Any, Callable

import httpx

from .cache import TTLCache
from .domain import ListingPage, Property
from .settings import Settings

LISTING_COLUMNS = (
    "id,title,name,unit_number,location,rent,monthly_rent,description,"
    "area,area_sqm,bedrooms,bathrooms,amenities,inclusions,images,"
    "image_url,property_type,furnishing_status,security_deposit,"
    "advance_rent_months,minimum_lease_months,updated_at"
)
LISTING_SORTS = {
    "newest": "updated_at.desc,id.asc",
    "rent_low": "monthly_rent.asc.nullslast,rent.asc.nullslast,id.asc",
    "rent_high": "monthly_rent.desc.nullslast,rent.desc.nullslast,id.asc",
}


class RepositoryNotConfigured(RuntimeError):
    pass


class PostgrestAPIError(RuntimeError):
    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


def create_supabase_rest_client(settings: Settings) -> httpx.Client | None:
    if not settings.supabase_url or not settings.supabase_publishable_key:
        return None
    return httpx.Client(
        base_url=f"{settings.supabase_url}/rest/v1/",
        headers={
            "apikey": settings.supabase_publishable_key,
            "Authorization": f"Bearer {settings.supabase_publishable_key}",
            "Accept": "application/json",
        },
        timeout=15.0,
    )


class SupabasePropertyRepository:
    def __init__(
        self, client: httpx.Client, listing_view: str, cache: TTLCache
    ) -> None:
        self._client = client
        self._listing_view = listing_view
        self._cache = cache

    def fetch_available_units(
        self,
        *,
        page: int = 1,
        page_size: int = 12,
        search: str = "",
        sort: str = "newest",
        property_type: str | None = None,
        furnishing_status: str | None = None,
        minimum_rent: float | None = None,
        maximum_rent: float | None = None,
        bedrooms: int | None = None,
    ) -> ListingPage:
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
            f"listings:{filters!r}",
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
        order = LISTING_SORTS.get(sort)
        if order is None:
            raise ValueError("Unsupported listing sort option.")
        start = (page - 1) * page_size
        params = {
            "select": LISTING_COLUMNS,
            "order": order,
            "offset": str(start),
            "limit": str(page_size + 1),
        }
        filters: list[str] = []
        safe_search = re.sub(r"\s+", " ", re.sub(r"[^a-zA-Z0-9 ]", " ", search)).strip()
        if safe_search:
            filters.append(
                "or("
                f"title.ilike.*{safe_search}*,"
                f"name.ilike.*{safe_search}*,"
                f"unit_number.ilike.*{safe_search}*,"
                f"location.ilike.*{safe_search}*"
                ")"
            )
        if property_type:
            params["property_type"] = f"eq.{property_type}"
        if furnishing_status:
            params["furnishing_status"] = f"eq.{furnishing_status}"
        if minimum_rent is not None:
            filters.append(_rent_filter("gte", minimum_rent))
        if maximum_rent is not None:
            filters.append(_rent_filter("lte", maximum_rent))
        if filters:
            if len(filters) == 1 and filters[0].startswith("or("):
                params["or"] = filters[0][3:]
            else:
                params["and"] = f"({','.join(filters)})"
        if bedrooms is not None:
            params["bedrooms"] = "gte.4" if bedrooms >= 4 else f"eq.{bedrooms}"

        rows = self._get(self._listing_view, params)
        properties = [
            property_
            for property_ in (
                Property.from_row(row) for row in rows[:page_size]
            )
            if property_.is_available
        ]
        return ListingPage(
            properties=properties,
            has_next_page=len(rows) > page_size,
        )

    def fetch_property_types(self) -> list[str]:
        return self._fetch_distinct_listing_values("property_type")

    def fetch_furnishing_statuses(self) -> list[str]:
        return self._fetch_distinct_listing_values("furnishing_status")

    def _fetch_distinct_listing_values(self, column: str) -> list[str]:
        return self._cache.get_or_load(
            f"listing-values:{column}",
            lambda: sorted(
                {
                    value.strip()
                    for row in self._get(
                        self._listing_view,
                        {"select": column, "order": column},
                    )
                    if (value := row.get(column))
                    and isinstance(value, str)
                    and value.strip()
                }
            ),
        )

    def fetch_property(self, property_id: str) -> Property | None:
        return self._cache.get_or_load(
            f"listing:{property_id}",
            lambda: self._fetch_property(property_id),
        )

    def _fetch_property(self, property_id: str) -> Property | None:
        rows = self._get(
            self._listing_view,
            {
                "select": LISTING_COLUMNS,
                "id": f"eq.{property_id}",
                "limit": "1",
            },
        )
        if not rows:
            return None
        property_ = Property.from_row(rows[0])
        return property_ if property_.is_available else None

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
        clean_name = name.strip()
        clean_email = email.strip().lower()
        clean_phone = phone.strip()
        clean_message = message.strip()
        primary_payload: dict[str, Any] = {
            "full_name": clean_name,
            "name": clean_name,
            "email": clean_email,
            "phone": clean_phone,
            "message": clean_message,
        }
        if unit_id:
            primary_payload.update(
                {"preferred_unit_id": unit_id, "unit_id": unit_id}
            )
        if unit_name:
            primary_payload["unit_name"] = unit_name
        if extra_data:
            primary_payload.update(extra_data)
        exact_payload: dict[str, Any] = {
            "full_name": clean_name,
            "email": clean_email,
            "phone": clean_phone,
            "message": clean_message,
        }
        if unit_id:
            exact_payload["preferred_unit_id"] = unit_id
        fallback_payload: dict[str, Any] = {
            "name": clean_name,
            "email": clean_email,
            "phone": clean_phone,
            "message": clean_message,
        }
        if unit_id:
            fallback_payload["unit_id"] = unit_id
        if unit_name:
            fallback_payload["unit_name"] = unit_name

        insert_tenant_inquiry_with_schema_fallback(
            primary=lambda: self._insert("tenant_inquiries", primary_payload),
            exact_schema=lambda: self._insert("tenant_inquiries", exact_payload),
            legacy=lambda: self._insert("inquiries", fallback_payload),
        )

    def _insert(self, table: str, payload: dict[str, Any]) -> None:
        response = self._client.post(
            table,
            json=payload,
            headers={"Prefer": "return=minimal"},
        )
        self._raise_for_postgrest_error(response)

    def _get(self, table: str, params: dict[str, str]) -> list[dict[str, Any]]:
        response = self._client.get(table, params=params)
        self._raise_for_postgrest_error(response)
        data = response.json()
        if not isinstance(data, list):
            raise RuntimeError("Supabase returned an unexpected listing response.")
        return [row for row in data if isinstance(row, dict)]

    @staticmethod
    def _raise_for_postgrest_error(response: httpx.Response) -> None:
        if response.is_success:
            return
        try:
            error = response.json()
        except ValueError:
            error = {}
        if not isinstance(error, dict):
            error = {}
        message = error.get("message")
        code = error.get("code")
        raise PostgrestAPIError(
            str(message) if message else "Supabase request failed.",
            str(code) if code else None,
        )


def _rent_filter(operator: str, amount: float) -> str:
    return (
        "or("
        f"monthly_rent.{operator}.{amount},"
        f"and(monthly_rent.is.null,rent.{operator}.{amount}),"
        f"and(monthly_rent.lte.0,rent.{operator}.{amount})"
        ")"
    )


def insert_tenant_inquiry_with_schema_fallback(
    *,
    primary: Callable[[], None],
    exact_schema: Callable[[], None],
    legacy: Callable[[], None],
) -> None:
    try:
        primary()
        return
    except PostgrestAPIError as error:
        if _is_missing_relation(error):
            legacy()
            return
        if not _is_missing_column(error):
            raise

    try:
        exact_schema()
    except PostgrestAPIError as error:
        if not _is_missing_relation(error):
            raise
        legacy()


def _is_missing_relation(error: PostgrestAPIError) -> bool:
    return error.code in {"PGRST205", "42P01"}


def _is_missing_column(error: PostgrestAPIError) -> bool:
    return error.code in {"PGRST204", "42703"}
