"""
class_performance_common.py  (Homerun)

Shared cleaning / weekly rollup / sheet-writing functions for Class
Performance, ported unchanged from the Enmei dashboard
(build_class_performance_dashboard_data.py). The report-294 fetching code
from that file is NOT carried over -- Homerun is built on raw data only.

Key rules carried over:
  - Admin Holds count as Paid Visits only when they cover MORE THAN HALF
    the room (a private class). Smaller counts are waitlist-alert holds.
  - Unavailable Holds are lost capacity, never paid visits.
  - Class types in config.EXCLUDED_CLASS_TYPES are flagged Is_Excluded_Class
    and kept out of every metric.
  - Blank instructor on a non-excluded class -> Unassigned_Instructor_Flag
    (data-quality issue, surfaced rather than dropped).
"""

import os
import sys
import json
import time
import numpy as np
import pandas as pd
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import DASHBOARD_SHEET_ID, EXCLUDED_CLASS_TYPES as _EXCLUDED  # noqa: E402

BASE_URL = os.environ["MARIANA_BASE_URL"].rstrip("/")
HEADERS = {"Authorization": f"Bearer {os.environ['MARIANA_API_KEY']}"}
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

EXCLUDED_CLASS_TYPES = {t.strip().lower() for t in _EXCLUDED}

# Admin holds count as paid visits ONLY when they cover MORE THAN this share
# of the room's capacity. Strictly greater than: exactly half is NOT a majority.
ADMIN_HOLD_MAJORITY_SHARE = 0.5


def clean_sessions_df(raw_df):
    """Clean report 294 output into a typed, de-duplicated,
    exclusion-flagged DataFrame."""
    df = raw_df.copy()
    df.columns = [c.strip() for c in df.columns]

    # BUG FIX: report 294 returns Class Date in ISO format (YYYY-MM-DD,
    # unambiguous) via the API -- confirmed during validation (dates like
    # '2026-07-06' seen directly in raw API responses). An earlier version
    # of this function used dayfirst=True (copied from the old CSV-export
    # workflow, which used DD/MM/YYYY), but dayfirst=True can silently
    # swap month/day even on unambiguous ISO strings (verified: pandas
    # turned '2025-01-06' into 2025-06-01). This corrupted every date in
    # the pipeline. Do NOT add dayfirst=True back without re-confirming
    # the actual raw format first.
    df["Class Date"] = pd.to_datetime(df["Class Date"], errors="coerce")
    df = df.dropna(subset=["Class Date"])
    df["Class Day of Week"] = df["Class Day of Week"].astype(str).str.strip()

    numeric_cols = ["Checked In Reservations", "Late Cancelled Reservations", "No Showed Reservations",
                     "Admin Holds", "Unavailable Holds", "Layout Capacity", "Actual Capacity"]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    df["% Utilization"] = (
        df["% Utilization"].astype(str).str.replace("%", "", regex=False).str.strip()
    )
    df["% Utilization"] = pd.to_numeric(df["% Utilization"], errors="coerce")
    if df["% Utilization"].dropna().max() is not None and df["% Utilization"].dropna().max() <= 1:
        df["% Utilization"] = df["% Utilization"] * 100

    # Admin Holds count as a paid visit only when they cover the MAJORITY
    # of the room (a private class) -- see ADMIN_HOLD_MAJORITY_SHARE above.
    # Validated example: Marylebone 2026-06-02 18:30, 12 holds in a
    # 16-capacity room. A lone hold is almost always a waitlist-alert hold
    # from a late cancel (not a paid visit; it can also double-count the
    # late cancel), so it is not counted. Unavailable Holds (broken
    # equipment etc.) deliberately NOT included -- lost capacity, not a
    # paid visit.
    # FUTURE ENHANCEMENT (not built yet, flagged per Remy): eventually
    # assign an average RPV (revenue per visit) specifically to Admin
    # Hold spots, since private-class economics likely differ.
    admin_share = np.where(df["Actual Capacity"] > 0, df["Admin Holds"] / df["Actual Capacity"], 0.0)
    df["Admin Holds Counted"] = np.where(admin_share > ADMIN_HOLD_MAJORITY_SHARE, df["Admin Holds"], 0)
    df["Paid Visits"] = (
        df["Checked In Reservations"] + df["Late Cancelled Reservations"]
        + df["No Showed Reservations"] + df["Admin Holds Counted"]
    )
    df["Total % Paid Visits"] = np.where(
        df["Actual Capacity"] > 0,
        100 * df["Paid Visits"] / df["Actual Capacity"],
        np.nan,
    )

    class_type_lower = df["Class Type"].astype(str).str.strip().str.lower()
    df["Is_Excluded_Class"] = class_type_lower.isin(EXCLUDED_CLASS_TYPES)

    # Defensive multi-instructor split (not observed in validation, but
    # the underlying API field is a list, so guard against it anyway).
    df["Instructors"] = df["Instructors"].fillna("").astype(str)
    df["Instructor_List"] = df["Instructors"].apply(
        lambda s: [n.strip() for n in s.split(",") if n.strip()] if s else []
    )
    df["Instructor_Count"] = df["Instructor_List"].apply(len)

    # Data-quality flag: null/blank instructor on a class type that
    # ISN'T one of our confirmed no-instructor-by-design types.
    df["Unassigned_Instructor_Flag"] = (df["Instructor_Count"] == 0) & (~df["Is_Excluded_Class"])

    return df


