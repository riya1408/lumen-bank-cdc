"""How often does each product appear in someone's top 3? (Lumen Bank recommendations check)"""
import os

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
c = MongoClient(os.environ["MONGO_URI"])[os.environ.get("MONGO_DB", "lumen_bank")]["customer_360"]

with_recs = c.count_documents({"recommendations.0": {"$exists": True}})
print(f"Customers with recommendations: {with_recs}\n")
print(f"{'Product':<26} {'in top 3':>9} {'as #1':>7}")
rows = c.aggregate([
    {"$unwind": {"path": "$recommendations", "includeArrayIndex": "pos"}},
    {"$group": {"_id": "$recommendations.name", "n": {"$sum": 1},
                "first": {"$sum": {"$cond": [{"$eq": ["$pos", 0]}, 1, 0]}}}},
    {"$sort": {"n": -1}},
])
for r in rows:
    print(f"{r['_id']:<26} {r['n']:>9} {r['first']:>7}")
