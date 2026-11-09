"""
build_intro_offers_dashboard_data.py  (Homerun)

Who started on an intro offer, what did they do next, and did they EVER
convert? Ported from Enmei's intro offers pipeline (same outcome rules),
but built on RAW data only -- no table reports -- so attendance comes from
real check-ins on the reservations endpoint.

Two kinds of intro offer:
  - Membership intro: INTRO_MEMBERSHIP_NAMES in config.py ("1 week unlimited").
  - Credit / class-pack intro: INTRO_CREDIT_NAMES in config.py, or, while that
    list is empty, any credit purchase Mariana Tek flags is_intro_offer.
    Every run prints all products with their intro flag, so the real name
    can be pinned in config once confirmed. (At Enmei the flag missed
    1 Week Unlimited, which is why membership intros match by name.)

Starter = a customer whose FIRST-EVER purchase is an intro offer.

Lifetime outcome (from purchases AFTER the intro; mutually exclusive, in
priority order -- same as Enmei):
  1. Converted to Membership   -- ever bought a real recurring membership
  2. Converted to Pack         -- ever bought a pack of PACK_CONVERSION_MIN+
  3. Tried Another Intro Offer -- bought another intro product, never converted
  4. Casual Re-engagement Only -- drop-ins / small packs / anything else
  5. No Further Purchase
Conversion rate (canonical, computed on the dashboard) = share of starters
who converted to a membership OR a pack. The dashboard shows "No Further
Purchase" within 30 days of the intro as Pending.

Classes attended on the intro = check-ins from the intro purchase date
until the day before their next purchase, capped at INTRO_ATTENDANCE_MAX_DAYS
days (an approximation: raw data doesn't link a check-in to the product
that paid for it without tracing every transaction).

Writes one tab, IntroOffers_Starters (one row per starter, IDs only, no
PII); the dashboard derives every KPI, the outcome flow, the weekly trend
and the attendance breakdown from it, client-side.

Required environment variables:
    MARIANA_API_KEY, MARIANA_BASE_URL, GOOGLE_SERVICE_ACCOUNT_JSON
Optional:
    DEBUG_ONLY_SKIP_SHEET_WRITE=true   print instead of writing to the Sheet
"""

import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from config import (  # noqa: E402
    DASHBOARD_SHEET_ID, STUDIO_NAME, EXCLUDED_MEMBERSHIP_KEYWORDS,
    INTRO_MEMBERSHIP_NAMES, INTRO_CREDIT_NAMES, CASUAL_CREDIT_NAMES,
    PACK_CONVERSION_MIN, INTRO_ATTENDANCE_MAX_DAYS, TAB_INTRO_STARTERS,
)
from sheet_writer import write_to_sheet  # noqa: E402

BASE_URL = os.environ["MARIANA_BASE_URL"].rstrip("/")
HEADERS = {"Authorization": f"Bearer {os.environ['MARIANA_API_KEY']}"}
PAGE_SIZE = 500

INTRO_MEMBERSHIPS = {n.strip().lower() for n in INTRO_MEMBERSHIP_NAMES}
INTRO_CREDITS = {n.strip().lower() for n in INTRO_CREDIT_NAMES}
CASUAL_CREDITS = {n.strip().lower() for n in CASUAL_CREDIT_NAMES}
NON_MEMBERSHIP_KEYWORDS = [k.lower() for k in EXCLUDED_MEMBERSHIP_KEYWORDS]
PACK_PATTERN = re.compile(r"(\d+)\s*(?:x\s*)?(?:classes|class|credits|credit|pack)\b")


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
def api_get(resource, params, max_retries=6):
    url = f"{BASE_URL}/{resource}"
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, headers=HEADERS, params=params, timeout=60)
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


def pull_pages(resource, params=None):
    page = 1
    while True:
        payload = api_get(resource, {**(params or {}), "page": page, "page_size": PAGE_SIZE})
        data = payload.get("data", [])
        yield from data
        pages = ((payload.get("meta") or {}).get("pagination") or {}).get("pages")
        if (pages is not None and page >= pages) or (pages is None and len(data) < PAGE_SIZE):
            return
        page += 1


def rel_id(record, name):
    data = ((record.get("relationships") or {}).get(name) or {}).get("data")
    return data.get("id") if isinstance(data, dict) else None


def month_windows(start, end):
    cur, last = pd.Timestamp(start), pd.Timestamp(end)
    while cur <= last:
        win_end = min(cur + pd.offsets.MonthEnd(0), last)
        yield cur.date().isoformat(), win_end.date().isoformat()
        cur = win_end + pd.Timedelta(days=1)


def to_utc(series):
    return pd.to_datetime(series, utc=True, format="ISO8601", errors="coerce")


