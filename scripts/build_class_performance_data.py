"""
build_class_performance_data.py  (Homerun)

Class Performance from Mariana Tek's RAW endpoints (class_sessions,
reservations, spot_holds), ported from Enmei's build_class_performance_raw.py
-- the version validated class-by-class against report 294 (18,326 classes,
every compared column 100%). The report comparison is not carried over.

HOW EACH COLUMN IS BUILT (unchanged from Enmei):
  Checked In Reservations      = reservations with status "check in"
  Late Cancelled Reservations  = status "penalty cancel"
  No Showed Reservations       = status "penalty no show"
      "Graced" (fee-waived) cancels / no-shows are NOT counted.
  Admin Holds                  = unreleased spot_holds of type "admin"
  Unavailable Holds            = unreleased spot_holds of type "unavailable"
  Layout / Actual Capacity     = class_sessions.capacity
  % Utilization                = checked in / capacity

Changed for Homerun: single studio. The Mariana Tek location ID is looked up
by STUDIO_NAME on every run (or the only location is used if there is just
one). Every run prints all class types and reservation statuses seen.

Third-party bookings: paid bookings carrying Mariana Tek's ClassPass /
Wellhub / FitReserve system tags (matched by slug). Each run prints a check
that the tags agree with "booking has no membership/credit transaction",
the evidence Enmei used to trust the split.

Writes: ClassPerformance_Sessions, ClassPerformance_History,
ClassPerformance_Cancellations, ClassPerformance_CancellationsDaily.

Required environment variables:
    MARIANA_API_KEY, MARIANA_BASE_URL, GOOGLE_SERVICE_ACCOUNT_JSON
Optional:
    CP_RAW_HISTORY_START   shorten a TEST run (debug only)
    CP_RAW_PAGE_SIZE       page size for raw endpoints (default 500)
    DEBUG_ONLY_SKIP_SHEET_WRITE=true   print instead of writing to the Sheet
"""

import os
import sys
import time
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
import class_performance_common as base  # noqa: E402
from config import (  # noqa: E402
    STUDIO_NAME, DASHBOARD_SHEET_ID, CLASS_HISTORY_START, INSTRUCTOR_NAME_OVERRIDES, THIRD_PARTY_TAG_SLUGS,
    TAB_CLASS_SESSIONS, TAB_CLASS_HISTORY, TAB_CLASS_CANCELLATIONS, TAB_CLASS_CANCELLATIONS_DAILY,
)

# Every reservation status we know about. Anything else seen in the data is
# reported loudly, because an unrecognised status would silently count as
# zero toward paid visits (e.g. if Mariana Tek once used a different name
# for a late cancel).
KNOWN_STATUSES = {
    "check in", "standard cancel", "graced cancel", "penalty cancel",
    "penalty no show", "graced no show", "no fee no show",
    "removed", "class cancelled", "pending",
    # A staff removal that carried a penalty. Report 294 does not count it as
    # a late cancel or no-show (verified: 100% parity without counting it),
    # and there are only a handful (4 in 22 months). Known, deliberately not counted.
    "penalty removed",
}

PAGE_SIZE = int(os.environ.get("CP_RAW_PAGE_SIZE", "500"))

# Reservation status -> what it means for the paid-visit logic.
STATUS_CHECKED_IN = "check in"
STATUS_LATE_CANCEL = "penalty cancel"
STATUS_NO_SHOW = "penalty no show"
STATUS_LATE_CANCEL_WAIVED = "graced cancel"
STATUS_NO_SHOW_WAIVED = "graced no show"


