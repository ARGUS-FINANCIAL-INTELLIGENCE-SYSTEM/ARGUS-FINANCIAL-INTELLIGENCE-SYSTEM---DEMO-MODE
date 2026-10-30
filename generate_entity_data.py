"""Populate ARGUS with fictional entity context for every existing account.

This script is for the synthetic demo dataset only. It creates deterministic
profiles, work/business relationships, and fictional sample case records.
It never modifies accounts, historical transactions, or existing alert rows.
Run once after copying the updated database.py: python generate_entity_data.py
"""

from datetime import date, timedelta
import random
import time

from database import DB_PATH, get_connection, initialize_database

SEED = 731_904
BATCH_SIZE = 5000
CITIES = ["Demo City North", "Demo City West", "Demo City Central", "Demo City South", "Demo City East"]
INDUSTRIES = ["Retail", "Food and beverage", "Wholesale trade", "Logistics", "Manufacturing", "Healthcare", "Education", "Professional services", "Construction", "Technology", "Agriculture", "Hospitality"]
OCCUPATIONS = ["Shopkeeper", "Retail associate", "Teacher", "Nurse", "Accountant", "Software developer", "Delivery partner", "Factory worker", "Sales executive", "Driver", "Small business owner", "Consultant", "Farmer", "Student", "Retired"]
INCOME_BANDS = ["Under ₹3 lakh/year", "₹3–8 lakh/year", "₹8–15 lakh/year", "₹15–30 lakh/year", "Above ₹30 lakh/year", "Not stated"]
TURNOVER_BANDS = ["Under ₹10 lakh/year", "₹10–50 lakh/year", "₹50 lakh–₹2 crore/year", "Above ₹2 crore/year", "Not stated"]
CASE_CATEGORIES = [
    "Payment fraud inquiry (fictional)",
    "Financial reporting inquiry (fictional)",
    "Money laundering allegation (fictional)",
    "Terrorist financing inquiry (fictional)",
    "Tax compliance inquiry (fictional)",
]
CASE_STATUSES = ["Pending", "Acquitted", "Convicted", "Dismissed", "Closed - no finding", "No charges filed"]
CASE_STATUS_WEIGHTS = [38, 17, 5, 16, 14, 10]
FINAL_STATUSES = {"Acquitted", "Convicted", "Dismissed", "Closed - no finding", "No charges filed"}


def stable_rng(account_id):
    digits = "".join(ch for ch in str(account_id) if ch.isdigit())
    return random.Random(SEED + (int(digits) if digits else sum(map(ord, str(account_id)))))


def make_case(account_id, rng, sequence, today):
    category = rng.choices(CASE_CATEGORIES, weights=[40, 24, 16, 4, 16], k=1)[0]
    status = rng.choices(CASE_STATUSES, weights=CASE_STATUS_WEIGHTS, k=1)[0]
    filed = today - timedelta(days=rng.randint(90, 3650))
    outcome_date = (filed + timedelta(days=rng.randint(30, 1500))).isoformat() if status in FINAL_STATUSES else None
    summaries = {
        "Pending": "Fictional demo matter remains pending; no finding of guilt is implied.",
        "Acquitted": "Fictional demo court outcome: acquitted in this case only. This is not a general certificate of innocence.",
        "Convicted": "Fictional demo court outcome only; this is not a real criminal-history record.",
        "Dismissed": "Fictional demo matter dismissed; this does not establish facts about any real person.",
        "Closed - no finding": "Fictional demo matter closed without a finding.",
        "No charges filed": "Fictional demo inquiry closed with no charges filed.",
    }
    suffix = "".join(ch for ch in account_id if ch.isdigit())
    return (
        f"DEMO-CASE-{suffix}-{sequence}", account_id, category,
        f"Sample allegation category: {category}. Fictional scenario only.",
        status, "Synthetic Demo Court/Authority", f"DEMO-{suffix}-{sequence}",
        filed.isoformat(), outcome_date, summaries[status], "SYNTHETIC_DEMO",
    )


