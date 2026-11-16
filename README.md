# Emin & Mila's Rentals — Python website

This Flask/Jinja2 website recreates the Flutter rental site as a responsive
server-rendered application. It includes the home, housing search, property
details, about, and contact pages; application and viewing-request forms;
Supabase REST API listing reads and inquiry writes; consistent self-hosted SVG
icons; reduced-motion-aware transitions and scroll reveals; accessible inline
field validation; local SQLite in development; Supabase as the production
source of truth; a 90-second in-process listing cache; and inquiry rate
limiting.

## Run locally (Windows PowerShell)

From this `python_web` directory:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Development automatically initializes a local SQLite database at
`instance/rentals.sqlite3`. Add local listing rows to its `properties` table to
show homes; inquiry submissions are saved to its `inquiries` table. Use a
SQLite database browser to maintain local listing rows. This local database is
not synchronized to Supabase. Production uses Supabase and requires
`SUPABASE_URL` and `SUPABASE_PUBLISHABLE_KEY` in the hosting provider's
environment settings. Use only a publishable key (or legacy anon key); the key
is used on the server and is never embedded in browser code. Then start the
site:

```powershell
.\.venv\Scripts\python.exe -m flask --app app.main run --debug
```

Open <http://127.0.0.1:8000>. Listings are empty until local property data is
added. The `LOCAL_DATABASE_PATH` setting can point to another SQLite file.

For Render, set the Build Command to `pip install -r requirements.txt` and the
Start Command to
`waitress-serve --listen=0.0.0.0:$PORT --call app.main:create_app`. Set
`APP_ENV=production`, `SESSION_SECRET`, `SUPABASE_URL`, and
`SUPABASE_PUBLISHABLE_KEY` in the Render environment settings. Do not upload
`.env`; it is for local development and is excluded from Git. Production
startup rejects missing API settings, non-HTTPS Supabase URLs, and known
service-role keys. The production session secret must be persistent, random,
and at least 32 characters long.

## Supabase setup

Production reads only from the configured `public.public_listings` object by
default. Set `SUPABASE_LISTING_VIEW` to `public_listings` to match the Flutter
app's current integration. The table or view must expose the fields selected in
`app/repository.py` and should enforce public availability and row-level
security. Do not query `public.units` directly or apply the legacy
`supabase/migrations/20261014102400_public_property_listings.sql` migration
for this website.

Set only the project's public/publishable key in `SUPABASE_PUBLISHABLE_KEY`.
Never put a service-role key in `.env` or browser code. Contact, application,
and viewing requests use the same `tenant_inquiries` schema-first insert and
missing-table fallback to `public.inquiries` as the Dart app. The anonymous
role must have the intended insert permissions; the server does not elevate
permissions or bypass RLS.

The configured project returned PostgREST `42501` for anonymous reads from
`public.public_listings`. The project owner confirmed that every row in this
table is approved for public display and that its public-read RLS policy is
present. Apply
`supabase/migrations/20261112211500_grant_anon_public_listings_read.sql` in
the Supabase SQL Editor to grant `anon` SELECT on this listing object only.
The migration does not change RLS or grant access to `public.units`; RLS
policies still determine which rows the anonymous role can read. The website
does not use a service-role key.

The forms validate contact details, require consent and a confirmation step,
and limit each client address to three successful/in-flight submissions per
30 minutes by default. Change `INQUIRY_LIMIT` and `INQUIRY_WINDOW_SECONDS` to
adjust this. The in-memory limiter and cache are per worker and reset on
restart; use a shared store if deploying multiple workers or instances. Listing
results are cached for 90 seconds by filter/sort/page. The Properties page
appends the next 12 matching homes as you scroll, with a **Load more homes**
control as a fallback; use **Refresh results** to bypass the current cached
entries when listings have just changed.

Inquiry, viewing, and application forms normalize names and occupations to
uppercase, lowercase email addresses, and normalize whitespace. They accept
Philippine mobile numbers in `09...` or `+639...` format and store them as
`09...`. Email domains are not restricted; malformed addresses and the common
disposable domains `mailinator.com`, `tempmail.com`, and `10minutemail.com`
(including subdomains) are rejected. Application income accepts at most two
decimal places; booking dates must be in the future. The site has no passwords,
age fields, or file uploads.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `APP_ENV` | `development` | Set to `production` to enable secure-only session cookies |
| `SESSION_SECRET` | random per process | Signing key for the session and CSRF token; required in production |
| `SUPABASE_URL` | none | Supabase project origin; required in production |
| `SUPABASE_PUBLISHABLE_KEY` | none | Server-side public/publishable key (legacy `SUPABASE_ANON_KEY` is also accepted); required in production |
| `LOCAL_DATABASE_PATH` | `instance/rentals.sqlite3` | SQLite database path used in development only |
| `SUPABASE_LISTING_VIEW` | `public_listings` | Public view name under the `public` schema |
| `LISTING_CACHE_TTL_SECONDS` | `90` | In-process cache lifetime; `0` disables caching |
| `INQUIRY_LIMIT` | `3` | Maximum submissions from one client in the rate-limit window |
| `INQUIRY_WINDOW_SECONDS` | `1800` | Rate-limit window duration |

Use a persistent random `SESSION_SECRET` of at least 32 characters and HTTPS
in production. When hosted
behind a reverse proxy, configure its trusted client-IP handling carefully;
the application does not trust arbitrary forwarded client-IP headers.

The Terms and Conditions page currently contains draft wording. Have it
reviewed and approved before deployment; the forms require users to accept it.

## Tests

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest
```
 