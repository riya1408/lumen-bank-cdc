"""
Lumen Bank - Step 1 test: does Atlas Automated Embedding work on our M0 cluster?

What it does:
  1. Upserts the 15-product catalogue into <MONGO_DB>.products
  2. Creates a vectorSearch index whose 'description' field is auto-embedded by Atlas (voyage-4)
  3. Waits until the index is queryable
  4. Runs 3 plain-English searches and prints the top 3 products for each

Run:  python test_auto_embed.py
Safe to re-run: products are upserted, the index is created only if missing.
"""
import os
import time

from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne
from pymongo.errors import OperationFailure
from pymongo.operations import SearchIndexModel

load_dotenv()
client = MongoClient(os.environ["MONGO_URI"])
db = client[os.environ.get("MONGO_DB", "lumen_bank")]
products = db["products"]

INDEX_NAME = "products_auto"
MODEL = "voyage-4"

# Fictional Lumen Bank catalogue. 'description' is what gets embedded;
# the other fields are hard eligibility rules checked in code / as filters.
CATALOGUE = [
    ("P01", "SkyMiles Travel Card", "credit_card", "Credit card for frequent travellers: air miles on flights and hotels, airport lounge access, low forex markup on international spends.", 12, ["MASS", "AFFLUENT", "PREMIUM"], ["LOW", "MEDIUM", "HIGH"]),
    ("P02", "DineOut Rewards Card", "credit_card", "Credit card with 5x reward points on restaurants, food delivery, movies and entertainment.", 6, ["MASS", "AFFLUENT", "PREMIUM"], ["LOW", "MEDIUM", "HIGH"]),
    ("P03", "EveryDay Cashback Card", "credit_card", "Flat cashback on groceries, fuel, utility bills and online shopping for everyday household spending.", 4, ["MASS", "AFFLUENT", "PREMIUM"], ["LOW", "MEDIUM", "HIGH"]),
    ("P04", "Premier Metal Card", "credit_card", "Premium metal credit card with concierge service, golf access, high credit limits and complimentary travel insurance.", 25, ["PREMIUM"], ["LOW", "MEDIUM"]),
    ("P05", "Secured Starter Card", "credit_card", "Credit card backed by a fixed deposit, for customers building their first credit history.", 0, ["MASS", "AFFLUENT", "PREMIUM"], ["LOW", "MEDIUM", "HIGH"]),
    ("P06", "Smart FD", "deposit", "Fixed deposit with flexible tenure and guaranteed returns for safe savings.", 0, ["MASS", "AFFLUENT", "PREMIUM"], ["LOW", "MEDIUM", "HIGH"]),
    ("P07", "Monthly SIP Plan", "investment", "Monthly mutual fund investment plan for long-term wealth creation.", 5, ["MASS", "AFFLUENT", "PREMIUM"], ["LOW", "MEDIUM", "HIGH"]),
    ("P08", "Tax Saver FD", "deposit", "Five-year fixed deposit that offers income tax benefits.", 0, ["MASS", "AFFLUENT", "PREMIUM"], ["LOW", "MEDIUM", "HIGH"]),
    ("P09", "Home Loan", "loan", "Housing loan for buying or building a home, with long repayment tenure.", 8, ["MASS", "AFFLUENT", "PREMIUM"], ["LOW", "MEDIUM"]),
    ("P10", "Personal Loan", "loan", "Unsecured personal loan for weddings, travel, medical or other emergencies.", 5, ["MASS", "AFFLUENT", "PREMIUM"], ["LOW", "MEDIUM"]),
    ("P11", "Car Loan", "loan", "Financing for buying a new or used car.", 6, ["MASS", "AFFLUENT", "PREMIUM"], ["LOW", "MEDIUM"]),
    ("P12", "Education Loan", "loan", "Loan for higher studies and university fees in India or abroad.", 0, ["MASS", "AFFLUENT", "PREMIUM"], ["LOW", "MEDIUM", "HIGH"]),
    ("P13", "Health Shield Insurance", "insurance", "Family health insurance with cashless treatment at network hospitals.", 0, ["MASS", "AFFLUENT", "PREMIUM"], ["LOW", "MEDIUM", "HIGH"]),
    ("P14", "Travel Insurance", "insurance", "Trip insurance covering medical emergencies abroad, flight delays and lost baggage.", 0, ["MASS", "AFFLUENT", "PREMIUM"], ["LOW", "MEDIUM", "HIGH"]),
    ("P15", "Gold Savings Account", "account", "Savings account with higher interest rates and a free premium debit card upgrade.", 0, ["AFFLUENT", "PREMIUM"], ["LOW", "MEDIUM", "HIGH"]),
]


def upsert_catalogue():
    ops = []
    for pid, name, ptype, desc, min_inc, segs, risks in CATALOGUE:
        ops.append(UpdateOne(
            {"_id": pid},
            {"$set": {"name": name, "type": ptype, "description": desc,
                      "min_income_lpa": min_inc, "segments": segs,
                      "allowed_risk_tiers": risks, "kyc_required": True}},
            upsert=True,
        ))
    res = products.bulk_write(ops)
    print(f"Catalogue upserted: {res.upserted_count} new, {res.modified_count} changed, "
          f"{products.count_documents({})} products in total")


def ensure_index():
    existing = {ix["name"]: ix for ix in products.list_search_indexes()}
    if INDEX_NAME in existing:
        print(f"Index '{INDEX_NAME}' already exists")
        return
    definition = {"fields": [
        {"type": "autoEmbed", "modality": "text", "path": "description", "model": MODEL},
        {"type": "filter", "path": "segments"},
        {"type": "filter", "path": "min_income_lpa"},
        {"type": "filter", "path": "allowed_risk_tiers"},
        {"type": "filter", "path": "type"},
    ]}
    products.create_search_index(SearchIndexModel(definition=definition, name=INDEX_NAME, type="vectorSearch"))
    print(f"Creating index '{INDEX_NAME}' with model {MODEL} ...")


def wait_until_queryable(timeout_s=600):
    start = time.time()
    while time.time() - start < timeout_s:
        ix = next((i for i in products.list_search_indexes() if i["name"] == INDEX_NAME), None)
        status = ix.get("status") if ix else "MISSING"
        if ix and ix.get("queryable"):
            print(f"Index is queryable (status {status}) after {int(time.time() - start)} s")
            return True
        print(f"  waiting... status={status}")
        time.sleep(15)
    print("Timed out waiting for the index. Check it in the Atlas UI (Search & Vector Search tab).")
    return False


def search(text):
    """The docs show two query shapes; try query.text first, then a plain string."""
    for query in ({"text": text}, text):
        pipeline = [
            {"$vectorSearch": {"index": INDEX_NAME, "path": "description", "query": query,
                               "numCandidates": 50, "limit": 3}},
            {"$project": {"_id": 1, "name": 1, "score": {"$meta": "vectorSearchScore"}}},
        ]
        try:
            return list(products.aggregate(pipeline)), ("query.text" if isinstance(query, dict) else "query string")
        except OperationFailure as e:
            last_error = e
    raise last_error


if __name__ == "__main__":
    upsert_catalogue()
    ensure_index()
    if wait_until_queryable():
        for q in [
            "frequent flyer who spends a lot on flights and hotels",
            "family with young children wanting medical cover",
            "loves eating out at restaurants and ordering food online",
        ]:
            # Free M0 query limit is about 3 requests per minute, so pause between searches.
            results, shape = search(q)
            print(f"\nQuery: {q}   (worked with: {shape})")
            for r in results:
                print(f"  {r['_id']}  {r['name']:<26} score={r['score']:.3f}")
            time.sleep(25)
