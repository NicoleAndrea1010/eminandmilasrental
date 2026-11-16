import hmac
import logging
import math
import re
import secrets
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
from flask import Flask, abort, current_app, redirect, render_template, request, session, url_for
from werkzeug.exceptions import HTTPException

from .cache import TTLCache
from .domain import Property
from .rate_limit import InquiryRateLimiter, RateLimitExceeded, Reservation
from .repository import (
    LISTING_SORTS,
    PostgrestAPIError,
    RepositoryNotConfigured,
    SupabasePropertyRepository,
    create_supabase_rest_client,
)
from .settings import Settings
from .sqlite_repository import SQLitePropertyRepository

logger = logging.getLogger(__name__)
APP_DIR = Path(__file__).resolve().parent
PAGE_SIZE = 12
MINIMUM_BOOKING_DATE = date(2026, 1, 1)
PROPERTY_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
DISPOSABLE_EMAIL_DOMAINS = frozenset(
    {"10minutemail.com", "mailinator.com", "tempmail.com"}
)
EMAIL_PATTERN = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63}$"
)


class FormValidationError(ValueError):
    pass


def create_app(settings: Settings | None = None) -> Flask:
    config = settings or Settings.from_env()
    app = Flask(
        __name__,
        template_folder=str(APP_DIR / "templates"),
        static_folder=str(APP_DIR / "static"),
    )
    app.secret_key = config.session_secret
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=config.app_env == "production",
        MAX_CONTENT_LENGTH=1024 * 1024,
    )
    app.extensions["settings"] = config
    app.extensions["listing_cache"] = TTLCache(config.listing_cache_ttl_seconds)
    app.extensions["rate_limiter"] = InquiryRateLimiter(
        config.inquiry_limit, config.inquiry_window_seconds
    )
    if config.app_env == "development":
        repository = SQLitePropertyRepository(
            config.local_database_path,
            app.extensions["listing_cache"],
        )
    else:
        client = create_supabase_rest_client(config)
        repository = (
            SupabasePropertyRepository(
                client,
                config.listing_view,
                app.extensions["listing_cache"],
            )
            if client is not None
            else None
        )
    app.extensions["repository"] = repository

    @app.errorhandler(HTTPException)
    def render_http_error(error: HTTPException):
        status_code = error.code or 500
        title = {
            403: "Please refresh and try again",
            404: "We couldn’t find that page",
            413: "That request is too large",
            422: "Please check your information",
        }.get(status_code, "Something went wrong")
        message = error.description or "Please return to the homepage and try again."
        return render_template("error.html", title=title, message=message), status_code

    @app.get("/")
    def home():
        properties: list[Property] = []
        listing_error = None
        try:
            page = _repository().fetch_available_units(page_size=3)
            properties = page.properties
        except (RepositoryNotConfigured, PostgrestAPIError, httpx.HTTPError) as error:
            listing_error = _public_listing_error(error)
            logger.warning("Could not load home page listings: %s", error)
        return render_template(
            "home.html",
            properties=properties,
            listing_error=listing_error,
            csrf_token=_csrf_token(),
        )

    @app.get("/properties")
    def properties():
        page = _query_integer("page", default=1, minimum=1, maximum=10000)
        search = request.args.get("search", "").strip()
        if len(search) > 120:
            abort(422, description="Search must be 120 characters or fewer.")
        sort = request.args.get("sort", "newest").strip()
        if sort not in LISTING_SORTS:
            abort(422, description="Choose a valid sorting option.")
        refresh_value = request.args.get("refresh", "")
        if refresh_value and refresh_value != "1":
            abort(422, description="Refresh must be set to 1.")
        force_refresh = refresh_value == "1"
        property_type = request.args.get("property_type", "").strip() or None
        furnishing_status = request.args.get("furnishing_status", "").strip() or None
        if property_type and len(property_type) > 80:
            abort(422, description="Property type must be 80 characters or fewer.")
        if furnishing_status and len(furnishing_status) > 80:
            abort(422, description="Furnishing must be 80 characters or fewer.")
        minimum_rent = _query_number("minimum_rent")
        maximum_rent = _query_number("maximum_rent")
        bedrooms = _query_integer("bedrooms", minimum=1, maximum=4, optional=True)
        if (
            minimum_rent is not None
            and maximum_rent is not None
            and minimum_rent > maximum_rent
        ):
            abort(422, description="Minimum rent cannot exceed maximum rent.")

        listing = None
        listing_error = None
        types: list[str] = []
        furnishings: list[str] = []
        try:
            repository = _repository()
            types = repository.fetch_property_types(force_refresh=force_refresh)
            furnishings = repository.fetch_furnishing_statuses(
                force_refresh=force_refresh
            )
            if property_type and property_type not in types:
                abort(422, description="Choose a property type from the available options.")
            if furnishing_status and furnishing_status not in furnishings:
                abort(422, description="Choose a furnishing option from the available options.")
            listing = repository.fetch_available_units(
                page=page,
                page_size=PAGE_SIZE,
                search=search,
                sort=sort,
                force_refresh=force_refresh,
                property_type=property_type,
                furnishing_status=furnishing_status,
                minimum_rent=minimum_rent,
                maximum_rent=maximum_rent,
                bedrooms=bedrooms,
            )
        except (RepositoryNotConfigured, PostgrestAPIError, httpx.HTTPError) as error:
            listing_error = _public_listing_error(error)
            logger.warning("Could not load property listings: %s", error)

        args = request.args.to_dict()
        args.pop("refresh", None)
        refresh_args = args.copy()
        refresh_args["refresh"] = 1
        refresh_url = url_for("properties", **refresh_args)
        previous_url = None
        next_url = None
        if page > 1:
            args["page"] = page - 1
            previous_url = url_for("properties", **args)
        if listing and listing.has_next_page:
            args["page"] = page + 1
            next_url = url_for("properties", **args)
        first_result = (page - 1) * PAGE_SIZE + 1 if listing and listing.properties else 0
        last_result = (
            (page - 1) * PAGE_SIZE + len(listing.properties)
            if listing and listing.properties
            else 0
        )
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            if listing is None:
                return (
                    listing_error or "Listings are temporarily unavailable.",
                    503,
                    {"Content-Type": "text/plain; charset=utf-8"},
                )
            headers = {
                "X-Last-Result": str(last_result),
                "X-Has-Next": str(bool(next_url)).lower(),
            }
            if next_url:
                headers["X-Next-Page"] = next_url
            return (
                render_template(
                    "_property_cards.html",
                    properties=listing.properties,
                ),
                200,
                headers,
            )
        return render_template(
            "properties.html",
            listing=listing,
            listing_error=listing_error,
            types=types,
            furnishings=furnishings,
            page=page,
            first_result=first_result,
            last_result=last_result,
            search=search,
            sort=sort,
            property_type=property_type or "",
            furnishing_status=furnishing_status or "",
            minimum_rent="" if minimum_rent is None else minimum_rent,
            maximum_rent="" if maximum_rent is None else maximum_rent,
            bedrooms=bedrooms,
            previous_url=previous_url,
            next_url=next_url,
            refresh_url=refresh_url,
        )

    @app.get("/property/<property_id>")
    def property_detail(property_id: str):
        if not PROPERTY_ID_PATTERN.fullmatch(property_id):
            abort(404, description="Property not found.")
        try:
            unit = _repository().fetch_property(property_id)
        except (RepositoryNotConfigured, PostgrestAPIError, httpx.HTTPError) as error:
            return (
                render_template(
                    "error.html",
                    title="Property unavailable",
                    message=_public_listing_error(error),
                ),
                503,
            )
        if unit is None:
            abort(404, description="Property not found.")
        return _render_detail(unit)

    @app.get("/about")
    def about():
        return render_template("about.html")

    @app.get("/terms")
    def terms():
        return render_template("terms.html")

    @app.get("/contact")
    def contact():
        unit_id = request.args.get("unit_id", "").strip()
        unit = None
        if unit_id:
            try:
                unit = _repository().fetch_property(unit_id)
            except (RepositoryNotConfigured, PostgrestAPIError, httpx.HTTPError) as error:
                logger.warning("Could not load selected contact property: %s", error)
        return render_template(
            "contact.html",
            unit=unit,
            sent=request.args.get("sent") == "1",
            csrf_token=_csrf_token(),
            values={},
            error=None,
        )

    @app.post("/contact")
    def submit_contact():
        if not _valid_csrf(request.form.get("csrf_token")):
            abort(403, description="Invalid form token.")
        values = request.form.to_dict()
        reservation: Reservation | None = None
        try:
            name, email, phone, message = _validate_contact(values)
            unit_id = _optional_text(values.get("unit_id"))
            unit = _find_selected_property(unit_id) if unit_id else None
            if unit_id and unit is None:
                raise FormValidationError("That property is no longer available.")
            reservation = _reserve_submission()
            _repository().submit_inquiry(
                name=name,
                email=email,
                phone=phone,
                message=message,
                unit_id=unit.id if unit else None,
                unit_name=unit.name if unit else None,
            )
            query: dict[str, Any] = {"sent": 1}
            if unit_id:
                query["unit_id"] = unit_id
            return redirect(url_for("contact", **query), code=303)
        except RateLimitExceeded as error:
            return _render_contact_error(values, str(error), status_code=429)
        except FormValidationError as error:
            return _render_contact_error(values, str(error))
        except (RepositoryNotConfigured, PostgrestAPIError, httpx.HTTPError) as error:
            if reservation:
                _rate_limiter().release(reservation)
            logger.exception("Could not save contact inquiry")
            return _render_contact_error(
                values, _public_submission_error(error), status_code=503
            )

    @app.post("/property/<property_id>/application")
    def submit_application(property_id: str):
        return _submit_property_form(property_id, form_kind="application")

    @app.post("/property/<property_id>/viewing")
    def submit_viewing(property_id: str):
        return _submit_property_form(property_id, form_kind="viewing")

    return app