def build_cancellations_history(sessions_df, start_date):
    """Weekly cancelled-class counts per studio, from a raw
    class_sessions pull (already location-tagged, all dates)."""
    cancelled = sessions_df[
        sessions_df["cancellation_datetime"].notna()
        & (sessions_df["start_date"] >= pd.Timestamp(start_date))
    ].copy()
    cancelled["week"] = cancelled["start_date"].dt.to_period("W-MON").apply(lambda p: p.start_time)

    out = (
        cancelled.groupby(["week", "location"])
        .size()
        .reset_index(name="cancelled_classes")
        .rename(columns={"location": "studio"})
        .sort_values(["week", "studio"])
    )
    return out


def build_class_history(sessions_df):
    """Weekly rollup per studio -- total real classes held, avg
    utilization, total classes cancelled would join in here from the
    separate cancellations tab (kept separate per the "two independent
    counts" decision -- see module docstring)."""
    real = sessions_df[~sessions_df["Is_Excluded_Class"]].copy()
    real["week"] = real["Class Date"].dt.to_period("W-MON").apply(lambda p: p.start_time)

    out = (
        real.groupby(["week", "Location"])
        .agg(
            total_classes=("Class Date", "count"),
            avg_utilization=("% Utilization", "mean"),
            avg_paid_pct=("Total % Paid Visits", "mean"),
            total_checked_in=("Checked In Reservations", "sum"),
        )
        .reset_index()
        .rename(columns={"Location": "studio"})
        .sort_values(["week", "studio"])
    )
    return out


def write_to_sheet(df, tab_name, max_retries=4):
    """Same retry-wrapped overwrite pattern used everywhere else."""
    creds_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)

    df_out = df.copy()
    for col in df_out.columns:
        if pd.api.types.is_datetime64_any_dtype(df_out[col]):
            df_out[col] = df_out[col].astype(str).replace("NaT", "")
    # Guard against any leftover list-typed columns (e.g. Instructor_List
    # if it isn't dropped before writing) -- gspread can't serialize them.
    for col in df_out.columns:
        if df_out[col].apply(lambda v: isinstance(v, list)).any():
            df_out[col] = df_out[col].apply(lambda v: ", ".join(v) if isinstance(v, list) else v)

    values = [df_out.columns.tolist()] + df_out.astype(object).where(df_out.notna(), "").values.tolist()

    for attempt in range(max_retries):
        try:
            sh = client.open_by_key(DASHBOARD_SHEET_ID)
            try:
                worksheet = sh.worksheet(tab_name)
            except gspread.exceptions.WorksheetNotFound:
                worksheet = sh.add_worksheet(title=tab_name, rows=max(len(df) + 10, 100), cols=len(df.columns) + 2)
            worksheet.clear()
            worksheet.update(values, "A1")
            print(f"Wrote {len(df_out)} rows to '{tab_name}' tab.")
            return
        except gspread.exceptions.APIError as e:
            wait = 2 ** attempt
            print(f"'{tab_name}': Google Sheets API error ({e}), retrying in {wait}s...")
            time.sleep(wait)

    raise RuntimeError(f"Failed to write '{tab_name}' after {max_retries} attempts")
