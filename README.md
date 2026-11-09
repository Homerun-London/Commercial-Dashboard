# Homerun Commercial Dashboard

Membership Overview, Intro Offers (Overview, Comparison & Conversion) and
Class Utilization (with direct vs third-party bookings) for Homerun Mayfair,
built on Mariana Tek data. Ported from the Enmei Commercial Dashboard: same pipeline
logic, set up for a single studio.

## How it fits together

1. `scripts/build_membership_dashboard_data.py` pulls `membership_instances`,
   rebuilds weekly history and writes the `Membership_*` tabs (IDs and
   counts only, no client PII).
2. `scripts/build_class_performance_data.py` pulls class sessions,
   reservations and spot holds and writes the `ClassPerformance_*` tabs.
3. `scripts/build_intro_offers_dashboard_data.py` rebuilds every customer's
   purchase timeline and writes `IntroOffers_Starters` (one row per intro
   starter, IDs only).
4. All three run daily via GitHub Actions (8:00, 8:30 and 9:00 UTC). Every
   write goes through `scripts/sheet_writer.py`, which clears any filter or
   hidden rows on the tab first (they hide rows from the dashboard).
5. `docs/index.html` reads the Sheet live through Google's `gviz` endpoint.

## Setup

1. **Google Sheet:** create a blank Sheet, share it with the service
   account's `client_email` (Editor), then File -> Share -> Publish to web
   (Entire document). Paste its ID into `config.py` (`DASHBOARD_SHEET_ID`)
   and into `docs/index.html` (`SHEET_ID`, near the top).
2. **Secrets** (Settings -> Secrets and variables -> Actions):
   `MARIANA_API_KEY`, `MARIANA_BASE_URL`, `GOOGLE_SERVICE_ACCOUNT_JSON`.
3. **First runs:** Actions -> run each workflow with `debug_only = true`
   and read the log. It lists every membership name with its tier and every
   class type. Add anything that isn't a real membership to
   `EXCLUDED_MEMBERSHIP_KEYWORDS`, and anything that isn't a coach-led
   class to `EXCLUDED_CLASS_TYPES` in `config.py`. Then run again with
   `debug_only = false` to write to the Sheet.
4. **Publish:** Settings -> Pages -> Deploy from branch -> `main` -> `/docs`.

The published Sheet can be queried by anyone with its ID, which is why only
aggregated numbers and IDs are written to it.