app = create_app()


def _submit_property_form(property_id: str, *, form_kind: str):
    if not _valid_csrf(request.form.get("csrf_token")):
        abort(403, description="Invalid form token.")
    if not PROPERTY_ID_PATTERN.fullmatch(property_id):
        abort(404, description="Property not found.")
    values = request.form.to_dict()
    reservation: Reservation | None = None
    try:
        repository = _repository()
        unit = repository.fetch_property(property_id)
        if unit is None:
            raise FormValidationError("This property is no longer available.")
        name, email, phone = _validate_person(values)
        if form_kind == "application":
            income, occupation, move_in, lease_months, occupants, notes = (
                _validate_application(values)
            )
            message = "[RENTAL APPLICATION]"
            message += f"\nPreferred Unit: {unit.name} ({unit.id})"
            message += f"\nMonthly Income: ₱{income:.2f}"
            message += f"\nOccupation: {occupation}"
            message += f"\nTarget Move-in Date: {move_in}"
            message += f"\nLease Duration: {lease_months} months"
            message += f"\nOccupants: {occupants}"
            if notes:
                message += f"\nAdditional Notes: {notes}"
            extra_data: dict[str, Any] = {
                "applicant_name": name,
                "monthly_income": income,
                "occupation": occupation,
                "target_move_in_date": move_in,
                "lease_months": lease_months,
                "occupants": occupants,
                "notes": notes or None,
            }
        else:
            viewing_date, time_slot, lease_months, message_text = (
                _validate_viewing(values)
            )
            message = "[VIEWING SCHEDULE REQUEST]"
            message += f"\nProperty Unit: {unit.name} ({unit.id})"
            message += f"\nRequested Viewing Date: {viewing_date}"
            message += f"\nTime Slot: {time_slot}"
            message += f"\nIntended Lease: {lease_months} months"
            if message_text:
                message += f"\nMessage: {message_text}"
            extra_data = {
                "viewing_date": viewing_date,
                "time_slot": time_slot,
                "lease_months": lease_months,
            }

        reservation = _reserve_submission()
        repository.submit_inquiry(
            name=name,
            email=email,
            phone=phone,
            message=message,
            unit_id=unit.id,
            unit_name=unit.name,
            extra_data=extra_data,
        )
        return redirect(
            url_for("property_detail", property_id=property_id, sent=form_kind),
            code=303,
        )
    except RateLimitExceeded as error:
        return _render_property_form_error(
            property_id, form_kind, values, str(error), status_code=429
        )
    except FormValidationError as error:
        return _render_property_form_error(property_id, form_kind, values, str(error))
    except (RepositoryNotConfigured, PostgrestAPIError, httpx.HTTPError) as error:
        if reservation:
            _rate_limiter().release(reservation)
        logger.exception("Could not save %s request", form_kind)
        return _render_property_form_error(
            property_id,
            form_kind,
            values,
            _public_submission_error(error),
            status_code=503,
        )


