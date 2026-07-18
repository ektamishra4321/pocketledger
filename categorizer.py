"""Deterministic keyword-rule categorizer for Indian merchants/UPI narrations.

No ML here on purpose. Rules are transparent, testable, and editable.
Order matters: first match wins. Extend RULES freely.
"""

from __future__ import annotations

import re

# (category, [keywords]) — matched case-insensitively against merchant + note
RULES: list[tuple[str, list[str]]] = [
    ("food", ["swiggy", "zomato", "dominos", "domino's", "mcdonald", "kfc", "pizza",
              "eatclub", "faasos", "box8", "biryani", "cafe", "restaurant", "dhaba",
              "chai", "bakery", "haldiram"]),
    ("groceries", ["blinkit", "zepto", "bigbasket", "instamart", "grofers", "dmart",
                   "d-mart", "kirana", "reliance fresh", "more supermarket", "jiomart",
                   "grocery", "sabzi", "vegetables"]),
    ("transport", ["uber", "ola", "rapido", "irctc", "redbus", "metro", "petrol",
                   "diesel", "fuel", "fastag", "indian oil", "hpcl", "bpcl", "parking",
                   "auto rickshaw", "namma yatri"]),
    ("entertainment", ["netflix", "hotstar", "prime video", "primevideo", "spotify",
                       "bookmyshow", "pvr", "inox", "youtube premium", "sonyliv",
                       "zee5", "jiocinema", "gaana", "wynk", "steam", "playstation"]),
    ("shopping", ["amazon", "flipkart", "myntra", "ajio", "meesho", "nykaa", "snapdeal",
                  "decathlon", "ikea", "croma", "vijay sales", "tata cliq"]),
    ("utilities", ["electricity", "bescom", "msedcl", "tneb", "adani electricity",
                   "tata power", "water bill", "bwssb", "gas", "indane", "hp gas",
                   "bharat gas", "piped gas", "mahanagar gas", "broadband", "wifi",
                   "airtel", "jio", "vi ", "vodafone", "bsnl", "act fibernet",
                   "hathway", "tata play", "dth", "recharge"]),
    ("rent", ["rent", "nobroker", "nestaway", "landlord", "society maintenance",
              "maintenance charge"]),
    ("health", ["pharmacy", "pharmeasy", "1mg", "netmeds", "apollo", "medplus",
                "hospital", "clinic", "diagnostic", "lab test", "cult.fit", "cultfit",
                "gym", "practo", "medical"]),
    ("education", ["udemy", "coursera", "byju", "unacademy", "upgrad", "school fee",
                   "college fee", "tuition", "books", "kindle", "course"]),
    ("investment", ["zerodha", "groww", "upstox", "coin dcx", "mutual fund", "sip ",
                    "ppf", "nps", "fd ", "fixed deposit", "gold bond", "etf"]),
    ("insurance", ["lic", "policy bazaar", "policybazaar", "insurance", "premium",
                   "acko", "digit insurance", "star health", "hdfc ergo"]),
    ("transfer", ["upi/p2p", "imps", "neft", "rtgs", "self transfer", "sent to",
                  "family transfer"]),
    ("fees", ["bank charge", "annual fee", "late fee", "penalty", "gst on charge",
              "sms charge", "amc charge"]),
    ("salary", ["salary", "payroll", "stipend"]),
]

_CLEAN_RE = re.compile(r"[^a-z0-9 .&'/-]+")


def normalize_merchant(text: str) -> str:
    """Lowercase, strip UPI/bank noise so 'UPI-SWIGGY-9876@ybl' → 'swiggy'."""
    t = text.lower().strip()
    # strip common bank narration prefixes
    for prefix in ("upi-", "upi/", "pos ", "pos-", "neft-", "imps-", "ach-", "nach-",
                   "atm-", "vps-", "mps-", "eaw-", "bil/", "vin/", "payu*", "razorpay*",
                   "razor*", "paytm*", "phonepe*", "gpay*"):
        if t.startswith(prefix):
            t = t[len(prefix):]
    # drop VPA handles and trailing reference numbers
    t = re.sub(r"@[a-z0-9.]+", " ", t)
    t = re.sub(r"\b\d{6,}\b", " ", t)
    t = _CLEAN_RE.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip()
    # take the meaningful head (bank narrations append refs after separators)
    head = re.split(r"[/|]", t)[0].strip()
    head = head.strip(" .-&'/")
    return head or t.strip(" .-&'/")


def categorize(merchant: str, note: str = "") -> str:
    """Return first-matching category, else 'uncategorized'."""
    haystack = f" {normalize_merchant(merchant)} {note.lower()} "
    for category, keywords in RULES:
        for kw in keywords:
            if kw in haystack:
                return category
    return "uncategorized"