# --------------------------------------------------------------------------
# API helpers
# --------------------------------------------------------------------------
def api_get(resource, params, max_retries=6):
    """GET with retries for network errors, 429 (honouring Retry-After) and 5xx."""
    url = f"{base.BASE_URL}/{resource}"
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, headers=base.HEADERS, params=params, timeout=60)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, OSError) as e:
            wait = 2 ** attempt
            print(f"  {resource}: network error ({e}), retrying in {wait}s...")
            time.sleep(wait)
            continue
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 2 ** attempt))
            print(f"  {resource}: rate limited, waiting {wait}s...")
            time.sleep(wait)
            continue
        if resp.status_code >= 500:
            wait = 2 ** attempt
            print(f"  {resource}: HTTP {resp.status_code}, retrying in {wait}s...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"Failed to fetch {resource} {params} after {max_retries} attempts")


def pull_pages(resource, params, label=""):
    """Yield every record of a paginated endpoint."""
    page = 1
    while True:
        payload = api_get(resource, {**params, "page": page, "page_size": PAGE_SIZE})
        data = payload.get("data", [])
        yield from data
        pagination = (payload.get("meta") or {}).get("pagination") or {}
        total_pages = pagination.get("pages")
        if total_pages is not None:
            if page >= total_pages:
                return
        elif len(data) < PAGE_SIZE:
            return
        page += 1


def rel_id(record, name):
    """ID of a to-one relationship, or None."""
    data = ((record.get("relationships") or {}).get(name) or {}).get("data")
    if isinstance(data, dict):
        return data.get("id")
    return None


def rel_ids(record, name):
    """IDs of a to-many relationship (empty list if absent)."""
    data = ((record.get("relationships") or {}).get(name) or {}).get("data")
    if isinstance(data, list):
        return [d.get("id") for d in data if isinstance(d, dict) and d.get("id") is not None]
    if isinstance(data, dict) and data.get("id") is not None:
        return [data["id"]]
    return []


def month_windows(start, end):
    """Inclusive (start, end) ISO date windows, one per calendar month."""
    cur, last = pd.Timestamp(start), pd.Timestamp(end)
    while cur <= last:
        win_end = min(cur + pd.offsets.MonthEnd(0), last)
        yield cur.date().isoformat(), win_end.date().isoformat()
        cur = win_end + pd.Timedelta(days=1)


# --------------------------------------------------------------------------
# Lookups and holds
# --------------------------------------------------------------------------
def load_class_categories():
    """class_session_type id -> category name (report's 'Class Category').
    Not used by any metric today; failure here must not stop the run."""
    try:
        cat_names = {c["id"]: c["attributes"].get("name", "")
                     for c in pull_pages("class_session_categories", {})}
        out = {}
        for t in pull_pages("class_session_types", {}):
            cat_id = rel_id(t, "class_session_category") or t["attributes"].get("class_session_category")
            out[t["id"]] = cat_names.get(str(cat_id), cat_names.get(cat_id, ""))
        return out
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: couldn't load class categories ({e}); 'Class Category' left blank.")
        return {}


def load_instructor_display_names():
    """employee_public_profile id -> schedule_display_name.
    Report 294 shows coaches by the display name set on their schedule
    profile ('Sam', 'Tammy', 'Olivia A.', 'Recovery Boots'), NOT by their
    legal first name -- so nicknames and two coaches sharing a first name
    come out right when we use the same field."""
    out = {}
    try:
        for p in pull_pages("employee_public_profiles", {}):
            name = (p["attributes"].get("schedule_display_name") or "").strip()
            if name:
                out[p["id"]] = name
        print(f"Loaded {len(out)} coach display names.")
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: couldn't load coach display names ({e}); falling back to the first-name rule.")
    return out


def load_third_party_tags():
    """reservation tag id -> platform label (ClassPass / Wellhub / ...),
    matched by slug (system tags), never by ID. Failure here must not stop
    the run: every booking is then treated as direct, loudly."""
    out = {}
    try:
        seen = []
        for t in pull_pages("reservation_tags", {}):
            slug = (t["attributes"].get("slug") or "").strip().lower()
            seen.append(slug or t["attributes"].get("name", ""))
            if slug in THIRD_PARTY_TAG_SLUGS:
                out[t["id"]] = THIRD_PARTY_TAG_SLUGS[slug]
        print(f"Reservation tags on this account: {seen}")
        print(f"Third-party tags matched: {sorted(set(out.values())) or 'NONE'}")
        if not out:
            print("!!! WARNING: no ClassPass/Wellhub/FitReserve tags found -- every booking will count as DIRECT.")
    except Exception as e:  # noqa: BLE001
        print(f"!!! WARNING: couldn't load reservation tags ({e}) -- every booking will count as DIRECT.")
    return out


def load_holds():
    """Unreleased admin / unavailable holds, counted per class. The report
    counts only holds that have not been released (validated: 100% match),
    and released holds stay in the data with a release_date, so nothing is
    lost."""
    active_admin = Counter()
    active_unavailable = Counter()
    for hold_type, bucket in (("admin", active_admin), ("unavailable", active_unavailable)):
        for h in pull_pages("spot_holds", {"hold_type": hold_type}):
            cs = rel_id(h, "class_session")
            if cs is not None and not h["attributes"].get("release_date"):
                bucket[cs] += 1
    print(f"Holds loaded: {sum(active_admin.values())} unreleased admin, {sum(active_unavailable.values())} unreleased unavailable.")
    return active_admin, active_unavailable


# --------------------------------------------------------------------------
# Per-location pulls
# --------------------------------------------------------------------------
def pull_sessions(loc_id, start, end, label=""):
    """All class sessions (held AND cancelled) for one location. Date
    filters work on this endpoint (min_date / max_date); validated."""
    seen = {}
    for w_start, w_end in month_windows(start, end):
        for rec in pull_pages("class_sessions", {"location": loc_id, "min_date": w_start, "max_date": w_end}):
            seen[rec["id"]] = rec
    print(f"  [{label}] class_sessions: {len(seen)} pulled ({start} -> {end})", flush=True)
    return list(seen.values())


PAID_STATUSES = {"check in", "penalty cancel", "penalty no show"}
PLATFORM_ORDER = list(dict.fromkeys(THIRD_PARTY_TAG_SLUGS.values()))


def aggregate_reservations(loc_id, start, end, label="", tag_map=None, tp_check=None):
    """Stream reservations and keep only per-class counters (never the rows
    themselves). Pulled by class date + location, then counted client-side
    -- the reservations 'status' filter is not reliable.

    Returns (status_counts, platform_counts): platform counts are kept in a
    SEPARATE counter (never mixed into statuses -- that caused a false
    "UNKNOWN STATUS" warning at Enmei) and only cover PAID bookings
    (check in, late cancel, no-show), since % direct is a share of paid visits."""
    tag_map = tag_map or {}
    counts = defaultdict(Counter)          # class id -> Counter of status keys
    platforms = defaultdict(Counter)       # class id -> Counter of platform labels
    n = 0
    for w_start, w_end in month_windows(start, end):
        for rec in pull_pages("reservations", {
            "class_session_location": loc_id,
            "class_session_min_date": w_start,
            "class_session_max_date": w_end,
        }):
            n += 1
            at = rec["attributes"]
            cs = rel_id(rec, "class_session")
            if cs is None:
                continue
            status = at.get("status")
            counts[cs][status] += 1
            if status == "pending":
                counts[cs][f"pending:{at.get('reservation_type')}"] += 1
            if status in PAID_STATUSES:
                labels = {tag_map[t] for t in rel_ids(rec, "tags") if t in tag_map}
                platform = next((p for p in PLATFORM_ORDER if p in labels), None)
                if platform:
                    platforms[cs][platform] += 1
                if tp_check is not None:
                    rels = rec.get("relationships") or {}
                    has_rel_fields = "membership_transactions" in rels or "credit_transactions" in rels
                    no_txn = not rel_ids(rec, "membership_transactions") and not rel_ids(rec, "credit_transactions")
                    tp_check["paid"] += 1
                    tp_check["tagged"] += bool(platform)
                    if has_rel_fields:
                        tp_check["checkable"] += 1
                        tp_check["no_txn"] += no_txn
                        tp_check["tagged_and_no_txn"] += bool(platform) and no_txn
        print(f"  [{label}] reservations through {w_end}: {n} so far", flush=True)
    print(f"  [{label}] {n} reservations aggregated.", flush=True)
    return counts, platforms


# --------------------------------------------------------------------------
# Build the report-shaped session table
# --------------------------------------------------------------------------
def first_name(full_name):
    """FALLBACK only (used when a class has no schedule-profile link):
    first name, with exceptions from INSTRUCTOR_NAME_OVERRIDES in config.py
    (keyed by full name or by first name)."""
    full_name = str(full_name).strip()
    if not full_name:
        return ""
    first = full_name.split()[0]
    return INSTRUCTOR_NAME_OVERRIDES.get(full_name) or INSTRUCTOR_NAME_OVERRIDES.get(first) or first


def instructors_text(value):
    if value is None:
        return ""
    parts = list(value) if isinstance(value, (list, tuple)) else str(value).split(",")
    return ",".join(first_name(p) for p in parts if str(p).strip())


def resolve_location():
    """{STUDIO_NAME: location_id}, looked up from Mariana Tek by name."""
    locs = list(pull_pages("locations", {}))
    names = {l["id"]: (l["attributes"].get("name") or "").strip() for l in locs}
    print("Locations on this account: " + ", ".join(f"{n} ({i})" for i, n in names.items()))
    match = [i for i, n in names.items() if n.lower() == STUDIO_NAME.lower()]
    if match:
        return {STUDIO_NAME: match[0]}
    if len(names) == 1:
        only_id, only_name = next(iter(names.items()))
        print(f"'{STUDIO_NAME}' not found by name; using the only location, '{only_name}' ({only_id}).")
        return {STUDIO_NAME: only_id}
    sys.exit(f"Couldn't find a location called '{STUDIO_NAME}'. Set STUDIO_NAME in config.py to one of the names above.")


def build_raw_frames(start, end):
    categories = load_class_categories()
    display_names = load_instructor_display_names()
    name_source = Counter()
    active_admin, active_unavailable = load_holds()
    tag_map = load_third_party_tags()
    tp_check = Counter()

    LOCATIONS = resolve_location()
    fetched = []
    for loc_name, loc_id in LOCATIONS.items():
        t0 = time.time()
        print(f"\nPulling {loc_name} ({start} -> {end}) ...", flush=True)
        recs = pull_sessions(loc_id, start, end, loc_name)
        counts, platforms = aggregate_reservations(loc_id, start, end, loc_name, tag_map, tp_check)
        print(f"[{loc_name}] finished in {time.time() - t0:.0f}s", flush=True)
        fetched.append((loc_name, recs, counts, platforms))

    session_rows, cancelled_rows = [], []
    for loc_name, recs, counts, platforms in fetched:
        print(f"[{loc_name}] {len(recs)} sessions ({sum(1 for r in recs if r['attributes'].get('cancellation_datetime'))} cancelled)")

        for rec in recs:
            at = rec["attributes"]
            if at.get("cancellation_datetime"):
                cancelled_rows.append({
                    "location": loc_name, "start_date": at.get("start_date"),
                    "start_time": at.get("start_time"), "class_type": at.get("class_type_display"),
                    "cancellation_datetime": at.get("cancellation_datetime"),
                    "instructor_names": at.get("instructor_names"),
                    "class_type_lower": str(at.get("class_type_display") or "").strip().lower(),
                })
                continue

            cs = rec["id"]
            c = counts.get(cs, Counter())
            capacity = at.get("capacity") or 0
            checked_in = c[STATUS_CHECKED_IN]
            sub = (rec.get("relationships") or {}).get("substituted_instructors", {}).get("data")
            profile_ids = rel_ids(rec, "employee_public_profiles")
            shown = [display_names[i] for i in profile_ids if i in display_names]
            if profile_ids and len(shown) == len(profile_ids):
                instructors = ",".join(shown)
                name_source["schedule display name"] += 1
            else:
                instructors = instructors_text(at.get("instructor_names"))
                name_source["first-name fallback"] += 1
            start_date = at.get("start_date")
            session_rows.append({
                "Location": loc_name,
                "Class Date": start_date,
                "Class Time": at.get("start_time"),
                "Class Day of Week": pd.Timestamp(start_date).day_name() if start_date else "",
                "Instructors": instructors,
                "Has Substitute?": bool(sub),
                "Classroom": at.get("classroom_display"),
                "Class Tags": "",
                "Class Is Free?": bool(at.get("is_free_class")),
                "Class Type": at.get("class_type_display"),
                "Class Category": categories.get(rel_id(rec, "class_session_type"), ""),
                "Pending Standard Reservations": c["pending:standard"],
                "Pending Standby Reservations": c["pending:standby"],
                "Pending Waitlist Reservations": c["pending:waitlist"],
                "Checked In Reservations": checked_in,
                "Late Cancelled Reservations": c[STATUS_LATE_CANCEL],
                "No Showed Reservations": c[STATUS_NO_SHOW],
                "Admin Holds": active_admin.get(cs, 0),
                "Unavailable Holds": active_unavailable.get(cs, 0),
                "Layout Capacity": capacity,
                "Actual Capacity": capacity,
                "% Utilization": round(100 * checked_in / capacity, 1) if capacity else np.nan,
                # extras, not in report 294
                "Waived Late Cancels": c[STATUS_LATE_CANCEL_WAIVED],
                "Waived No Shows": c[STATUS_NO_SHOW_WAIVED],
                # Third-party PAID bookings (Direct = Paid Visits - Third-Party, added in cleaning)
                "ClassPass Bookings": platforms.get(cs, Counter())["ClassPass"],
                "Wellhub Bookings": platforms.get(cs, Counter())["Wellhub"],
                "Other Third-Party Bookings": platforms.get(cs, Counter())["Other Third-Party"],
            })

    print(f"Instructor names came from: {dict(name_source)}")

    status_totals = Counter()
    for _, _, counts, _ in fetched:
        for per_class in counts.values():
            status_totals.update({k: v for k, v in per_class.items() if not str(k).startswith("pending:")})
    print("\nReservation statuses seen (count):")
    for st, n in status_totals.most_common():
        print(f"  {str(st):<20} {n:>8}{'' if st in KNOWN_STATUSES else '   <-- UNKNOWN STATUS'}")
    unknown = {k: v for k, v in status_totals.items() if k not in KNOWN_STATUSES}
    if unknown:
        print(f"\n!!! WARNING: {len(unknown)} reservation status(es) not recognised: {unknown}. "
              f"They are NOT counted as paid visits. Tell Claude so they can be classified.")
    print("\nThird-party check (paid bookings only):")
    print(f"  paid bookings: {tp_check['paid']}, tagged third-party: {tp_check['tagged']}")
    if tp_check["checkable"]:
        print(f"  with no membership/credit transaction: {tp_check['no_txn']}; tagged AND no transaction: {tp_check['tagged_and_no_txn']}")
        if tp_check["tagged"] == tp_check["no_txn"] == tp_check["tagged_and_no_txn"]:
            print("  -> tags and 'no transaction' agree exactly: third-party split is trustworthy.")
        else:
            print("  -> tags and 'no transaction' DIFFER: tell Claude these numbers before relying on % direct.")
    else:
        print("  (transaction links not present on reservations -- agreement check skipped)")

    raw_sessions = pd.DataFrame(session_rows)
    if len(raw_sessions):
        print("\nClass types seen (count) -- add any that aren't coach-led classes to EXCLUDED_CLASS_TYPES in config.py:")
        for ct, n in raw_sessions["Class Type"].fillna("(blank)").value_counts().items():
            flag = "   <-- excluded" if str(ct).strip().lower() in base.EXCLUDED_CLASS_TYPES else ""
            print(f"  {n:>6}  {ct}{flag}")
    cancelled = pd.DataFrame(cancelled_rows)
    if len(cancelled):
        cancelled["start_date"] = pd.to_datetime(cancelled["start_date"], errors="coerce")
    else:
        cancelled = pd.DataFrame(columns=["location", "start_date", "cancellation_datetime", "class_type_lower"])
        cancelled["start_date"] = pd.to_datetime(cancelled["start_date"])
    return raw_sessions, cancelled


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main():
    debug = os.environ.get("DEBUG_ONLY_SKIP_SHEET_WRITE") == "true"
    if "PASTE" in DASHBOARD_SHEET_ID and not debug:
        sys.exit("Set DASHBOARD_SHEET_ID in config.py first (or run with debug_only=true).")
    end_date = date.today().isoformat()
    custom_start = os.environ.get("CP_RAW_HISTORY_START", "").strip()
    start_date = custom_start or CLASS_HISTORY_START

    raw_sessions, cancelled = build_raw_frames(start_date, end_date)
    if raw_sessions.empty:
        sys.exit("No held classes found in this window -- nothing to build yet.")
    sessions_df = base.clean_sessions_df(raw_sessions)
    print(f"\nRaw build: {len(sessions_df)} real class-session rows after cleaning.")

    cancellations_df = base.build_cancellations_history(cancelled, start_date)
    print(f"Raw build: {int(cancellations_df['cancelled_classes'].sum()) if len(cancellations_df) else 0} cancelled classes since {start_date}.")
    cancellations_daily_df = base.build_cancellations_daily(cancelled, start_date)
    history_df = base.build_class_history(sessions_df)
    sessions_export = sessions_df.drop(columns=["Instructor_List"]).copy()

    if custom_start and not debug:
        sys.exit(f"Refusing to write: this run used a shortened history start ({custom_start}) and would overwrite "
                 f"the Sheet tabs with partial history. Re-run with debug_only=true, or leave history_start blank.")

    if debug:
        print("\nDEBUG_ONLY_SKIP_SHEET_WRITE is set -- skipping the actual write.")
        print("\n--- Sessions (head) ---")
        print(sessions_export.head(15).to_string())
        print("\n--- History ---")
        print(history_df.tail(12).to_string())
        print("\n--- Cancellations ---")
        print(cancellations_df.tail(12).to_string())
        print("\n--- Cancellations daily (last 10) ---")
        print(cancellations_daily_df.tail(10).to_string())
        return

    base.write_to_sheet(sessions_export, TAB_CLASS_SESSIONS)
    base.write_to_sheet(history_df, TAB_CLASS_HISTORY)
    base.write_to_sheet(cancellations_df, TAB_CLASS_CANCELLATIONS)
    base.write_to_sheet(cancellations_daily_df, TAB_CLASS_CANCELLATIONS_DAILY)


if __name__ == "__main__":
    main()
