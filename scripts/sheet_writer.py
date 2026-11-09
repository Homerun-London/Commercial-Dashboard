"""
sheet_writer.py  (Homerun)

The one function every pipeline uses to write a tab. Same retry-wrapped
"clear and overwrite" pattern as Enmei, plus the Enmei fix for the
"dashboard shows fewer rows than the Sheet" incident: any filter left on
a tab, or any hidden row, hides rows from the gviz feed the dashboard
reads. So before every write we clear the tab's basic filter and unhide
all rows.

Values are written RAW (text stays text, dates stay YYYY-MM-DD strings),
so Google doesn't auto-convert them into Sheets dates.
"""

import json
import os
import sys
import time
from pathlib import Path

import gspread
import pandas as pd
from google.oauth2.service_account import Credentials

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import DASHBOARD_SHEET_ID  # noqa: E402

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_CLIENT = None


def _client():
    global _CLIENT
    if _CLIENT is None:
        raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
        try:
            info = json.loads(raw)
        except json.JSONDecodeError as e:
            sys.exit("GOOGLE_SERVICE_ACCOUNT_JSON isn't valid JSON "
                     f"({e}). Re-paste the whole key file into the secret, from the first {{ to the last }}.")
        _CLIENT = gspread.authorize(Credentials.from_service_account_info(info, scopes=SCOPES))
    return _CLIENT


def _reset_tab_view(sh, ws):
    """Clear any basic filter and unhide every row, so gviz serves all rows."""
    try:
        ws.clear_basic_filter()
    except Exception as e:  # noqa: BLE001 -- no filter is fine
        if "filter" not in str(e).lower():
            print(f"  note: couldn't clear filter on '{ws.title}' ({e})")
    try:
        sh.batch_update({"requests": [{"updateDimensionProperties": {
            "range": {"sheetId": ws.id, "dimension": "ROWS", "startIndex": 0, "endIndex": ws.row_count},
            "properties": {"hiddenByUser": False}, "fields": "hiddenByUser"}}]})
    except Exception as e:  # noqa: BLE001
        print(f"  note: couldn't unhide rows on '{ws.title}' ({e})")


def _to_values(df):
    out = df.copy()
    for col in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[col]) or isinstance(out[col].dtype, pd.PeriodDtype):
            out[col] = out[col].astype(str).replace({"NaT": "", "nan": ""})
        else:
            vals = list(out[col])
            if any(isinstance(v, (pd.Timestamp, pd.Period)) for v in vals):
                out[col] = [("" if pd.isna(v) else str(v.start_time.date() if isinstance(v, pd.Period) else (v.date() if v == v.normalize() else v)))
                            if isinstance(v, (pd.Timestamp, pd.Period)) or v is pd.NaT else v for v in vals]
            elif any(isinstance(v, list) for v in vals):
                out[col] = [", ".join(map(str, v)) if isinstance(v, list) else v for v in vals]
    return [out.columns.tolist()] + out.astype(object).where(out.notna(), "").values.tolist()


def write_to_sheet(df, tab_name, max_retries=4):
    """OVERWRITE tab_name with df (full recompute each run, never appended)."""
    values = _to_values(df)
    n_rows, n_cols = len(values), max(len(values[0]), 1)
    for attempt in range(max_retries):
        try:
            sh = _client().open_by_key(DASHBOARD_SHEET_ID)
            try:
                ws = sh.worksheet(tab_name)
            except gspread.exceptions.WorksheetNotFound:
                ws = sh.add_worksheet(title=tab_name, rows=max(n_rows + 10, 100), cols=n_cols + 2)
            _reset_tab_view(sh, ws)
            if ws.row_count < n_rows or ws.col_count < n_cols:
                ws.resize(rows=max(ws.row_count, n_rows + 10), cols=max(ws.col_count, n_cols + 2))
            ws.clear()
            ws.update(values, "A1", value_input_option="RAW")
            print(f"Wrote {n_rows - 1} rows to '{tab_name}' tab.")
            return
        except gspread.exceptions.APIError as e:
            wait = 2 ** attempt
            print(f"'{tab_name}': Google Sheets API error ({e}), retrying in {wait}s...")
            time.sleep(wait)
    raise RuntimeError(f"Failed to write '{tab_name}' after {max_retries} attempts")