def _render_property_form_error(
    property_id: str,
    form_kind: str,
    values: dict[str, str],
    error: str,
    *,
    status_code: int = 400,
):
    try:
        unit = _repository().fetch_property(property_id)
    except (RepositoryNotConfigured, PostgrestAPIError, httpx.HTTPError) as database_error:
        logger.warning("Could not reload property after form error: %s", database_error)
        unit = None
    if unit is None:
        return (
            render_template(
                "error.html",
                title="Property unavailable",
                message="This property is no longer listed. Please browse available homes.",
            ),
            404,
        )
    return _render_detail(
        unit,
        active_modal=form_kind,
        form_error=error,
        form_values=values,
        status_code=status_code,
    )


def _render_contact_error(
    values: dict[str, str],
    error: str,
    *,
    status_code: int = 400,
):
    unit_id = values.get("unit_id", "")
    try:
        unit = _find_selected_property(unit_id) if unit_id else None
    except (RepositoryNotConfigured, PostgrestAPIError, httpx.HTTPError) as database_error:
        logger.warning("Could not reload selected property after form error: %s", database_error)
        unit = None
    return (
        render_template(
            "contact.html",
            unit=unit,
            sent=False,
            csrf_token=_csrf_token(),
            values=values,
            error=error,
        ),
        status_code,
    )


