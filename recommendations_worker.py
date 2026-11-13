"""
Lumen Bank - live recommendations worker.

Watches customer_360 with a MongoDB change stream. When the CDC consumer changes something a
recommendation depends on (spending, accounts, segment, income, KYC, risk), it:
  1. rebuilds profile_text; if the text changed, saves it (Atlas re-embeds it automatically)
  2. checks recs_input_hash; if unchanged, nothing can change, so it stops there (0 searches)
  3. otherwise queues the customer

Queued customers are processed in MICRO-BATCHES: one round of 15 product searches filtered to
just the queued customers. A round costs the same 15 searches whether 1 or 500 customers
changed, which is what keeps us inside the free tier's ~3 searches/minute.

Run (with the stack and consumer_360.py running):  python recommendations_worker.py
Stop with Ctrl+C. Don't run refresh_recommendations.py at the same time.
"""
import hashlib
import time

from pymongo import UpdateOne

from build_profiles import build_profile_text
from refresh_recommendations import (customers, meta, recs_input_hash, score_customers,
                                     write_recommendations)

# Fields written by the CDC consumer that can change a recommendation.
WATCHED = {"spend_30d", "accounts", "segment", "annual_income", "city", "kyc_status",
           "risk_tier", "_deleted"}
SETTLE_S = 45      # wait for changes to stop arriving (and for Atlas to re-embed) before a round
MAX_WAIT_S = 180   # but never let a queued customer wait longer than this


def refresh_profile(doc):
    """Rebuild profile_text + eligibility hash. Returns True if recommendations need recomputing."""
    text = build_profile_text(doc)
    phash = hashlib.sha256(text.encode()).hexdigest()[:16]
    income_lpa = round((doc.get("annual_income") or 0) / 100_000, 2)
    if phash != doc.get("profile_hash") or income_lpa != doc.get("income_lpa"):
        customers.update_one({"_id": doc["_id"]}, {"$set": {
            "profile_text": text, "profile_hash": phash, "income_lpa": income_lpa}})
        doc.update(profile_text=text, profile_hash=phash, income_lpa=income_lpa)
    return recs_input_hash(doc) != doc.get("recs_input_hash")


def run_round(ids):
    dists = (meta.find_one({"_id": "score_distributions"}) or {}).get("products", {})
    started = time.time()
    print(f"[round] scoring {len(ids)} customer(s) with 15 product searches ...")
    candidates, _ = score_customers(only_ids=ids, dists=dists, log=lambda *_: None)
    with_recs, total = write_recommendations(ids, candidates, mode="live")
    print(f"[round] done in {int(time.time() - started)} s: {with_recs}/{total} have recommendations")
    for d in customers.find({"_id": {"$in": list(ids)[:5]}}, {"recommendations": 1}):
        names = ", ".join(r["name"] for r in d.get("recommendations", [])) or "(none)"
        print(f"         customer {d['_id']}: {names}")


def main():
    pending, first_at, last_at = set(), None, None
    pipeline = [{"$match": {"operationType": {"$in": ["insert", "update", "replace"]}}}]
    print("Watching customer_360 for profile changes ... (Ctrl+C to stop)")
    with customers.watch(pipeline, full_document="updateLookup") as stream:
        while True:
            change = stream.try_next()
            if change:
                if change["operationType"] == "update":
                    fields = {k.split(".")[0] for k in
                              list(change["updateDescription"].get("updatedFields", {})) +
                              list(change["updateDescription"].get("removedFields", []))}
                    # Ignore our own writes (profile_text, recommendations...) or we'd loop forever.
                    if not fields & WATCHED:
                        continue
                doc = change.get("fullDocument")
                if doc and refresh_profile(doc):
                    if doc["_id"] not in pending:
                        print(f"queued customer {doc['_id']}")
                    pending.add(doc["_id"])
                    first_at = first_at or time.time()
                    last_at = time.time()
                continue

            now = time.time()
            if pending and (now - last_at >= SETTLE_S or now - first_at >= MAX_WAIT_S):
                batch, pending, first_at, last_at = pending, set(), None, None
                run_round(batch)
            else:
                time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
