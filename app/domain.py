import math
from dataclasses import dataclass
from typing import Any


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return number if number is not None and math.isfinite(number) else None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number.is_integer() else None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


@dataclass(frozen=True)
class Property:
    id: str
    name: str
    property_type: str
    location: str
    furnishing_status: str
    monthly_rent: float | None
    security_deposit: float | None
    advance_rent_months: int | None
    minimum_lease_months: int | None
    bedrooms: int | None
    bathrooms: int | None
    area_sqm: float | None
    description: str
    images: list[str]
    inclusions: list[str]
    amenities: list[str]
    status: str

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Property":
        name = next(
            (
                value
                for key in ("name", "title", "unit_number")
                if (value := _text(row.get(key)))
            ),
            "",
        )
        rent = _number(row.get("monthly_rent"))
        if rent is None or rent <= 0:
            rent = _number(row.get("rent"))
        images = _string_list(row.get("images"))
        image_url = _text(row.get("image_url"))
        if image_url and image_url not in images:
            images.append(image_url)
        return cls(
            id=_text(row.get("id")) or "",
            name=name,
            property_type=_text(row.get("property_type")) or "",
            location=_text(row.get("location")) or "",
            furnishing_status=_text(row.get("furnishing_status")) or "",
            monthly_rent=rent,
            security_deposit=_number(row.get("security_deposit")),
            advance_rent_months=_integer(row.get("advance_rent_months")),
            minimum_lease_months=_integer(row.get("minimum_lease_months")),
            bedrooms=_integer(row.get("bedrooms")),
            bathrooms=_integer(row.get("bathrooms")),
            area_sqm=_number(row.get("area_sqm")) or _number(row.get("area")),
            description=_text(row.get("description")) or "",
            images=images,
            inclusions=_string_list(row.get("inclusions")),
            amenities=_string_list(row.get("amenities")),
            status=_text(row.get("status")) or "Available",
        )

    @property
    def is_available(self) -> bool:
        return (
            bool(self.id and self.name)
            and self.monthly_rent is not None
            and self.monthly_rent > 0
            and self.status.casefold() in {"vacant", "available"}
        )

    @property
    def cover_image(self) -> str | None:
        return self.images[0] if self.images else None


@dataclass(frozen=True)
class ListingPage:
    properties: list[Property]
    has_next_page: bool