def _render_detail(
    unit: Property,
    *,
    active_modal: str | None = None,
    form_error: str | None = None,
    form_values: dict[str, str] | None = None,
    status_code: int = 200,
):
    sent = request.args.get("sent")
    return (
        render_template(
            "property_detail.html",
            unit=unit,
            sent=sent if sent in {"application", "viewing"} else None,
            active_modal=active_modal,
            form_error=form_error,
            form_values=form_values or {},
            csrf_token=_csrf_token(),
            minimum_move_in=max(date.today(), MINIMUM_BOOKING_DATE).isoformat(),
            default_move_in=max(
                date.today() + timedelta(days=14), MINIMUM_BOOKING_DATE
            ).isoformat(),
            default_viewing=max(
                date.today() + timedelta(days=1), MINIMUM_BOOKING_DATE
            ).isoformat(),
            time_slots=[
                "09:00 AM - 10:00 AM",
                "10:00 AM - 11:00 AM",
                "01:30 PM - 02:30 PM",
                "03:30 PM - 04:30 PM",
                "05:00 PM - 06:00 PM",
            ],
        ),
        status_code,
    )


def _repository() -> SupabasePropertyRepository | SQLitePropertyRepository:
    repository = current_app.extensions["repository"]
    if repository is None:
        raise RepositoryNotConfigured(
            "Configure SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY."
        )
    return repository


def _find_selected_property(property_id: str) -> Property | None:
    if not PROPERTY_ID_PATTERN.fullmatch(property_id):
        return None
    return _repository().fetch_property(property_id)


def _public_listing_error(error: Exception) -> str:
    if isinstance(error, RepositoryNotConfigured):
        return (
            "Connect Supabase by setting SUPABASE_URL and "
            "SUPABASE_PUBLISHABLE_KEY in python_web/.env."
        )
    if getattr(error, "code", None) == "42501":
        return (
            "Supabase denied access to public.public_listings. Apply the public "
            "listing SELECT grant and check its row-level security policy."
        )
    if getattr(error, "code", None) == "PGRST205":
        return (
            "Supabase could not find public.public_listings or one of its columns. "
            "Check the configured public listing table or view."
        )
    return "Could not load rental listings. Check the Supabase connection and try again."


def _public_submission_error(error: Exception) -> str:
    if isinstance(error, RepositoryNotConfigured):
        return "Inquiry storage is not configured. Set up the Supabase environment first."
    return (
        "We could not save your request. Check the Supabase inquiry table and "
        "permissions, then try again."
    )


def _csrf_token() -> str:
    token = session.get("csrf_token")
    if not isinstance(token, str):
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def _valid_csrf(submitted: Any) -> bool:
    expected = session.get("csrf_token")
    return (
        isinstance(expected, str)
        and isinstance(submitted, str)
        and hmac.compare_digest(expected, submitted)
    )


