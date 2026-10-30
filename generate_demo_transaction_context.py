"""Add fictional device, region, payment-purpose, and invoice context to ARGUS.

Run after the account and transaction demo data exists:
    python generate_demo_transaction_context.py

This script adds rows to demo-only context tables. It does not update or delete
accounts, transactions, balances, alerts, entity profiles, or case records.
Every generated value is fictional and must never be presented as a real
identity, device, location, invoice, or background check.
"""

from datetime import datetime, timezone
import random
import time

from database import DB_PATH, get_connection


BATCH_SIZE = 5000
SEED = 6_281_047
ACCOUNT_CURSOR_KEY = "demo_context_account_cursor"
TRANSACTION_CURSOR_KEY = "demo_context_transaction_rowid"
REGIONS = (
    "Demo City North", "Demo City West", "Demo City Central",
    "Demo City South", "Demo City East",
)
CHANNELS = ("UPI", "IMPS", "NEFT", "Card", "Wallet")
CHANNEL_WEIGHTS = (42, 18, 20, 12, 8)


def number_from_id(value):
    digits = "".join(character for character in str(value) if character.isdigit())
    return int(digits or 0)


def account_context(row):
    account_id = str(row["account_id"])
    number = number_from_id(account_id)
    rng = random.Random(SEED + number)
    region = str(row["city"] or rng.choice(REGIONS))

    # A small fictional shared-device group exists for demonstrating why a
    # shared device is a lead to review, not proof that accounts are linked.
    if rng.random() < 0.002:
        device = f"DEMO-SHARED-{number % 20:03d}"
        device_context = "Fictional shared-device example"
    else:
        device = f"DEMO-DEVICE-{number:06d}"
        device_context = "Fictional account device"

    return (
        account_id, region, device, device_context, "SYNTHETIC_DEMO",
        datetime.now(timezone.utc).isoformat(),
    )


def purpose_for(description, rng):
    text = str(description or "").lower()
    if any(word in text for word in ("salary", "payroll", "wage")):
        return "Salary or payroll (inferred from demo description)"
    if any(word in text for word in ("invoice", "supplier", "business", "service")):
        return "Business payment (inferred from demo description)"
    if any(word in text for word in ("retail", "purchase", "order", "product")):
        return "Purchase (inferred from demo description)"
    if any(word in text for word in ("utility", "electric", "water", "bill")):
        return "Bill payment (inferred from demo description)"
    return rng.choice((
        "Personal transfer (demo label)",
        "General payment (demo label)",
        "Purpose not stated in demo record",
    ))


