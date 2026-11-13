"""
Lumen Bank - product recommendations: full refresh (and shared logic for the live worker).

Instead of 1,000 searches ("which products fit this customer?"), run 15
("which ELIGIBLE customers fit this product?"). Eligibility is applied as
pre-filters, so a product can only ever score customers who qualify for it.

Raw cosine scores are NOT comparable across products (generic descriptions score high
for everyone), so each score becomes a PER-PRODUCT PERCENTILE: "how well does this
customer fit this product compared with the other eligible customers?". Customers keep
their top 3 by percentile, at most MAX_PER_TYPE per product type.

The full refresh also saves each product's score distribution (recs_meta), so the live
worker can turn a new score into the same percentile scale without re-scoring everyone.

Free M0 limit: ~3 searches/minute, so we pause between products (~5 minutes total).
Don't run this while recommendations_worker.py is running: they share that limit.

Run:  python refresh_recommendations.py
"""
import hashlib
import os
import time
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timezone

from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne

load_dotenv()
db = MongoClient(os.environ["MONGO_URI"])[os.environ.get("MONGO_DB", "lumen_bank")]
customers, products, meta = db["customer_360"], db["products"], db["recs_meta"]

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


def owned_types(doc):
    return {a.get("type") for a in (doc.get("accounts") or {}).values()
            if not a.get("_deleted") and a.get("status") == "ACTIVE"}


def recs_input_hash(doc):
    """Everything a recommendation depends on: the profile plus the eligibility facts.
    If this hash is unchanged, the recommendations can't change, so we skip the work."""
    parts = [doc.get("profile_hash") or "", doc.get("kyc_status") or "", doc.get("segment") or "",
             doc.get("risk_tier") or "", str(doc.get("income_lpa")), str(bool(doc.get("_deleted"))),
             ",".join(sorted(t for t in owned_types(doc) if t))]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def eligible_customers_for(product, only_ids=None):
    """One vector search: rank ELIGIBLE customers by how well their profile fits the product."""
    conditions = [
        {"_deleted": {"$eq": False}},
        {"kyc_status": {"$eq": "VERIFIED"}},
        {"income_lpa": {"$gte": product["min_income_lpa"]}},
        {"segment": {"$in": product["segments"]}},
        {"risk_tier": {"$in": product["allowed_risk_tiers"]}},
    ]
    if only_ids is not None:
        conditions.append({"_id": {"$in": list(only_ids)}})
    pipeline = [
        {"$vectorSearch": {
            "index": INDEX_NAME,
            "path": "profile_text",
            "query": {"text": product["description"]},
            "filter": {"$and": conditions},
            "numCandidates": 2000,
            "limit": 1000,
        }},
        {"$project": {"_id": 1, "accounts": 1, "score": {"$meta": "vectorSearchScore"}}},
    ]
    return list(customers.aggregate(pipeline))


def pick_top(cands):
    """cands: [(percentile, score, product_id, name, type)] -> top-N list for one customer."""
    ranked, per_type = [], defaultdict(int)
    for pct, score, pid, name, ptype in sorted(cands, reverse=True):
        if pct < MIN_PERCENTILE or per_type[ptype] >= MAX_PER_TYPE:
            continue
        ranked.append({"product_id": pid, "name": name, "percentile": round(pct, 3), "score": round(score, 3)})
        per_type[ptype] += 1
        if len(ranked) == TOP_N:
            break
    return ranked


def percentile_in(dist, score):
    """Where a score sits in a product's saved distribution (0 = worst, 1 = best)."""
    if not dist:
        return 0.0
    return bisect_right(dist, score) / len(dist)


def score_customers(only_ids=None, dists=None, log=print):
    """Run the 15 searches. Returns (candidates per customer, raw score lists per product)."""
    candidates, raw = defaultdict(list), {}
    catalogue = list(products.find().sort("_id", 1))
    for i, p in enumerate(catalogue):
        if i:
            time.sleep(PAUSE_S)
        hits = eligible_customers_for(p, only_ids)
        raw[p["_id"]] = sorted(h["score"] for h in hits)
        blocked = OWNED_TYPE_BLOCKS.get(p["_id"], set())
        n, kept = len(hits), 0
        for rank, h in enumerate(hits):                    # hits come back best-first
            if owned_types(h) & blocked:
                continue
            if dists is None:                              # full refresh: rank among everyone
                pct = 1.0 if n == 1 else 1 - rank / (n - 1)
            else:                                          # live: compare with the saved distribution
                pct = percentile_in(dists.get(p["_id"], []), h["score"])
            candidates[h["_id"]].append((pct, h["score"], p["_id"], p["name"], p["type"]))
            kept += 1
        if only_ids is None:
            scores = raw[p["_id"]] or [0]
            log(f"{p['_id']} {p['name']:<26} eligible={n:>4} kept={kept:>4} "
                f"score min/max={min(scores):.3f}/{max(scores):.3f}")
    return candidates, raw


def write_recommendations(ids, candidates, mode):
    """Save top-N for each customer in ids (customers with no candidates get an empty list)."""
    now = datetime.now(timezone.utc)
    ops, with_recs = [], 0
    proj = {"profile_hash": 1, "kyc_status": 1, "segment": 1, "risk_tier": 1,
            "income_lpa": 1, "_deleted": 1, "accounts": 1}
    for doc in customers.find({"_id": {"$in": list(ids)}}, proj):
        recs = pick_top(candidates.get(doc["_id"], []))
        with_recs += bool(recs)
        ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": {
            "recommendations": recs,
            "recs_model": MODEL,
            "recs_input_hash": recs_input_hash(doc),
            "recs_computed_at": now,
            "recs_mode": mode,
        }}))
    for i in range(0, len(ops), 500):
        customers.bulk_write(ops[i:i + 500], ordered=False)
    return with_recs, len(ops)


def main():
    started = time.time()
    candidates, raw = score_customers()
    meta.replace_one({"_id": "score_distributions"},
                     {"model": MODEL, "computed_at": datetime.now(timezone.utc), "products": raw},
                     upsert=True)
    ids = [d["_id"] for d in customers.find({"_deleted": {"$ne": True}}, {"_id": 1})]
    with_recs, total = write_recommendations(ids, candidates, mode="full")

    print(f"\nDone in {int(time.time() - started)} s: {with_recs} of {total} customers have recommendations")
    harsh = customers.find_one({"_id": 421}, {"profile_text": 1, "recommendations": 1})
    if harsh:
        print("\nCustomer 421:", harsh.get("profile_text"))
        for r in harsh.get("recommendations", []):
            print(f"  {r['product_id']} {r['name']:<26} percentile={r['percentile']}  score={r['score']}")


if __name__ == "__main__":
    main()