def _validate_person(values: dict[str, str]) -> tuple[str, str, str]:
    name = _normalize_whitespace(values.get("name", values.get("applicant_name", ""))).upper()
    email = values.get("email", "").strip().lower()
    phone = _normalize_phone(values.get("phone", ""))
    if (
        not 2 <= len(name) <= 100
        or not any(character.isalpha() for character in name)
        or any(
            not (character.isalpha() or character in " .'-")
            for character in name
        )
        or _is_repeated_character(name)
    ):
        raise FormValidationError(
            "Enter a name between 2 and 100 characters using letters, spaces, "
            "hyphens, periods, or apostrophes."
        )
    email_parts = email.split("@")
    if (
        len(email) > 320
        or len(email_parts) != 2
        or len(email_parts[0]) > 64
        or len(email_parts[1]) > 253
        or not EMAIL_PATTERN.fullmatch(email)
        or email_parts[0].startswith(".")
        or email_parts[0].endswith(".")
        or ".." in email_parts[0]
        or any(
            email_parts[1] == domain or email_parts[1].endswith(f".{domain}")
            for domain in DISPOSABLE_EMAIL_DOMAINS
        )
    ):
        raise FormValidationError("Enter a valid email address.")
    if values.get("consent") != "yes":
        raise FormValidationError(
            "Please confirm consent to process your contact details."
        )
    if values.get("terms") != "yes":
        raise FormValidationError("Please agree to the Terms and Conditions.")
    return name, email, phone


def _validate_contact(values: dict[str, str]) -> tuple[str, str, str, str]:
    name, email, phone = _validate_person(values)
    message = _normalize_whitespace(values.get("message", ""))
    if len(message) < 10 or len(message) > 2000:
        raise FormValidationError(
            "Inquiry details must be between 10 and 2000 characters."
        )
    if _is_repeated_character(message):
        raise FormValidationError("Please enter meaningful inquiry details.")
    return name, email, phone, message


def _validate_application(
    values: dict[str, str],
) -> tuple[float, str, str, int, int, str]:
    income_value = values.get("monthly_income", "").strip()
    if not re.fullmatch(
        r"(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d{1,2})?",
        income_value,
    ):
        raise FormValidationError(
            "Enter a valid monthly income with no more than two decimal places."
        )
    try:
        income = float(income_value.replace(",", ""))
    except ValueError as error:
        raise FormValidationError("Enter a valid monthly income.") from error
    if not math.isfinite(income) or not 0 < income <= 1_000_000_000:
        raise FormValidationError(
            "Monthly income must be greater than ₱0 and at most ₱1,000,000,000."
        )
    occupation = _normalize_whitespace(values.get("occupation", "")).upper()
    if not 2 <= len(occupation) <= 150:
        raise FormValidationError("Occupation must be between 2 and 150 characters.")
    if _is_repeated_character(occupation):
        raise FormValidationError("Enter a valid occupation or industry.")
    move_in = values.get("target_move_in_date", "").strip()
    move_in_date = _parse_form_date(move_in, "Choose a valid move-in date.")
    if move_in_date < max(date.today(), MINIMUM_BOOKING_DATE):
        raise FormValidationError(
            "Move-in date must be 2026 or later and cannot be in the past."
        )
    lease_months = _bounded_integer(
        values.get("lease_months"), 1, 120, "Lease duration"
    )
    occupants = _bounded_integer(
        values.get("occupants"), 1, 50, "Number of occupants"
    )
    notes = _normalize_whitespace(values.get("notes", ""))
    if len(notes) > 2000:
        raise FormValidationError("Additional notes must be 2000 characters or fewer.")
    if _is_repeated_character(notes):
        raise FormValidationError("Please enter meaningful additional notes.")
    return income, occupation, move_in, lease_months, occupants, notes


