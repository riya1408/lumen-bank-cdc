# Lumen Bank: Real-time Customer 360 with Oracle CDC

**Live demo:** https://lumen-dashboard-682652351786.asia-south1.run.app
(Runs on Google Cloud Run and scales to zero, so the first load may take a few seconds.)

A simulated bank whose Oracle core system streams every change through **Debezium** and **Kafka** into a live **Customer 360** in **MongoDB Atlas**, with real-time spend profiles, fraud alerts and **product recommendations** (Atlas Vector Search with Voyage AI embeddings), shown on a **Streamlit** dashboard deployed to **Cloud Run** with **Terraform** and keyless **GitHub Actions**.

![Demo: a fraud scenario in Oracle (right) raises an alert on the dashboard (left) on its own](docs/demo.gif)

*Demo: `simulate_bank.py fraud` uses one customer's card in several cities within seconds. Debezium → Kafka → the Customer 360 consumer detects the pattern and the alert appears on the dashboard (the middle part is sped up 3×).*

> **Lumen Bank is fictional and all data is synthetic.** The data pipeline runs on a single laptop (Docker Desktop on Windows/WSL2) with a free-tier MongoDB Atlas cluster; the dashboard runs on Google Cloud.

---

## The problem it solves

A bank's core system (here, Oracle) is the system of record, but:
- a customer's data is spread across many tables (profile, accounts, transactions),
- new applications aren't allowed to run heavy queries against the core database,
- reporting usually comes from overnight batch jobs, so it's hours old.

This project streams every committed change out of Oracle (**change data capture**, no changes to the core system) and maintains **one always-current document per customer** in a separate database that apps can read freely, plus **alerts and product recommendations computed from the event stream**.

## Architecture

```mermaid
flowchart LR
    A[(Oracle 26ai Free<br/>CUSTOMER · ACCOUNTS · TRANSACTIONS)] -- redo log / LogMiner --> B[Debezium<br/>Kafka Connect]
    B -- 3 topics --> C[[Kafka 4.3<br/>KRaft]]
    C --> D[consumer_360.py<br/>Customer 360 builder]
    D -- guarded upserts + recomputed fields --> E[(MongoDB Atlas<br/>customer_360 · transactions · products)]
    E -- change stream --> W[recommendations_worker.py<br/>stable profiles + micro-batches]
    W -- profile_text, top 3 --> E
    E -. Automated Embedding<br/>voyage-4 .-> V[Atlas Vector Search]
    W -- 15 filtered searches per round --> V
    E --> F[Streamlit dashboard<br/>Cloud Run]
    G[simulate_bank.py] -. live activity .-> A
```

| Component | Role |
|---|---|
| Oracle AI Database 26ai Free | Lumen Bank's core system: 1,000 customers, 1,838 accounts, 16,911 seeded transactions |
| Debezium 3.6 (Kafka Connect) | Snapshot of existing rows (`op=r`), then every insert / update / delete from the redo log |
| Kafka 4.3 (KRaft) | One topic per table, keyed by primary key; buffers bursts safely |
| `consumer_360.py` | Merges the three topics into one document per customer; computes balances, 30-day spend, last 10 transactions and fraud alerts |
| MongoDB Atlas (M0) | `customer_360` (current state), `transactions` (90-day hot window, TTL index), `products` (15-product catalogue) |
| Atlas Vector Search + Automated Embedding | Embeds product descriptions and customer profiles with `voyage-4` on write; eligibility rules as pre-filters |
| `refresh_recommendations.py` / `recommendations_worker.py` | Full refresh (15 searches for all customers) and live updates for changed customers |
| `dashboard.py` on Cloud Run | Bank overview, live transaction feed, alerts, customer lookup with recommendations |
| `simulate_bank.py` | Everyday payments, salary-day burst, fraud attack, KYC expiry, address change |

