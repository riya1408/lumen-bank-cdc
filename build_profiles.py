"""
Lumen Bank - Recommendations step 2: build stable customer profiles and the auto-embedding index.

For every customer in customer_360:
  - builds a short, STABLE profile_text (coarse buckets, no exact amounts)
  - hashes it, and writes it only if the hash changed  -> Atlas re-embeds only real changes
  - stores income_lpa (a number) so products can filter on it

Then creates the 'customers_auto' Automated Embedding index on profile_text (voyage-4),
with eligibility fields as filters.

Run:  python build_profiles.py        (safe to re-run; unchanged profiles are skipped)
"""
import hashlib
import os
import time
from collections import Counter

from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne
from pymongo.operations import SearchIndexModel

load_dotenv()
db = MongoClient(os.environ["MONGO_URI"])[os.environ.get("MONGO_DB", "lumen_bank")]
customers = db["customer_360"]

INDEX_NAME = "customers_auto"
MODEL = "voyage-4"

# Categories that describe lifestyle. SALARY is a credit; RENT is reported separately.
NOT_LIFESTYLE = {"SALARY", "RENT"}
ACCOUNT_WORDS = {
    "SAVINGS": "savings account", "CURRENT": "current account", "CREDIT_CARD": "credit card",
    "FD": "fixed deposit", "FIXED_DEPOSIT": "fixed deposit", "HOME_LOAN": "home loan",
    "PERSONAL_LOAN": "personal loan", "CAR_LOAN": "car loan", "LOAN": "loan",
}


def income_band(annual_income):
    lpa = (annual_income or 0) / 100_000
    for upper, label in [(5, "under 5 LPA"), (10, "5-10 LPA"), (15, "10-15 LPA"),
                         (25, "15-25 LPA"), (50, "25-50 LPA")]:
        if lpa < upper:
            return label
    return "above 50 LPA"


def build_profile_text(doc):
    """Coarse on purpose: one more dinner or a salary credit must NOT change this text."""
    spend = {k: v for k, v in (doc.get("spend_30d") or {}).items() if v and v > 0}
    lifestyle = sorted((k for k in spend if k not in NOT_LIFESTYLE), key=lambda k: -spend[k])[:3]

    owned = sorted({
        ACCOUNT_WORDS.get(a.get("type"), str(a.get("type", "")).lower().replace("_", " "))
        for a in (doc.get("accounts") or {}).values()
        if not a.get("_deleted") and a.get("status") == "ACTIVE"
    })

    lines = [
        f"{doc.get('segment', 'MASS').title()} customer in {doc.get('city', 'India')}, "
        f"income band {income_band(doc.get('annual_income'))}.",
        ("Spends mostly on: " + ", ".join(c.lower() for c in lifestyle) + ".")
        if lifestyle else "Little recent card or UPI spending.",
    ]
    if spend.get("RENT"):
        lines.append("Pays monthly rent.")
    lines.append("Owns: " + (", ".join(owned) if owned else "no active products") + ".")
    return " ".join(lines)


def build_all():
    ops, changed, unchanged, types = [], 0, 0, Counter()
    for doc in customers.find({"_deleted": {"$ne": True}}):
        for a in (doc.get("accounts") or {}).values():
            types[a.get("type")] += 1
        text = build_profile_text(doc)
        h = hashlib.sha256(text.encode()).hexdigest()[:16]
        if doc.get("profile_hash") == h:
            unchanged += 1
            continue
        changed += 1
        ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": {
            "profile_text": text,
            "profile_hash": h,
            "income_lpa": round((doc.get("annual_income") or 0) / 100_000, 2),
        }}))
        if len(ops) == 500:
            customers.bulk_write(ops, ordered=False)
            ops = []
    if ops:
        customers.bulk_write(ops, ordered=False)
    print(f"Profiles written: {changed} changed, {unchanged} unchanged")
    print("Account types seen:", dict(types))
    sample = customers.find_one({"_id": 421}, {"profile_text": 1})
    if sample:
        print("Customer 421:", sample["profile_text"])


def ensure_index():
    if any(ix["name"] == INDEX_NAME for ix in customers.list_search_indexes()):
        print(f"Index '{INDEX_NAME}' already exists")
        return
    definition = {"fields": [
        {"type": "autoEmbed", "modality": "text", "path": "profile_text", "model": MODEL},
        {"type": "filter", "path": "segment"},
        {"type": "filter", "path": "income_lpa"},
        {"type": "filter", "path": "risk_tier"},
        {"type": "filter", "path": "kyc_status"},
        {"type": "filter", "path": "_deleted"},
    ]}
    customers.create_search_index(SearchIndexModel(definition=definition, name=INDEX_NAME, type="vectorSearch"))
    print(f"Creating index '{INDEX_NAME}' ...")
    start = time.time()
    while time.time() - start < 900:
        ix = next((i for i in customers.list_search_indexes() if i["name"] == INDEX_NAME), {})
        if ix.get("queryable"):
            print(f"Index queryable after {int(time.time() - start)} s")
            return
        print(f"  waiting... status={ix.get('status')}")
        time.sleep(20)
    print("Still building; check the Atlas UI and re-run later.")


if __name__ == "__main__":
    build_all()
    ensure_index()