def ensure_demo_context_schema():
    """Create only the two additive demo context tables and their indexes."""
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS demo_account_context (
                account_id TEXT PRIMARY KEY,
                usual_region TEXT NOT NULL,
                primary_device_id TEXT NOT NULL,
                device_context TEXT NOT NULL DEFAULT 'SYNTHETIC_DEMO',
                context_source TEXT NOT NULL DEFAULT 'SYNTHETIC_DEMO',
                generated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(account_id) REFERENCES accounts(account_id)
            );
            CREATE TABLE IF NOT EXISTS demo_transaction_context (
                transaction_id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL,
                origin_region TEXT NOT NULL,
                payment_channel TEXT NOT NULL,
                payment_purpose TEXT NOT NULL,
                merchant_category TEXT NOT NULL DEFAULT 'Not applicable',
                invoice_reference TEXT,
                context_source TEXT NOT NULL DEFAULT 'SYNTHETIC_DEMO',
                generated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(transaction_id) REFERENCES transactions(transaction_id)
            );
            CREATE INDEX IF NOT EXISTS idx_demo_account_device
                ON demo_account_context(primary_device_id);
            CREATE INDEX IF NOT EXISTS idx_demo_transaction_device
                ON demo_transaction_context(device_id);
            CREATE INDEX IF NOT EXISTS idx_demo_transaction_region
                ON demo_transaction_context(origin_region);
        """)


def main():
    started = time.perf_counter()
    ensure_demo_context_schema()
    print(f"Database: {DB_PATH}")
    print("Generated device, region, channel, purpose, and invoice values are fictional demo data.")
    print("Existing accounts, transactions, balances, alerts, and historical records will not be changed.")

    with get_connection() as conn:
        accounts_total = int(conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0])
        transactions_total = int(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
        account_cursor = str(conn.execute(
            "SELECT value FROM argus_metadata WHERE key = ?", (ACCOUNT_CURSOR_KEY,)
        ).fetchone()[0]) if conn.execute(
            "SELECT 1 FROM argus_metadata WHERE key = ?", (ACCOUNT_CURSOR_KEY,)
        ).fetchone() else ""
        transaction_cursor_row = conn.execute(
            "SELECT value FROM argus_metadata WHERE key = ?", (TRANSACTION_CURSOR_KEY,)
        ).fetchone()
        transaction_cursor = int(transaction_cursor_row[0]) if transaction_cursor_row else 0

    accounts_done = 0
    while True:
        with get_connection() as conn:
            batch = list(conn.execute("""
                SELECT a.account_id, p.city
                FROM accounts AS a
                LEFT JOIN entity_profiles AS p ON p.account_id = a.account_id
                WHERE a.account_id > ?
                ORDER BY a.account_id LIMIT ?
            """, (account_cursor, BATCH_SIZE)))
            if not batch:
                break
            rows = [account_context(row) for row in batch]
            conn.executemany("""
                INSERT OR IGNORE INTO demo_account_context
                    (account_id, usual_region, primary_device_id,
                     device_context, context_source, generated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, rows)
            account_cursor = str(batch[-1]["account_id"])
            conn.execute("""
                INSERT INTO argus_metadata(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """, (ACCOUNT_CURSOR_KEY, account_cursor))
        accounts_done += len(batch)
        print(f"\rAccount context: {min(accounts_done, accounts_total):,}/{accounts_total:,}", end="", flush=True)
    print()

    transactions_done = 0
    while True:
        with get_connection() as conn:
            batch = list(conn.execute("""
                SELECT t.rowid AS source_rowid, t.transaction_id,
                       t.sender_id, t.receiver_id, t.description,
                       s.primary_device_id, s.usual_region,
                       rp.entity_kind AS receiver_kind,
                       rp.industry AS receiver_industry
                FROM transactions AS t
                JOIN demo_account_context AS s ON s.account_id = t.sender_id
                LEFT JOIN entity_profiles AS rp ON rp.account_id = t.receiver_id
                WHERE t.rowid > ?
                ORDER BY t.rowid LIMIT ?
            """, (transaction_cursor, BATCH_SIZE)))
            if not batch:
                break

            generated = []
            now_text = datetime.now(timezone.utc).isoformat()
            for row in batch:
                transaction_id = str(row["transaction_id"])
                rng = random.Random(SEED + number_from_id(transaction_id))
                region = str(row["usual_region"] or rng.choice(REGIONS))
                if rng.random() < 0.02:
                    alternatives = [item for item in REGIONS if item != region]
                    region = rng.choice(alternatives)
                device = str(row["primary_device_id"])
                if rng.random() < 0.01:
                    device = f"DEMO-ALT-DEVICE-{number_from_id(transaction_id) % 100000:05d}"

                purpose = purpose_for(row["description"], rng)
                merchant_category = "Not applicable"
                if str(row["receiver_kind"] or "").lower() == "business":
                    merchant_category = str(row["receiver_industry"] or "Business category not stated")
                invoice_reference = None
                business_words = ("business", "supplier", "invoice", "service", "purchase", "retail")
                if any(word in str(row["description"] or "").lower() for word in business_words) and rng.random() < 0.65:
                    invoice_reference = f"DEMO-INV-{number_from_id(transaction_id):012d}"

                generated.append((
                    transaction_id, device, region,
                    rng.choices(CHANNELS, weights=CHANNEL_WEIGHTS, k=1)[0],
                    purpose, merchant_category, invoice_reference,
                    "SYNTHETIC_DEMO", now_text,
                ))

            conn.executemany("""
                INSERT OR IGNORE INTO demo_transaction_context
                    (transaction_id, device_id, origin_region, payment_channel,
                     payment_purpose, merchant_category, invoice_reference,
                     context_source, generated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, generated)
            transaction_cursor = int(batch[-1]["source_rowid"])
            conn.execute("""
                INSERT INTO argus_metadata(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """, (TRANSACTION_CURSOR_KEY, transaction_cursor))

        transactions_done += len(batch)
        print(f"\rTransaction context: {min(transactions_done, transactions_total):,}/{transactions_total:,}", end="", flush=True)
    print()

    with get_connection() as conn:
        account_context_count = int(conn.execute("SELECT COUNT(*) FROM demo_account_context").fetchone()[0])
        transaction_context_count = int(conn.execute("SELECT COUNT(*) FROM demo_transaction_context").fetchone()[0])
        conn.execute("""
            INSERT INTO argus_metadata(key, value) VALUES ('demo_context_dataset', 'synthetic-demo-v1')
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """)

    print("Fictional demo context generation finished.")
    print(f"Account context rows: {account_context_count:,}")
    print(f"Transaction context rows: {transaction_context_count:,}")
    print(f"Elapsed: {time.perf_counter() - started:,.1f}s")
    print("No existing transaction, account, balance, alert, profile, or case row was modified.")


if __name__ == "__main__":
    main()
