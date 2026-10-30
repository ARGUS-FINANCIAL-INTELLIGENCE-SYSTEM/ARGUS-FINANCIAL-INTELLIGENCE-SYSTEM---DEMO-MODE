
"""Scalable, resumable synthetic dataset generator for ARGUS.

First run (replaces the current demo dataset):
    python generate_data.py --reset

Resume an interrupted run with the same targets:
    python generate_data.py

Optional smaller benchmark:
    python generate_data.py --reset --accounts 10000 --transactions 100000

Generation is batched so the entire dataset is never held in memory.
All records are fictional. The generator does not label transactions as suspicious.
"""

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import random
import time

from database import DB_PATH, get_connection, initialize_database

DEFAULT_ACCOUNT_COUNT = 500_000
DEFAULT_TRANSACTION_COUNT = 5_000_000
DEFAULT_BATCH_SIZE = 10_000
RANDOM_SEED = 42

FIRST_NAMES = [
    "Aarav", "Diya", "Arjun", "Ananya", "Kabir", "Meera", "Ishaan",
    "Aadhya", "Rohan", "Tara", "Vivaan", "Nila", "Advik", "Sana",
    "Reyansh", "Mira", "Dev", "Ira", "Kiran", "Leela",
]

LAST_NAMES = [
    "Sharma", "Patel", "Iyer", "Reddy", "Nair", "Mehta", "Rao",
    "Kapoor", "Das", "Menon", "Joshi", "Singh", "Kulkarni", "Bose",
]

COMPANIES = [
    "Blueleaf Traders", "Northstar Supplies", "Cedarline Services",
    "Brightpath Retail", "Silveroak Logistics", "Riverstone Foods",
    "Eastbridge Components", "Sunfield Manufacturing",
]

DESCRIPTIONS = [
    "Retail purchase", "Invoice payment", "Service payment",
    "Supplier settlement", "Online purchase", "Monthly expense",
    "Personal transfer", "Utility payment", "Business payment",
    "Travel reimbursement", "Equipment purchase", "Consulting fee",
]

JOB_KEY = "synthetic_generation_job"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate ARGUS synthetic data in batches."
    )

    parser.add_argument("--accounts", type=int, default=DEFAULT_ACCOUNT_COUNT)
    parser.add_argument(
        "--transactions",
        type=int,
        default=DEFAULT_TRANSACTION_COUNT,
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)

    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete existing ARGUS demo data and start a fresh generation job.",
    )

    args = parser.parse_args()

    if args.accounts < 2 or args.transactions < 12 or args.batch_size < 1:
        parser.error(
            "--accounts must be >= 2, --transactions >= 12, "
            "and --batch-size >= 1"
        )

    return args


def count_rows(conn, table):
    if table not in {"accounts", "transactions", "alerts", "cases"}:
        raise ValueError("Unsupported table")

    return int(
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    )


def reset_dataset(conn, accounts_target, transactions_target):
    # Delete dependent tables before parent tables due to foreign keys.
    conn.execute("DELETE FROM alerts")
    conn.execute("DELETE FROM cases")
    conn.execute("DELETE FROM transactions")
    conn.execute("DELETE FROM accounts")
    conn.execute("DELETE FROM argus_metadata")

    conn.execute(
        "INSERT INTO argus_metadata(key, value) VALUES (?, ?)",
        (
            JOB_KEY,
            f"accounts={accounts_target};"
            f"transactions={transactions_target};seed={RANDOM_SEED}",
        ),
    )

    conn.execute(
        "INSERT INTO argus_metadata(key, value) "
        "VALUES ('generator_accounts_done', '0')"
    )

    conn.execute(
        "INSERT INTO argus_metadata(key, value) "
        "VALUES ('generator_transactions_done', '0')"
    )


def read_int_metadata(conn, key):
    row = conn.execute(
        "SELECT value FROM argus_metadata WHERE key = ?",
        (key,),
    ).fetchone()

    return int(row["value"]) if row else 0