def make_profile(row, businesses, individuals, today):
    account_id, entity_name, account_type = row
    rng = stable_rng(account_id)
    city = rng.choice(CITIES)
    years_active = rng.randint(0, 30)
    relationships = []
    employer_id = employer_name = business_name = None
    registration = "Not applicable"
    if account_type == "business":
        entity_kind = "Business"
        occupation = "Business owner / operator"
        employment_status = "Self-employed"
        industry = rng.choice(INDUSTRIES)
        business_name = entity_name
        registration = rng.choices(["Demo registered", "Demo unverified"], weights=[72, 28], k=1)[0]
        band = rng.choice(TURNOVER_BANDS)
        if individuals and rng.random() < 0.30:
            owner_id, owner_name = rng.choice(individuals)
            if owner_id != account_id:
                relationships.append((f"REL-OWN-{account_id}", account_id, owner_id, "OWNED_BY", today.year - rng.randint(0, max(1, years_active)), "SYNTHETIC_DEMO", "Fictional ownership link generated for demonstration."))
    else:
        entity_kind = "Individual"
        employment_status = rng.choices(["Employed", "Self-employed", "Student", "Retired", "Not employed"], weights=[55, 22, 8, 10, 5], k=1)[0]
        if employment_status == "Student":
            occupation = "Student"
        elif employment_status == "Retired":
            occupation = "Retired worker"
        elif employment_status == "Not employed":
            occupation = "Not currently employed"
        elif employment_status == "Self-employed":
            occupation = rng.choice(["Shopkeeper", "Small business owner", "Independent consultant", "Farmer", "Food vendor", "Freelance service provider"])
        else:
            occupation = rng.choice(["Retail associate", "Teacher", "Nurse", "Accountant", "Software developer", "Delivery partner", "Factory worker", "Sales executive", "Driver", "Healthcare worker", "Logistics worker"])
        industry = rng.choice(INDUSTRIES) if employment_status in {"Employed", "Self-employed"} else "Not applicable"
        band = rng.choice(INCOME_BANDS) if employment_status not in {"Student", "Not employed"} else "Not stated"
        if employment_status == "Employed" and businesses:
            employer_id, employer_name = rng.choice(businesses)
            relationships.append((f"REL-EMP-{account_id}", account_id, employer_id, "EMPLOYED_BY", today.year - rng.randint(0, max(1, years_active)), "SYNTHETIC_DEMO", "Fictional employment link generated for demonstration."))
    profile = (
        account_id, entity_kind, occupation, employment_status, industry,
        employer_id, employer_name, business_name, registration, city,
        years_active, band, "SYNTHETIC_DEMO",
    )
    cases = []
    if rng.random() < 0.003:
        case_count = 2 if rng.random() < 0.08 else 1
        cases = [make_case(account_id, rng, i + 1, today) for i in range(case_count)]
    return profile, relationships, cases


def main():
    start = time.perf_counter()
    initialize_database()
    today = date.today()
    with get_connection() as conn:
        businesses = [tuple(row) for row in conn.execute("SELECT account_id, entity_name FROM accounts WHERE account_type = 'business' ORDER BY account_id")]
        individuals = [tuple(row) for row in conn.execute("SELECT account_id, entity_name FROM accounts WHERE account_type <> 'business' ORDER BY account_id")]
        total = int(conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0])
    print(f"Database: {DB_PATH}")
    print(f"Accounts to enrich: {total:,}")
    print("All generated profiles, relationships, and case records are fictional demo data.")
    done = 0
    cursor = ""
    while True:
        with get_connection() as conn:
            rows = [tuple(row) for row in conn.execute("""
                SELECT a.account_id, a.entity_name, a.account_type
                FROM accounts AS a
                LEFT JOIN entity_profiles AS p ON p.account_id = a.account_id
                WHERE p.account_id IS NULL AND a.account_id > ?
                ORDER BY a.account_id LIMIT ?
            """, (cursor, BATCH_SIZE))]
        if not rows:
            break
        profiles, relationships, cases = [], [], []
        for row in rows:
            profile, rels, case_rows = make_profile(row, businesses, individuals, today)
            profiles.append(profile)
            relationships.extend(rels)
            cases.extend(case_rows)
        with get_connection() as conn:
            conn.executemany("""
                INSERT OR IGNORE INTO entity_profiles
                (account_id, entity_kind, occupation, employment_status, industry,
                 employer_account_id, employer_name, business_name,
                 business_registration_status, city, years_active,
                 income_or_turnover_band, profile_source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, profiles)
            if relationships:
                conn.executemany("""
                    INSERT OR IGNORE INTO entity_relationships
                    (relationship_id, account_id, related_account_id,
                     relationship_type, start_year, relationship_source, evidence_note)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, relationships)
            if cases:
                conn.executemany("""
                    INSERT OR IGNORE INTO entity_case_records
                    (case_id, account_id, case_category, allegation, case_status,
                     court_or_authority, reference_number, filed_date, outcome_date,
                     outcome_summary, record_source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, cases)
        cursor = rows[-1][0]
        done += len(rows)
        print(f"\rProfiles: {done:,}/{total:,}", end="", flush=True)
    print()
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO argus_metadata(key, value)
            VALUES ('entity_profile_dataset', 'synthetic-demo-v1')
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """)
        profile_count = conn.execute("SELECT COUNT(*) FROM entity_profiles").fetchone()[0]
        relationship_count = conn.execute("SELECT COUNT(*) FROM entity_relationships").fetchone()[0]
        case_count = conn.execute("SELECT COUNT(*) FROM entity_case_records").fetchone()[0]
    print("Entity context generation finished.")
    print(f"Profiles: {profile_count:,}; relationships: {relationship_count:,}; fictional case records: {case_count:,}")
    print(f"Elapsed: {time.perf_counter() - start:,.1f}s")
    print("Existing accounts, transactions, balances, and alerts were not modified.")


if __name__ == "__main__":
    main()
