"""
config.py

Shared constants for the Homerun Commercial Dashboard.
Ported from the Enmei Commercial Dashboard -- same pipeline logic, set up
for a single studio.
"""

# Homerun has one studio. The class pipeline looks its Mariana Tek location
# ID up by this name on every run (or uses the only location if there is
# just one), so no ID needs to be pasted here.
STUDIO_NAME = "Mayfair"

# Google Sheet used as the dashboard's data layer. Share it with the service
# account (Editor) and publish it to web (File -> Share -> Publish to web)
# so docs/index.html can read it. Paste the ID from the Sheet's URL here --
# the long string between /d/ and /edit.
DASHBOARD_SHEET_ID = "12mmWJHy6dm9E3iIMvDgKrXwRbxHVIhqWZfBBcK2F0tc"

# How far back the class pipeline pulls. Months before Homerun had any
# classes just come back empty, so it's safe to leave this early.
CLASS_HISTORY_START = "2025-01-01"

# --- Membership Health ------------------------------------------------------
# Membership types that are NOT real recurring memberships (intro offers,
# short promos, perks). Matched by name, case-insensitive, "contains".
# Mariana Tek's is_intro_offer flag proved unreliable at Enmei, so we match
# by name. Empty until we've seen Homerun's real product names -- every run
# prints the full list in the log.
EXCLUDED_MEMBERSHIP_KEYWORDS = ["1 week unlimited"]

# Names containing any of these are classed as "Reduced Price".
REDUCED_PRICE_KEYWORDS = ["student", "teacher", "healthcare", "influencer", "staff"]

# --- Class Performance ------------------------------------------------------
# Class types that aren't coach-led classes (e.g. at Enmei: Recovery boots,
# Full Body). Lowercase, exact match. Every run prints all class types seen.
EXCLUDED_CLASS_TYPES = set()

# Coach names come from each coach's schedule display name in Mariana Tek.
# Only used as a fallback correction, e.g. {"Samantha": "Sam"}.
INSTRUCTOR_NAME_OVERRIDES = {}

# --- Sheet tabs (all fully overwritten each run) ---------------------------
TAB_LIVE_STATUS = "Membership_Live_Status"
TAB_HISTORY = "Membership_History"
TAB_CUSTOMER_LOG = "Membership_Customer_Log"
TAB_AGE_PYRAMID = "Membership_Age_Pyramid"

TAB_CLASS_SESSIONS = "ClassPerformance_Sessions"
TAB_CLASS_HISTORY = "ClassPerformance_History"
TAB_CLASS_CANCELLATIONS = "ClassPerformance_Cancellations"
