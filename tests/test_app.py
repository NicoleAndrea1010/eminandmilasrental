import re
import sqlite3
from datetime import date, timedelta

import httpx
import pytest
from app.cache import TTLCache
from app.domain import ListingPage, Property
from app.main import (
    FormValidationError,
    _validate_application,
    _validate_contact,
    _validate_viewing,
    create_app,
)
from app.rate_limit import InquiryRateLimiter, RateLimitExceeded
from app.repository import (
    PostgrestAPIError,
    SupabasePropertyRepository,
    create_supabase_rest_client,
)
from app.settings import Settings
from app.sqlite_repository import SQLitePropertyRepository


PROPERTY_ROW = {
    "id": "unit-1",
    "title": "Garden Studio",
    "location": "Quezon City",
    "monthly_rent": 0,
    "rent": 12000,
    "bedrooms": 1,
    "bathrooms": 1,
    "area_sqm": 28,
    "images": ["https://example.com/studio.jpg"],
    "image_url": "https://example.com/studio.jpg",
    "amenities": ["Air conditioning", " "],
    "inclusions": ["Water"],
    "furnishing_status": "Furnished",
    "security_deposit": 24000,
    "advance_rent_months": 1,
    "minimum_lease_months": 6,
}


class FakeRepository:
    def __init__(self) -> None:
        self.saved: list[dict[str, object]] = []
        self.last_filters: dict[str, object] = {}
        self.has_next_page = False
        self.property = Property.from_row(PROPERTY_ROW)

    def fetch_available_units(self, **filters: object) -> ListingPage:
        self.last_filters = filters
        return ListingPage([self.property], has_next_page=self.has_next_page)

    def fetch_property_types(self, **_: object) -> list[str]:
        return ["Apartment", "Studio"]

    def fetch_furnishing_statuses(self, **_: object) -> list[str]:
        return ["Furnished", "Unfurnished"]

    def fetch_property(self, property_id: str) -> Property | None:
        return self.property if property_id == self.property.id else None

    def submit_inquiry(self, **payload: object) -> None:
        self.saved.append(payload)


def build_client():
    settings = Settings(
        app_env="test",
        session_secret="test-session-secret",
        supabase_url="",
        supabase_publishable_key="",
        listing_view="public_listings",
        listing_cache_ttl_seconds=90,
        inquiry_limit=3,
        inquiry_window_seconds=1800,
    )
    repository = FakeRepository()
    app = create_app(settings)
    app.extensions["repository"] = repository
    return app.test_client(), repository


@pytest.mark.parametrize(
    "secret",
    ["", "replace-with-a-long-random-secret", "x" * 31],
)
def test_production_rejects_missing_or_weak_session_secret(
    monkeypatch: pytest.MonkeyPatch, secret: str
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SESSION_SECRET", secret)

    with pytest.raises(ValueError, match="random SESSION_SECRET"):
        Settings.from_env()


def test_production_requires_supabase_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SESSION_SECRET", "random-production-secret-" * 2)
    monkeypatch.setenv("SUPABASE_URL", "")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "")
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)

    with pytest.raises(ValueError, match="SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY"):
        Settings.from_env()


def test_production_accepts_complete_supabase_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SESSION_SECRET", "random-production-secret-" * 2)
    monkeypatch.setenv("SUPABASE_URL", "https://project.example")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "publishable-key")

    settings = Settings.from_env()

    assert settings.supabase_url == "https://project.example"
    assert settings.supabase_publishable_key == "publishable-key"


@pytest.mark.parametrize(
    ("url", "key"),
    [
        ("https://project.example", ""),
        ("", "publishable-key"),
    ],
)
def test_settings_reject_partial_supabase_configuration(
    monkeypatch: pytest.MonkeyPatch, url: str, key: str
) -> None:
    monkeypatch.setenv("SUPABASE_URL", url)
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", key)

    with pytest.raises(ValueError, match="Set both SUPABASE_URL"):
        Settings.from_env()