# --------------------------------------------------------------------------
# Purchases -> one timeline per customer
# --------------------------------------------------------------------------
def classify_membership(name):
    n = (name or "").strip().lower()
    if n in INTRO_MEMBERSHIPS:
        return "intro_offer"
    if any(k in n for k in NON_MEMBERSHIP_KEYWORDS):
        return "casual"          # promos/perks: not a real membership decision
    return "membership"


def classify_credit(name, is_intro_flag):
    n = (name or "").strip().lower()
    if (INTRO_CREDITS and n in INTRO_CREDITS) or (not INTRO_CREDITS and bool(is_intro_flag)):
        return "intro_offer"
    if n in CASUAL_CREDITS:
        return "casual"
    m = PACK_PATTERN.search(n)
    if m:
        return "pack_5plus" if int(m.group(1)) >= PACK_CONVERSION_MIN else "pack_small"
    return "casual"


def print_catalog(title, df, name_col, flag_col):
    print(f"\n{title} (count, how many flagged is_intro_offer -> category):")
    if df.empty:
        print("  (none)")
        return
    g = df.groupby(df[name_col].fillna("(blank)")).agg(n=("category", "size"),
                                                         flagged=(flag_col, lambda s: int(s.fillna(False).astype(bool).sum())),
                                                         category=("category", "first"))
    for name, r in g.sort_values("n", ascending=False).iterrows():
        print(f"  {r['n']:>6}  flagged {r['flagged']:>5}  {name}  ->  {r['category']}")


def pull_events():
    print("Pulling membership_instances...")
    mem = []
    for r in pull_pages("membership_instances"):
        a = r["attributes"]
        mem.append({"user_id": rel_id(r, "user"), "event_date": a.get("purchase_date") or a.get("start_date"),
                    "product_name": a.get("membership_name"), "is_intro_offer": a.get("is_intro_offer")})
    mem = pd.DataFrame(mem, columns=["user_id", "event_date", "product_name", "is_intro_offer"])
    mem["category"] = mem["product_name"].apply(classify_membership)
    mem["intro_kind"] = "membership"
    print(f"  {len(mem)} memberships")

    print("Pulling credit_transactions...")
    cred = []
    for r in pull_pages("credit_transactions"):
        a = r["attributes"]
        if a.get("origination_type") != "purchase":
            continue
        cred.append({"user_id": rel_id(r, "user"), "event_date": a.get("transaction_datetime"),
                     "product_name": a.get("credit_name"), "is_intro_offer": a.get("is_intro_offer")})
    cred = pd.DataFrame(cred, columns=["user_id", "event_date", "product_name", "is_intro_offer"])
    cred["category"] = [classify_credit(n, f) for n, f in zip(cred["product_name"], cred["is_intro_offer"])]
    cred["intro_kind"] = "credit"
    print(f"  {len(cred)} credit purchases")

    print_catalog("Membership products", mem, "product_name", "is_intro_offer")
    print_catalog("Credit products", cred, "product_name", "is_intro_offer")
    if not INTRO_CREDITS:
        print("\nINTRO_CREDIT_NAMES is empty, so credit intros = credit purchases flagged is_intro_offer above.")

    events = pd.concat([mem, cred], ignore_index=True)
    events["event_date"] = to_utc(events["event_date"])
    events = events.dropna(subset=["event_date", "user_id"])
    events["user_id"] = events["user_id"].astype(str)
    events = events.sort_values(["user_id", "event_date"], kind="mergesort").reset_index(drop=True)
    events["purchase_rank"] = events.groupby("user_id").cumcount() + 1
    return events


def build_outcomes(events):
    rows = []
    for uid, ev in events.groupby("user_id", sort=False):
        first = ev.iloc[0]
        if first["category"] != "intro_offer":
            continue
        later = ev.iloc[1:]
        cats = later["category"]
        conv_date, days_to_convert = None, None
        if (cats == "membership").any():
            outcome = "Converted to Membership"
            d = later.loc[cats == "membership", "event_date"].iloc[0]
            conv_date, days_to_convert = d, (d - first["event_date"]).days
        elif (cats == "pack_5plus").any():
            outcome = "Converted to Pack"
            conv_date = later.loc[cats == "pack_5plus", "event_date"].iloc[0]
        elif (cats == "intro_offer").any():
            outcome = "Tried Another Intro Offer"
        elif len(later):
            outcome = "Casual Re-engagement Only"
        else:
            outcome = "No Further Purchase"
        next_purchase = later["event_date"].iloc[0] if len(later) else None
        rows.append({
            "user_id": uid, "intro_type": first["product_name"], "intro_kind": first["intro_kind"],
            "intro_datetime": first["event_date"], "next_purchase": next_purchase,
            "outcome": outcome, "days_to_convert": days_to_convert, "conversion_datetime": conv_date,
        })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# Attendance on the intro (raw check-ins)