def write_metadata(conn, key, value):
    conn.execute(
        """
        INSERT INTO argus_metadata(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, str(value)),
    )


def make_account_batch(start_number, end_number):
    rng = random.Random(RANDOM_SEED + start_number)
    rows = []

    for number in range(start_number, end_number + 1):
        account_id = f"ACC{number:06d}"

        account_type = rng.choices(
            ["individual", "business"],
            weights=[85, 15],
            k=1,
        )[0]

        if account_type == "business":
            entity_name = f"{rng.choice(COMPANIES)} {number:06d}"
        else:
            entity_name = (
                f"{rng.choice(FIRST_NAMES)} "
                f"{rng.choice(LAST_NAMES)} {number:06d}"
            )

        rows.append((account_id, entity_name, account_type))

    return rows


def special_transaction(index, account_count, now):
    """Return an illustrative chain/cycle/burst record for the first 12 IDs."""
    # The scenarios are mixed into the baseline and are not pre-labelled.
    if index < 3:
        sender = index + 1
        receiver = index + 2
        timestamp = now - timedelta(minutes=12 - index * 3)
        amount = 48_500.00 + index * 250
        description = "Business payment"

    elif index < 6:
        offset = index - 3
        sender = 10 + offset
        receiver = 10 + ((offset + 1) % 3)
        timestamp = now - timedelta(minutes=20 - offset * 2)
        amount = 32_000.00
        description = "Supplier settlement"

    elif index < 12:
        offset = index - 6
        sender = 20
        receiver = 21 + offset
        timestamp = now - timedelta(minutes=8) + timedelta(
            seconds=offset * 30
        )
        amount = 18_000.00 + offset * 100
        description = "Personal transfer"

    else:
        return None

    sender = ((sender - 1) % account_count) + 1
    receiver = ((receiver - 1) % account_count) + 1

    if sender == receiver:
        receiver = (receiver % account_count) + 1

    return (
        f"TX{index + 1:012d}",
        f"ACC{sender:06d}",
        f"ACC{receiver:06d}",
        amount,
        "INR",
        timestamp.isoformat(),
        description,
    )


def make_transaction_batch(
    start_index,
    end_index,
    account_count,
    start_time,
    end_time,
):
    """Create one batch; per-batch RNG seeds make batches reproducible."""
    rng = random.Random(RANDOM_SEED + 100_000_000 + start_index)
    now = datetime.now(timezone.utc)
    rows = []

    total_seconds = max(
        1,
        int((end_time - start_time).total_seconds()),
    )

    for index in range(start_index, end_index):
        special = (
            special_transaction(index, account_count, now)
            if index < 12
            else None
        )

        if special is not None:
            rows.append(special)
            continue

        sender_number = rng.randint(1, account_count)
        receiver_number = rng.randint(1, account_count - 1)

        if receiver_number >= sender_number:
            receiver_number += 1

        amount = round(rng.lognormvariate(7.0, 0.85), 2)
        amount = min(max(amount, 50.0), 250_000.0)

        timestamp = start_time + timedelta(
            seconds=rng.randint(0, total_seconds)
        )

        rows.append(
            (
                f"TX{index + 1:012d}",
                f"ACC{sender_number:06d}",
                f"ACC{receiver_number:06d}",
                amount,
                "INR",
                timestamp.isoformat(),
                rng.choice(DESCRIPTIONS),
            )
        )

    return rows


def main():
    args = parse_args()
    initialize_database()
    start_clock = time.perf_counter()

    print(f"Database: {DB_PATH}")
    print(
        f"Targets: {args.accounts:,} accounts; "
        f"{args.transactions:,} transactions"
    )
    print(f"Batch size: {args.batch_size:,}")

    with get_connection() as conn:
        existing_job = conn.execute(
            "SELECT value FROM argus_metadata WHERE key = ?",
            (JOB_KEY,),
        ).fetchone()

        if args.reset:
            reset_dataset(conn, args.accounts, args.transactions)
            conn.commit()
            print(
                "Existing demo data cleared. "
                "Starting a fresh generation job."
            )

        elif existing_job is None:
            if count_rows(conn, "accounts") or count_rows(
                conn, "transactions"
            ):
                raise SystemExit(
                    "The database already contains data from a different "
                    "generator. Run once with --reset to replace it, then "
                    "rerun without --reset if generation is interrupted."
                )

            reset_dataset(conn, args.accounts, args.transactions)
            conn.commit()

        else:
            expected = (
                f"accounts={args.accounts};"
                f"transactions={args.transactions};seed={RANDOM_SEED}"
            )

            if existing_job["value"] != expected:
                raise SystemExit(
                    "A generation job with different targets already exists. "
                    "Resume with its original arguments, or use --reset "
                    "to start over."
                )

        accounts_done = read_int_metadata(
            conn,
            "generator_accounts_done",
        )

        transactions_done = read_int_metadata(
            conn,
            "generator_transactions_done",
        )

    # Generate and commit accounts in small batches.
    for start in range(
        accounts_done + 1,
        args.accounts + 1,
        args.batch_size,
    ):
        end = min(args.accounts, start + args.batch_size - 1)
        rows = make_account_batch(start, end)

        with get_connection() as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO accounts
                    (account_id, entity_name, account_type)
                VALUES (?, ?, ?)
                """,
                rows,
            )

            write_metadata(conn, "generator_accounts_done", end)

        print(
            f"\rAccounts: {end:,}/{args.accounts:,}",
            end="",
            flush=True,
        )

    print()

    # Fixed synthetic 90-day history, generated in batches.
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(days=90)

    for start in range(
        transactions_done,
        args.transactions,
        args.batch_size,
    ):
        end = min(args.transactions, start + args.batch_size)

        rows = make_transaction_batch(
            start,
            end,
            args.accounts,
            start_time,
            end_time,
        )

        with get_connection() as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO transactions
                    (
                        transaction_id,
                        sender_id,
                        receiver_id,
                        amount,
                        currency,
                        timestamp,
                        description
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

            write_metadata(conn, "generator_transactions_done", end)

        print(
            f"\rTransactions: {end:,}/{args.transactions:,}",
            end="",
            flush=True,
        )

    print()

    elapsed = time.perf_counter() - start_clock

    with get_connection() as conn:
        actual_accounts = count_rows(conn, "accounts")
        actual_transactions = count_rows(conn, "transactions")

    print("\nARGUS synthetic dataset generation finished.")
    print(f"Database: {DB_PATH}")
    print(f"Accounts currently stored: {actual_accounts:,}")
    print(f"Transactions currently stored: {actual_transactions:,}")
    print(f"Elapsed time this run: {elapsed:,.1f} seconds")
    print("All generated entities and transactions are fictional.")
    print("If interrupted, rerun with the same arguments and without --reset.")


if __name__ == "__main__":
    main()