def _validate_viewing(values: dict[str, str]) -> tuple[str, str, int, str]:
    viewing_date = values.get("viewing_date", "").strip()
    parsed_date = _parse_form_date(viewing_date, "Choose a valid viewing date.")
    if parsed_date < max(
        date.today() + timedelta(days=1), MINIMUM_BOOKING_DATE
    ):
        raise FormValidationError(
            "Choose a viewing date from tomorrow onward in 2026 or later."
        )
    time_slot = values.get("time_slot", "").strip()
    allowed_slots = {
        "09:00 AM - 10:00 AM",
        "10:00 AM - 11:00 AM",
        "01:30 PM - 02:30 PM",
        "03:30 PM - 04:30 PM",
        "05:00 PM - 06:00 PM",
    }
    if time_slot not in allowed_slots:
        raise FormValidationError("Choose one of the available viewing times.")
    lease_months = _bounded_integer(
        values.get("lease_months"), 1, 120, "Lease duration"
    )
    message = _normalize_whitespace(values.get("message", ""))
    if len(message) > 2000:
        raise FormValidationError("Message must be 2000 characters or fewer.")
    if _is_repeated_character(message):
        raise FormValidationError("Please enter a meaningful message.")
    return viewing_date, time_slot, lease_months, message


def _normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _is_repeated_character(value: str) -> bool:
    characters = "".join(value.split()).casefold()
    return len(characters) >= 3 and len(set(characters)) == 1


def _normalize_phone(value: str) -> str:
    number = value.strip()
    if not re.fullmatch(r"[+0-9()\s-]+", number):
        raise FormValidationError(
            "Enter a Philippine mobile number starting with 09 or +639."
        )
    compact = re.sub(r"[\s()-]", "", number)
    if re.fullmatch(r"09\d{9}", compact):
        normalized = compact
    elif re.fullmatch(r"\+639\d{9}", compact):
        normalized = f"0{compact[3:]}"
    else:
        raise FormValidationError(
            "Enter 11 digits starting with 09 or +639 followed by 9 digits."
        )

    subscriber = normalized[2:]
    ascending = all(
        (int(subscriber[index]) + 1) % 10 == int(subscriber[index + 1])
        for index in range(len(subscriber) - 1)
    )
    descending = all(
        (int(subscriber[index]) - 1) % 10 == int(subscriber[index + 1])
        for index in range(len(subscriber) - 1)
    )
    if len(set(subscriber)) == 1 or ascending or descending:
        raise FormValidationError("Enter a valid Philippine mobile number.")
    return normalized


def _parse_form_date(value: str, message: str) -> date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise FormValidationError(message)
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise FormValidationError(message) from error


def _bounded_integer(
    value: str | None, minimum: int, maximum: int, label: str
) -> int:
    try:
        number = int(value or "")
    except ValueError as error:
        raise FormValidationError(f"{label} must be a whole number.") from error
    if not minimum <= number <= maximum:
        raise FormValidationError(
            f"{label} must be between {minimum} and {maximum}."
        )
    return number


def _query_number(name: str) -> float | None:
    value = request.args.get(name, "").strip()
    if not value:
        return None
    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", value):
        abort(
            422,
            description=(
                f"{name.replace('_', ' ').capitalize()} must be a non-negative "
                "amount with at most two decimal places."
            ),
        )
    try:
        number = float(value)
    except ValueError:
        abort(422, description=f"{name.replace('_', ' ').capitalize()} must be a number.")
    if not math.isfinite(number) or not 0 <= number <= 1_000_000_000:
        abort(
            422,
            description=(
                f"{name.replace('_', ' ').capitalize()} must be between "
                "₱0 and ₱1,000,000,000."
            ),
        )
    return number


def _query_integer(
    name: str,
    *,
    default: int = 1,
    minimum: int,
    maximum: int,
    optional: bool = False,
) -> int | None:
    value = request.args.get(name, "").strip()
    if optional and not value:
        return None
    if not value:
        return default
    try:
        number = int(value)
    except ValueError:
        abort(422, description=f"{name.replace('_', ' ').capitalize()} must be a whole number.")
    if not minimum <= number <= maximum:
        abort(
            422,
            description=(
                f"{name.replace('_', ' ').capitalize()} must be between "
                f"{minimum} and {maximum}."
            ),
        )
    return number


def _optional_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _rate_limiter() -> InquiryRateLimiter:
    return current_app.extensions["rate_limiter"]


def _reserve_submission() -> Reservation:
    return _rate_limiter().reserve(request.remote_addr or "unknown")