### The Customer 360 document
A real document from the pipeline (trimmed):
```json
{
  "_id": 421, "name": "Harsh Iyer", "city": "Hyderabad", "segment": "AFFLUENT",
  "kyc_status": "VERIFIED", "risk_tier": "LOW", "_last_scn": 3120882, "_last_txseq": 0,
  "accounts": {
    "100775": { "type": "SAVINGS",     "balance": 1080897.35, "status": "ACTIVE", "_scn": 3189997, "_txseq": 96 },
    "100776": { "type": "CREDIT_CARD", "balance": -300177.28, "status": "ACTIVE", "_scn": 3180305, "_txseq": 2 }
  },
  "spend_30d": { "SHOPPING": 261460.66, "RENT": 47800, "TRAVEL": 8575.6, "GROCERY": 6769.78, "FUEL": 5500.18 },
  "recent_transactions": [ "...last 10..." ],
  "profile_text": "Affluent customer in Hyderabad, income band 15-25 LPA. Spends mostly on: shopping, travel, grocery. Pays monthly rent. Owns: credit card, savings account.",
  "recommendations": [
    { "product_id": "P09", "name": "Home Loan",          "percentile": 0.772, "score": 0.622 },
    { "product_id": "P07", "name": "Monthly SIP Plan",   "percentile": 0.673, "score": 0.627 },
    { "product_id": "P14", "name": "Travel Insurance",   "percentile": 0.648, "score": 0.603 }
  ]
}
```

---

## Key results

