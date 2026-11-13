"""
Lumen Bank - Recommendations step 3: full refresh with the query flipped.

Instead of 1,000 searches ("which products fit this customer?"), run 15
("which ELIGIBLE customers fit this product?"). Eligibility is applied as
pre-filters, so a product can only ever score customers who qualify for it.
Raw cosine scores are NOT comparable across products (generic descriptions score high
for everyone), so each score is converted to a PER-PRODUCT PERCENTILE: "how well does this
customer fit this product compared with the other eligible customers?". Customers keep their
top 3 by percentile (raw score breaks ties), at most MAX_PER_TYPE per product type.

Free M0 limit: ~3 searches/minute, so we pause between products (~5-6 minutes total).

Run:  python refresh_recommendations.py
"""
import os
import time
from collections import defaultdict
from datetime import datetime, timezone

from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne

load_dotenv()
db = MongoClient(os.environ["MONGO_URI"])[os.environ.get("MONGO_DB", "lumen_bank")]
customers, products = db["customer_360"], db["products"]

INDEX_NAME = "customers_auto"
MODEL = "voyage-4"
TOP_N = 3
MIN_PERCENTILE = 0.50     # must be in the top half of that product's eligible customers
MAX_PER_TYPE = 2          # e.g. at most 2 credit cards in one customer's top 3
PAUSE_S = 21              # keeps us under 3 searches per minute

# A customer who already holds this account type shouldn't be offered the matching product.
OWNED_TYPE_BLOCKS = {
    "P05": {"CREDIT_CARD"},                       # starter card is for people with no card
    "P06": {"FD", "FIXED_DEPOSIT"},
    "P09": {"HOME_LOAN"},
    "P10": {"PERSONAL_LOAN"},
    "P11": {"CAR_LOAN"},
}


def eligible_customers_for(product):
    """One vector search: rank ELIGIBLE customers by how well their profile fits the product."""
    pipeline = [
        {"$vectorSearch": {
            "index": INDEX_NAME,
            "path": "profile_text",
            "query": {"text": product["description"]},
            "filter": {"$and": [
                {"_deleted": {"$eq": False}},
                {"kyc_status": {"$eq": "VERIFIED"}},
                {"income_lpa": {"$gte": product["min_income_lpa"]}},
                {"segment": {"$in": product["segments"]}},
                {"risk_tier": {"$in": product["allowed_risk_tiers"]}},
            ]},
            "numCandidates": 2000,
            "limit": 1000,
        }},
        {"$project": {"_id": 1, "accounts": 1, "profile_hash": 1,
                      "score": {"$meta": "vectorSearchScore"}}},
    ]
    return list(customers.aggregate(pipeline))


def main():
    candidates = defaultdict(list)            # customer_id -> [(percentile, score, product_id, name, type)]
    profile_hash = {}
    catalogue = list(products.find().sort("_id", 1))
    started = time.time()

    for i, p in enumerate(catalogue):
        if i:
            time.sleep(PAUSE_S)
        hits = eligible_customers_for(p)
        blocked = OWNED_TYPE_BLOCKS.get(p["_id"], set())
        # Percentile within THIS product's eligible customers (hits come back best-first).
        n = len(hits)
        kept = 0
        for rank, h in enumerate(hits):
            owned = {a.get("type") for a in (h.get("accounts") or {}).values() if not a.get("_deleted")}
            if owned & blocked:
                continue
            pct = 1.0 if n == 1 else 1 - rank / (n - 1)
            candidates[h["_id"]].append((pct, h["score"], p["_id"], p["name"], p["type"]))
            profile_hash[h["_id"]] = h.get("profile_hash")
            kept += 1
        scores = [h["score"] for h in hits] or [0]
        print(f"{p['_id']} {p['name']:<26} eligible={len(hits):>4} kept={kept:>4} "
              f"score min/max={min(scores):.3f}/{max(scores):.3f}")

    now = datetime.now(timezone.utc)
    ops, with_recs = [], 0
    for doc in customers.find({"_deleted": {"$ne": True}}, {"_id": 1}):
        ranked, per_type = [], defaultdict(int)
        for pct, score, pid, name, ptype in sorted(candidates.get(doc["_id"], []), reverse=True):
            if pct < MIN_PERCENTILE or per_type[ptype] >= MAX_PER_TYPE:
                continue
            ranked.append({"product_id": pid, "name": name, "percentile": round(pct, 3), "score": round(score, 3)})
            per_type[ptype] += 1
            if len(ranked) == TOP_N:
                break
        recs = ranked
        with_recs += bool(recs)
        ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": {
            "recommendations": recs,
            "recs_model": MODEL,
            "recs_profile_hash": profile_hash.get(doc["_id"]),
            "recs_computed_at": now,
        }}))
    for i in range(0, len(ops), 500):
        customers.bulk_write(ops[i:i + 500], ordered=False)

    print(f"\nDone in {int(time.time() - started)} s: {with_recs} of {len(ops)} customers have recommendations")
    harsh = customers.find_one({"_id": 421}, {"profile_text": 1, "recommendations": 1})
    if harsh:
        print("\nCustomer 421:", harsh.get("profile_text"))
        for r in harsh.get("recommendations", []):
            print(f"  {r['product_id']} {r['name']:<26} percentile={r['percentile']}  score={r['score']}")


if __name__ == "__main__":
    main()