@pytest.mark.parametrize(
    "url",
    [
        "http://project.example",
        "https://user:password@project.example",
        "https://project.example/rest/v1",
        "https://project.example?key=value",
    ],
)
def test_production_rejects_unsafe_supabase_urls(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SESSION_SECRET", "random-production-secret-" * 2)
    monkeypatch.setenv("SUPABASE_URL", url)
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "publishable-key")

    with pytest.raises(ValueError, match="SUPABASE_URL"):
        Settings.from_env()


@pytest.mark.parametrize(
    "key",
    [
        "sb_secret_sensitive",
        "eyJhbGciOiJub25lIn0.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.signature",
    ],
)
def test_settings_reject_supabase_service_role_keys(
    monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://project.example")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", key)

    with pytest.raises(ValueError, match="never a service-role key"):
        Settings.from_env()


def test_supabase_configuration_comes_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUPABASE_URL", "")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "")
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)

    settings = Settings.from_env()

    assert settings.supabase_url == ""
    assert settings.supabase_publishable_key == ""
    assert create_supabase_rest_client(settings) is None


def test_local_database_path_comes_from_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    database_path = tmp_path / "custom-local.db"
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("SUPABASE_URL", "")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "")
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
    monkeypatch.setenv("LOCAL_DATABASE_PATH", str(database_path))

    settings = Settings.from_env()

    assert settings.local_database_path == database_path


def test_supabase_client_uses_configured_server_side_key() -> None:
    settings = Settings(
        app_env="test",
        session_secret="test-session-secret",
        supabase_url="https://project.example",
        supabase_publishable_key="configured-publishable-key",
        listing_view="public_listings",
        listing_cache_ttl_seconds=90,
        inquiry_limit=3,
        inquiry_window_seconds=1800,
    )

    client = create_supabase_rest_client(settings)

    assert client is not None
    assert client.base_url == "https://project.example/rest/v1/"
    assert client.headers["apikey"] == "configured-publishable-key"
    assert client.headers["Authorization"] == "Bearer configured-publishable-key"
    client.close()


def test_supabase_key_is_not_rendered_into_html(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "server-only-publishable-key"
    monkeypatch.setattr("app.main.create_supabase_rest_client", lambda _: None)
    settings = Settings(
        app_env="test",
        session_secret="test-session-secret",
        supabase_url="https://project.example",
        supabase_publishable_key=key,
        listing_view="public_listings",
        listing_cache_ttl_seconds=90,
        inquiry_limit=3,
        inquiry_window_seconds=1800,
    )
    app = create_app(settings)
    app.extensions["repository"] = FakeRepository()

    response = app.test_client().get("/")

    assert response.status_code == 200
    assert key not in response.text


def test_development_uses_local_sqlite_repository(tmp_path) -> None:
    settings = Settings(
        app_env="development",
        session_secret="test-session-secret",
        supabase_url="",
        supabase_publishable_key="",
        listing_view="public_listings",
        listing_cache_ttl_seconds=90,
        inquiry_limit=3,
        inquiry_window_seconds=1800,
        local_database_path=tmp_path / "rentals.sqlite3",
    )

    app = create_app(settings)

    assert isinstance(
        app.extensions["repository"], SQLitePropertyRepository
    )
    assert (tmp_path / "rentals.sqlite3").exists()


def test_production_uses_supabase_and_secure_session_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = httpx.Client(
        base_url="https://project.example/rest/v1/",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[])),
    )
    monkeypatch.setattr("app.main.create_supabase_rest_client", lambda _: client)
    settings = Settings(
        app_env="production",
        session_secret="random-production-secret-" * 2,
        supabase_url="https://project.example",
        supabase_publishable_key="publishable-key",
        listing_view="public_listings",
        listing_cache_ttl_seconds=90,
        inquiry_limit=3,
        inquiry_window_seconds=1800,
    )

    app = create_app(settings)
    assert isinstance(app.extensions["repository"], SupabasePropertyRepository)
    app.extensions["repository"] = FakeRepository()
    response = app.test_client().get("/")
    cookie = response.headers["Set-Cookie"]
    client.close()

    assert response.status_code == 200
    assert "Secure" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie


def test_sqlite_repository_filters_sorts_pages_and_saves_inquiries(
    tmp_path,
) -> None:
    database_path = tmp_path / "rentals.sqlite3"
    repository = SQLitePropertyRepository(database_path, TTLCache(0))
    with sqlite3.connect(database_path) as connection:
        connection.executemany(
            """
            INSERT INTO properties (
                id, name, location, rent, monthly_rent, property_type,
                furnishing_status, updated_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "unit-1", "Garden Studio", "Laguna", 12000, 0,
                    "Studio", "Furnished", "2026-03-01", "Available",
                ),
                (
                    "unit-2", "Garden Loft", "Laguna", 10000, 10000,
                    "Studio", "Furnished", "2026-04-01", "Available",
                ),
                (
                    "unit-3", "Garden Home", "Laguna", 20000, 20000,
                    "House", "Unfurnished", "2026-05-01", "Available",
                ),
            ],
        )

    first_page = repository.fetch_available_units(
        search="Garden", sort="rent_low", page_size=1
    )
    second_page = repository.fetch_available_units(
        search="Garden", sort="rent_low", page=2, page_size=1
    )

    assert [property_.id for property_ in first_page.properties] == ["unit-2"]
    assert first_page.has_next_page
    assert [property_.id for property_ in second_page.properties] == ["unit-1"]
    assert repository.fetch_property_types() == ["House", "Studio"]
    assert repository.fetch_furnishing_statuses() == ["Furnished", "Unfurnished"]

    repository.submit_inquiry(
        name=" Alex Example ",
        email="ALEX@example.com",
        phone="09171234567",
        message="Please tell me about the home.",
        unit_id="unit-2",
        unit_name="Garden Loft",
    )
    with sqlite3.connect(database_path) as connection:
        saved = connection.execute(
            "SELECT full_name, email, unit_id FROM inquiries"
        ).fetchone()

    assert saved == ("Alex Example", "alex@example.com", "unit-2")


def test_sqlite_listing_cache_refreshes_updated_rows(tmp_path) -> None:
    database_path = tmp_path / "cached-rentals.sqlite3"
    repository = SQLitePropertyRepository(database_path, TTLCache(60))
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO properties (id, name, rent, updated_at)
            VALUES ('unit-1', 'Garden Studio', 12000, '2026-03-01')
            """
        )

    cached = repository.fetch_available_units(search="Garden", page_size=10)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO properties (id, name, rent, updated_at)
            VALUES ('unit-2', 'Garden Loft', 15000, '2026-04-01')
            """
        )
    still_cached = repository.fetch_available_units(search="Garden", page_size=10)
    refreshed = repository.fetch_available_units(
        search="Garden", page_size=10, force_refresh=True
    )

    assert len(cached.properties) == 1
    assert len(still_cached.properties) == 1
    assert {property_.id for property_ in refreshed.properties} == {"unit-1", "unit-2"}


def csrf_from(response_text: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', response_text)
    assert match
    return match.group(1)


def test_property_maps_rent_fallback_and_available_fields() -> None:
    property_ = Property.from_row(PROPERTY_ROW)

    assert property_.name == "Garden Studio"
    assert property_.monthly_rent == 12000
    assert property_.images == ["https://example.com/studio.jpg"]
    assert property_.amenities == ["Air conditioning"]
    assert property_.is_available


def test_property_rejects_nonfinite_numbers_and_fractional_counts() -> None:
    property_ = Property.from_row(
        {
            **PROPERTY_ROW,
            "monthly_rent": float("inf"),
            "rent": float("nan"),
            "bedrooms": 1.5,
            "bathrooms": True,
        }
    )

    assert property_.monthly_rent is None
    assert property_.bedrooms is None
    assert property_.bathrooms is None
    assert not property_.is_available


def test_ttl_cache_loads_once_until_cleared() -> None:
    cache = TTLCache(60)
    calls = 0

    def loader() -> list[int]:
        nonlocal calls
        calls += 1
        return [calls]

    assert cache.get_or_load("properties", loader) == [1]
    assert cache.get_or_load("properties", loader) == [1]
    cache.clear()
    assert cache.get_or_load("properties", loader) == [2]
    assert calls == 2


def test_ttl_cache_can_force_refresh_an_existing_key() -> None:
    cache = TTLCache(60)
    calls = 0

    def loader() -> int:
        nonlocal calls
        calls += 1
        return calls

    assert cache.get_or_load("properties", loader) == 1
    assert cache.get_or_load("properties", loader) == 1
    assert cache.get_or_load("properties", loader, force_refresh=True) == 2
    assert cache.get_or_load("properties", loader) == 2
    assert calls == 2


def test_rate_limiter_releases_failed_submission_reservations() -> None:
    limiter = InquiryRateLimiter(1, 60)
    first = limiter.reserve("client")
    limiter.release(first)

    second = limiter.reserve("client")
    with pytest.raises(RateLimitExceeded):
        limiter.reserve("client")


def test_home_and_properties_render_with_filters() -> None:
    client, repository = build_client()

    home = client.get("/")
    listings = client.get(
        "/properties?search=Garden&bedrooms=1&minimum_rent=5000&sort=rent_low"
    )

    assert home.status_code == 200
    assert "Find a home" in home.text
    assert listings.status_code == 200
    assert "Garden Studio" in listings.text
    assert repository.last_filters["search"] == "Garden"
    assert repository.last_filters["bedrooms"] == 1
    assert repository.last_filters["minimum_rent"] == 5000
    assert repository.last_filters["sort"] == "rent_low"
    assert "Showing 1–1 properties" in listings.text


def test_pagination_preserves_search_and_sort() -> None:
    client, repository = build_client()
    repository.has_next_page = True

    response = client.get("/properties?search=Garden&sort=rent_low")

    assert response.status_code == 200
    assert 'href="/properties?search=Garden&amp;sort=rent_low&amp;page=2"' in response.text


def test_load_more_request_returns_cards_and_preserves_filters() -> None:
    client, repository = build_client()
    repository.has_next_page = True

    response = client.get(
        "/properties?search=Garden&sort=rent_low&page=2",
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert response.status_code == 200
    assert "Garden Studio" in response.text
    assert "<html" not in response.text
    assert response.headers["X-Last-Result"] == "13"
    assert response.headers["X-Has-Next"] == "true"
    assert response.headers["X-Next-Page"] == (
        "/properties?search=Garden&sort=rent_low&page=3"
    )


def test_refresh_bypasses_cache_and_preserves_filters() -> None:
    client, repository = build_client()

    response = client.get(
        "/properties?search=Garden&sort=rent_low&property_type=Studio&refresh=1"
    )

    assert response.status_code == 200
    assert repository.last_filters["force_refresh"] is True
    assert (
        'href="/properties?search=Garden&amp;sort=rent_low&amp;property_type=Studio&amp;refresh=1"'
        in response.text
    )


def test_refresh_parameter_rejects_unexpected_values() -> None:
    client, repository = build_client()

    response = client.get("/properties?refresh=true")

    assert response.status_code == 422
    assert not repository.last_filters


@pytest.mark.parametrize(
    "query",
    ["minimum_rent=-1", "maximum_rent=1.001", "minimum_rent=1e3"],
)
def test_property_rent_filters_require_nonnegative_two_decimal_amounts(
    query: str,
) -> None:
    client, repository = build_client()

    response = client.get(f"/properties?{query}")

    assert response.status_code == 422
    assert not repository.last_filters


def test_property_sort_rejects_unknown_options() -> None:
    client, repository = build_client()

    response = client.get("/properties?sort=unrecognized")

    assert response.status_code == 422
    assert "sorting option" in response.text.lower()
    assert not repository.last_filters


def test_property_filters_reject_unknown_categories() -> None:
    client, repository = build_client()

    response = client.get("/properties?property_type=Unknown")

    assert response.status_code == 422
    assert "property type" in response.text.lower()
    assert not repository.last_filters


def test_property_detail_shows_application_and_viewing_forms() -> None:
    client, _ = build_client()

    response = client.get("/property/unit-1")

    assert response.status_code == 200
    assert "Schedule a viewing" in response.text
    assert "Apply for this home" in response.text
    assert "at least 18 years old" in response.text
    assert f'min="{max(date.today(), date(2026, 1, 1)).isoformat()}"' in response.text
    assert f'min="{max(date.today() + timedelta(days=1), date(2026, 1, 1)).isoformat()}"' in response.text


def test_terms_and_privacy_pages_are_linked_and_informative() -> None:
    client, _ = build_client()

    terms_page = client.get("/terms")
    privacy_page = client.get("/privacy")

    assert terms_page.status_code == 200
    assert "Cernan Anthony L. Manalo" in terms_page.text
    assert "Draft" not in terms_page.text
    assert "at least 18 years old" in terms_page.text
    assert "does not accept rent" in terms_page.text
    assert privacy_page.status_code == 200
    assert "monthly income" in privacy_page.text
    assert "do not currently use a fixed automatic deletion schedule" in (
        privacy_page.text
    )
    assert 'href="/terms"' in client.get("/").text
    assert 'href="/privacy"' in client.get("/").text


def test_contact_form_writes_to_repository_after_consent() -> None:
    client, repository = build_client()
    page = client.get("/contact")
    token = csrf_from(page.text)

    response = client.post(
        "/contact",
        data={
            "csrf_token": token,
            "name": "  Alex   Example ",
            "email": " Alex@Example.COM ",
            "phone": " +63 (917) 123-4567 ",
            "message": "I would like to   know more about this rental.",
            "consent": "yes",
            "terms": "yes",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith("/contact?sent=1")
    assert repository.saved[0]["name"] == "ALEX EXAMPLE"
    assert repository.saved[0]["email"] == "alex@example.com"
    assert repository.saved[0]["phone"] == "09171234567"
    assert repository.saved[0]["message"] == (
        "I would like to know more about this rental."
    )


def test_contact_form_rejects_missing_consent_without_saving() -> None:
    client, repository = build_client()
    page = client.get("/contact")
    token = csrf_from(page.text)

    response = client.post(
        "/contact",
        data={
            "csrf_token": token,
            "name": "Alex Example",
            "email": "alex@example.com",
            "phone": "09171234567",
            "message": "I would like to know more about this rental.",
        },
    )

    assert response.status_code == 400
    assert "consent" in response.text.lower()
    assert not repository.saved


def test_contact_form_requires_terms_acceptance() -> None:
    client, repository = build_client()
    token = csrf_from(client.get("/contact").text)

    response = client.post(
        "/contact",
        data={
            "csrf_token": token,
            "name": "Alex Example",
            "email": "alex@example.com",
            "phone": "09171234567",
            "message": "I would like to know more about this rental.",
            "consent": "yes",
        },
    )

    assert response.status_code == 400
    assert "terms and conditions" in response.text.lower()
    assert not repository.saved


def test_contact_rate_limit_reports_retry_timer() -> None:
    client, repository = build_client()
    token = csrf_from(client.get("/contact").text)
    payload = {
        "csrf_token": token,
        "name": "Alex Example",
        "email": "alex@example.com",
        "phone": "09171234567",
        "message": "I would like to know more about this rental.",
        "consent": "yes",
        "terms": "yes",
    }

    for _ in range(3):
        response = client.post("/contact", data=payload)
        assert response.status_code == 303
    limited = client.post("/contact", data=payload)

    assert limited.status_code == 429
    assert "wait" in limited.text.lower()
    assert len(repository.saved) == 3


def test_contact_form_rejects_invalid_csrf_token() -> None:
    client, repository = build_client()

    response = client.post(
        "/contact",
        data={
            "csrf_token": "invalid",
            "name": "Alex Example",
            "email": "alex@example.com",
            "phone": "09171234567",
            "message": "I would like to know more about this rental.",
            "consent": "yes",
            "terms": "yes",
        },
    )

    assert response.status_code == 403
    assert not repository.saved


def test_application_and_viewing_forms_submit_existing_inquiry_payloads() -> None:
    client, repository = build_client()
    token = csrf_from(client.get("/property/unit-1").text)
    application_response = client.post(
        "/property/unit-1/application",
        data={
            "csrf_token": token,
            "applicant_name": "Alex Example",
            "email": "alex@example.com",
            "phone": "09171234567",
            "consent": "yes",
            "terms": "yes",
            "adult_confirmation": "yes",
            "monthly_income": "50000",
            "occupation": "Designer",
            "target_move_in_date": (date.today() + timedelta(days=14)).isoformat(),
            "lease_months": "12",
            "occupants": "2",
        },
        follow_redirects=False,
    )
    viewing_response = client.post(
        "/property/unit-1/viewing",
        data={
            "csrf_token": token,
            "name": "Alex Example",
            "email": "alex@example.com",
            "phone": "09171234567",
            "consent": "yes",
            "terms": "yes",
            "viewing_date": (date.today() + timedelta(days=1)).isoformat(),
            "time_slot": "10:00 AM - 11:00 AM",
            "lease_months": "12",
        },
        follow_redirects=False,
    )

    assert application_response.status_code == 303
    assert viewing_response.status_code == 303
    assert len(repository.saved) == 2
    assert repository.saved[0]["extra_data"]["applicant_name"] == "ALEX EXAMPLE"
    assert repository.saved[0]["extra_data"]["occupation"] == "DESIGNER"
    assert "adult_confirmation" not in repository.saved[0]["extra_data"]
    assert repository.saved[1]["extra_data"]["time_slot"] == "10:00 AM - 11:00 AM"


def test_repository_builds_server_side_filter_and_range_query() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[PROPERTY_ROW, PROPERTY_ROW])

    http_client = httpx.Client(
        base_url="https://example.supabase.co/rest/v1/",
        transport=httpx.MockTransport(handle),
    )
    repository = SupabasePropertyRepository(http_client, "public_listings", TTLCache(60))

    page = repository.fetch_available_units(
        page=1,
        page_size=1,
        search="Garden?",
        sort="rent_low",
        minimum_rent=5000,
        maximum_rent=20000,
        bedrooms=4,
    )

    params = requests[0].url.params
    assert page.has_next_page
    assert len(page.properties) == 1
    assert params["limit"] == "2"
    assert params["order"] == "monthly_rent.asc.nullslast,rent.asc.nullslast,id.asc"
    assert "Garden" in params["and"]
    assert "monthly_rent.gte.5000" in params["and"]
    assert "monthly_rent.lte.20000" in params["and"]
    assert params["bedrooms"] == "gte.4"


def test_repository_refresh_bypasses_cached_filtered_results() -> None:
    requests: list[httpx.Request] = []
    rows = [PROPERTY_ROW]

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=rows)

    http_client = httpx.Client(
        base_url="https://example.supabase.co/rest/v1/",
        transport=httpx.MockTransport(handle),
    )
    repository = SupabasePropertyRepository(
        http_client, "public_listings", TTLCache(60)
    )
    first = repository.fetch_available_units(search="Garden")
    rows = [{**PROPERTY_ROW, "id": "unit-2"}]
    cached = repository.fetch_available_units(search="Garden")
    refreshed = repository.fetch_available_units(
        search="Garden", force_refresh=True
    )
    http_client.close()

    assert first.properties[0].id == "unit-1"
    assert cached.properties[0].id == "unit-1"
    assert refreshed.properties[0].id == "unit-2"
    assert len(requests) == 2


def test_repository_retries_exact_schema_only_for_missing_columns() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                400,
                json={"code": "PGRST204", "message": "column missing"},
            )
        return httpx.Response(201)

    http_client = httpx.Client(
        base_url="https://example.supabase.co/rest/v1/",
        transport=httpx.MockTransport(handle),
    )
    repository = SupabasePropertyRepository(http_client, "public_listings", TTLCache(60))

    repository.submit_inquiry(
        name=" Alex Example ",
        email="ALEX@example.com",
        phone="09171234567",
        message="Please tell me about the home.",
    )

    assert [request.url.path.rsplit("/", 1)[-1] for request in requests] == [
        "tenant_inquiries",
        "tenant_inquiries",
    ]
    assert requests[0].read() != requests[1].read()


def test_repository_uses_legacy_table_only_when_primary_is_missing() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                404,
                json={"code": "PGRST205", "message": "table missing"},
            )
        return httpx.Response(201)

    http_client = httpx.Client(
        base_url="https://example.supabase.co/rest/v1/",
        transport=httpx.MockTransport(handle),
    )
    repository = SupabasePropertyRepository(http_client, "public_listings", TTLCache(60))

    repository.submit_inquiry(
        name="Alex Example",
        email="alex@example.com",
        phone="09171234567",
        message="Please tell me about the home.",
        unit_id="unit-1",
        unit_name="Garden Studio",
    )

    assert [request.url.path.rsplit("/", 1)[-1] for request in requests] == [
        "tenant_inquiries",
        "inquiries",
    ]


def test_repository_never_falls_back_after_permission_denial() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            403,
            json={"code": "42501", "message": "permission denied"},
        )

    http_client = httpx.Client(
        base_url="https://example.supabase.co/rest/v1/",
        transport=httpx.MockTransport(handle),
    )
    repository = SupabasePropertyRepository(http_client, "public_listings", TTLCache(60))

    with pytest.raises(PostgrestAPIError) as error:
        repository.submit_inquiry(
            name="Alex Example",
            email="alex@example.com",
            phone="09171234567",
            message="Please tell me about the home.",
        )

    assert error.value.code == "42501"
    assert len(requests) == 1
    assert requests[0].url.path.endswith("/tenant_inquiries")


def test_contact_validation_covers_each_field() -> None:
    valid = {
        "name": "Alex Example",
        "email": "alex@example.com",
        "phone": "09171234567",
        "message": "I would like to know more about this rental.",
        "consent": "yes",
        "terms": "yes",
    }
    invalid_values = [
        ("name", "A1"),
        ("name", "AAAA"),
        ("name", "Alex$ Example"),
        ("name", "A"),
        ("name", "A" * 101),
        ("email", "not-an-email"),
        ("phone", "12345678901"),
        ("message", "short"),
        ("message", "x" * 10),
        ("message", "x" * 2001),
        ("consent", "no"),
        ("terms", "no"),
    ]

    for field, value in invalid_values:
        with pytest.raises(FormValidationError):
            _validate_contact({**valid, field: value})


def test_contact_normalizes_supported_email_phone_name_and_message() -> None:
    name, email, phone, message = _validate_contact(
        {
            "name": "  Ma.   Theresa O'Connor-Cruz  ",
            "email": "  Alex.Example+Home@GMAIL.COM ",
            "phone": "+63 (917) 123-4567",
            "message": "  I   would like to   ask about this home.  ",
            "consent": "yes",
            "terms": "yes",
        }
    )

    assert name == "MA. THERESA O'CONNOR-CRUZ"
    assert email == "alex.example+home@gmail.com"
    assert phone == "09171234567"
    assert message == "I would like to ask about this home."


@pytest.mark.parametrize(
    "email",
    [
        "user@gmail",
        "a..b@example.com",
        ".user@example.com",
        "user@example..com",
        "user@mailinator.com",
        "user@sub.tempmail.com",
        "user@10minutemail.com",
    ],
)
def test_contact_rejects_malformed_and_known_disposable_emails(email: str) -> None:
    with pytest.raises(FormValidationError):
        _validate_contact(
            {
                "name": "Alex Example",
                "email": email,
                "phone": "09171234567",
                "message": "I would like to know more about this rental.",
                "consent": "yes",
                "terms": "yes",
            }
        )


@pytest.mark.parametrize(
    ("phone", "normalized"),
    [
        ("0917-123-4567", "09171234567"),
        ("+639171234567", "09171234567"),
        ("09875555641", "09875555641"),
    ],
)
def test_contact_accepts_and_normalizes_philippine_mobile_numbers(
    phone: str, normalized: str
) -> None:
    result = _validate_contact(
        {
            "name": "Alex Example",
            "email": "alex@example.edu.ph",
            "phone": phone,
            "message": "I would like to know more about this rental.",
            "consent": "yes",
            "terms": "yes",
        }
    )

    assert result[2] == normalized


@pytest.mark.parametrize(
    "phone",
    [
        "09999999999",
        "09000000000",
        "09123456789",
        "09876543210",
        "+6391712345678",
        "0917abc4567",
    ],
)
def test_contact_rejects_dummy_or_invalid_philippine_mobile_numbers(phone: str) -> None:
    with pytest.raises(FormValidationError):
        _validate_contact(
            {
                "name": "Alex Example",
                "email": "alex@example.com",
                "phone": phone,
                "message": "I would like to know more about this rental.",
                "consent": "yes",
                "terms": "yes",
            }
        )


def test_application_validation_covers_each_field() -> None:
    valid = {
        "adult_confirmation": "yes",
        "monthly_income": "50000",
        "occupation": "Designer",
        "target_move_in_date": (date.today() + timedelta(days=14)).isoformat(),
        "lease_months": "12",
        "occupants": "2",
        "notes": "",
    }
    invalid_values = [
        ("adult_confirmation", "no"),
        ("monthly_income", "NaN"),
        ("monthly_income", "0"),
        ("monthly_income", "50000.999"),
        ("monthly_income", "50,00"),
        ("occupation", "x" * 151),
        ("occupation", "AAAA"),
        ("target_move_in_date", (date.today() - timedelta(days=1)).isoformat()),
        ("target_move_in_date", "2025-12-31"),
        ("target_move_in_date", "20261130"),
        ("lease_months", "121"),
        ("occupants", "51"),
        ("notes", "x" * 2001),
    ]

    for field, value in invalid_values:
        with pytest.raises(FormValidationError):
            _validate_application({**valid, field: value})


def test_viewing_validation_covers_each_field() -> None:
    valid = {
        "viewing_date": (date.today() + timedelta(days=1)).isoformat(),
        "time_slot": "10:00 AM - 11:00 AM",
        "lease_months": "12",
        "message": "",
    }
    invalid_values = [
        ("viewing_date", date.today().isoformat()),
        ("viewing_date", "2025-12-31"),
        ("viewing_date", "20261130"),
        ("time_slot", "Anytime"),
        ("lease_months", "0"),
        ("message", "x" * 2001),
    ]

    for field, value in invalid_values:
        with pytest.raises(FormValidationError):
            _validate_viewing({**valid, field: value})
