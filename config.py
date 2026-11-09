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
DASHBOARD_SHEET_ID = "PASTE_HOMERUN_SHEET_ID_HERE"

# How far back the class pipeline pulls. Months before Homerun had any
# classes just come back empty, so it's safe to leave this early.
CLASS_HISTORY_START = "2026-09-01"

# --- Membership Health ------------------------------------------------------
# Membership types that are NOT real recurring memberships (intro offers,
# short promos, perks). Matched by name, case-insensitive, "contains".
# Mariana Tek's is_intro_offer flag proved unreliable at Enmei, so we match
# by name. Every run prints all membership names with their tier in the log.
EXCLUDED_MEMBERSHIP_KEYWORDS = ["1 week unlimited"]

# Names containing any of these are classed as "Unlimited" (as at Enmei).
UNLIMITED_KEYWORDS = ["unlimited", "founding membership"]

# Names containing any of these are classed as "Reduced Price".
REDUCED_PRICE_KEYWORDS = ["student", "teacher", "healthcare", "influencer", "staff"]

# --- Class Performance ------------------------------------------------------
# Class types that aren't coach-led classes (e.g. at Enmei: Recovery boots,
# Full Body). Lowercase, exact match. Every run prints all class types seen.
EXCLUDED_CLASS_TYPES = set()

# Coach names come from each coach's schedule display name in Mariana Tek.
# Only used as a fallback correction, e.g. {"Samantha": "Sam"}.
INSTRUCTOR_NAME_OVERRIDES = {}

# --- Third-party bookings ----------------------------------------------------
# Mariana Tek system reservation tags (matched by slug, not ID). A booking
# with more than one tag counts once, under the first match in this order.
THIRD_PARTY_TAG_SLUGS = {
    "classpass-reservation": "ClassPass",
    "gympass-reservation": "Wellhub",
    "fitreserve-reservation": "Other Third-Party",
}

# --- Intro Offers -------------------------------------------------------------
# Intro offers sold as a MEMBERSHIP product (lowercase, exact name match).
INTRO_MEMBERSHIP_NAMES = ["1 week unlimited"]
# Intro offers sold as CREDITS (a class-pack intro). Lowercase, exact name.
# Leave empty to use Mariana Tek's is_intro_offer flag on credit purchases
# instead -- the intro workflow log lists every flagged product so we can
# pin the real name here once confirmed.
INTRO_CREDIT_NAMES = []
# Single drop-ins: re-engagement, never a conversion.
CASUAL_CREDIT_NAMES = ["single", "1 credit", "drop in", "drop-in"]
# A pack of this many classes/credits or more counts as converting.
PACK_CONVERSION_MIN = 5
# "Classes attended on the intro" = check-ins from the intro purchase until
# their next purchase, capped at this many days.
INTRO_ATTENDANCE_MAX_DAYS = 30

# --- Sheet tabs (all fully overwritten each run) ---------------------------
TAB_LIVE_STATUS = "Membership_Live_Status"
TAB_HISTORY = "Membership_History"
TAB_CUSTOMER_LOG = "Membership_Customer_Log"
TAB_AGE_PYRAMID = "Membership_Age_Pyramid"
TAB_MEMBERSHIP_DAILY = "Membership_Daily"

TAB_CLASS_SESSIONS = "ClassPerformance_Sessions"
TAB_CLASS_HISTORY = "ClassPerformance_History"
TAB_CLASS_CANCELLATIONS = "ClassPerformance_Cancellations"
TAB_CLASS_CANCELLATIONS_DAILY = "ClassPerformance_CancellationsDaily"

TAB_INTRO_STARTERS = "IntroOffers_Starters"
