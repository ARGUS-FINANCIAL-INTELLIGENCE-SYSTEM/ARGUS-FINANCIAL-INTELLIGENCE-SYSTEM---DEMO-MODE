
from pathlib import Path
import random
import sqlite3
from contextlib import contextmanager

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "argus.db"

BUSY_TIMEOUT_MS = 30_000


@contextmanager
def get_connection():
    """Yield a configured SQLite connection and always close it."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(
        DB_PATH,
        timeout=BUSY_TIMEOUT_MS / 1000,
        isolation_level="DEFERRED",
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA temp_store = MEMORY")
    connection.execute("PRAGMA cache_size = -64000")

    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


@contextmanager
def get_write_connection():
    """Connection context intended for batched writes."""
    with get_connection() as conn:
        conn.execute("BEGIN")
        yield conn


def initialize_database():
    """Create tables and safely migrate existing account balances."""
    with get_connection() as conn:
        conn.execute("PRAGMA journal_mode = WAL")

        conn.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                account_id TEXT PRIMARY KEY,
                entity_name TEXT NOT NULL,
                account_type TEXT NOT NULL DEFAULT 'individual',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS transactions (
                transaction_id TEXT PRIMARY KEY,
                sender_id TEXT NOT NULL,
                receiver_id TEXT NOT NULL,
                amount REAL NOT NULL CHECK(amount >= 0),
                currency TEXT NOT NULL DEFAULT 'INR',
                timestamp TEXT NOT NULL,
                description TEXT DEFAULT '',
                FOREIGN KEY(sender_id) REFERENCES accounts(account_id),
                FOREIGN KEY(receiver_id) REFERENCES accounts(account_id)
            );

            CREATE TABLE IF NOT EXISTS alerts (
                alert_id TEXT PRIMARY KEY,
                transaction_id TEXT,
                account_id TEXT,
                risk_score REAL NOT NULL DEFAULT 0,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Open',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(transaction_id)
                    REFERENCES transactions(transaction_id),
                FOREIGN KEY(account_id)
                    REFERENCES accounts(account_id)
            );

            CREATE TABLE IF NOT EXISTS cases (
                case_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Open',
                analyst_notes TEXT DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS entity_profiles (
                account_id TEXT PRIMARY KEY,
                entity_kind TEXT NOT NULL,
                occupation TEXT NOT NULL,
                employment_status TEXT NOT NULL,
                industry TEXT NOT NULL,
                employer_account_id TEXT,
                employer_name TEXT,
                business_name TEXT,
                business_registration_status TEXT NOT NULL,
                city TEXT NOT NULL,
                years_active INTEGER NOT NULL DEFAULT 0,
                income_or_turnover_band TEXT NOT NULL,
                profile_source TEXT NOT NULL DEFAULT 'SYNTHETIC_DEMO',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(account_id) REFERENCES accounts(account_id),
                FOREIGN KEY(employer_account_id) REFERENCES accounts(account_id)
            );

            CREATE TABLE IF NOT EXISTS entity_relationships (
                relationship_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                related_account_id TEXT NOT NULL,
                relationship_type TEXT NOT NULL,
                start_year INTEGER,
                relationship_source TEXT NOT NULL DEFAULT 'SYNTHETIC_DEMO',
                evidence_note TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(account_id) REFERENCES accounts(account_id),
                FOREIGN KEY(related_account_id) REFERENCES accounts(account_id),
                CHECK(account_id <> related_account_id)
            );

            CREATE TABLE IF NOT EXISTS entity_case_records (
                case_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                case_category TEXT NOT NULL,
                allegation TEXT NOT NULL,
                case_status TEXT NOT NULL,
                court_or_authority TEXT NOT NULL,
                reference_number TEXT NOT NULL,
                filed_date TEXT NOT NULL,
                outcome_date TEXT,
                outcome_summary TEXT NOT NULL,
                record_source TEXT NOT NULL DEFAULT 'SYNTHETIC_DEMO',
                FOREIGN KEY(account_id) REFERENCES accounts(account_id)
            );

            -- Clearly fictional context for demonstrating future checks.
            -- These rows are separate from financial transactions and profiles.
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

            CREATE TABLE IF NOT EXISTS argus_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)

        account_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(accounts)")
        }

        # Add current balance if the database does not have it yet.
        if "balance" not in account_columns:
            conn.execute("""
                ALTER TABLE accounts
                ADD COLUMN balance REAL NOT NULL DEFAULT 10000.0
            """)
            account_columns.add("balance")

        # Preserve the original starting balance separately.
        if "opening_balance" not in account_columns:
            conn.execute("""
                ALTER TABLE accounts
                ADD COLUMN opening_balance REAL NOT NULL DEFAULT 10000.0
            """)
            account_columns.add("opening_balance")

        # Randomize existing balances once. Metadata prevents re-randomizing
        # balances when ARGUS is reopened.
        migrated = conn.execute("""
            SELECT value
            FROM argus_metadata
            WHERE key = 'random_balances_migrated'
        """).fetchone()

        if migrated is None:
            account_rows = conn.execute("SELECT account_id FROM accounts")
            updates = []

            for row in account_rows:
                roll = random.random()

                if roll < 0.70:
                    amount = round(random.uniform(500, 250_000), 2)
                elif roll < 0.90:
                    amount = round(random.uniform(250_000, 5_000_000), 2)
                elif roll < 0.98:
                    amount = round(random.uniform(5_000_000, 50_000_000), 2)
                else:
                    amount = round(random.uniform(50_000_000, 500_000_000), 2)

                updates.append((amount, amount, row["account_id"]))
                if len(updates) >= 5000:
                    conn.executemany("""
                        UPDATE accounts
                        SET opening_balance = ?, balance = ?
                        WHERE account_id = ?
                    """, updates)
                    updates.clear()

            if updates:
                conn.executemany("""
                    UPDATE accounts
                    SET opening_balance = ?, balance = ?
                    WHERE account_id = ?
                """, updates)

            conn.execute("""
                INSERT INTO argus_metadata(key, value)
                VALUES ('random_balances_migrated', '1')
            """)

            conn.execute("""
                INSERT INTO argus_metadata(key, value)
                VALUES (
                    'account_balance_semantics',
                    'Fictional randomized opening balance; current balance persists'
                )
                ON CONFLICT(key)
                DO UPDATE SET value = excluded.value
            """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_transactions_timestamp
            ON transactions(timestamp DESC, transaction_id DESC)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_transactions_sender_timestamp
            ON transactions(sender_id, timestamp DESC)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_transactions_receiver_timestamp
            ON transactions(receiver_id, timestamp DESC)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_transactions_sender_receiver
            ON transactions(sender_id, receiver_id)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_alerts_status_created
            ON alerts(status, created_at DESC)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_alerts_account_created
            ON alerts(account_id, created_at DESC)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_alerts_transaction
            ON alerts(transaction_id)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_entity_relationships_account
            ON entity_relationships(account_id, relationship_type)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_entity_relationships_related
            ON entity_relationships(related_account_id, relationship_type)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_entity_cases_account
            ON entity_case_records(account_id, case_status, filed_date DESC)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_entity_profiles_employer
            ON entity_profiles(employer_account_id)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_demo_account_device
            ON demo_account_context(primary_device_id)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_demo_transaction_device
            ON demo_transaction_context(device_id)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_demo_transaction_region
            ON demo_transaction_context(origin_region)
        """)


def get_metadata(key, default=None):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT value FROM argus_metadata WHERE key = ?",
            (key,),
        ).fetchone()

    return row["value"] if row else default


def set_metadata(key, value, connection=None):
    """Set metadata, optionally using an existing transaction."""
    if connection is not None:
        connection.execute("""
            INSERT INTO argus_metadata(key, value)
            VALUES (?, ?)
            ON CONFLICT(key)
            DO UPDATE SET value = excluded.value
        """, (key, str(value)))
        return

    with get_connection() as conn:
        conn.execute("""
            INSERT INTO argus_metadata(key, value)
            VALUES (?, ?)
            ON CONFLICT(key)
            DO UPDATE SET value = excluded.value
        """, (key, str(value)))


if __name__ == "__main__":
    initialize_database()
    print(f"ARGUS database initialized at: {DB_PATH}")