| What | Result |
|---|---|
| Initial load (snapshot) | **19,749 rows** from 3 tables → Kafka → MongoDB, counts matched at every stage, 0 missing |
| Customer 360 build | 16,911 transactions in **79.5 s** (~210 events/s, including recomputing derived fields) on a free-tier cluster |
| Salary-day burst | **906 salaries = 1,812 events** produced in 1.9 s; absorbed in ~20 s with **0 lost, 0 rejected**; Kafka buffered the difference |
| Fraud scenario | Alert on the customer's 360 view within seconds of the last card payment |
| Crash recovery | Rewound consumer offsets to simulate a crash: replayed events **rejected as stale**, data unchanged |
| Throughput tuning (single-table stage) | **237 → 308 → 430 events/s** via batching, then parallel writers partitioned by key |
| Profile embeddings | 1,000 customer profiles embedded by Atlas in **84 s** (first index build isn't rate-limited) |
| Full recommendation refresh | **15 searches in ~308 s** for all customers, instead of 1,000 searches (~5.5 h at 3 searches/min) |
| Recommendation bias fix | Generic "EveryDay Cashback" card in top 3 for **816 → 314** of ~900 customers; 14 of 15 products now reach their best-fit customers |

---

## Product recommendations

Each customer gets their **top 3 eligible products** from a 15-product catalogue, kept up to date from the CDC stream.

### Rules decide, AI ranks
Eligibility (income floor, segment, verified KYC, allowed risk tier, products already held) is **hard rules**, applied as vector-search **pre-filters** and in code. Vector search only **ranks** what is allowed: similarity can't know that a 5 LPA customer may not hold a 12 LPA travel card. Expiring a customer's KYC removes all their recommendations, because no product passes the rules.

### Atlas Automated Embedding instead of an embedding loader
Atlas embeds `products.description` and `customer_360.profile_text` with **voyage-4** on every write, and embeds query text at search time with the same model. No embedding code, no model mismatch, and the first full build isn't rate-limited (1,000 profiles in 84 s). Model choice was driven by the free allowance: `voyage-3.5` had **0** free tokens on this account, `voyage-4` had 200 M.

### Stable profiles
`profile_text` uses coarse buckets (income band, top-3 spend categories, products held), never exact amounts, so a salary credit or one more dinner doesn't change it. A content hash gates every write: unchanged text means no re-embedding and no recommendation work.

### The query is flipped to fit the rate limit
The free cluster allows ~3 searches per minute. One search per customer would take ~5.5 hours for 1,000 customers. Instead, **one search per product** ("which eligible customers fit this product?") scores everyone at once: **15 searches, ~5 minutes**.

### Percentiles, not raw scores
Raw cosine scores aren't comparable across products: a generic description ("groceries, fuel, online shopping") scores high for almost everyone. Before the fix, one cashback card was #1 for **712** customers. Each score is now converted to a **percentile within that product's eligible customers**, with a minimum of the top half and at most 2 products of one type per customer.

### Live updates in micro-batches
`recommendations_worker.py` watches `customer_360` with a **change stream**, rebuilds the profile, and queues a customer only if anything a recommendation depends on changed (profile, KYC, segment, income, risk, products held). Queued customers are scored together in one round of 15 searches filtered to their IDs, so a burst of 500 changes costs the same as one. The worker ignores its own writes (otherwise each write would trigger itself forever) and reuses the full refresh's saved score distributions, so live and batch percentiles mean the same thing.

### Limitations
There are no click or conversion outcomes in a synthetic bank, so recommendations were evaluated by **coverage and spot checks**, not accuracy. In production they would be tuned on outcome data with A/B tests. Percentiles can also over-correct: they spread weak-fit products as widely as strong ones.

---

## Cloud deployment

| Piece | How |
|---|---|
| Container | Slim Python image, non-root user, layers ordered for caching, `.env` excluded via `.dockerignore` |
| Cloud Run (asia-south1) | Min instances 0 (scale to zero), max 1, request-based CPU; database credentials from **Secret Manager** at runtime |
| Identities | Least-privilege service accounts: the dashboard can read one secret; the deployer can push to one repository and deploy one service |
| Terraform | Every resource in code, existing ones brought in with `import` blocks until the plan showed **no changes**; `prevent_destroy` on stateful resources; state in a **versioned GCS bucket** |
| CI/CD | GitHub Actions on push to `main`: build, push to Artifact Registry, deploy. **Workload Identity Federation**, so there are no service account keys, and only this repo's `main` branch can deploy. Images are tagged with the commit SHA for traceable rollbacks |
| Cost guards | Budget alert, Artifact Registry cleanup policy (keep last 3 images), Terraform owns settings while deploys only change the image |

---

## Quickstart

**Requirements:** Docker Desktop (about 4 GB free RAM), Python 3.10+, a MongoDB Atlas cluster (free tier is fine).

```bash
# 1. Configure
cp .env.example .env              # fill in passwords and MONGO_URI; MONGO_DB=lumen_bank
pip install -r requirements.txt
# Oracle JDBC driver for Debezium: download ojdbc11.jar from Maven Central
# (com.oracle.database.jdbc:ojdbc11) and place it in the repo root.

# 2. Start Oracle, Kafka and Kafka Connect (Oracle is configured for CDC automatically)
docker compose up -d
docker compose logs oracle-free | grep -E "cdc-setup|migration-02"   # expect both "... Done."

# 3. Create the bank, then start capturing it
python seed_bank.py               # 1,000 customers / 1,838 accounts / 16,911 transactions (fixed seed)
python register_connector.py      # Debezium snapshots all three tables

# 4. Build the Customer 360 and open the dashboard
python consumer_360.py            # terminal 1
python -m streamlit run dashboard.py    # terminal 2

# 5. Recommendations
python test_auto_embed.py         # product catalogue + its auto-embedding index
python build_profiles.py          # stable customer profiles + their auto-embedding index
python refresh_recommendations.py # full refresh (~5 min on the free tier)
python recommendations_worker.py  # terminal 3: live updates

# 6. Make the bank run
python simulate_bank.py normal --seconds 120
python simulate_bank.py fraud
python simulate_bank.py salary-day
python simulate_bank.py kyc-expire --customer 42
python simulate_bank.py move --customer 42 --city Pune
```

| Command | Effect |
|---|---|
| `docker compose stop` | Stop everything, keep data |
| `docker compose up -d` | Start again (setup scripts detect they already ran and skip) |
| `docker compose down -v` | Delete containers **and** data for a completely fresh start |

---

## Design decisions (CDC pipeline)

### 1. One version guard per source row
Kafka delivers **at least once**, so every write must be safe to repeat. Each Oracle row has its own version in MongoDB (Debezium's `scn` + `txSeq`): the customer row guards the top-level fields, and **each account guards its own entry** under `accounts.<id>`. A change is applied only if it's newer, with the check and the write in **one atomic upsert**. Per-row versions matter because topics aren't ordered relative to each other: an older account change must not be blocked by a newer profile change.

### 2. SCN alone is not enough
Rows changed in one Oracle transaction can share an SCN (and one transaction can span several SCNs). `txSeq`, the position inside the transaction, breaks ties. I reproduced the "silently dropped second update" bug live before fixing it.

### 3. Events arrive in any order
Three topics have no ordering between them. Whichever event arrives first creates the customer document (a **stub**); the others fill in their own fields with `$set`, never overwriting each other.

### 4. Transactions find their customer through a lookup, with a durable waiting room
A transaction row only knows its account. The consumer maps account → customer via `account_index`; transactions whose account hasn't arrived yet are **parked in MongoDB** (`pending_txns`) and retried every batch. Parking in memory would lose them on a crash, because offsets are committed after each batch.

### 5. Derived fields are recomputed, never incremented
`net_position`, `spend_30d`, `recent_transactions` and `alerts` are recomputed from stored data for the customers each batch touched. Incrementing (`$inc`) would double-count on a replay.

### 6. Time-window fields need a heartbeat
A 30-day total or a 24-hour alert goes stale as **time** passes, even with no new events: a fraud alert stayed visible for over a day because nothing touched that customer again. An idle-time heartbeat now recomputes time-window fields in small batches, and the dashboard also filters alerts to the last 24 hours.

### 7. Operational store vs history
MongoDB keeps **current state plus a 90-day hot window** of transactions (TTL index), which keeps the free cluster small. Full history belongs in an analytics store (next project: a lakehouse with Spark and Delta Lake).

### 8. Soft deletes and commit-after-write
Deletes keep the document with `_deleted: true` **and its version**, so a replayed older insert can't resurrect it. Offsets are committed only after the MongoDB write: a crash means replay, never loss.

### 9. Reproducible setup
Health-checked start order, named volumes, **idempotent numbered Oracle scripts** (CDC setup, then the bank schema), a fixed random seed for the data, secrets only in `.env`, a least-privilege LogMiner user, and `.gitattributes` to keep shell scripts in LF format.

---

## Fraud rule

> **5+ card payments across 3+ different cities within any 10-minute window** (checked over the last 24 hours) → `UNUSUAL_CARD_ACTIVITY` on the customer's document.

It's a simple streaming rule, not an ML model, but it shows the pattern: raw CDC events become a business signal within seconds, without touching the core system.

---

## What I'd do differently in production

- **Network:** private connectivity or a fixed egress IP (Cloud NAT) to the database instead of allowing all IPs, and a **read-only database user** for the dashboard.
- **Events:** Avro or Protobuf with a schema registry, and a **dead-letter topic** for poison messages.
- **Observability:** alerts on consumer lag, worker queue size and recommendation round duration; a scheduled Oracle-vs-MongoDB **reconciliation** job.
- **Recommendations:** tune on click and conversion data with A/B tests; a paid tier (higher search limits) would allow per-customer queries for small changes.
- **Scale:** more Kafka partitions with consumers in a group, and the worker as a Cloud Run job or service.

---

## Earlier stages of the project

<details>
<summary>Single-table load test (batching, parallel writers) and failure tests</summary>

Each round inserted 10,000 rows into Oracle (commit every 1,000) and measured each change's journey using three timestamps: Oracle commit, Kafka message, MongoDB write. One variable changed per round.

| Round | Change | Throughput | End-to-end p50 / p95 | Kafka→MongoDB p50 / p95 | Avg batch write |
|---|---|---|---|---|---|
| 1 | Batch 500 | 210 ev/s | 28.0s / 51.8s | – | – |
| 2 | Batch 500, stages split | 237 ev/s | 27.2s / 45.2s | 23.6s / 40.8s | 2.09s |
| 3 | Batch 2000 | 308 ev/s | 22.0s / 35.9s | 18.1s / 31.4s | 6.41s |
| 4 | Batch 2000 + 4 parallel writers | **430 ev/s** | 16.8s / 24.7s | 14.8s / 22.3s | 4.54s |

- The consumer writing to Atlas was the bottleneck, not Debezium (~2–4 s).
- Batch time ≈ 0.65 s per round trip + 2.9 ms per document, so bigger batches had diminishing returns.
- Parallel writers (split by customer ID to keep per-key order) gave 1.4×, not 4×: the bottleneck moved to the shared free-tier cluster.
- Clock skew between container and host checked: < 200 ms.

| Failure test | Result |
|---|---|
| Insert → update → delete | ACCEPTED (c), ACCEPTED (u), DELETED, tombstone skipped |
| Restart with the same consumer group | Nothing reprocessed |
| Crash replay (`--reset-offsets --shift-by -4`) | All replayed events rejected as stale |
| Laptop sleep (session timeout) | Partition revoked, consumer rejoined and resumed from its committed offset |
</details>

## Bugs and lessons along the way

- **Same-SCN updates silently dropped** → `txSeq` tiebreaker. The bug was first hidden by an unrelated Debezium resend; testing on purpose found it.
- **Stale time-window fields** → heartbeat refresh (design decision 6).
- **Oracle `setup` scripts never ran** because the image ships a pre-built database → moved to `startup`, made idempotent.
- **A rebuilt database starts a new SCN timeline** → a fresh MongoDB database for the new stack, or every event looks stale.
- **`kafka-python` couldn't complete group coordination with Kafka 4.3 on Windows** → `confluent-kafka`.
- **Kafka Connect refused to start**: its offsets topic had `cleanup.policy=delete` instead of `compact`.
- **Decimals arrived as base64** → `decimal.handling.mode=string`; IDs and amounts are converted consistently so `"1"` and `1` never mismatch.
- **One product won almost every recommendation** → raw similarity favoured a generic description; fixed with per-product percentiles (counted before and after, not eyeballed).
- **Terraform plan wanted to clear two Cloud Run settings** (`cpu_idle`, `startup_cpu_boost`) after import, which could have changed billing → added to the config until the plan showed no changes.

## Project structure

| Path | Purpose |
|---|---|
| `docker-compose.yml` | Oracle, Kafka (KRaft) and Kafka Connect with health checks and volumes |
| `oracle/startup/01_cdc_setup.sh` | Idempotent: ARCHIVELOG, supplemental logging, LogMiner user, BANK user |
| `oracle/startup/02_bank_schema.sh` | Idempotent: Lumen Bank tables with supplemental logging |
| `seed_bank.py` | Reproducible synthetic bank (fixed seed) |
| `connector.json`, `register_connector.py` | Debezium connector on three tables; idempotent registration (PUT) |
| `consumer_360.py` | Customer 360 builder |
| `test_auto_embed.py` | Product catalogue + its Automated Embedding index |
| `build_profiles.py` | Stable customer profiles + their Automated Embedding index |
| `refresh_recommendations.py` | Full refresh: 15 filtered searches, percentile ranking, saved score distributions |
| `recommendations_worker.py` | Live updates from a change stream, in micro-batches |
| `rec_stats.py` | How often each product is recommended (bias check) |
| `dashboard.py`, `Dockerfile`, `requirements-dashboard.txt` | Live Streamlit dashboard and its container |
| `infra/` | Terraform: Artifact Registry, Cloud Run, Secret Manager, service accounts, Workload Identity Federation, remote state |
| `.github/workflows/deploy.yml` | Keyless build-and-deploy on push to `main` |
| `simulate_bank.py` | Live bank activity and scenarios |
| `consumer_v1.py`, `consumer_v2.py`, `consumer_load.py`, `consumer_parallel.py` | Earlier single-table stages (version guard, deletes, load tests), kept for reference |
| `.env.example` | Required settings, no real values |

## Roadmap

- [x] **Cloud**: dashboard on Cloud Run with Terraform, GitHub Actions (keyless) and Secret Manager
- [x] **CDC-driven recommendations**: stable profiles, Atlas Automated Embedding, rate-limit-aware refresh and live micro-batches
- [ ] **Hardening**: Oracle-vs-MongoDB reconciliation, consumer lag alerts, dead-letter topic, pytest suite
- [ ] **Next project**: a batch lakehouse on the same bank (PySpark on Databricks, Delta Lake with SCD Type 2, dbt, Airflow)