# --------------------------------------------------------------------------
def resolve_location_id():
    names = {l["id"]: (l["attributes"].get("name") or "").strip() for l in pull_pages("locations")}
    match = [i for i, n in names.items() if n.lower() == STUDIO_NAME.lower()]
    if match:
        return match[0]
    if len(names) == 1:
        return next(iter(names))
    sys.exit(f"Couldn't find a location called '{STUDIO_NAME}' among {list(names.values())}.")


def pull_checkins(start):
    """user_id -> sorted list of class dates (YYYY-MM-DD) they checked in to."""
    loc = resolve_location_id()
    end = date.today().isoformat()
    session_dates, checkins = {}, []
    for w_start, w_end in month_windows(start, end):
        for s in pull_pages("class_sessions", {"location": loc, "min_date": w_start, "max_date": w_end}):
            session_dates[s["id"]] = s["attributes"].get("start_date")
        for r in pull_pages("reservations", {"class_session_location": loc,
                                             "class_session_min_date": w_start, "class_session_max_date": w_end}):
            if r["attributes"].get("status") == "check in":
                checkins.append((rel_id(r, "user"), rel_id(r, "class_session")))
    by_user = defaultdict(list)
    for uid, cs in checkins:
        d = session_dates.get(cs)
        if uid and d:
            by_user[str(uid)].append(d)
    print(f"Check-ins pulled since {start}: {len(checkins)} ({len(by_user)} people)")
    return {u: sorted(v) for u, v in by_user.items()}


def classes_on_intro(row, checkins):
    start = row["intro_datetime"].date()
    cap = (row["intro_datetime"] + pd.Timedelta(days=INTRO_ATTENDANCE_MAX_DAYS)).date()
    if row["next_purchase"] is not None and pd.notna(row["next_purchase"]):
        cap = min(cap, (row["next_purchase"] - pd.Timedelta(days=1)).date())
    s, e = start.isoformat(), cap.isoformat()
    return sum(1 for d in checkins.get(row["user_id"], []) if s <= d <= e)


def build_starters(outcomes, checkins):
    df = outcomes.copy()
    df["classes_attended"] = [classes_on_intro(r, checkins) for _, r in df.iterrows()] if len(df) else []
    df["intro_date"] = df["intro_datetime"].dt.strftime("%Y-%m-%d")
    df["conversion_date"] = df["conversion_datetime"].apply(lambda d: d.strftime("%Y-%m-%d") if d is not None and pd.notna(d) else "")
    df["studio"] = STUDIO_NAME
    df["days_to_convert"] = df["days_to_convert"].apply(lambda v: "" if v is None or pd.isna(v) else int(v))
    cols = ["user_id", "intro_type", "intro_kind", "intro_date", "studio", "outcome",
            "days_to_convert", "conversion_date", "classes_attended"]
    return df[cols].sort_values("intro_date").reset_index(drop=True)


def main():
    debug = os.environ.get("DEBUG_ONLY_SKIP_SHEET_WRITE") == "true"
    if "PASTE" in DASHBOARD_SHEET_ID and not debug:
        sys.exit("Set DASHBOARD_SHEET_ID in config.py first (or run with debug_only=true).")

    events = pull_events()
    outcomes = build_outcomes(events)
    if outcomes.empty:
        print("\nNo intro offer starters found yet (no customer whose first purchase is an intro offer).")
        starters = pd.DataFrame(columns=["user_id", "intro_type", "intro_kind", "intro_date", "studio", "outcome",
                                         "days_to_convert", "conversion_date", "classes_attended"])
    else:
        first_intro = outcomes["intro_datetime"].min().date().replace(day=1).isoformat()
        starters = build_starters(outcomes, pull_checkins(first_intro))

    print(f"\nIntro offer starters: {len(starters)}")
    if len(starters):
        print(starters.groupby(["intro_type", "outcome"]).size().to_string())
        conv = starters["outcome"].isin(["Converted to Membership", "Converted to Pack"]).mean() * 100
        print(f"Converted (membership or {PACK_CONVERSION_MIN}+ pack), all time: {conv:.1f}%")

    if debug:
        print("\nDEBUG_ONLY_SKIP_SHEET_WRITE is set -- skipping the actual write.")
        print(starters.drop(columns=["user_id"]).tail(15).to_string())
        return
    write_to_sheet(starters, TAB_INTRO_STARTERS)


if __name__ == "__main__":
    main()
