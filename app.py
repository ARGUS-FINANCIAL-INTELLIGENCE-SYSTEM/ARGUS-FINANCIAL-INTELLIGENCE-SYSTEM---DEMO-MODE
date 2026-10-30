from datetime import datetime, timezone

from pathlib import Path

import random

import time

import uuid

import math
import re
import html
import io
import zipfile
from collections import Counter



import pandas as pd

import plotly.graph_objects as go

import streamlit as st
import streamlit.components.v1 as components



from database import get_connection, initialize_database

from detection_engine import ALERT_THRESHOLD, score_live_batch, load_recent_live_alerts

from transfer_map import load_transfer_network, summarize_network, load_account_branches, search_accounts as search_map_accounts
from pattern_catalog import PATTERN_CATALOG, evaluate_pattern_checks



BASE_DIR = Path(__file__).resolve().parent

# The project folder is historically named "assests"; support both spellings.
CSS_PATH = next((path for path in (BASE_DIR / "assets" / "style.css", BASE_DIR / "assests" / "style.css") if path.exists()), BASE_DIR / "assets" / "style.css")



st.set_page_config(

    page_title="ARGUS | Financial Intelligence",

    page_icon="◈",

    layout="wide",

    initial_sidebar_state="expanded",

)



@st.cache_resource(show_spinner="Preparing ARGUS database...")
def _initialize_database_once():
    """Run schema checks/index creation once per Streamlit process, not rerun."""
    initialize_database()


_initialize_database_once()


if CSS_PATH.exists():

    st.markdown(

        f"<style>{CSS_PATH.read_text(encoding='utf-8')}</style>",

        unsafe_allow_html=True,

    )





# --------------------------------------------------

# DATABASE HELPERS

# --------------------------------------------------



def _plain_alert_reason(value):
    """Translate older saved alert wording into plain language for display."""
    text = str(value or "")
    text = re.sub(r"^LIVE_SIMULATION\s*\|\s*event=[^|]*\|\s*level=[^|]*\|\s*", "", text, flags=re.IGNORECASE)
    replacements = (
        (r"Temporal fan-in: (\d+) senders within ([^;|]+)", r"Money came from \1 different accounts within \2"),
        (r"Temporal fan-out: (\d+) recipients within ([^;|]+)", r"Money was sent to \1 different accounts within \2"),
        (r"Repeated pair over time: (\d+) transfers within ([^;|]+)", r"The same two accounts transferred money \1 times within \2"),
        (r"Possible amount-matched pass-through within 1-hour", "A similar amount came into this account and was sent onward within 1 hour"),
        (r"Broad fan-in: (\d+) distinct senders", r"Receiver got money from \1 different accounts"),
        (r"Multiple senders: (\d+) distinct senders", r"Receiver got money from \1 different accounts"),
        (r"Broad fan-out: (\d+) distinct recipients", r"Sender paid \1 different accounts"),
        (r"Multiple recipients: (\d+) distinct recipients", r"Sender paid \1 different accounts"),
        (r"Short-cycle candidate in recent graph", "Money may be moving back through a recent transfer path"),
        (r"Amount high versus sender's live baseline", "This payment is much higher than this sender's usual amount"),
        (r"Amount is at least 4x sender's prior median live amount", "This payment is at least four times the sender's usual payment"),
        (r"Demo context adjustment ([+-]?\d+)", r"Possible explanation changed the demo score by \1 points"),
        (r"Repeated sender-receiver relationship", "These two accounts have transferred money several times"),
        (r"Rapid incoming counterparty growth", "Many different accounts sent money in a short time"),
        (r"Rapid outgoing counterparty growth", "Money was sent to many different accounts in a short time"),
        (r"Repeated identical-amount burst", "Same amount sent repeatedly"),
        (r"Rapid amount-matched pass-through", "Money received and quickly sent onward"),
        (r"Similar-value multi-hop chain", "Similar amount moved through several accounts"),
        (r"New counterparty after established activity", "First transfer to a new account after prior activity"),
        (r"Activity after an observed quiet period", "Transfers resumed after a week or more of quiet"),
        (r"Accumulated multi-payment burst", "Several payments added up quickly"),
        (r"Multi-leg transfer hub", "Account received and paid many accounts within one hour"),
        (r"Reciprocal transfers between an account pair", "Money moved both ways between the same two accounts"),
        (r"Cluster of round-amount transfers", "Several round-number payments within one hour"),
        (r"Very large demo transfer amount", "Large payment amount"),
        (r"Large demo transfer amount", "Payment amount is high"),
        (r"Amount high versus sender's live baseline", "This payment is much higher than this sender's usual payment"),
        (r"Possible explanation changed the demo score by ([+-]?\d+) points?:", r"A possible business or salary explanation changed this demo score by \1 points:"),
        (r"Illustrative demo scoring adjustment:", "Possible explanation considered:"),
        (r"risk score", "review score"),
        (r"fan-in", "many accounts sending money to one account"),
        (r"fan-out", "one account sending money to many accounts"),
        (r"counterparty", "other account"),
        (r"pass-through", "money received and sent onward"),
        (r"multi-hop", "through several accounts"),
        (r"velocity", "number of transfers over time"),
        (r"baseline", "usual activity"),
    )
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    text = re.sub(r"\bP\d{3}\b", "pattern check", text)
    text = re.sub(r"\s*;\s*", "; ", text)
    return text


def _plain_reason_items(value, limit=None):
    """Return short, deduplicated reason sentences for people reviewing alerts."""
    text = _plain_alert_reason(value)
    items = []
    for part in re.split(r"\s*;\s*|\s*\|\s*", text):
        part = part.strip(" .;|\n\t")
        if part and part not in items:
            items.append(part)
    return items if limit is None else items[:limit]


@st.cache_data(ttl=120, show_spinner=False)

def count_rows(table_name: str) -> int:

    allowed_tables = {"accounts", "transactions", "alerts", "cases"}

    if table_name not in allowed_tables:

        raise ValueError("Unsupported table name.")



    with get_connection() as conn:

        result = conn.execute(

            f"SELECT COUNT(*) FROM {table_name}"

        ).fetchone()



    return int(result[0])


@st.cache_data(ttl=5, show_spinner=False)
def count_saved_alerts_fresh() -> int:
    """Refresh the persisted-alert total often without recounting large tables."""
    with get_connection() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0])


@st.cache_data(ttl=120, show_spinner=False)
def account_rowid_bounds() -> tuple[int, int]:
    """Read the account rowid range once for fast random sampling."""
    with get_connection() as conn:
        row = conn.execute("SELECT MIN(rowid), MAX(rowid) FROM accounts").fetchone()
    if not row or row[0] is None or row[1] is None:
        return (0, 0)
    return int(row[0]), int(row[1])






@st.cache_data(ttl=15, show_spinner=False)
def load_recent_transactions(limit: int = 25) -> pd.DataFrame:

    limit = max(1, min(int(limit), 500))



    with get_connection() as conn:

        return pd.read_sql_query(

            """

            SELECT

                transaction_id AS "Transaction ID",

                timestamp AS "Timestamp",

                sender_id AS "Sender",

                receiver_id AS "Receiver",

                amount AS "Amount (INR)",

                description AS "Description"

            FROM transactions

            ORDER BY timestamp DESC, transaction_id DESC

            LIMIT ?

            """,

            conn,

            params=(limit,),

        )





@st.cache_data(ttl=120, show_spinner=False)
def load_overview_snapshot(sample_size: int = 50_000):
    """Return cached, bounded overview statistics and recent active accounts."""
    sample_size = max(1, min(int(sample_size), 50_000))
    with get_connection() as conn:
        summary_row = conn.execute("""
            WITH recent AS (
                SELECT sender_id, receiver_id, amount, timestamp
                FROM transactions
                ORDER BY timestamp DESC, transaction_id DESC
                LIMIT ?
            )
            SELECT COUNT(*) AS sampled_transfers,
                   COALESCE(SUM(amount), 0) AS sampled_value,
                   COALESCE(AVG(amount), 0) AS average_amount,
                   COALESCE(MAX(amount), 0) AS largest_amount,
                   COALESCE((SELECT COUNT(*) FROM (
                       SELECT sender_id AS account_id FROM recent
                       UNION
                       SELECT receiver_id AS account_id FROM recent
                   )), 0) AS accounts_in_sample,
                   COALESCE(MAX(timestamp), '') AS latest_transfer
            FROM recent
        """, (sample_size,)).fetchone()

        active_accounts = pd.read_sql_query("""
            WITH recent AS (
                SELECT sender_id, receiver_id, amount
                FROM transactions
                ORDER BY timestamp DESC, transaction_id DESC
                LIMIT ?
            ), account_activity AS (
                SELECT account_id, COUNT(*) AS recent_transfers,
                       SUM(incoming_count) AS incoming_transfers,
                       SUM(outgoing_count) AS outgoing_transfers,
                       SUM(incoming_amount) AS incoming_value,
                       SUM(outgoing_amount) AS outgoing_value,
                       COUNT(DISTINCT counterparty_id) AS distinct_contacts
                FROM (
                    SELECT receiver_id AS account_id, sender_id AS counterparty_id,
                           1 AS incoming_count, 0 AS outgoing_count,
                           amount AS incoming_amount, 0 AS outgoing_amount
                    FROM recent
                    UNION ALL
                    SELECT sender_id AS account_id, receiver_id AS counterparty_id,
                           0 AS incoming_count, 1 AS outgoing_count,
                           0 AS incoming_amount, amount AS outgoing_amount
                    FROM recent
                )
                GROUP BY account_id
            )
            SELECT x.account_id AS "Account ID",
                   COALESCE(a.entity_name, x.account_id) AS "Account name",
                   COALESCE(p.occupation, p.entity_kind, 'Not listed') AS "Role",
                   COALESCE(p.industry, 'Not listed') AS "Industry",
                   x.recent_transfers AS "Transfers in sample",
                   x.distinct_contacts AS "Distinct contacts",
                   x.incoming_transfers AS "Incoming",
                   x.incoming_value AS "Incoming value (INR)",
                   x.outgoing_transfers AS "Outgoing",
                   x.outgoing_value AS "Outgoing value (INR)"
            FROM account_activity x
            LEFT JOIN accounts a ON a.account_id = x.account_id
            LEFT JOIN entity_profiles p ON p.account_id = x.account_id
            ORDER BY x.recent_transfers DESC, x.distinct_contacts DESC, x.account_id
            LIMIT 10
        """, conn, params=(sample_size,))

        coverage = {
            "profiles": int(conn.execute("SELECT COUNT(*) FROM entity_profiles").fetchone()[0]),
            "relationships": int(conn.execute("SELECT COUNT(*) FROM entity_relationships").fetchone()[0]),
            "demo_cases": int(conn.execute("SELECT COUNT(*) FROM entity_case_records").fetchone()[0]),
            "open_alerts": int(conn.execute("SELECT COUNT(*) FROM alerts WHERE lower(status) = 'open'").fetchone()[0]),
        }
        alert_summary = conn.execute("""
            SELECT COUNT(*) AS total_alerts,
                   COUNT(DISTINCT account_id) AS distinct_accounts,
                   COALESCE(AVG(risk_score), 0) AS average_score,
                   COALESCE(MAX(risk_score), 0) AS peak_score,
                   SUM(CASE WHEN risk_score >= 75 THEN 1 ELSE 0 END) AS elevated_alerts,
                   MAX(created_at) AS latest_alert_at
            FROM alerts
        """).fetchone()
        top_alert_account = conn.execute("""
            SELECT al.account_id,
                   COALESCE(ac.entity_name, al.account_id) AS entity_name,
                   MAX(al.risk_score) AS peak_score,
                   COUNT(*) AS alert_count
            FROM alerts al
            LEFT JOIN accounts ac ON ac.account_id = al.account_id
            WHERE al.account_id IS NOT NULL
            GROUP BY al.account_id, ac.entity_name
            ORDER BY peak_score DESC, alert_count DESC, al.account_id
            LIMIT 1
        """).fetchone()

    return dict(summary_row), active_accounts, coverage, dict(alert_summary), (dict(top_alert_account) if top_alert_account else None)


@st.cache_data(ttl=60, show_spinner=False)
def load_overview_recent_alerts(limit: int = 8) -> pd.DataFrame:
    """Load a small, readable sample of saved alerts for the dashboard."""
    limit = max(1, min(int(limit), 25))
    with get_connection() as conn:
        return pd.read_sql_query("""
            SELECT al.created_at AS "Time",
                   al.account_id AS "Account",
                   COALESCE(ac.entity_name, al.account_id) AS "Name",
                   al.risk_score AS "Score",
                   al.reason AS "Reason",
                   al.status AS "Status"
            FROM alerts al
            LEFT JOIN accounts ac ON ac.account_id = al.account_id
            ORDER BY al.created_at DESC
            LIMIT ?
        """, conn, params=(limit,))


@st.cache_data(ttl=60, show_spinner=False)
def load_overview_focus_context(account_id: str) -> dict:
    """Load concise profile context for the overview's review-focus account."""
    account_id = str(account_id or "").strip()
    if not account_id:
        return {}
    with get_connection() as conn:
        row = conn.execute("""
            SELECT a.account_id,
                   COALESCE(a.entity_name, a.account_id) AS entity_name,
                   COALESCE(p.occupation, p.entity_kind, 'Role not listed') AS role,
                   COALESCE(p.industry, 'Industry not listed') AS industry,
                   COALESCE(p.profile_source, 'No profile available') AS profile_source,
                   (SELECT COUNT(*) FROM entity_relationships r
                    WHERE r.account_id = a.account_id OR r.related_account_id = a.account_id) AS declared_links,
                   (SELECT COUNT(*) FROM entity_case_records c
                    WHERE c.account_id = a.account_id) AS fictional_cases
            FROM accounts a
            LEFT JOIN entity_profiles p ON p.account_id = a.account_id
            WHERE a.account_id = ?
        """, (account_id,)).fetchone()
    return dict(row) if row else {"account_id": account_id, "entity_name": account_id}


@st.cache_data(ttl=60, show_spinner=False)
def load_transaction_activity(sample_size: int = 50_000) -> pd.DataFrame:

    with get_connection() as conn:

        return pd.read_sql_query(

            """

            SELECT

                CAST(strftime('%H', timestamp) AS INTEGER) AS hour,

                COUNT(*) AS transaction_count

            FROM (

                SELECT timestamp

                FROM transactions

                ORDER BY timestamp DESC, transaction_id DESC

                LIMIT ?

            )

            GROUP BY hour

            ORDER BY hour

            """,

            conn,

            params=(sample_size,),

        )





@st.cache_data(ttl=60, show_spinner=False)
def search_accounts(search_term: str, limit: int = 100) -> pd.DataFrame:

    search_term = search_term.strip()

    if not search_term:

        return pd.DataFrame()



    pattern = f"%{search_term}%"



    with get_connection() as conn:

        return pd.read_sql_query(

            """

            SELECT

                account_id AS "Account Number",

                entity_name AS "User / Entity Name",

                account_type AS "Account Type",

                opening_balance AS "Opening Balance (INR)",

                balance AS "Current Balance (INR)",

                created_at AS "Created At"

            FROM accounts

            WHERE account_id LIKE ? OR entity_name LIKE ?

            ORDER BY

                CASE WHEN account_id = ? THEN 0 ELSE 1 END,

                entity_name

            LIMIT ?

            """,

            conn,

            params=(pattern, pattern, search_term, limit),

        )





@st.cache_data(ttl=15, show_spinner=False)
def load_account_transactions(

    account_id: str,

    limit: int = 100,

) -> pd.DataFrame:

    limit = max(1, min(int(limit), 500))



    with get_connection() as conn:

        return pd.read_sql_query(

            """

            SELECT

                transaction_id AS "Transaction ID",

                timestamp AS "Timestamp",

                sender_id AS "Sender Account",

                receiver_id AS "Receiver Account",

                CASE

                    WHEN sender_id = ? THEN 'Outgoing'

                    ELSE 'Incoming'

                END AS "Direction",

                amount AS "Amount (INR)",

                currency AS "Currency",

                description AS "Description"

            FROM transactions

            WHERE sender_id = ? OR receiver_id = ?

            ORDER BY timestamp DESC, transaction_id DESC

            LIMIT ?

            """,

            conn,

            params=(account_id, account_id, account_id, limit),

        )





@st.cache_data(ttl=5, show_spinner=False)
def load_account_summary(account_id: str):

    with get_connection() as conn:

        account = conn.execute(

            """

            SELECT

                account_id,

                entity_name,

                account_type,

                opening_balance,

                balance,

                created_at

            FROM accounts

            WHERE account_id = ?

            """,

            (account_id,),

        ).fetchone()



        if account is None:

            return None, None



        totals = conn.execute(

            """

            SELECT

                COUNT(*) AS transaction_count,

                COALESCE(

                    SUM(CASE WHEN sender_id = ? THEN amount ELSE 0 END), 0

                ) AS outgoing_total,

                COALESCE(

                    SUM(CASE WHEN receiver_id = ? THEN amount ELSE 0 END), 0

                ) AS incoming_total

            FROM transactions

            WHERE sender_id = ? OR receiver_id = ?

            """,

            (account_id, account_id, account_id, account_id),

        ).fetchone()



    return dict(account), dict(totals)





# --------------------------------------------------

# SIMULATION ENGINE

# --------------------------------------------------



def get_variable_batch_size(target_rate: int) -> int:

    target_rate = max(1, int(target_rate))

    multiplier = random.uniform(0.94, 1.06)

    event = random.random()



    if event < 0.10:

        multiplier *= random.uniform(0.78, 0.90)

    elif event > 0.92:

        multiplier *= random.uniform(1.10, 1.25)



    return max(1, min(round(target_rate * multiplier), 625))





def generate_simulation_batch(

    requested_count: int,

    session_balances: dict,

) -> tuple[list[dict], float, float]:

    """

    Generate fictional transfers and persist updated account balances.



    Simulated events are displayed in memory only. They are NOT inserted

    into the historical transactions table.

    """

    requested_count = max(1, min(int(requested_count), 625))

    started = time.perf_counter()



    rowid_min, rowid_max = account_rowid_bounds()
    if rowid_max - rowid_min < 1:
        return [], time.perf_counter() - started, 0.0

    target_count = min(rowid_max - rowid_min + 1, max(2, requested_count * 2))
    selected_rows = {}
    rowid_width = rowid_max - rowid_min + 1

    with get_connection() as conn:
        # Random rowid probes avoid sorting the full 500,000-row account table
        # with ORDER BY RANDOM() for every simulation batch.
        for _attempt in range(8):
            needed = target_count - len(selected_rows)
            if needed <= 0:
                break
            probe_count = min(rowid_width, max(needed * 4, needed))
            candidates = random.sample(range(rowid_min, rowid_max + 1), probe_count)
            for offset in range(0, len(candidates), 400):
                chunk = candidates[offset:offset + 400]
                marks = ",".join("?" for _ in chunk)
                for row in conn.execute(
                    f"SELECT account_id, balance FROM accounts WHERE rowid IN ({marks})",
                    chunk,
                ):
                    selected_rows[str(row["account_id"])] = row
                    if len(selected_rows) >= target_count:
                        break
                if len(selected_rows) >= target_count:
                    break

        # Recover safely if a database has a very sparse rowid range.
        if len(selected_rows) < 2:
            fallback = conn.execute(
                "SELECT account_id, balance FROM accounts ORDER BY account_id LIMIT ?",
                (max(2, target_count),),
            ).fetchall()
            selected_rows.update({str(row["account_id"]): row for row in fallback})

    rows = list(selected_rows.values())[:target_count]


    stored_balances = {

        row["account_id"]: float(row["balance"])

        for row in rows

    }

    account_ids = list(stored_balances)



    if len(account_ids) < 2:

        return [], time.perf_counter() - started, 0.0



    # On a new session, hydrate balances from SQLite.

    for account_id, balance in stored_balances.items():

        session_balances.setdefault(account_id, balance)



    now = datetime.now(timezone.utc).isoformat()



    descriptions = [

        "Retail purchase",

        "Invoice payment",

        "Service payment",

        "Supplier settlement",

        "Online purchase",

        "Personal transfer",

        "Utility payment",

        "Business payment",

    ]



    generated = []

    total_volume = 0.0

    attempts = 0

    max_attempts = requested_count * 4

    touched_accounts = set()



    while len(generated) < requested_count and attempts < max_attempts:

        attempts += 1

        sender, receiver = random.sample(account_ids, 2)



        sender_balance = float(

            session_balances.get(sender, stored_balances[sender])

        )



        if sender_balance < 50.0:

            continue



        amount = round(random.lognormvariate(7.0, 0.85), 2)

        amount = round(

            min(max(amount, 50.0), 250_000.0, sender_balance),

            2,

        )



        if amount < 50.0:

            continue



        session_balances[sender] = round(sender_balance - amount, 2)



        receiver_balance = float(

            session_balances.get(receiver, stored_balances[receiver])

        )

        session_balances[receiver] = round(receiver_balance + amount, 2)



        touched_accounts.update((sender, receiver))

        total_volume += amount



        generated.append({

            "Transaction ID": f"SIM{uuid.uuid4().hex[:20].upper()}",

            "Timestamp": now,

            "Sender": sender,

            "Receiver": receiver,

            "Amount (INR)": amount,

            "Sender balance after (INR)": session_balances[sender],

            "Receiver balance after (INR)": session_balances[receiver],

            "Description": random.choice(descriptions),

        })



    # Save all affected balances together. No transaction rows are added.

    if touched_accounts:

        with get_connection() as conn:

            conn.executemany(

                """

                UPDATE accounts

                SET balance = ?

                WHERE account_id = ?

                """,

                [

                    (

                        round(float(session_balances[account_id]), 2),

                        account_id,

                    )

                    for account_id in touched_accounts

                ],

            )



    elapsed = time.perf_counter() - started

    return generated, elapsed, total_volume


def advance_simulation_session():
    """Advance one simulation tick and keep all demo events session-only."""
    if not st.session_state.get("argus_sim_running", False):
        return
    if accounts_count < 2:
        st.session_state.argus_sim_running = False
        return

    requested_count = get_variable_batch_size(
        st.session_state.get("argus_sim_target_rate", 100)
    )
    generated, elapsed, batch_volume = generate_simulation_batch(
        requested_count,
        st.session_state.argus_sim_balances,
    )
    st.session_state.argus_sim_total += len(generated)
    st.session_state.argus_sim_volume += batch_volume
    if generated:
        st.session_state.argus_sim_largest = max(
            st.session_state.argus_sim_largest,
            max(row["Amount (INR)"] for row in generated),
        )
    st.session_state.argus_sim_last_batch = len(generated)
    st.session_state.argus_sim_last_rate = len(generated) / max(elapsed, 0.001)
    st.session_state.argus_sim_recent = (
        generated + st.session_state.argus_sim_recent
    )[:100]

    # Keep generated transfers and resulting alerts in memory only.
    scored = score_live_batch(generated, st.session_state.argus_sim_events)
    st.session_state.argus_sim_events = (
        scored + st.session_state.argus_sim_events
    )[:5000]
    flagged = [
        event for event in scored
        if int(event.get("risk_score", 0) or 0)
        >= int(event.get("alert_threshold", ALERT_THRESHOLD))
    ]
    if flagged:
        st.session_state.argus_live_alerts = (
            flagged + st.session_state.argus_live_alerts
        )[:100]
    if scored:
        st.session_state.argus_live_risk_peak = max(
            st.session_state.argus_live_risk_peak,
            max(event["risk_score"] for event in scored),
        )

# --------------------------------------------------

# SIDEBAR

# --------------------------------------------------



with st.sidebar:

    st.markdown(

        '<div class="argus-brand"><span class="argus-brand-mark">◈</span><span>ARGUS</span></div>',

        unsafe_allow_html=True,

    )



    st.markdown(

        '<div class="argus-subtitle">'

        'Financial Intelligence System'

        '</div>',

        unsafe_allow_html=True,

    )



    st.divider()



    navigation_labels = {
        "Overview": "⌂  Overview",
        "Live Transfers": "◉  Live Transfers",
        "Account Search": "⌕  Account Search",
        "Transfer Map": "↗  Transfer Map",
        "Detection Alerts": "⚑  Detection Alerts",
        "Investigations": "▤  Investigations",
        "Entity Intelligence": "◎  Entity Intelligence",
        "Reports": "▧  Reports",
    }

    page = st.radio(

        "NAVIGATION",

        [

            "Overview",

            "Live Transfers",

            "Account Search",

            "Transfer Map",

            "Detection Alerts",

            "Investigations",

            "Entity Intelligence",

            "Reports",

        ],

        label_visibility="visible",
        format_func=lambda value: navigation_labels.get(value, value),

    )



    st.divider()

    st.markdown(
        '''<div class="argus-status-block">
            <div class="argus-status-title">SYSTEM STATUS</div>
            <div class="argus-status-row"><i class="argus-status-dot"></i><span>Local application</span><b>READY</b></div>
            <div class="argus-status-row"><i class="argus-lock-mark">⌑</i><span>Offline-first mode</span></div>
            <div class="argus-build-label">Scalable development build 0.4</div>
        </div>''',
        unsafe_allow_html=True,
    )





# --------------------------------------------------

# TOP-LEVEL METRICS

# --------------------------------------------------



st.markdown(
    f'''<div class="argus-app-topbar">
        <div class="argus-app-crumb"><span class="argus-crumb-brand">ARGUS</span><span class="argus-crumb-separator">/</span><span>{html.escape(str(page))}</span></div>
        <div class="argus-topbar-status"><span class="argus-topbar-dot"></span> LOCAL DEMO <span class="argus-topbar-divider"></span> OFFLINE-FIRST <span class="argus-topbar-avatar">A</span></div>
    </div>''',
    unsafe_allow_html=True,
)


accounts_count = count_rows("accounts")

transactions_count = count_rows("transactions")

alerts_count = count_rows("alerts")

cases_count = count_rows("cases")



if page == "Overview":

    @st.fragment(run_every="1s")
    def live_overview_panel():
        # Keep the active simulation flowing while the dashboard is visible.
        advance_simulation_session()
        overview_stats, active_accounts, coverage, alert_summary, top_alert_account = load_overview_snapshot()
        simulator_running = bool(st.session_state.get("argus_sim_running", False))
        live_events = [dict(item) for item in st.session_state.get("argus_live_alerts", [])]
        live_transfer_count = int(st.session_state.get("argus_sim_total", 0) or 0)
        saved_alert_count = count_saved_alerts_fresh()

        st.markdown(
            '<section class="argus-hero argus-dashboard-hero">'
            '<div class="argus-hero-copy"><div class="argus-eyebrow">'
            '<span class="argus-live-dot"></span> ARGUS · FINANCIAL INTELLIGENCE</div>'
            '<h1>Financial overview</h1>'
            '<p>A clear view of transfer activity and accounts that may need review.</p></div>'
            f'<div class="argus-hero-side"><div class="argus-hero-badge">◈ LOCAL DEMO</div>'
            f'<div class="argus-session-status"><i class="{"is-running" if simulator_running else ""}"></i>'
            f'Simulation {"running" if simulator_running else "paused"}</div></div></section>',
            unsafe_allow_html=True,
        )

        live_count = len(live_events)
        metric_cols = st.columns(4, gap="medium")
        metric_cols[0].metric("Accounts", f"{accounts_count:,}")
        metric_cols[1].metric("Saved alerts", f"{saved_alert_count:,}", help="Existing alerts saved in the database.")
        metric_cols[2].metric("Live alerts", f"{live_count:,}", help="Alerts from this simulation session. These are not saved to the database.")
        metric_cols[3].metric("Transfers this session", f"{live_transfer_count:,}")

        live_account_ids = sorted({str(value) for item in live_events for value in (item.get("Sender"), item.get("Receiver")) if value})
        live_names = {}
        if live_account_ids:
            with get_connection() as conn:
                for offset in range(0, len(live_account_ids), 350):
                    group = live_account_ids[offset:offset + 350]
                    marks = ",".join("?" for _ in group)
                    for row in conn.execute(f"SELECT account_id, entity_name FROM accounts WHERE account_id IN ({marks})", group):
                        live_names[str(row["account_id"])] = str(row["entity_name"] or row["account_id"])

        live_by_account = {}
        for item in live_events:
            for account_id in dict.fromkeys(str(item.get(key) or "") for key in ("Sender", "Receiver")):
                if not account_id:
                    continue
                item_score = int(item.get("risk_score", 0) or 0)
                item_time = str(item.get("Timestamp") or "")
                current = live_by_account.get(account_id)
                if current is None:
                    live_by_account[account_id] = {
                        "Account ID": account_id,
                        "Name": live_names.get(account_id, account_id),
                        "Reason": _plain_alert_reason(item.get("risk_reasons", "Review reason not stored")),
                        "Review score": item_score,
                        "Latest update": item_time,
                        "Source": "Live session",
                        "Transfers flagged": 0,
                    }
                current = live_by_account[account_id]
                current["Review score"] = max(int(current["Review score"]), item_score)
                current["Transfers flagged"] += 1
                current["Latest update"] = max(str(current["Latest update"]), item_time)
                if item_time >= str(current.get("_reason_time", "")):
                    current["Reason"] = _plain_alert_reason(item.get("risk_reasons", "Review reason not stored"))
                    current["_reason_time"] = item_time

        saved_recent = load_overview_recent_alerts(8)
        recent_rows = []
        if not saved_recent.empty:
            for item in saved_recent.to_dict("records"):
                account_id = str(item.get("Account") or "Unknown account")
                recent_rows.append({
                    "Account ID": account_id,
                    "Name": str(item.get("Name") or account_id),
                    "Reason": _plain_alert_reason(item.get("Reason", "Review reason not stored")),
                    "Review score": int(float(item.get("Score") or 0)),
                    "Latest update": str(item.get("Time") or ""),
                    "Source": "Saved alert",
                    "Transfers flagged": 1,
                })
        recent_rows.extend(live_by_account.values())
        recent_frame = pd.DataFrame(recent_rows)
        if not recent_frame.empty:
            recent_frame = recent_frame.sort_values(["Latest update", "Review score"], ascending=[False, False])
            recent_frame = recent_frame.drop_duplicates(subset=["Account ID"], keep="first").head(6)

        focus_id = None
        focus_source = "Saved alert"
        focus_score = 0
        focus_alert_count = 0
        focus_latest = ""
        if live_by_account:
            live_focus = sorted(live_by_account.values(), key=lambda row: (row["Review score"], row["Latest update"]), reverse=True)[0]
            focus_id = live_focus["Account ID"]
            focus_source = "Live session"
            focus_score = int(live_focus["Review score"])
            focus_alert_count = int(live_focus["Transfers flagged"])
            focus_latest = str(live_focus["Latest update"])
        elif top_alert_account:
            focus_id = str(top_alert_account.get("account_id") or "")
            focus_score = int(float(top_alert_account.get("peak_score") or 0))
            focus_alert_count = int(top_alert_account.get("alert_count") or 0)
            focus_latest = str(alert_summary.get("latest_alert_at") or "")

        if focus_id:
            focus = load_overview_focus_context(focus_id)
            focus_name = str(focus.get("entity_name") or live_names.get(focus_id) or focus_id)
            focus_role = str(focus.get("role") or "Role not listed")
            focus_industry = str(focus.get("industry") or "Industry not listed")
            focus_source_label = "Fictional demo profile" if focus.get("profile_source") == "SYNTHETIC_DEMO" else "Profile not available"
            focus_relationships = int(focus.get("declared_links") or 0)
            focus_cases = int(focus.get("fictional_cases") or 0)
        else:
            focus = {}
            focus_name, focus_role, focus_industry = "No account to review yet", "Not listed", "Not listed"
            focus_source_label, focus_relationships, focus_cases = "Waiting for alerts", 0, 0

        left_col, right_col = st.columns([1.65, 1], gap="large")
        with left_col:
            st.markdown('<div class="argus-section-kicker">ACTIVITY · LATEST SESSION</div>', unsafe_allow_html=True)
            st.subheader("Transfer activity")
            sim_events = st.session_state.get("argus_sim_events", [])[:5000]
            live_chart_data = pd.DataFrame(sim_events)
            use_live_sample = False
            if not live_chart_data.empty and "Timestamp" in live_chart_data:
                event_times = pd.to_datetime(live_chart_data["Timestamp"], utc=True, errors="coerce")
                latest_time = event_times.max()
                if pd.notna(latest_time):
                    use_live_sample = True
                    latest_bucket = latest_time.floor("5min")
                    bucket_starts = pd.date_range(end=latest_bucket, periods=12, freq="5min")
                    live_chart_data["_event_time"] = event_times
                    live_chart_data = live_chart_data[live_chart_data["_event_time"] >= bucket_starts[0]]
                    live_chart_data["_bucket"] = live_chart_data["_event_time"].dt.floor("5min")
                    live_counts = live_chart_data.groupby("_bucket").size()
                    chart_data = pd.DataFrame({
                        "Interval": [stamp.strftime("%H:%M") for stamp in bucket_starts],
                        "Transfers": [int(live_counts.get(stamp, 0)) for stamp in bucket_starts],
                    })
                    chart_caption = "Current simulation · transfers grouped into 5-minute intervals."
            if not use_live_sample:
                activity = load_transaction_activity()
                hourly = {int(row["hour"]): int(row["transaction_count"]) for _, row in activity.iterrows() if pd.notna(row["hour"])}
                chart_data = pd.DataFrame({"Interval": [f"{hour:02d}:00" for hour in range(24)], "Transfers": [hourly.get(hour, 0) for hour in range(24)]})
                chart_caption = "No current-session transfers yet · showing hourly totals from the latest saved history."
            st.caption(chart_caption)
            if chart_data.empty or not int(chart_data["Transfers"].sum()):
                st.info("Start Live Transfers to see current session activity.")
            else:
                peak_row = chart_data.loc[chart_data["Transfers"].idxmax()]
                bar_colors = ["#315166"] * len(chart_data)
                bar_colors[int(chart_data["Transfers"].values.argmax())] = "#55dfd0"
                fig = go.Figure(go.Bar(x=chart_data["Interval"], y=chart_data["Transfers"], marker=dict(color=bar_colors, line=dict(width=0)), hovertemplate="%{x}<br>%{y:,} transfers<extra></extra>"))
                fig.update_layout(template="plotly_dark", height=255, margin=dict(l=8, r=8, t=8, b=4), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", xaxis_title=None, yaxis_title=None, showlegend=False, bargap=0.28)
                fig.update_xaxes(type="category", nticks=10 if use_live_sample else 8, gridcolor="rgba(130,160,190,0.08)", zeroline=False)
                fig.update_yaxes(rangemode="tozero", gridcolor="rgba(130,160,190,0.10)", zeroline=False)
                st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
                chart_meta = st.columns(3)
                chart_meta[0].metric("Transfers shown", f"{int(chart_data['Transfers'].sum()):,}")
                chart_meta[1].metric("Busiest interval", str(peak_row["Interval"]))
                chart_meta[2].metric("Peak transfers", f"{int(peak_row['Transfers']):,}")

            st.markdown('<div class="argus-section-kicker">REVIEW QUEUE</div>', unsafe_allow_html=True)
            st.subheader("Latest accounts for review")
            st.caption("One row per account · live alerts and saved alerts are identified separately.")
            if recent_frame.empty:
                st.info("No alerts yet. Start Live Transfers or wait for saved alert records.")
            else:
                view = recent_frame.rename(columns={"Account ID": "Account", "Name": "Account name", "Reason": "Main reason", "Review score": "Score", "Latest update": "Latest update", "Source": "Source"})
                st.dataframe(view[["Account", "Account name", "Main reason", "Score", "Source"]], use_container_width=True, hide_index=True, column_config={"Score": st.column_config.ProgressColumn("Review score", min_value=0, max_value=100, format="%d")})

        with right_col:
            st.markdown('<div class="argus-section-kicker">REVIEW FOCUS</div>', unsafe_allow_html=True)
            st.subheader("Account to look at first")
            if focus_id:
                initials = "".join(part[0] for part in focus_name.split()[:2] if part).upper() or "A"
                st.markdown(f'<div class="argus-focus-profile"><span class="argus-focus-avatar">{html.escape(initials)}</span><span><strong>{html.escape(focus_name)}</strong><small>{html.escape(focus_id)} · {html.escape(focus_role)} · {html.escape(focus_industry)}</small></span></div>', unsafe_allow_html=True)
                st.markdown(f'<div class="argus-focus-source">{html.escape(focus_source_label)} · {html.escape(focus_source)}</div>', unsafe_allow_html=True)
                focus_metrics = st.columns(2)
                focus_metrics[0].metric("Review score", f"{focus_score}/100")
                focus_metrics[1].metric("Flagged transfers", f"{focus_alert_count:,}")
                st.markdown(f'<div class="argus-focus-row"><span>Listed work / ownership links</span><strong>{focus_relationships:,}</strong></div><div class="argus-focus-row"><span>Fictional case records</span><strong>{focus_cases:,}</strong></div>', unsafe_allow_html=True)
                st.caption("The score is a review signal. The profile and case details are fictional and are not verified.")
            else:
                st.info("No account has been flagged in the current session or saved alert records.")

            st.markdown('<div class="argus-section-kicker">LATEST UPDATES</div>', unsafe_allow_html=True)
            st.subheader("Recent activity")
            updates = []
            for item in sorted(live_events, key=lambda row: str(row.get("Timestamp") or ""), reverse=True)[:4]:
                endpoint = str(item.get("Receiver") or item.get("Sender") or "Account unavailable")
                event_reason = _plain_reason_items(item.get("risk_reasons", ""), 1)
                updates.append((live_names.get(endpoint, endpoint), event_reason[0] if event_reason else "A transfer was flagged for review", str(item.get("Timestamp") or "Current session"), "Live"))
            if not updates and not saved_recent.empty:
                for item in saved_recent.head(4).to_dict("records"):
                    updates.append((str(item.get("Name") or item.get("Account") or "Account unavailable"), _plain_reason_items(item.get("Reason", ""), 1)[0] if item.get("Reason") else "Saved alert needs review", str(item.get("Time") or "Saved record"), "Saved"))
            if updates:
                update_html = []
                for name, message, time_text, source_text in updates:
                    update_html.append(f'<div class="argus-update-row"><span class="argus-update-dot"></span><span class="argus-update-copy"><strong>{html.escape(name)}</strong><small>{html.escape(message)}</small></span><span class="argus-update-time">{html.escape(time_text[-8:] if len(time_text) > 8 else time_text)}<br>{html.escape(source_text)}</span></div>')
                st.markdown('<div class="argus-updates-list">' + "".join(update_html) + '</div>', unsafe_allow_html=True)
            else:
                st.caption("No new alert updates yet. Simulation events appear here when they are detected.")

        st.caption(f"Historical database: {transactions_count:,} transfers · {saved_alert_count:,} saved alerts. Live simulation alerts stay in this session and do not change existing database records.")

    live_overview_panel()

elif page == "Live Transfers":

    st.subheader("Live Transaction Stream")



    st.caption(

        "Simulated transfers update persistent account balances. "

        "Simulated transfer events are not added to historical transactions."

    )



    defaults = {

        "argus_sim_running": False,

        "argus_sim_total": 0,

        "argus_sim_volume": 0.0,

        "argus_sim_largest": 0.0,

        "argus_sim_balances": {},

        "argus_sim_last_rate": 0.0,

        "argus_sim_last_batch": 0,

        "argus_sim_recent": [],

        "argus_sim_events": [],

        "argus_live_alerts": [],

        "argus_live_risk_peak": 0,

    }



    for key, value in defaults.items():

        if key not in st.session_state:

            st.session_state[key] = value



    @st.fragment(run_every="1s")

    def simulator_panel():

        st.markdown("#### Simulation controls")

        top1, top2, top3 = st.columns(3)



        with top1:

            if st.button(

                "▶ Start",

                disabled=st.session_state.argus_sim_running,

                use_container_width=True,

            ):

                st.session_state.argus_sim_running = True



        with top2:

            if st.button(

                "Ⅱ Pause",

                disabled=not st.session_state.argus_sim_running,

                use_container_width=True,

            ):

                st.session_state.argus_sim_running = False



        with top3:

            if st.button("↻ Reset session", use_container_width=True):

                st.session_state.argus_sim_running = False

                st.session_state.argus_sim_total = 0

                st.session_state.argus_sim_volume = 0.0

                st.session_state.argus_sim_largest = 0.0

                st.session_state.argus_sim_last_rate = 0.0

                st.session_state.argus_sim_last_batch = 0

                st.session_state.argus_sim_recent = []

                st.session_state.argus_sim_events = []

                st.session_state.argus_live_alerts = []

                st.session_state.argus_live_risk_peak = 0



                # Clear only session metrics/cache, not saved account balances.

                st.session_state.argus_sim_balances = {}



        target_rate = st.slider(

            "Transfers generated per second",

            min_value=10,

            max_value=500,

            value=100,

            step=10,

            help="Controls how quickly the simulator creates new example transfers.",

            key="argus_sim_target_rate",

        )



        if st.session_state.argus_sim_running:

            if accounts_count < 2:

                st.session_state.argus_sim_running = False

                st.error("At least two accounts are required.")

            else:

                requested_count = get_variable_batch_size(target_rate)



                generated, elapsed, batch_volume = generate_simulation_batch(

                    requested_count,

                    st.session_state.argus_sim_balances,

                )



                st.session_state.argus_sim_total += len(generated)

                st.session_state.argus_sim_volume += batch_volume



                if generated:

                    st.session_state.argus_sim_largest = max(

                        st.session_state.argus_sim_largest,

                        max(row["Amount (INR)"] for row in generated),

                    )



                st.session_state.argus_sim_last_batch = len(generated)

                st.session_state.argus_sim_last_rate = (

                    len(generated) / max(elapsed, 0.001)

                )

                st.session_state.argus_sim_recent = (

                    generated + st.session_state.argus_sim_recent

                )[:100]

                # Simulated events and alerts stay in session memory only.

                scored = score_live_batch(

                    generated,

                    st.session_state.argus_sim_events,

                )

                st.session_state.argus_sim_events = (

                    scored + st.session_state.argus_sim_events

                )[:5000]

                flagged = [
                    event for event in scored
                    if int(event.get("risk_score", 0) or 0)
                    >= int(event.get("alert_threshold", ALERT_THRESHOLD))
                ]

                if flagged:

                    # Keep live simulation alerts in session memory only.

                    st.session_state.argus_live_alerts = (

                        flagged + st.session_state.argus_live_alerts

                    )[:100]

                if scored:

                    st.session_state.argus_live_risk_peak = max(

                        st.session_state.argus_live_risk_peak,

                        max(event["risk_score"] for event in scored),

                    )



        st.divider()

        if st.session_state.argus_sim_running:

            st.success("Simulation is running · new events refresh automatically")

        else:

            st.info("Simulation is paused · your current session results are kept")


        summary = st.columns(4)

        summary[0].metric("Transfers this session", f"{st.session_state.argus_sim_total:,}")

        summary[1].metric("Total simulated flow", f"₹{st.session_state.argus_sim_volume:,.2f}")

        summary[2].metric("Current speed", f"{st.session_state.argus_sim_last_rate:,.0f}/s")

        summary[3].metric("Peak risk", f"{st.session_state.argus_live_risk_peak}/100")


        monitor_tab, alerts_tab, balances_tab = st.tabs([

            "Activity",

            f"Risk alerts ({len(st.session_state.argus_live_alerts)})",

            f"Account balances ({len(st.session_state.argus_sim_balances)})",

        ])


        with monitor_tab:

            activity_left, activity_right = st.columns(2)

            activity_left.metric("Largest transfer", f"₹{st.session_state.argus_sim_largest:,.2f}")

            activity_right.metric("Accounts touched", f"{len(st.session_state.argus_sim_balances):,}")

            st.markdown("#### Latest simulated transfers")

            recent_events = st.session_state.argus_sim_recent

            if recent_events:

                recent_df = pd.DataFrame(recent_events)

                if "Timestamp" in recent_df.columns:

                    event_times = pd.to_datetime(recent_df["Timestamp"], errors="coerce")

                    valid_times = event_times.notna()

                    if valid_times.any():

                        timeline = pd.DataFrame({"Minute": event_times[valid_times].dt.floor("min")})

                        timeline = timeline.groupby("Minute").size().rename("Transfers").to_frame()

                        st.markdown("**Simulation activity by minute**")

                        st.line_chart(timeline, height=190)

                visible = [c for c in ["Timestamp", "Transaction ID", "Sender", "Receiver", "Amount (INR)"] if c in recent_df.columns]

                st.dataframe(

                    recent_df[visible].head(30),

                    use_container_width=True,

                    hide_index=True,

                    column_config={"Amount (INR)": st.column_config.NumberColumn(format="₹%.2f")},

                )

                st.caption("Showing the newest 30 of up to 100 simulated transfers kept in this session.")

            else:

                st.info("No simulated transfers yet. Press Start to begin the stream.")

            with st.expander("Compare with saved transaction history"):

                st.caption("These are existing database records. New simulation transfers are not added here.")

                st.dataframe(load_recent_transactions(10), use_container_width=True, hide_index=True)


        with alerts_tab:

            alert_events = st.session_state.argus_live_alerts

            if not alert_events:

                st.info("No high-risk patterns detected in this session yet. Detection continues while the simulator runs.")

            else:

                alert_df = pd.DataFrame(alert_events)

                if "Sender" not in alert_df:

                    alert_df["Sender"] = "Unknown account"

                alert_df["Sender"] = alert_df["Sender"].fillna("Unknown account").astype(str)

                grouped = alert_df.groupby("Sender", dropna=False).agg(

                    Events=("Sender", "size"),

                    Connected_accounts=("Receiver", "nunique") if "Receiver" in alert_df else ("Sender", "size"),

                    Total_flagged_flow=("Amount (INR)", "sum") if "Amount (INR)" in alert_df else ("risk_score", "size"),

                    Peak_risk=("risk_score", "max") if "risk_score" in alert_df else ("Sender", "size"),

                ).reset_index().rename(columns={

                    "Sender":"Suspect account",

                    "Connected_accounts":"Connected accounts",

                    "Total_flagged_flow":"Flagged flow (INR)",

                    "Peak_risk":"Peak risk",

                }).sort_values(["Peak risk", "Events"], ascending=False)

                metrics = st.columns(3)

                metrics[0].metric("Suspect accounts", f"{len(grouped):,}")

                metrics[1].metric("Alert events", f"{len(alert_df):,}")

                metrics[2].metric("Highest risk", f"{int(grouped['Peak risk'].max()):,}/100" if "Peak risk" in grouped else "—")

                st.dataframe(grouped, use_container_width=True, hide_index=True)

                st.markdown("#### Account details")

                for _, account in grouped.iterrows():

                    account_id = account["Suspect account"]

                    account_events = alert_df[alert_df["Sender"] == account_id].copy()
                    if "risk_reasons" in account_events:
                        account_events["risk_reasons"] = account_events["risk_reasons"].map(_plain_alert_reason)

                    with st.expander(f"{account_id} · {int(account['Events'])} event(s) · peak risk {int(account['Peak risk']) if pd.notna(account['Peak risk']) else 0}/100"):

                        other_accounts = sorted(set(account_events["Receiver"].dropna().astype(str))) if "Receiver" in account_events else []

                        st.write("**Connected accounts:** " + (", ".join(other_accounts) if other_accounts else "No connected account details"))

                        visible = [c for c in ["Timestamp", "Transaction ID", "Sender", "Receiver", "Amount (INR)", "risk_score", "risk_level", "risk_reasons", "entity_context"] if c in account_events.columns]

                        st.dataframe(account_events[visible], use_container_width=True, hide_index=True)

                st.caption("These are synthetic session alerts for review. They are not saved to SQLite and do not indicate real-world wrongdoing.")


        with balances_tab:

            live_balances = st.session_state.argus_sim_balances

            if live_balances:

                balance_ids = list(live_balances.keys())[-100:]

                placeholders = ",".join("?" for _ in balance_ids)

                with get_connection() as conn:

                    balance_entities = pd.read_sql_query(

                        f"""

                        SELECT account_id AS "Account Number",

                               entity_name AS "Entity Name",

                               account_type AS "Account Type",

                               opening_balance AS "Opening Balance (INR)",

                               balance AS "Stored Current Balance (INR)"

                        FROM accounts

                        WHERE account_id IN ({placeholders})

                        """,

                        conn,

                        params=balance_ids,

                    )

                balance_entities["Current Balance (INR)"] = balance_entities["Account Number"].map(live_balances)

                balance_entities = balance_entities.sort_values("Current Balance (INR)", ascending=False)

                st.dataframe(

                    balance_entities,

                    use_container_width=True,

                    hide_index=True,

                    column_config={

                        "Opening Balance (INR)": st.column_config.NumberColumn(format="₹%.2f"),

                        "Current Balance (INR)": st.column_config.NumberColumn(format="₹%.2f"),

                        "Stored Current Balance (INR)": st.column_config.NumberColumn(format="₹%.2f"),

                    },

                )

                st.caption("Balances are updated in the account records. Simulated transfer events remain separate from historical transactions.")

            else:

                st.info("No accounts have been touched in this session yet. Start the simulator to see affected balances.")


    simulator_panel()

# --------------------------------------------------

# ACCOUNT SEARCH

# --------------------------------------------------



elif page == "Account Search":

    st.subheader("🔎 Account Search")



    st.markdown(

        "Find an account by user/entity name or account number. "

        "Select a matching account to inspect its stored transaction history."

    )



    st.caption(

        "Search supports partial matches. For example, searching "

        "`ACC100` can return account IDs containing that text."

    )



    with st.form("account_search_form"):

        search_term = st.text_input(

            "User name or account number",

            placeholder="Enter a name or account ID...",

        )



        max_results = st.selectbox(

            "Maximum search results",

            options=[25, 50, 100, 200],

            index=1,

        )



        submitted = st.form_submit_button(

            "Search accounts",

            use_container_width=True,

            type="primary",

        )



    if submitted:

        if not search_term.strip():

            st.warning("Enter a user name or account number first.")

            st.session_state.pop("argus_account_search_results", None)

        else:

            with st.spinner("Searching accounts..."):

                results = search_accounts(search_term, max_results)



            st.session_state.argus_account_search_results = results

            st.session_state.argus_account_search_term = search_term.strip()



    results = st.session_state.get("argus_account_search_results")



    if results is not None:

        search_label = st.session_state.get(

            "argus_account_search_term",

            "",

        )



        st.divider()

        st.subheader("Search Results")



        if results.empty:

            st.warning(

                f"No accounts matched **{search_label}**. "

                "Check the spelling or try a shorter search."

            )

        else:

            st.success(

                f"Found {len(results):,} matching account(s) "

                f"for **{search_label}**."

            )



            st.dataframe(

                results,

                use_container_width=True,

                hide_index=True,

                column_config={

                    "Opening Balance (INR)": st.column_config.NumberColumn(

                        format="₹%.2f"

                    ),

                    "Current Balance (INR)": st.column_config.NumberColumn(

                        format="₹%.2f"

                    ),

                },

            )



            account_options = results["Account Number"].tolist()

            name_lookup = dict(

                zip(

                    results["Account Number"],

                    results["User / Entity Name"],

                )

            )



            st.divider()

            st.subheader("Inspect an Account")



            selected_account = st.selectbox(

                "Choose an account",

                options=account_options,

                format_func=lambda account_id: (

                    f"{name_lookup.get(account_id, 'Unknown entity')} "

                    f"— {account_id}"

                ),

                key="argus_selected_account",

            )



            if selected_account:

                account, totals = load_account_summary(selected_account)



                if account is None:

                    st.error("That account could not be found.")

                else:

                    st.markdown(f"### {account['entity_name']}")

                    st.caption(f"Account number: {account['account_id']}")



                    a1, a2, a3, a4 = st.columns(4)



                    a1.metric(

                        "Account Type",

                        str(account["account_type"]).title(),

                    )

                    a2.metric(

                        "Opening Balance",

                        f"₹{float(account['opening_balance']):,.2f}",

                    )

                    a3.metric(

                        "Current Balance",

                        f"₹{float(account['balance']):,.2f}",

                    )

                    a4.metric(

                        "Stored Transactions",

                        f"{int(totals['transaction_count']):,}",

                    )



                    st.caption(

                        "Balances are fictional demo values. Opening balance "

                        "is the assigned starting value; current balance "

                        "includes simulator transfers. Transaction totals below "

                        "reflect stored historical transactions only."

                    )



                    incoming_col, outgoing_col = st.columns(2)

                    incoming_col.metric(

                        "Historical Incoming Transfers",

                        f"₹{float(totals['incoming_total']):,.2f}",

                    )

                    outgoing_col.metric(

                        "Historical Outgoing Transfers",

                        f"₹{float(totals['outgoing_total']):,.2f}",

                    )



                    history_limit = st.selectbox(

                        "Transaction history size",

                        options=[25, 50, 100, 250, 500],

                        index=2,

                        key=f"history_limit_{selected_account}",

                    )



                    history = load_account_transactions(

                        selected_account,

                        history_limit,

                    )



                    st.subheader("Recent Stored Transaction History")



                    if history.empty:

                        st.info(

                            "No stored transactions were found for this account."

                        )

                    else:

                        st.dataframe(

                            history,

                            use_container_width=True,

                            hide_index=True,

                            column_config={

                                "Amount (INR)": st.column_config.NumberColumn(

                                    format="₹%.2f"

                                ),

                            },

                        )



                        csv_data = history.to_csv(index=False).encode("utf-8")



                        st.download_button(

                            "Download displayed transaction history as CSV",

                            data=csv_data,

                            file_name=(

                                f"argus_{selected_account}_transactions.csv"

                            ),

                            mime="text/csv",

                        )





# --------------------------------------------------

# TRANSFER MAP

# --------------------------------------------------



elif page == "Transfer Map":
    st.subheader("Connected transfer branches")
    st.caption("Follow transfers around one selected suspect account. The map groups repeated transfers, keeps arrow direction, and shows each account once.")

    # Keep current-session suspects and saved historical suspects in separate lists.
    with get_connection() as conn:
        saved_suspects = pd.read_sql_query("""
            SELECT al.account_id AS account_id,
                   COALESCE(ac.entity_name, al.account_id) AS account_name,
                   COALESCE(ac.account_type, 'Unknown') AS account_type,
                   COUNT(*) AS alert_count,
                   MAX(al.risk_score) AS peak_risk
            FROM alerts AS al
            LEFT JOIN accounts AS ac ON ac.account_id = al.account_id
            WHERE al.account_id IS NOT NULL AND TRIM(al.account_id) <> ''
            GROUP BY al.account_id, ac.entity_name, ac.account_type
            ORDER BY alert_count DESC, peak_risk DESC
            LIMIT 1000
        """, conn)

    live_alerts = pd.DataFrame(st.session_state.get("argus_live_alerts", []))
    live_suspects = pd.DataFrame(columns=["account_id", "account_name", "account_type", "alert_count", "peak_risk"])
    if not live_alerts.empty and "Sender" in live_alerts:
        live_alerts["Sender"] = live_alerts["Sender"].fillna("").astype(str)
        live_alerts = live_alerts[live_alerts["Sender"].str.strip() != ""]
        if not live_alerts.empty:
            live_suspects = live_alerts.groupby("Sender").agg(
                alert_count=("Sender", "size"),
                peak_risk=("risk_score", "max") if "risk_score" in live_alerts else ("Sender", "size"),
            ).reset_index().rename(columns={"Sender":"account_id"})
            live_suspects["account_name"] = live_suspects["account_id"]
            live_suspects["account_type"] = "Live simulation"

    account_source = st.radio(
        "Choose account list",
        ["Live simulation suspects", "Saved historical suspects"],
        horizontal=True,
        key="suspect_map_account_source",
        help="These lists are separate. Live mode uses only current-session simulation events; historical mode uses saved alerts and transactions.",
    )
    if account_source == "Live simulation suspects":
        suspects = live_suspects.copy()
    else:
        suspects = saved_suspects.copy()

    if suspects.empty:
        if account_source == "Live simulation suspects":
            st.info("No live simulation suspects have been detected in this session yet. Start Live Transfers and switch to Saved historical suspects to view older alerts.")
        else:
            st.info("No saved historical suspect accounts are available.")
    else:
        suspects["account_id"] = suspects["account_id"].astype(str)
        suspects = suspects.sort_values(["alert_count", "peak_risk"], ascending=False, na_position="last").reset_index(drop=True)
        suspect_display = suspects.rename(columns={"account_id":"Suspect account", "account_name":"Account name", "alert_count":"Alerts", "peak_risk":"Peak risk"})
        st.markdown("#### Select an account")
        selection = st.dataframe(suspect_display[["Suspect account", "Account name", "Alerts", "Peak risk"]], use_container_width=True, hide_index=True, height=220, on_select="rerun", selection_mode="single-row", key="suspect_map_account_picker")
        account_ids = suspects["account_id"].tolist()
        selected_rows = selection.selection.rows
        selected_from_table = str(suspect_display.iloc[selected_rows[0]]["Suspect account"]) if selected_rows else None
        old_root = st.session_state.get("suspect_map_root_account")
        selected_root = selected_from_table if selected_from_table in account_ids else old_root if old_root in account_ids else account_ids[0]
        st.session_state["suspect_map_root_account"] = selected_root
        selected_info = suspects[suspects["account_id"] == selected_root].iloc[0]
        st.caption(f"Selected: {selected_info['account_name']} · {int(selected_info['alert_count'])} alert event(s) · peak risk {int(selected_info['peak_risk']) if pd.notna(selected_info['peak_risk']) else 0}/100")

        branch_depth = st.select_slider(
            "How many transfer steps should the map follow?",
            options=[1, 2, 3, 4, 5], value=3,
            key="suspect_map_branch_depth",
            help="Step 1 shows direct transfers. Higher steps follow connected sender and recipient accounts.",
        )
        max_map_accounts = st.select_slider(
            "Maximum accounts to draw",
            options=[100, 200, 300], value=200,
            key="suspect_map_branch_accounts",
            help="The map stays readable by limiting drawn accounts. The transfer table keeps all links found in the search.",
        )

        branch_truncated = False
        if account_source == "Saved historical suspects":
            with st.spinner("Following the selected account's transfer branches..."):
                direct = load_account_branches(selected_root, depth=branch_depth, max_edges=5000)
            direct = direct.copy() if direct is not None else pd.DataFrame()
            if not direct.empty:
                direct["flow_source"] = "Historical"
                branch_truncated = len(direct) >= 5000
        else:
            sim_all = pd.DataFrame(st.session_state.get("argus_sim_events", []))
            if not sim_all.empty:
                sim_all = sim_all.rename(columns={
                    "Transaction ID": "transaction_id", "Timestamp": "timestamp",
                    "Sender": "sender_id", "Receiver": "receiver_id",
                    "Amount (INR)": "amount",
                })
                sim_all["sender_id"] = sim_all["sender_id"].fillna("").astype(str)
                sim_all["receiver_id"] = sim_all["receiver_id"].fillna("").astype(str)
                frontier = {selected_root}
                visited = {selected_root}
                selected_indexes = set()
                for _step in range(int(branch_depth)):
                    if not frontier:
                        break
                    connected = sim_all[
                        sim_all["sender_id"].isin(frontier)
                        | sim_all["receiver_id"].isin(frontier)
                    ]
                    selected_indexes.update(connected.index.tolist())
                    next_frontier = set(connected["sender_id"]) | set(connected["receiver_id"])
                    next_frontier -= visited
                    visited.update(next_frontier)
                    frontier = next_frontier
                direct = sim_all.loc[sorted(selected_indexes)].copy() if selected_indexes else pd.DataFrame()
                branch_truncated = len(sim_all) >= 5000
                if not direct.empty:
                    if "transaction_id" not in direct:
                        direct["transaction_id"] = [f"LIVE-{i}" for i in direct.index]
                    direct["flow_source"] = "Live simulation"
                    live_scores = pd.to_numeric(direct.get("risk_score", pd.Series(0, index=direct.index)), errors="coerce").fillna(0)
                    cutoffs = pd.to_numeric(direct.get("alert_threshold", pd.Series(ALERT_THRESHOLD, index=direct.index)), errors="coerce").fillna(ALERT_THRESHOLD)
                    direct["is_flagged"] = live_scores >= cutoffs
                    direct["alert_reasons"] = direct.get("risk_reasons", pd.Series("", index=direct.index)).map(_plain_alert_reason)
                    direct["alert_score"] = direct.get("risk_score", 0)
                    direct["currency"] = "INR"
                    direct["sender_name"] = direct["sender_id"]
                    direct["receiver_name"] = direct["receiver_id"]
                    direct["sender_type"] = "Live simulation"
                    direct["receiver_type"] = "Live simulation"

        if not direct.empty:
            direct["sender_id"] = direct["sender_id"].fillna("").astype(str)
            direct["receiver_id"] = direct["receiver_id"].fillna("").astype(str)
            direct["amount"] = pd.to_numeric(direct.get("amount", 0), errors="coerce").fillna(0)
            if "is_flagged" not in direct:
                direct["is_flagged"] = False
            direct["is_flagged"] = direct["is_flagged"].fillna(False).astype(bool)
            if "flow_source" not in direct:
                direct["flow_source"] = "Historical"

        if direct.empty:
            st.info("No transfers were found within the selected branch depth for this account and source.")
        else:
            flows = direct.groupby(["sender_id", "receiver_id"], as_index=False).agg(
                transaction_count=("transaction_id", "count"),
                total_amount=("amount", "sum"),
                alert_linked=("is_flagged", "max"),
                sources=("flow_source", lambda values: ", ".join(sorted(set(values.astype(str))))),
                reasons=("alert_reasons", lambda values: " · ".join(dict.fromkeys(str(v) for v in values.dropna() if str(v).strip()))),
            )
            flows["direct_link"] = (flows["sender_id"] == selected_root) | (flows["receiver_id"] == selected_root)
            incoming_direct = flows[(flows["receiver_id"] == selected_root) & (flows["sender_id"] != selected_root)]
            outgoing_direct = flows[(flows["sender_id"] == selected_root) & (flows["receiver_id"] != selected_root)]
            in_value = float(incoming_direct["total_amount"].sum())
            out_value = float(outgoing_direct["total_amount"].sum())

            # Build a layered, non-circle layout. The first transfer from the
            # focus chooses the incoming or outgoing side; later accounts keep
            # that side while the shortest path distance sets their column.
            adjacency = {}
            direct_in = {}
            direct_out = {}
            for edge in flows.itertuples(index=False):
                adjacency.setdefault(edge.sender_id, set()).add(edge.receiver_id)
                adjacency.setdefault(edge.receiver_id, set()).add(edge.sender_id)
                if edge.receiver_id == selected_root:
                    direct_in[edge.sender_id] = direct_in.get(edge.sender_id, 0.0) + float(edge.total_amount)
                if edge.sender_id == selected_root:
                    direct_out[edge.receiver_id] = direct_out.get(edge.receiver_id, 0.0) + float(edge.total_amount)

            node_depth = {selected_root: 0}
            node_side = {selected_root: 0}
            for account_id in set(direct_in) | set(direct_out):
                node_depth[account_id] = 1
                node_side[account_id] = -1 if direct_in.get(account_id, 0.0) >= direct_out.get(account_id, 0.0) else 1
            frontier = set(node_depth) - {selected_root}
            for step in range(2, int(branch_depth) + 1):
                next_frontier = set()
                for account_id in sorted(frontier):
                    for neighbor in adjacency.get(account_id, set()):
                        if neighbor != selected_root and neighbor not in node_depth:
                            node_depth[neighbor] = step
                            node_side[neighbor] = node_side[account_id]
                            next_frontier.add(neighbor)
                frontier = next_frontier

            account_strength = {}
            for edge in flows.itertuples(index=False):
                account_strength[edge.sender_id] = account_strength.get(edge.sender_id, 0.0) + float(edge.total_amount)
                account_strength[edge.receiver_id] = account_strength.get(edge.receiver_id, 0.0) + float(edge.total_amount)
            ranked_accounts = sorted(
                (account_id for account_id in node_depth if account_id != selected_root),
                key=lambda account_id: (node_depth[account_id], -account_strength.get(account_id, 0.0), account_id),
            )
            shown_accounts = set(ranked_accounts[:int(max_map_accounts)])
            shown_accounts.add(selected_root)
            shown_flows = flows[
                flows["sender_id"].isin(shown_accounts)
                & flows["receiver_id"].isin(shown_accounts)
            ].copy()
            shown_flows = shown_flows.sort_values(
                ["direct_link", "alert_linked", "total_amount"],
                ascending=[False, False, False],
            ).head(500)
            map_truncated = len(shown_accounts) < len(node_depth) or len(flows) > len(shown_flows)

            metric_cols = st.columns(4)
            metric_cols[0].metric("Into selected account", f"₹{in_value:,.2f}", f"{len(direct_in):,} direct accounts")
            metric_cols[1].metric("Out from selected account", f"₹{out_value:,.2f}", f"{len(direct_out):,} direct accounts")
            metric_cols[2].metric("Connected accounts", f"{len(node_depth) - 1:,}", f"{len(direct_in.keys() | direct_out.keys()):,} direct")
            metric_cols[3].metric("Transfers explored", f"{len(direct):,}", f"{len(flows):,} grouped links")

            positions = {selected_root: (0.0, 0.0)}
            columns = {}
            for account_id in shown_accounts:
                if account_id == selected_root:
                    continue
                key = (node_side.get(account_id, 1), node_depth.get(account_id, 1))
                columns.setdefault(key, []).append(account_id)
            for (side, depth), account_ids_in_column in columns.items():
                account_ids_in_column.sort(key=lambda account_id: (-account_strength.get(account_id, 0.0), account_id))
                count = len(account_ids_in_column)
                x_value = float(side * depth)
                for index, account_id in enumerate(account_ids_in_column):
                    positions[account_id] = (x_value, (count - 1) / 2 - index)

            max_column_size = max((len(value) for value in columns.values()), default=1)
            chart_height = max(520, min(3000, 360 + max_column_size * 19))
            fig = go.Figure()
            link_hover_x, link_hover_y, link_hover_text = [], [], []
            for edge in shown_flows.itertuples(index=False):
                x1, y1 = positions[edge.sender_id]
                x2, y2 = positions[edge.receiver_id]
                edge_color = "#EF5350" if bool(edge.alert_linked) else "#37B7A4" if "Live simulation" in edge.sources else "#5B9BFF"
                edge_width = 1.2 + min(3.0, float(edge.transaction_count) * .3)
                fig.add_annotation(
                    x=x2, y=y2, ax=x1, ay=y1, xref="x", yref="y", axref="x", ayref="y",
                    showarrow=True, arrowhead=2, arrowsize=.9, arrowwidth=edge_width,
                    arrowcolor=edge_color, text="",
                )
                link_hover_x.append((x1 + x2) / 2)
                link_hover_y.append((y1 + y2) / 2)
                direction_label = (
                    "Money sent toward selected account" if edge.receiver_id == selected_root
                    else "Money sent from selected account" if edge.sender_id == selected_root
                    else "Connected branch transfer"
                )
                link_hover_text.append(
                    f"{direction_label}<br>{edge.sender_id} → {edge.receiver_id}"
                    f"<br>Transfers grouped: {int(edge.transaction_count)}"
                    f"<br>Total value: ₹{float(edge.total_amount):,.2f}"
                    f"<br>Alert-linked: {'Yes' if edge.alert_linked else 'No'}"
                    f"<br>{edge.reasons}"
                )
            if link_hover_x:
                fig.add_trace(go.Scatter(
                    x=link_hover_x, y=link_hover_y, mode="markers",
                    marker=dict(size=15, color="#FFFFFF", opacity=.01),
                    hovertext=link_hover_text, hovertemplate="%{hovertext}<extra></extra>",
                    showlegend=False,
                ))

            names = {str(row.account_id): str(row.account_name) for row in suspects.itertuples(index=False)}
            for edge in direct.itertuples(index=False):
                names.setdefault(str(getattr(edge, "sender_id", "")), str(getattr(edge, "sender_name", "") or getattr(edge, "sender_id", "")))
                names.setdefault(str(getattr(edge, "receiver_id", "")), str(getattr(edge, "receiver_name", "") or getattr(edge, "receiver_id", "")))
            nodes = [selected_root] + sorted(shown_accounts - {selected_root}, key=lambda account_id: (node_side.get(account_id, 0), node_depth.get(account_id, 0), account_id))
            colors = ["#F5B942" if account_id == selected_root else "#AFC7FF" if node_side.get(account_id) == -1 else "#67CDB8" for account_id in nodes]
            symbols = ["diamond" if account_id == selected_root else "square" for account_id in nodes]
            sizes = [34 if account_id == selected_root else 16 if node_depth.get(account_id) == 1 else 12 for account_id in nodes]
            labels = nodes
            node_hover = [
                f"{'SELECTED SUSPECT' if account_id == selected_root else ('Incoming side' if node_side.get(account_id) == -1 else 'Outgoing side')}"
                f"<br>Account: {account_id}<br>Name: {names.get(account_id, account_id)}"
                f"<br>Step: {node_depth.get(account_id, 0)}"
                f"<br>Connected transfer value: ₹{account_strength.get(account_id, 0):,.2f}"
                for account_id in nodes
            ]
            xs = [positions[account_id][0] for account_id in nodes]
            ys = [positions[account_id][1] for account_id in nodes]
            fig.add_trace(go.Scatter(
                x=xs, y=ys, mode="markers+text",
                marker=dict(size=sizes, color=colors, symbol=symbols, line=dict(width=1, color="#FFFFFF")),
                text=labels, textposition="middle center", textfont=dict(size=8, color="#172033"),
                hovertext=node_hover, hovertemplate="%{hovertext}<extra></extra>", showlegend=False,
            ))
            max_y = max([abs(value) for value in ys] + [1.0]) + 1.0
            x_max = max([abs(value) for value in xs] + [1.0]) + .8
            fig.update_layout(
                height=chart_height, margin=dict(l=35, r=35, t=80, b=25),
                xaxis=dict(visible=False, range=[-x_max, x_max]),
                yaxis=dict(visible=False, range=[-max_y, max_y]),
                plot_bgcolor="#111827", paper_bgcolor="#111827",
                font=dict(color="#E5E7EB"), title=dict(text=f"Transfer branches around {selected_root}", x=.5),
                annotations=list(fig.layout.annotations) + [
                    dict(text="INCOMING SIDE · MONEY TOWARD FOCUS", x=-x_max * .72, y=max_y-.1, showarrow=False, font=dict(color="#AFC7FF", size=11)),
                    dict(text="SELECTED SUSPECT", x=0, y=max_y-.1, showarrow=False, font=dict(color="#F5B942", size=11)),
                    dict(text="OUTGOING SIDE · MONEY FROM FOCUS", x=x_max * .72, y=max_y-.1, showarrow=False, font=dict(color="#67CDB8", size=11)),
                ],
            )
            st.plotly_chart(fig, use_container_width=True, config={"displaylogo": False})
            st.caption("Each account appears once. Branch columns show how many steps it is from the selected suspect. Arrows always point from sender to receiver; hover over an arrow for transfer count, value, and alert details.")
            if map_truncated:
                st.warning(f"The map draws up to {max_map_accounts:,} accounts and 500 grouped links. The table below includes all {len(flows):,} links found in this search.")
            if branch_truncated:
                st.warning("The branch search reached its 5,000-transfer limit. Additional transfers may exist outside this search.")

            all_flow_table = flows.copy()
            all_flow_table["Step"] = all_flow_table.apply(
                lambda row: max(1, min(node_depth.get(row["sender_id"], int(branch_depth) + 1), node_depth.get(row["receiver_id"], int(branch_depth) + 1)) + 1),
                axis=1,
            )
            all_flow_table = all_flow_table.rename(columns={
                "sender_id": "From account", "receiver_id": "To account",
                "transaction_count": "Transfers", "total_amount": "Total value (INR)",
                "alert_linked": "Alert-linked",
            })
            st.markdown("#### Transfer links in these branches")
            st.dataframe(
                all_flow_table.sort_values(["Step", "Total value (INR)"], ascending=[True, False]).head(25)[
                    ["Step", "From account", "To account", "Transfers", "Total value (INR)", "Alert-linked"]
                ],
                use_container_width=True, hide_index=True,
            )
            with st.expander(f"Show all {len(all_flow_table):,} grouped transfer links"):
                st.dataframe(
                    all_flow_table.sort_values(["Step", "Total value (INR)"], ascending=[True, False])[
                        ["Step", "From account", "To account", "Transfers", "Total value (INR)", "Alert-linked"]
                    ],
                    use_container_width=True, hide_index=True,
                )


elif page == "Detection Alerts":
    st.subheader("Detection Alerts")
    st.caption("Choose one account from the queue to see why it was flagged, its background, and the flagged money moving in and out.")
    st.info("An alert means the activity should be reviewed; it does not prove wrongdoing. Live alerts are session-only. Saved alerts are read from the existing database. Demo profiles and case details are fictional.")

    live_events = [dict(event) for event in st.session_state.get("argus_live_alerts", [])]
    for event in live_events:
        event["risk_reasons"] = _plain_alert_reason(event.get("risk_reasons", ""))
        event["risk_patterns"] = [dict(item) for item in (event.get("risk_patterns") or []) if isinstance(item, dict)]
        for pattern in event["risk_patterns"]:
            pattern["pattern"] = _plain_alert_reason(pattern.get("pattern", ""))
            pattern["evidence"] = _plain_alert_reason(pattern.get("evidence", ""))
    live_df = pd.DataFrame(live_events)

    account_events = {}
    for event in live_events:
        sender = str(event.get("Sender", "") or "").strip()
        receiver = str(event.get("Receiver", "") or "").strip()
        for account_id, role, other in ((sender, "Sender", receiver), (receiver, "Receiver", sender)):
            if account_id:
                account_events.setdefault(account_id, []).append((event, role, other))

    account_profiles = {}
    live_account_ids = sorted(account_events)
    if live_account_ids:
        with get_connection() as conn:
            for start in range(0, len(live_account_ids), 400):
                chunk = live_account_ids[start:start + 400]
                marks = ",".join("?" for _ in chunk)
                for row in conn.execute(f"""
                    SELECT a.account_id, a.entity_name, a.account_type,
                           p.occupation, p.industry, p.employment_status, p.profile_source
                    FROM accounts a LEFT JOIN entity_profiles p ON p.account_id = a.account_id
                    WHERE a.account_id IN ({marks})
                """, chunk):
                    account_profiles[str(row["account_id"])] = dict(row)

    live_rows = []
    for account_id, items in account_events.items():
        unique_events = {}
        for event, _, _ in items:
            key = str(event.get("Transaction ID") or f"{event.get('Timestamp')}|{event.get('Sender')}|{event.get('Receiver')}|{event.get('Amount (INR)')}")
            unique_events[key] = event
        events = list(unique_events.values())
        profile = account_profiles.get(account_id, {})
        reasons = list(dict.fromkeys(
            reason for event in events
            for reason in _plain_reason_items(event.get("risk_reasons", ""))
            if reason.lower() != "no unusual pattern was found by the current checks"
        ))
        counterparties = {str(other) for _, _, other in items if other and str(other) != account_id}
        live_rows.append({
            "Account ID": account_id,
            "Account name": profile.get("entity_name") or account_id,
            "Profession": profile.get("occupation") or ("Business owner / operator" if profile.get("account_type") == "business" else "Profession not listed"),
            "Industry": profile.get("industry") or "Not listed",
            "Transfers flagged": len(events),
            "Other accounts": len(counterparties),
            "Money in (INR)": sum(float(event.get("Amount (INR)", 0) or 0) for event, role, _ in items if role == "Receiver"),
            "Money out (INR)": sum(float(event.get("Amount (INR)", 0) or 0) for event, role, _ in items if role == "Sender"),
            "Review score": max((int(event.get("risk_score", 0) or 0) for event in events), default=0),
            "Main reason": " · ".join(reasons[:2]) or "Review reason not stored",
            "Latest update": max((str(event.get("Timestamp", "")) for event in events), default=""),
        })
    live_groups = pd.DataFrame(live_rows)
    if not live_groups.empty:
        live_groups = live_groups.sort_values(["Review score", "Latest update"], ascending=[False, False]).reset_index(drop=True)

    with get_connection() as conn:
        hist_df = pd.read_sql_query("""
            SELECT al.alert_id, al.transaction_id, al.account_id, al.risk_score, al.reason,
                   al.status, al.created_at, t.sender_id, t.receiver_id, t.amount, t.timestamp,
                   dc.device_id AS demo_device_id, dc.origin_region AS demo_origin_region,
                   dc.payment_channel AS demo_payment_channel, dc.payment_purpose AS demo_payment_purpose,
                   dc.merchant_category AS demo_merchant_category, dc.invoice_reference AS demo_invoice_reference
            FROM alerts al
            LEFT JOIN transactions t ON t.transaction_id = al.transaction_id
            LEFT JOIN demo_transaction_context dc ON dc.transaction_id = al.transaction_id
            ORDER BY al.created_at DESC LIMIT 1000
        """, conn)
    if not hist_df.empty:
        hist_df["reason"] = hist_df["reason"].map(_plain_alert_reason)
        hist_df["account_id"] = hist_df["account_id"].fillna("Unknown account").astype(str)
        hist_groups = hist_df.groupby("account_id", dropna=False).agg(
            Saved_alerts=("alert_id", "nunique"),
            Review_score=("risk_score", "max"),
            Open_alerts=("status", lambda values: int(values.astype(str).str.lower().eq("open").sum())),
            Latest_alert=("created_at", "max"),
            Main_reason=("reason", lambda values: " · ".join(dict.fromkeys(reason for value in values.dropna() for reason in _plain_reason_items(value, 2)))),
        ).reset_index().rename(columns={
            "account_id": "Account ID", "Saved_alerts": "Saved alerts",
            "Review_score": "Review score", "Open_alerts": "Open alerts",
            "Latest_alert": "Latest update", "Main_reason": "Main reason",
        }).sort_values(["Review score", "Latest update"], ascending=[False, False]).reset_index(drop=True)
    else:
        hist_groups = pd.DataFrame(columns=["Account ID", "Saved alerts", "Review score", "Open alerts", "Latest update", "Main reason"])

    live_tab, saved_tab = st.tabs([f"Live simulation · {len(live_events):,}", f"Saved alerts · {len(hist_df):,}"])

    def render_account_focus(account_id, live_source):
        if live_source:
            profile = account_profiles.get(account_id, {})
            items = account_events.get(account_id, [])
            unique_events = {}
            for event, _, _ in items:
                key = str(event.get("Transaction ID") or f"{event.get('Timestamp')}|{event.get('Sender')}|{event.get('Receiver')}|{event.get('Amount (INR)')}")
                unique_events[key] = event
            events = list(unique_events.values())
            name = profile.get("entity_name") or account_id
            profession = profile.get("occupation") or ("Business owner / operator" if profile.get("account_type") == "business" else "Profession not listed")
            industry = profile.get("industry") or "Not listed"
            profile_source = "Fictional demo profile" if profile.get("profile_source") == "SYNTHETIC_DEMO" else "Profile not verified"
            score = max((int(event.get("risk_score", 0) or 0) for event in events), default=0)
            incoming = [(event, other) for event, role, other in items if role == "Receiver"]
            outgoing = [(event, other) for event, role, other in items if role == "Sender"]
            main_reasons = list(dict.fromkeys(reason for event in events for reason in _plain_reason_items(event.get("risk_reasons", "")) if reason.lower() != "no unusual pattern was found by the current checks"))
            saved_for_account = None
        else:
            rows = hist_df[hist_df["account_id"] == account_id].copy()
            unique_rows = rows.drop_duplicates(subset=["transaction_id"], keep="first") if "transaction_id" in rows else rows
            with get_connection() as conn:
                profile_row = conn.execute("""
                    SELECT a.entity_name, a.account_type, p.occupation, p.industry, p.employment_status, p.profile_source
                    FROM accounts a LEFT JOIN entity_profiles p ON p.account_id = a.account_id
                    WHERE a.account_id = ?
                """, (account_id,)).fetchone()
                profile = dict(profile_row) if profile_row else {}
                endpoint_ids = sorted({str(value) for value in unique_rows[["sender_id", "receiver_id"]].values.ravel() if pd.notna(value) and str(value).strip()}) if not unique_rows.empty else []
                endpoint_names = {}
                for offset in range(0, len(endpoint_ids), 350):
                    chunk = endpoint_ids[offset:offset + 350]
                    marks = ",".join("?" for _ in chunk)
                    for row in conn.execute(f"SELECT account_id, entity_name FROM accounts WHERE account_id IN ({marks})", chunk):
                        endpoint_names[str(row["account_id"])] = str(row["entity_name"] or row["account_id"])
            name = profile.get("entity_name") or account_id
            profession = profile.get("occupation") or ("Business owner / operator" if profile.get("account_type") == "business" else "Profession not listed")
            industry = profile.get("industry") or "Not listed"
            profile_source = "Fictional demo profile" if profile.get("profile_source") == "SYNTHETIC_DEMO" else "Profile not verified"
            score = int(pd.to_numeric(rows["risk_score"], errors="coerce").fillna(0).max()) if not rows.empty else 0
            incoming = [(dict(row), str(row.get("sender_id") or "")) for row in unique_rows.to_dict("records") if row.get("receiver_id") == account_id]
            outgoing = [(dict(row), str(row.get("receiver_id") or "")) for row in unique_rows.to_dict("records") if row.get("sender_id") == account_id]
            main_reasons = list(dict.fromkeys(reason for value in rows["reason"].dropna() for reason in _plain_reason_items(value))) if not rows.empty else []
            events = []
            saved_for_account = rows

        st.markdown(f"### {html.escape(str(name))}")
        st.caption(f"{account_id} · {profession} · {industry} · {profile_source}")
        metrics = st.columns(3)
        metrics[0].metric("Review score", f"{score}/100")
        metrics[1].metric("Flagged transfers", f"{len(events) if live_source else len(saved_for_account):,}")
        metrics[2].metric("Other accounts in these transfers", f"{len({other for _, other in incoming + outgoing if other and other != account_id}):,}")

        st.markdown("#### Why this account was flagged")
        if main_reasons:
            for reason in main_reasons[:6]:
                st.markdown(f'<div class="argus-detection-reason"><span class="argus-detection-reason-mark">•</span><span>{html.escape(reason)}</span></div>', unsafe_allow_html=True)
        else:
            st.info("No alert reason was saved for this account. The connected transfers below are shown as context only.")

        money_in, money_out = st.columns(2, gap="medium")
        with money_in:
            st.markdown("#### Money in")
            in_rows = [{
                "From account": endpoint_names.get(other, other) if not live_source else (account_profiles.get(other, {}).get("entity_name") or other),
                "Account ID": other,
                "Amount (INR)": event.get("Amount (INR)", event.get("amount", 0)),
                "Time": event.get("Timestamp", event.get("timestamp", "")),
            } for event, other in incoming]
            st.dataframe(pd.DataFrame(in_rows), use_container_width=True, hide_index=True)
        with money_out:
            st.markdown("#### Money out")
            out_rows = [{
                "To account": endpoint_names.get(other, other) if not live_source else (account_profiles.get(other, {}).get("entity_name") or other),
                "Account ID": other,
                "Amount (INR)": event.get("Amount (INR)", event.get("amount", 0)),
                "Time": event.get("Timestamp", event.get("timestamp", "")),
            } for event, other in outgoing]
            st.dataframe(pd.DataFrame(out_rows), use_container_width=True, hide_index=True)

        if live_source:
            with st.expander("More about these live transfers"):
                st.caption("Extra device, location, payment, and purpose fields are fictional demo values. Missing details mean no value was generated.")
                context_rows = [{
                    "Transaction ID": event.get("Transaction ID", ""),
                    "Device": event.get("demo_device_id", ""),
                    "Region": event.get("demo_origin_region", ""),
                    "Payment type": event.get("demo_payment_channel", ""),
                    "Purpose": event.get("demo_payment_purpose", ""),
                } for event in events]
                if context_rows:
                    st.dataframe(pd.DataFrame(context_rows), use_container_width=True, hide_index=True)
                notes = list(dict.fromkeys(str(event.get("entity_context", "")) for event in events if event.get("entity_context")))
                if notes:
                    st.markdown("**Account background / relationship notes**")
                    for note in notes[:5]:
                        st.write("• " + note)
                event_rows = [{
                    "Time": event.get("Timestamp", ""),
                    "From": event.get("Sender", ""),
                    "To": event.get("Receiver", ""),
                    "Amount (INR)": event.get("Amount (INR)", 0),
                    "Reason": _plain_alert_reason(event.get("risk_reasons", "")),
                } for event in events]
                st.dataframe(pd.DataFrame(event_rows), use_container_width=True, hide_index=True)
        else:
            with st.expander(f"Show all {len(saved_for_account):,} saved alert record(s)"):
                display_rows = saved_for_account.rename(columns={
                    "created_at": "Alert saved", "timestamp": "Transfer time",
                    "sender_id": "From account", "receiver_id": "To account",
                    "amount": "Amount (INR)", "risk_score": "Review score",
                    "reason": "Reason for review", "status": "Status",
                    "transaction_id": "Transaction ID",
                })
                columns = [name for name in ["Alert saved", "Transfer time", "Transaction ID", "From account", "To account", "Amount (INR)", "Review score", "Reason for review", "Status"] if name in display_rows]
                st.dataframe(display_rows[columns], use_container_width=True, hide_index=True)

    with live_tab:
        if live_groups.empty:
            st.info("No live simulation alerts in this session yet. Start Live Transfers to fill the account queue.")
        else:
            metrics = st.columns(3)
            metrics[0].metric("Accounts to review", f"{len(live_groups):,}")
            metrics[1].metric("Flagged transfers", f"{len(live_events):,}")
            metrics[2].metric("Highest review score", f"{int(live_groups['Review score'].max()):,}/100")
            queue_col, focus_col = st.columns([1.1, 1.9], gap="large")
            with queue_col:
                st.markdown("#### Account queue")
                st.caption("Select one account")
                queue_view = live_groups.rename(columns={"Account ID": "Account", "Account name": "Name", "Review score": "Score"})
                selection = st.dataframe(queue_view[["Account", "Name", "Score", "Transfers flagged"]], use_container_width=True, hide_index=True, height=500, on_select="rerun", selection_mode="single-row", key="detection_live_account_queue", column_config={"Score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%d")})
                selected_rows = selection.selection.rows
                selected_index = selected_rows[0] if selected_rows else 0
                selected_account = str(live_groups.iloc[selected_index]["Account ID"])
            with focus_col:
                render_account_focus(selected_account, live_source=True)

    with saved_tab:
        if hist_groups.empty:
            st.info("No saved historical alerts are available.")
        else:
            metrics = st.columns(3)
            metrics[0].metric("Accounts to review", f"{len(hist_groups):,}")
            metrics[1].metric("Saved alerts", f"{len(hist_df):,}")
            metrics[2].metric("Open alerts", f"{int(hist_groups['Open alerts'].sum()):,}")
            queue_col, focus_col = st.columns([1.1, 1.9], gap="large")
            with queue_col:
                st.markdown("#### Account queue")
                st.caption("Select one account")
                queue_view = hist_groups.rename(columns={"Account ID": "Account", "Review score": "Score"})
                selection = st.dataframe(queue_view[["Account", "Saved alerts", "Score", "Open alerts"]], use_container_width=True, hide_index=True, height=500, on_select="rerun", selection_mode="single-row", key="detection_saved_account_queue", column_config={"Score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%d")})
                selected_rows = selection.selection.rows
                selected_index = selected_rows[0] if selected_rows else 0
                selected_account = str(hist_groups.iloc[selected_index]["Account ID"])
            with focus_col:
                render_account_focus(selected_account, live_source=False)

    st.caption("Live alerts are held in session memory. Historical alerts and transfers are read from the existing database; this page does not change saved records.")

elif page == "Investigations":

    st.markdown("# Investigation walkthrough")
    st.caption("Follow one flagged account and see what ARGUS checked at each step.")
    st.markdown('<span class="argus-investigation-page-tag">FICTIONAL DEMO · REVIEW AID</span>', unsafe_allow_html=True)

    live_simulation_events = [dict(row) for row in st.session_state.get("argus_sim_events", [])]
    live_flagged_events = [dict(row) for row in st.session_state.get("argus_live_alerts", [])]
    live_candidate_ids = sorted({str(row.get(key)) for row in live_flagged_events for key in ("Sender", "Receiver") if row.get(key)})
    with get_connection() as conn:
        saved_candidates = pd.read_sql_query("""
            SELECT al.account_id, COUNT(*) AS alert_count, MAX(al.risk_score) AS peak_risk,
                   MAX(al.created_at) AS latest_alert
            FROM alerts AS al
            WHERE al.account_id IS NOT NULL AND TRIM(al.account_id) <> ''
            GROUP BY al.account_id
            ORDER BY peak_risk DESC, alert_count DESC, latest_alert DESC
            LIMIT 2000
        """, conn)
        saved_account_names = {}
        if not saved_candidates.empty:
            saved_ids = saved_candidates["account_id"].astype(str).tolist()
            for offset in range(0, len(saved_ids), 400):
                group = saved_ids[offset:offset + 400]
                marks = ",".join("?" for _ in group)
                for row in conn.execute(f"SELECT account_id, entity_name FROM accounts WHERE account_id IN ({marks})", group):
                    saved_account_names[str(row["account_id"])] = str(row["entity_name"] or row["account_id"])
        live_account_names = {}
        if live_candidate_ids:
            for offset in range(0, len(live_candidate_ids), 400):
                group = live_candidate_ids[offset:offset + 400]
                marks = ",".join("?" for _ in group)
                for row in conn.execute(f"SELECT account_id, entity_name FROM accounts WHERE account_id IN ({marks})", group):
                    live_account_names[str(row["account_id"])] = str(row["entity_name"] or row["account_id"])

    live_tab, saved_tab = st.tabs(["Live simulation", "Saved historical alerts"])

    def render_investigation(source_label, candidate_ids, name_lookup, live_source=False):
        if not candidate_ids:
            st.info("No flagged accounts are available in this source. Run Live Transfers to create current-session alerts, or use the saved historical tab when historical alerts exist.")
            return
        id_to_label = {str(account_id): f"{name_lookup.get(str(account_id), str(account_id))} · {account_id}" for account_id in candidate_ids}
        selected_account = st.selectbox("Choose the account to review", list(candidate_ids), format_func=lambda value: id_to_label.get(str(value), str(value)), key=f"investigation_{source_label.lower().replace(' ', '_')}_account")
        branch_depth = st.select_slider("How far should ARGUS follow transfers?", options=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10], value=5, key=f"investigation_{source_label.lower().replace(' ', '_')}_depth", help="ARGUS follows money in both directions from the selected account, then checks transfers around each connected account. The search has safety limits to keep the app responsive.")
        max_investigation_links = 5000
        max_investigation_accounts = 5000
        investigation_limit_reached = False
        if live_source:
            days_back = 0
            st.caption("Live review uses transfers currently held in this simulation session (up to the latest 5,000 events).")
        else:
            days_back = st.selectbox("Which saved transfer period?", [30, 90, 365, 0], format_func=lambda value: "All available history" if value == 0 else f"Last {value} days", index=1, key=f"investigation_{source_label.lower().replace(' ', '_')}_period")

        alert_rows = []
        if live_source:
            alert_rows = [row for row in live_flagged_events if str(row.get("Sender", "")) == str(selected_account) or str(row.get("Receiver", "")) == str(selected_account)]
        else:
            with get_connection() as conn:
                alert_rows = [dict(row) for row in conn.execute("""
                    SELECT al.alert_id, al.transaction_id, al.account_id, al.risk_score, al.reason, al.status, al.created_at,
                           t.sender_id AS Sender, t.receiver_id AS Receiver, t.amount AS Amount,
                           t.timestamp AS Timestamp, t.description AS Description,
                           dc.device_id AS demo_device_id, dc.origin_region AS demo_origin_region,
                           dc.payment_channel AS demo_payment_channel, dc.payment_purpose AS demo_payment_purpose,
                           dc.merchant_category AS demo_merchant_category, dc.invoice_reference AS demo_invoice_reference
                    FROM alerts AS al LEFT JOIN transactions AS t ON t.transaction_id = al.transaction_id
                    LEFT JOIN demo_transaction_context AS dc ON dc.transaction_id = al.transaction_id
                    WHERE al.account_id = ? ORDER BY al.created_at DESC
                """, (str(selected_account),))]
            if days_back:
                from datetime import timedelta
                alert_cutoff = (datetime.now() - timedelta(days=int(days_back))).strftime("%Y-%m-%d %H:%M:%S")
                alert_rows = [row for row in alert_rows if str(row.get("Timestamp") or "") >= alert_cutoff]

        edges_by_id = {}
        alert_rows_by_pair = {}
        for row in alert_rows:
            event_id = str(row.get("Transaction ID") or row.get("transaction_id") or f"{row.get('Timestamp') or row.get('timestamp')}|{row.get('Sender') or row.get('sender_id')}|{row.get('Receiver') or row.get('receiver_id')}|{row.get('Amount (INR)') or row.get('Amount') or row.get('amount')}")
            if live_source:
                edges_by_id[event_id] = row
            else:
                sender = str(row.get("Sender") or "")
                receiver = str(row.get("Receiver") or "")
                if sender and receiver:
                    pair_key = f"{sender}|{receiver}"
                    existing = alert_rows_by_pair.get(pair_key, {})
                    reasons = [part for part in (existing.get("reasons", ""), row.get("reason", "")) if part]
                    alert_rows_by_pair[pair_key] = {
                        "alert_count": int(existing.get("alert_count", 0)) + 1,
                        "peak_risk": max(float(existing.get("peak_risk", 0) or 0), float(row.get("risk_score", 0) or 0)),
                        "reasons": " · ".join(dict.fromkeys(reasons)),
                    }

        frontier = {str(selected_account)}
        visited = set(frontier)
        account_depth = {str(selected_account): 0}
        if live_source:
            event_pool = live_simulation_events
            for depth in range(branch_depth):
                if len(edges_by_id) >= max_investigation_links or len(visited) >= max_investigation_accounts:
                    investigation_limit_reached = True
                    break
                next_frontier = set()
                for row in event_pool:
                    sender = str(row.get("Sender", "") or "")
                    receiver = str(row.get("Receiver", "") or "")
                    if sender in frontier or receiver in frontier:
                        key = str(row.get("Transaction ID") or f"{row.get('Timestamp')}|{sender}|{receiver}|{row.get('Amount (INR)', row.get('Amount', 0))}")
                        if key not in edges_by_id and len(edges_by_id) >= max_investigation_links:
                            investigation_limit_reached = True
                            break
                        edges_by_id[key] = row
                        if sender and sender not in visited:
                            if len(visited) + len(next_frontier) < max_investigation_accounts:
                                next_frontier.add(sender)
                            else:
                                investigation_limit_reached = True
                        if receiver and receiver not in visited:
                            if len(visited) + len(next_frontier) < max_investigation_accounts:
                                next_frontier.add(receiver)
                            else:
                                investigation_limit_reached = True
                visited.update(next_frontier)
                for account_id in next_frontier:
                    account_depth.setdefault(account_id, depth + 1)
                frontier = next_frontier
                if not frontier:
                    break
        else:
            from datetime import timedelta
            cutoff = (datetime.now() - timedelta(days=int(days_back))).strftime("%Y-%m-%d %H:%M:%S") if days_back else None
            with get_connection() as conn:
                for _depth in range(branch_depth):
                    remaining_links = max_investigation_links - len(edges_by_id)
                    if remaining_links <= 0 or len(visited) >= max_investigation_accounts:
                        investigation_limit_reached = True
                        break
                    next_frontier = set()
                    frontier_list = sorted(frontier)
                    for offset in range(0, len(frontier_list), 350):
                        group = frontier_list[offset:offset + 350]
                        marks = ",".join("?" for _ in group)
                        date_clause = " AND timestamp >= ?" if cutoff else ""
                        params = group + ([cutoff] if cutoff else [])
                        remaining_links = max_investigation_links - len(edges_by_id)
                        if remaining_links <= 0:
                            investigation_limit_reached = True
                            break
                        sql = f"""
                            SELECT sender_id AS Sender, receiver_id AS Receiver,
                                   COUNT(*) AS transfer_count, SUM(amount) AS total_amount,
                                   MIN(timestamp) AS first_seen, MAX(timestamp) AS last_seen,
                                   SUM(CASE WHEN EXISTS(
                                       SELECT 1 FROM alerts al WHERE al.transaction_id = transactions.transaction_id
                                   ) THEN 1 ELSE 0 END) AS alert_count,
                                   MAX((SELECT MAX(al.risk_score) FROM alerts al
                                        WHERE al.transaction_id = transactions.transaction_id)) AS peak_alert_score
                            FROM transactions
                            WHERE (sender_id IN ({marks}) OR receiver_id IN ({marks})){date_clause}
                            GROUP BY sender_id, receiver_id
                            ORDER BY MAX(timestamp) DESC
                            LIMIT ?
                        """
                        grouped_params = group + group + ([cutoff] if cutoff else [])
                        for row in conn.execute(sql, grouped_params + [remaining_links]):
                            item = dict(row)
                            pair_key = f"{item['Sender']}|{item['Receiver']}"
                            if pair_key not in edges_by_id and len(edges_by_id) >= max_investigation_links:
                                investigation_limit_reached = True
                                break
                            edges_by_id.setdefault(pair_key, item)
                            for endpoint in (str(item.get("Sender") or ""), str(item.get("Receiver") or "")):
                                if endpoint and endpoint not in visited:
                                    if len(visited) + len(next_frontier) < max_investigation_accounts:
                                        next_frontier.add(endpoint)
                                    else:
                                        investigation_limit_reached = True
                    visited.update(next_frontier)
                    for account_id in next_frontier:
                        account_depth.setdefault(account_id, _depth + 1)
                    frontier = next_frontier
                    if not frontier:
                        break

                # Include saved alert reasons for every transfer in the examined
                # network, not only alerts attached to the selected root account.
                for start in range(0, len(account_ids := sorted(visited)), 350):
                    group = account_ids[start:start + 350]
                    marks = ",".join("?" for _ in group)
                    for row in conn.execute(f"""
                        SELECT t.sender_id AS Sender, t.receiver_id AS Receiver,
                               COUNT(*) AS alert_count, MAX(a.risk_score) AS peak_risk,
                               GROUP_CONCAT(DISTINCT a.reason) AS reasons
                        FROM alerts a JOIN transactions t ON t.transaction_id = a.transaction_id
                        WHERE (t.sender_id IN ({marks}) OR t.receiver_id IN ({marks}))
                        {"AND t.timestamp >= ?" if cutoff else ""}
                        GROUP BY t.sender_id, t.receiver_id
                    """, group + group + ([cutoff] if cutoff else [])):
                        pair_key = f"{row['Sender']}|{row['Receiver']}"
                        existing = alert_rows_by_pair.get(pair_key, {})
                        alert_rows_by_pair[pair_key] = {
                            "alert_count": max(int(existing.get("alert_count", 0)), int(row["alert_count"] or 0)),
                            "peak_risk": max(float(existing.get("peak_risk", 0) or 0), float(row["peak_risk"] or 0)),
                            "reasons": " · ".join(dict.fromkeys(part for part in (existing.get("reasons", ""), row["reasons"] or "") if part)),
                        }

        normalized_edges = []
        for key, row in edges_by_id.items():
            sender = str(row.get("Sender") or row.get("sender_id") or "")
            receiver = str(row.get("Receiver") or row.get("receiver_id") or "")
            amount = row.get("Amount (INR)", row.get("total_amount", row.get("Amount", row.get("amount", 0))))
            timestamp = row.get("Timestamp", row.get("last_seen", row.get("timestamp", "")))
            reasons = row.get("risk_reasons", row.get("reason", ""))
            patterns = row.get("risk_patterns", [])
            pair_key = f"{sender}|{receiver}"
            alert_record = alert_rows_by_pair.get(pair_key, {})
            if not reasons and alert_record:
                reasons = alert_record.get("reasons", "")
            if isinstance(reasons, list):
                reasons = "; ".join(str(value) for value in reasons)
            normalized_edges.append({
                "Transaction ID": str(row.get("Transaction ID") or row.get("transaction_id") or key),
                "From account": sender, "To account": receiver,
                "Amount (INR)": float(amount or 0), "Time": str(timestamp or ""),
                "Transfers": int(row.get("transfer_count", 1) or 1),
                "First seen": row.get("first_seen", timestamp),
                "Alert count": int(row.get("alert_count", alert_record.get("alert_count", 0)) or 0) if not live_source else int(bool(row.get("risk_score", 0) and int(row.get("risk_score", 0) or 0) >= int(row.get("alert_threshold", ALERT_THRESHOLD)))),
                "Alert linked": bool(alert_record.get("alert_count", 0) or (row.get("risk_score", 0) and int(row.get("risk_score", 0) or 0) >= int(row.get("alert_threshold", ALERT_THRESHOLD)))),
                "Risk score": row.get("risk_score", row.get("peak_alert_score", alert_record.get("peak_risk", ""))),
                "Why flagged": _plain_alert_reason(str(reasons or "")),
                "Pattern details": _plain_alert_reason(" · ".join(str(item.get("pattern", "")) + (": " + str(item.get("evidence", "")) if item.get("evidence") else "") for item in patterns if isinstance(item, dict))) if isinstance(patterns, list) else "",
                "Transfer note": str(row.get("Description") or row.get("description") or ""),
                "Demo device": str(row.get("demo_device_id") or ""),
                "Demo origin": str(row.get("demo_origin_region") or ""),
                "Demo channel": str(row.get("demo_payment_channel") or ""),
                "Demo purpose": str(row.get("demo_payment_purpose") or ""),
                "Demo merchant category": str(row.get("demo_merchant_category") or ""),
                "Demo invoice": str(row.get("demo_invoice_reference") or ""),
            })
        transfer_df = pd.DataFrame(normalized_edges)
        account_ids = sorted(visited | {str(selected_account)})
        edge_total = len(transfer_df)
        if edge_total:
            total_examined_transfers = int(transfer_df["Transfers"].sum())
            if investigation_limit_reached:
                st.warning(f"This review reached its limit of {max_investigation_links:,} transfer links or {max_investigation_accounts:,} accounts. More connected transfers may remain beyond the results shown.")
        else:
            st.info("No transfers were found for this account within the selected source and period.")
        if not live_source and alert_rows:
            st.caption(f"Step 1 · {len(alert_rows):,} saved alert record(s) are linked to this account. Existing database records are read only.")
        if not live_source and not alert_rows:
            st.caption("This account is in the saved-alert list, but its alert record is not linked to a transaction row. Its surrounding transfers are still examined.")
        alert_context_rows = [{
            "Transaction ID": row.get("Transaction ID", row.get("transaction_id", "")),
            "Device": row.get("demo_device_id", ""),
            "Origin region": row.get("demo_origin_region", ""),
            "Payment channel": row.get("demo_payment_channel", ""),
            "Purpose": row.get("demo_payment_purpose", ""),
            "Merchant category": row.get("demo_merchant_category", ""),
            "Invoice reference": row.get("demo_invoice_reference", ""),
        } for row in alert_rows]
        if alert_context_rows:
            with st.expander("See extra details attached to the selected account's alert transfers"):
                st.caption("Device, location, payment type, purpose, and invoice details are fictional demo values, not verified facts.")
                st.dataframe(pd.DataFrame(alert_context_rows), use_container_width=True, hide_index=True)

        if not transfer_df.empty:
            transfer_df["Amount (INR)"] = pd.to_numeric(transfer_df["Amount (INR)"], errors="coerce").fillna(0)

        profiles = {}
        demo_account_contexts = {}
        relationship_rows = []
        case_rows = []
        with get_connection() as conn:
            for offset in range(0, len(account_ids), 350):
                group = account_ids[offset:offset + 350]
                marks = ",".join("?" for _ in group)
                for row in conn.execute(f"""
                    SELECT a.account_id, a.entity_name, a.account_type, a.created_at, a.opening_balance, a.balance,
                           p.entity_kind, p.occupation, p.employment_status, p.industry,
                           p.employer_account_id, p.employer_name, p.business_name,
                           p.business_registration_status, p.city, p.years_active,
                           p.income_or_turnover_band, p.profile_source
                    FROM accounts a LEFT JOIN entity_profiles p ON p.account_id = a.account_id
                    WHERE a.account_id IN ({marks})
                """, group):
                    profiles[str(row["account_id"])] = dict(row)
                for row in conn.execute(f"""
                    SELECT account_id, usual_region, primary_device_id, context_source
                    FROM demo_account_context WHERE account_id IN ({marks})
                """, group):
                    demo_account_contexts[str(row["account_id"])] = dict(row)
                rel_sql = f"""
                    SELECT r.account_id, a.entity_name AS account_name, r.related_account_id,
                           b.entity_name AS related_name, r.relationship_type, r.start_year,
                           r.relationship_source, r.evidence_note
                    FROM entity_relationships r
                    LEFT JOIN accounts a ON a.account_id = r.account_id
                    LEFT JOIN accounts b ON b.account_id = r.related_account_id
                    WHERE r.account_id IN ({marks}) OR r.related_account_id IN ({marks})
                """
                for row in conn.execute(rel_sql, group + group):
                    relationship_rows.append(dict(row))
                for row in conn.execute(f"""
                    SELECT account_id, case_category, allegation, case_status, court_or_authority,
                           reference_number, filed_date, outcome_date, outcome_summary, record_source
                    FROM entity_case_records WHERE account_id IN ({marks}) ORDER BY filed_date DESC
                """, group):
                    case_rows.append(dict(row))

            # Continue one further layer through declared employer/ownership links.
            # These entities are context only; they are not added to the transfer graph.
            transfer_account_set = set(account_ids)
            entity_seen = set(transfer_account_set)
            entity_frontier = {
                str(row.get("related_account_id"))
                for row in relationship_rows
                if row.get("related_account_id") and str(row.get("related_account_id")) not in entity_seen
            } | {
                str(row.get("account_id"))
                for row in relationship_rows
                if row.get("account_id") and str(row.get("account_id")) not in entity_seen
            }
            relationship_keys = {
                (str(row.get("account_id")), str(row.get("related_account_id")), str(row.get("relationship_type")))
                for row in relationship_rows
            }
            for _entity_depth in range(2):
                current_entities = sorted(entity_frontier - entity_seen)
                if not current_entities:
                    break
                entity_seen.update(current_entities)
                next_entities = set()
                for offset in range(0, len(current_entities), 350):
                    group = current_entities[offset:offset + 350]
                    marks = ",".join("?" for _ in group)
                    for row in conn.execute(f"""
                        SELECT a.account_id, a.entity_name, a.account_type, a.created_at, a.opening_balance, a.balance,
                               p.entity_kind, p.occupation, p.employment_status, p.industry,
                               p.employer_account_id, p.employer_name, p.business_name,
                               p.business_registration_status, p.city, p.years_active,
                               p.income_or_turnover_band, p.profile_source
                        FROM accounts a LEFT JOIN entity_profiles p ON p.account_id = a.account_id
                        WHERE a.account_id IN ({marks})
                    """, group):
                        profiles[str(row["account_id"])] = dict(row)
                    for row in conn.execute(f"""
                        SELECT account_id, case_category, allegation, case_status, court_or_authority,
                               reference_number, filed_date, outcome_date, outcome_summary, record_source
                        FROM entity_case_records WHERE account_id IN ({marks}) ORDER BY filed_date DESC
                    """, group):
                        case_rows.append(dict(row))
                    rel_sql = f"""
                        SELECT r.account_id, a.entity_name AS account_name, r.related_account_id,
                               b.entity_name AS related_name, r.relationship_type, r.start_year,
                               r.relationship_source, r.evidence_note
                        FROM entity_relationships r
                        LEFT JOIN accounts a ON a.account_id = r.account_id
                        LEFT JOIN accounts b ON b.account_id = r.related_account_id
                        WHERE r.account_id IN ({marks}) OR r.related_account_id IN ({marks})
                    """
                    for row in conn.execute(rel_sql, group + group):
                        item = dict(row)
                        key = (str(item.get("account_id")), str(item.get("related_account_id")), str(item.get("relationship_type")))
                        if key not in relationship_keys:
                            relationship_keys.add(key)
                            relationship_rows.append(item)
                        for endpoint in (str(item.get("account_id") or ""), str(item.get("related_account_id") or "")):
                            if endpoint and endpoint not in entity_seen:
                                next_entities.add(endpoint)
                entity_frontier = next_entities

        report_account_ids = set(entity_seen)
        report_account_ids.update(profiles)
        report_account_ids.update(str(row.get("account_id")) for row in case_rows if row.get("account_id"))

        st.markdown("#### Background and account connections")
        st.caption("ARGUS follows money transfers separately from listed work or ownership links. It checks the profiles and fictional case details available for connected accounts, including links up to two steps away.")
        account_summary = []
        for account_id in sorted(report_account_ids):
            profile = profiles.get(account_id, {})
            related_transfers = transfer_df[(transfer_df["From account"] == account_id) | (transfer_df["To account"] == account_id)] if not transfer_df.empty and account_id in account_ids else pd.DataFrame()
            incoming_rows = related_transfers[related_transfers["To account"] == account_id] if not related_transfers.empty else pd.DataFrame()
            outgoing_rows = related_transfers[related_transfers["From account"] == account_id] if not related_transfers.empty else pd.DataFrame()
            account_summary.append({
                "Account": account_id, "Name": profile.get("entity_name", "Not available"),
                "Role / occupation": profile.get("occupation") or profile.get("entity_kind") or "Not listed",
                "Industry": profile.get("industry") or "Not listed",
                "Usual demo region": demo_account_contexts.get(account_id, {}).get("usual_region") or "Not available",
                "Usual demo device": demo_account_contexts.get(account_id, {}).get("primary_device_id") or "Not available",
                "How it is connected": f"Transfer step {account_depth[account_id]}" if account_id in account_depth else "Listed work/ownership link",
                "Incoming transfers": int(incoming_rows["Transfers"].sum()) if not incoming_rows.empty else 0,
                "Incoming value (INR)": incoming_rows["Amount (INR)"].sum() if not incoming_rows.empty else 0,
                "Outgoing transfers": int(outgoing_rows["Transfers"].sum()) if not outgoing_rows.empty else 0,
                "Outgoing value (INR)": outgoing_rows["Amount (INR)"].sum() if not outgoing_rows.empty else 0,
                "Flagged transfers": int(related_transfers["Alert count"].sum()) if not related_transfers.empty else 0,
                "Fictional case records": sum(1 for item in case_rows if str(item.get("account_id")) == account_id),
            })
        summary_df = pd.DataFrame(account_summary)
        if summary_df.empty:
            st.info("Account profiles were not returned for this branch.")
            return

        suspect_profile = profiles.get(str(selected_account), {})
        suspect_name = suspect_profile.get("entity_name") or str(selected_account)
        suspect_role = suspect_profile.get("occupation") or suspect_profile.get("entity_kind") or "no role listed"
        suspect_industry = suspect_profile.get("industry") or "no industry listed"
        suspect_source = "fictional demo profile" if suspect_profile.get("profile_source") == "SYNTHETIC_DEMO" else "no profile available in ARGUS"
        suspect_summary = summary_df[summary_df["Account"] == str(selected_account)].iloc[0]
        suspect_alert_reasons = list(dict.fromkeys(reason for row in normalized_edges if row.get("Alert linked") and str(selected_account) in (str(row.get("From account")), str(row.get("To account"))) for reason in _plain_reason_items(row.get("Why flagged", ""))))
        suspect_direct_contacts = set()
        if not transfer_df.empty:
            for _, flow in transfer_df.iterrows():
                if str(flow["From account"]) == str(selected_account):
                    suspect_direct_contacts.add(str(flow["To account"]))
                elif str(flow["To account"]) == str(selected_account):
                    suspect_direct_contacts.add(str(flow["From account"]))
        shared_contact_set = set()
        if not transfer_df.empty:
            for connected_id in account_ids:
                if connected_id == str(selected_account):
                    continue
                connected_rows = transfer_df[(transfer_df["From account"] == connected_id) | (transfer_df["To account"] == connected_id)]
                connected_contacts = set(connected_rows["From account"].astype(str)) | set(connected_rows["To account"].astype(str))
                connected_contacts.discard(connected_id)
                shared_contact_set.update(connected_contacts & suspect_direct_contacts - {str(selected_account), connected_id})
        scope_relationships = [row for row in relationship_rows if str(row.get("account_id")) in account_ids or str(row.get("related_account_id")) in account_ids]
        inbound = transfer_df[transfer_df["To account"] == str(selected_account)] if not transfer_df.empty else pd.DataFrame()
        outbound = transfer_df[transfer_df["From account"] == str(selected_account)] if not transfer_df.empty else pd.DataFrame()
        review_score = max([float(row.get("risk_score", 0) or 0) for row in alert_rows] + [float(row.get("Risk score", 0) or 0) for row in normalized_edges if str(selected_account) in (str(row.get("From account")), str(row.get("To account")))]) if (alert_rows or normalized_edges) else 0
        review_badge = "REVIEW" if suspect_alert_reasons or alert_rows else "CONTEXT"
        initials = "".join(part[0] for part in str(suspect_name).split()[:2]).upper() or "AC"
        focus_title = f"{suspect_name} · {selected_account}"
        st.markdown(f'<div class="argus-investigation-focus"><span class="argus-investigation-avatar">{html.escape(initials)}</span><span><strong>{html.escape(str(suspect_name))}</strong><small>{html.escape(str(selected_account))} · {html.escape(str(suspect_role))} · {html.escape(str(suspect_industry))} · {html.escape(suspect_source)}</small></span><span class="argus-investigation-badge">{review_badge} · {review_score:.0f} / 100</span></div>', unsafe_allow_html=True)
        metrics = st.columns(3)
        metrics[0].metric("Transfer steps checked", f"{int(branch_depth)}", "Incoming and outgoing")
        metrics[1].metric("Connected accounts checked", f"{max(0, len(account_ids) - 1):,}", "Each account counted once")
        metrics[2].metric("Shared contacts found", f"{len(shared_contact_set):,}", "Contacts linked to both")
        left_col, right_col = st.columns([1.55, .9], gap="large")
        with left_col:
            st.markdown('<div class="argus-investigation-panel-title"><strong>What ARGUS checked</strong><span>In order</span></div>', unsafe_allow_html=True)
            if suspect_alert_reasons:
                step_one_reason = "; ".join(suspect_alert_reasons[:5])
            elif live_source and alert_rows:
                step_one_reason = "; ".join(dict.fromkeys(reason for row in alert_rows for reason in _plain_reason_items(row.get("risk_reasons", ""))))
            else:
                step_one_reason = "The saved alert has no reason text. Connected transfers are shown as context, not automatically treated as suspicious."
            st.markdown(f'<div class="argus-investigation-step"><span class="argus-investigation-number">1</span><span><strong>Reviewed the selected account</strong><small>Checked its alert reasons, flagged transfers, and available profession and industry details. {html.escape(step_one_reason)}</small></span></div>', unsafe_allow_html=True)
            st.markdown(f'<div class="argus-investigation-step"><span class="argus-investigation-number">2</span><span><strong>Followed incoming and outgoing transfers</strong><small>Received ₹{suspect_summary["Incoming value (INR)"]:,.2f} across {int(suspect_summary["Incoming transfers"]):,} transfer(s); sent ₹{suspect_summary["Outgoing value (INR)"]:,.2f} across {int(suspect_summary["Outgoing transfers"]):,} transfer(s).</small></span></div>', unsafe_allow_html=True)
            st.markdown(f'<div class="argus-investigation-step"><span class="argus-investigation-number">3</span><span><strong>Checked the connected accounts</strong><small>Followed up to {int(branch_depth)} transfer step(s), checking {max(0, len(account_ids) - 1):,} connected account(s) once.</small></span></div>', unsafe_allow_html=True)
            st.markdown(f'<div class="argus-investigation-step"><span class="argus-investigation-number">4</span><span><strong>Compared shared contacts and listed links</strong><small>Found {len(suspect_direct_contacts):,} direct transfer contact(s) and {len(scope_relationships):,} listed work or ownership relationship record(s).</small></span></div>', unsafe_allow_html=True)
            st.markdown(f'<div class="argus-investigation-step"><span class="argus-investigation-number">5</span><span><strong>Read available background</strong><small>Reviewed {len(profiles):,} available profiles and {len(case_rows):,} fictional case record(s). Case records do not change the transfer score.</small></span></div>', unsafe_allow_html=True)

            st.markdown('<div class="argus-investigation-panel-title"><strong>Transfer path examples</strong><span>Money direction shown</span></div>', unsafe_allow_html=True)
            path_rows = []
            if not inbound.empty:
                path_rows.extend(("in", row) for _, row in inbound.sort_values("Amount (INR)", ascending=False).head(2).iterrows())
            if not outbound.empty:
                path_rows.extend(("out", row) for _, row in outbound.sort_values("Amount (INR)", ascending=False).head(2).iterrows())
            if path_rows:
                for direction, flow in path_rows:
                    if direction == "in":
                        peer_id, arrow, desc = str(flow["From account"]), "→", "sent to focus account"
                        line = f'<div class="argus-investigation-flow-node">{html.escape(peer_id)}<small>Sender</small></div><b class="argus-investigation-flow-arrow">→</b><div class="argus-investigation-flow-node is-focus">{html.escape(str(suspect_name))}<small>Selected account · received</small></div>'
                    else:
                        peer_id, arrow, desc = str(flow["To account"]), "→", "sent by focus account"
                        line = f'<div class="argus-investigation-flow-node is-focus">{html.escape(str(suspect_name))}<small>Selected account · sent</small></div><b class="argus-investigation-flow-arrow">→</b><div class="argus-investigation-flow-node">{html.escape(peer_id)}<small>Recipient</small></div>'
                    label = "Incoming transfer" if direction == "in" else "Outgoing transfer"
                    st.markdown(f'<div class="argus-investigation-flow"><div class="argus-investigation-flow-head"><strong>{label}</strong><span>₹{float(flow["Amount (INR)"]):,.2f} · {int(flow["Transfers"]):,} transfer(s)</span></div><div class="argus-investigation-flow-line">{line}</div></div>', unsafe_allow_html=True)
            else:
                st.info("No incoming or outgoing transfer paths are available in this period.")
            st.caption("Each line shows a direct transfer to or from the selected account. Lines are not combined into a sequence unless the underlying records show that sequence.")

            st.markdown('<div class="argus-investigation-panel-title"><strong>Accounts reached at each step</strong><span>Transfer branch</span></div>', unsafe_allow_html=True)
            for step in range(1, int(branch_depth) + 1):
                newly_reached = [account for account, distance in account_depth.items() if distance == step]
                if not newly_reached:
                    continue
                step_links = transfer_df[(transfer_df["From account"].isin(newly_reached)) | (transfer_df["To account"].isin(newly_reached))] if not transfer_df.empty else pd.DataFrame()
                step_value = float(step_links["Amount (INR)"].sum()) if not step_links.empty else 0.0
                step_transfers = int(step_links["Transfers"].sum()) if not step_links.empty else 0
                st.markdown(f'<div class="argus-investigation-branch"><strong>Step {step}</strong><span>{len(newly_reached):,} new account(s) · {step_transfers:,} transfer(s) · ₹{step_value:,.2f} total value</span></div>', unsafe_allow_html=True)
            st.caption("Branch totals describe the selected time period. Listed work or ownership links are generated demo information, not verified facts.")

        with right_col:
            st.markdown('<div class="argus-investigation-panel-title"><strong>What the result means</strong><span class="argus-investigation-review-tag">Review aid</span></div>', unsafe_allow_html=True)
            if suspect_alert_reasons:
                summary_reason = "; ".join(suspect_alert_reasons[:2])
            else:
                summary_reason = "No alert reason was attached to a direct transfer in this selection. Connected activity is context for review."
            st.markdown(f'<div class="argus-investigation-plain">{html.escape(summary_reason)} The listed occupation may help explain activity, but this demo profile has not been independently verified.</div>', unsafe_allow_html=True)
            st.markdown(f'<div class="argus-investigation-kv"><span>Alert reason</span><b>{html.escape(suspect_alert_reasons[0] if suspect_alert_reasons else "No direct alert reason")}</b></div>', unsafe_allow_html=True)
            st.markdown(f'<div class="argus-investigation-kv"><span>Review score</span><b>{review_score:.0f} / 100</b></div>', unsafe_allow_html=True)
            st.markdown('<div class="argus-investigation-kv"><span>Outside records checked</span><b>No</b></div>', unsafe_allow_html=True)
            st.markdown('<div class="argus-investigation-panel-title argus-investigation-side-title"><strong>Connected account checks</strong><span>Automatically reviewed</span></div>', unsafe_allow_html=True)
            # A concise sample sits in the focus panel; full account-by-account
            # background and shared-contact checks remain available below.
            preview_ids = sorted((report_account_ids - {str(selected_account)}), key=lambda account: (account_depth.get(account, 999), account))[:2]
            for preview_id in preview_ids:
                preview = profiles.get(preview_id, {})
                preview_name = preview.get("entity_name") or preview_id
                preview_role = preview.get("occupation") or preview.get("entity_kind") or "Profession not listed"
                preview_shared = f"transfer step {account_depth[preview_id]}" if preview_id in account_depth else "listed relationship link"
                st.markdown(f'<div class="argus-investigation-preview"><strong>{html.escape(str(preview_id))} · {html.escape(str(preview_name))}</strong><small>{html.escape(str(preview_role))} · {preview_shared} · checked through available records</small></div>', unsafe_allow_html=True)
            if len(report_account_ids) - 1 > len(preview_ids):
                st.markdown(f'<div class="argus-investigation-preview"><strong>{len(report_account_ids) - 1 - len(preview_ids):,} more connected account(s)</strong><small>Transfers and available background records checked below.</small></div>', unsafe_allow_html=True)
            st.markdown('<div class="argus-investigation-notice">Demo people, relationships, and case records are fictional. Missing information is unknown, not proof of clearance.</div>', unsafe_allow_html=True)

        st.markdown("#### Step 2 · Transfer activity")
        if not transfer_df.empty:
            link_view = transfer_df.copy()
            link_view["Flow size (INR)"] = link_view["Amount (INR)"]
            link_view = link_view.sort_values(["Alert linked", "Flow size (INR)"], ascending=[False, False])
            key_links = link_view[link_view["Alert linked"]].head(20)
            if key_links.empty:
                key_links = link_view.head(20)
                st.caption("No saved alert was found on a transfer in this branch. The table shows its largest observed account-to-account links for context.")
            simple_link_columns = {"From account": "From account", "To account": "To account", "Transfers": "Number of transfers", "Flow size (INR)": "Total value (INR)", "Alert count": "Transfers flagged", "Risk score": "Review score", "Why flagged": "Reason for review", "Time": "Latest transfer"}
            with st.expander(f"Review {len(key_links):,} key transfer links", expanded=False):
                st.dataframe(key_links[list(simple_link_columns)].rename(columns=simple_link_columns), use_container_width=True, hide_index=True)
            if len(link_view) > len(key_links):
                with st.expander(f"Show all {len(link_view):,} connected account-to-account links"):
                    st.dataframe(link_view[list(simple_link_columns)].rename(columns=simple_link_columns), use_container_width=True, hide_index=True)

        # Keep the selected alert account as the sole investigation focus. Every other
        # account reached through its transfer branches is checked automatically below.
        st.markdown("#### Step 4 · Accounts connected to the focus account")
        st.caption("The selected account remains the focus. ARGUS checks each connected account once, then compares available background, transfer contacts, listed work or ownership links, and fictional case details.")

        suspect_id = str(selected_account)
        suspect_contacts = set()
        if not transfer_df.empty:
            for _, transfer in transfer_df.iterrows():
                left, right = str(transfer["From account"]), str(transfer["To account"])
                if left == suspect_id:
                    suspect_contacts.add(right)
                elif right == suspect_id:
                    suspect_contacts.add(left)

        associate_ids = sorted(report_account_ids, key=lambda account: (account_depth.get(account, 999), account))
        associate_ids = [account for account in associate_ids if account != suspect_id]
        associate_checks = []
        associate_details = {}
        for associate_id in associate_ids:
            profile = profiles.get(associate_id, {})
            associated_transfers = transfer_df[(transfer_df["From account"] == associate_id) | (transfer_df["To account"] == associate_id)].copy() if not transfer_df.empty else pd.DataFrame()
            own_contacts = set()
            if not associated_transfers.empty:
                own_contacts = set(associated_transfers["From account"].astype(str)) | set(associated_transfers["To account"].astype(str))
                own_contacts.discard(associate_id)
            shared_contacts = sorted((own_contacts & suspect_contacts) - {suspect_id, associate_id})
            linked_relationships = [row for row in relationship_rows if associate_id in (str(row.get("account_id") or ""), str(row.get("related_account_id") or ""))]
            associate_cases = [row for row in case_rows if str(row.get("account_id")) == associate_id]
            incoming = associated_transfers[associated_transfers["To account"] == associate_id] if not associated_transfers.empty else pd.DataFrame()
            outgoing = associated_transfers[associated_transfers["From account"] == associate_id] if not associated_transfers.empty else pd.DataFrame()
            display_name = profile.get("entity_name") or associate_id
            occupation = profile.get("occupation") or profile.get("entity_kind") or "Not listed"
            industry = profile.get("industry") or "Not listed"
            associate_checks.append({
                "Associated account": associate_id, "Name": display_name, "Role": occupation, "Industry": industry,
                "Location": profile.get("city") or "Not listed", "Connection": f"Transfer branch · step {account_depth[associate_id]}" if associate_id in account_depth else "Declared entity link only",
                "Direct contacts shared with focus account": len(shared_contacts),
                "Incoming (INR)": float(incoming["Amount (INR)"].sum()) if not incoming.empty else 0.0,
                "Outgoing (INR)": float(outgoing["Amount (INR)"].sum()) if not outgoing.empty else 0.0,
                "Declared entity links": len(linked_relationships), "Fictional case records": len(associate_cases),
            })
            associate_details[associate_id] = (profile, associated_transfers, shared_contacts, linked_relationships, associate_cases, display_name, occupation, industry)

        if not associate_checks:
            st.info("No other account was reached in this selected transfer period and branch depth.")
        else:
            associates_df = pd.DataFrame(associate_checks)
            st.write(f"ARGUS checked **{len(associate_checks):,} different connected account(s)** across the transfer links and listed work or ownership links. These accounts provide context; they are not separate flagged accounts.")
            display_associates = associates_df.rename(columns={
                "Associated account": "Account ID", "Name": "Account name", "Role": "Profession",
                "Connection": "How it connects", "Direct contacts shared with focus account": "Shared contacts",
                "Incoming (INR)": "Money in (INR)", "Outgoing (INR)": "Money out (INR)",
                "Declared entity links": "Listed links",
            })
            with st.expander(f"See the {len(display_associates):,} connected accounts checked", expanded=False):
                st.dataframe(display_associates, use_container_width=True, hide_index=True)
            with st.expander("Open the full background and contact check for each account"):
                for associate_id in associate_ids:
                    profile, associated_transfers, shared_contacts, linked_relationships, associate_cases, display_name, occupation, industry = associate_details[associate_id]
                    st.markdown(f"**{display_name} · {associate_id}** — transfer step {account_depth.get(associate_id, 'unknown')}")
                    background = []
                    if occupation != "Not listed": background.append(f"role: {occupation}")
                    if industry != "Not listed": background.append(f"industry: {industry}")
                    for label, key in (("work status", "employment_status"), ("business", "business_name"), ("business registration", "business_registration_status"), ("city", "city"), ("years active", "years_active"), ("income/turnover band", "income_or_turnover_band")):
                        if profile.get(key) not in (None, ""):
                            background.append(f"{label}: {profile[key]}")
                    st.write("**Available profile/background:** " + ("; ".join(background) if background else "No profile/background details are stored.") + (". Fictional demo data." if profile.get("profile_source") == "SYNTHETIC_DEMO" else "."))
                    if shared_contacts:
                        st.write(f"**Contacts also directly connected to the focus account ({len(shared_contacts):,}):** " + ", ".join(shared_contacts[:30]) + (" …" if len(shared_contacts) > 30 else ""))
                        shared_contact_rows = []
                        for contact_id in shared_contacts:
                            contact_profile = profiles.get(contact_id, {})
                            shared_contact_rows.append({
                                "Matching contact": contact_id,
                                "Name": contact_profile.get("entity_name") or contact_id,
                                "Role": contact_profile.get("occupation") or contact_profile.get("entity_kind") or "Not listed",
                                "Industry": contact_profile.get("industry") or "Not listed",
                                "Location": contact_profile.get("city") or "Not listed",
                                "Fictional case records": sum(1 for item in case_rows if str(item.get("account_id")) == contact_id),
                            })
                        st.dataframe(pd.DataFrame(shared_contact_rows), use_container_width=True, hide_index=True)
                    else:
                        st.write("**Matching contacts:** no shared direct transfer contacts with the focus account were found in the selected data and period.")
                    if linked_relationships:
                        rel_text = []
                        for rel in linked_relationships:
                            if str(rel.get("account_id")) == associate_id:
                                other = str(rel.get("related_account_id") or "unknown")
                                relation = str(rel.get("relationship_type") or "linked").replace("_", " ").lower()
                                rel_text.append(f"{relation} {rel.get('related_name') or other} ({other})")
                            else:
                                other = str(rel.get("account_id") or "unknown")
                                relation = str(rel.get("relationship_type") or "linked").replace("_", " ").lower()
                                rel_text.append(f"listed as {relation} to {rel.get('account_name') or other} ({other})")
                        st.write("**Declared entity relationships:** " + "; ".join(rel_text) + ". These are stored demo links, not independently verified facts.")
                    else:
                        st.write("**Declared entity relationships:** none found in the available records.")
                    if associate_cases:
                        for case in associate_cases:
                            st.write(f"**Fictional case record:** {case.get('case_category', 'Case')} — {case.get('case_status', 'status not listed')}; {case.get('outcome_summary', 'no outcome summary')}.")
                    else:
                        st.write("**Fictional case records:** none stored for this account; this is not an external background check.")
                    own_reasons = list(dict.fromkeys(str(row.get("Why flagged", "")).strip() for row in associated_transfers.to_dict("records") if row.get("Alert linked") and row.get("Why flagged"))) if not associated_transfers.empty else []
                    if own_reasons:
                        st.write("**Alert-linked transfer context:** " + "; ".join(own_reasons[:5]))
                    st.divider()

        st.caption("ARGUS checks the records available in this app: transfer paths and directions, alert reasons, stored profile and declared relationship links, account contacts, and fictional case records. It cannot access external identity, employment, police, court, or government databases. Missing records are shown as unknown, never as proof of innocence or wrongdoing.")

    with live_tab:
        render_investigation("Live Simulation", live_candidate_ids, live_account_names, live_source=True)
    with saved_tab:
        if saved_candidates.empty:
            st.info("No saved historical alert accounts were found.")
        else:
            st.caption(f"{len(saved_candidates):,} saved-alert accounts are available for review. Choose one below to build its transfer and entity report.")
            render_investigation("Saved Historical", saved_candidates["account_id"].astype(str).tolist(), saved_account_names, live_source=False)





# --------------------------------------------------

# ENTITY INTELLIGENCE

# --------------------------------------------------



elif page == "Entity Intelligence":
    st.subheader("Entity Intelligence")
    st.warning("Synthetic demo profiles and case records only. ARGUS is not connected to police, court, sanctions, or government registries. Do not treat these fictional records as real background checks or evidence of wrongdoing.")

    with get_connection() as conn:
        profile_count = int(conn.execute("SELECT COUNT(*) FROM entity_profiles").fetchone()[0])
        case_count = int(conn.execute("SELECT COUNT(*) FROM entity_case_records").fetchone()[0])
    status_cols = st.columns(3)
    status_cols[0].metric("Accounts in demo", f"{accounts_count:,}")
    status_cols[1].metric("Synthetic profiles", f"{profile_count:,}")
    status_cols[2].metric("Fictional case records", f"{case_count:,}")
    if profile_count < accounts_count:
        st.info("Entity profiles have not been generated for every account yet. After installing the updated files, run `python generate_entity_data.py` once from the ARGUS project folder. It adds fictional profile records and leaves existing transactions and alerts unchanged.")

    query = st.text_input("Find a person, business, or account ID", placeholder="For example: ACC000123 or a name", key="entity_intelligence_search")
    with get_connection() as conn:
        if query.strip():
            pattern = f"%{query.strip()}%"
            entity_rows = pd.read_sql_query("""
                SELECT a.account_id, a.entity_name, a.account_type, a.opening_balance, a.balance,
                       p.occupation, p.industry, p.employment_status
                FROM accounts AS a LEFT JOIN entity_profiles AS p ON p.account_id = a.account_id
                WHERE a.account_id LIKE ? OR a.entity_name LIKE ?
                ORDER BY a.account_id LIMIT 100
            """, conn, params=(pattern, pattern))
        else:
            entity_rows = pd.read_sql_query("""
                SELECT a.account_id, a.entity_name, a.account_type, a.opening_balance, a.balance,
                       p.occupation, p.industry, p.employment_status
                FROM accounts AS a LEFT JOIN entity_profiles AS p ON p.account_id = a.account_id
                ORDER BY a.account_id LIMIT 25
            """, conn)
    if entity_rows.empty:
        st.info("No accounts matched that search.")
    else:
        shown = entity_rows.rename(columns={"account_id":"Account ID", "entity_name":"Name", "account_type":"Account type", "occupation":"Occupation", "industry":"Industry", "employment_status":"Work status"})
        st.dataframe(shown, use_container_width=True, hide_index=True)
        options = entity_rows["account_id"].astype(str).tolist()
        labels = dict(zip(entity_rows["account_id"].astype(str), entity_rows["entity_name"].astype(str)))
        selected_entity = st.selectbox("Open an entity profile", options=options, format_func=lambda value: f"{labels.get(value, value)} · {value}", key="entity_intelligence_selected_account")

        with get_connection() as conn:
            profile_rows = conn.execute("""
                SELECT a.account_id, a.entity_name, a.account_type, a.opening_balance, a.balance, a.created_at,
                       p.entity_kind, p.occupation, p.employment_status, p.industry,
                       p.employer_account_id, p.employer_name, p.business_name,
                       p.business_registration_status, p.city, p.years_active,
                       p.income_or_turnover_band, p.profile_source
                FROM accounts AS a LEFT JOIN entity_profiles AS p ON p.account_id = a.account_id
                WHERE a.account_id = ?
            """, (selected_entity,)).fetchone()
            profile = dict(profile_rows) if profile_rows else None
            if profile:
                demo_account_context = conn.execute("""
                    SELECT usual_region, primary_device_id, context_source
                    FROM demo_account_context WHERE account_id = ?
                """, (selected_entity,)).fetchone()
                demo_transaction_context = pd.read_sql_query("""
                    SELECT t.transaction_id AS "Transaction ID", t.timestamp AS "Time",
                           t.sender_id AS "From account", t.receiver_id AS "To account",
                           t.amount AS "Amount (INR)", c.device_id AS "Demo device",
                           c.origin_region AS "Demo origin", c.payment_channel AS "Demo channel",
                           c.payment_purpose AS "Demo purpose", c.merchant_category AS "Demo merchant category",
                           c.invoice_reference AS "Demo invoice"
                    FROM transactions t
                    LEFT JOIN demo_transaction_context c ON c.transaction_id = t.transaction_id
                    WHERE t.sender_id = ? OR t.receiver_id = ?
                    ORDER BY t.timestamp DESC LIMIT 100
                """, conn, params=(selected_entity, selected_entity))
                flow_summary = conn.execute("""
                    SELECT COUNT(*) AS transaction_count,
                           SUM(CASE WHEN sender_id = ? THEN amount ELSE 0 END) AS outgoing_total,
                           SUM(CASE WHEN receiver_id = ? THEN amount ELSE 0 END) AS incoming_total,
                           COUNT(DISTINCT CASE WHEN sender_id = ? THEN receiver_id WHEN receiver_id = ? THEN sender_id END) AS counterparties
                    FROM transactions
                    WHERE sender_id = ? OR receiver_id = ?
                """, (selected_entity, selected_entity, selected_entity, selected_entity, selected_entity, selected_entity)).fetchone()
                alerts_summary = conn.execute("""
                    SELECT COUNT(*) AS alert_count, MAX(risk_score) AS peak_risk
                    FROM alerts WHERE account_id = ?
                """, (selected_entity,)).fetchone()
                relationships = pd.read_sql_query("""
                    SELECT r.relationship_type, r.account_id, a.entity_name AS account_name,
                           r.related_account_id, b.entity_name AS related_name,
                           r.start_year, r.evidence_note, r.relationship_source
                    FROM entity_relationships AS r
                    LEFT JOIN accounts AS a ON a.account_id = r.account_id
                    LEFT JOIN accounts AS b ON b.account_id = r.related_account_id
                    WHERE r.account_id = ? OR r.related_account_id = ?
                    ORDER BY r.relationship_type, r.related_account_id
                    LIMIT 100
                """, conn, params=(selected_entity, selected_entity))
                case_records = pd.read_sql_query("""
                    SELECT case_category, allegation, case_status, court_or_authority,
                           reference_number, filed_date, outcome_date, outcome_summary, record_source
                    FROM entity_case_records WHERE account_id = ?
                    ORDER BY filed_date DESC
                """, conn, params=(selected_entity,))
                recent_alerts = pd.read_sql_query("""
                    SELECT transaction_id AS "Transaction ID", risk_score AS "Risk score",
                           status AS "Status", reason AS "Reason", created_at AS "Alert date"
                    FROM alerts WHERE account_id = ? ORDER BY created_at DESC LIMIT 25
                """, conn, params=(selected_entity,))
                incoming_flows = pd.read_sql_query("""
                    SELECT t.sender_id AS "Other account", a.entity_name AS "Entity name",
                           COUNT(*) AS Transfers, SUM(t.amount) AS "Total flow (INR)",
                           MAX(t.timestamp) AS "Latest transfer"
                    FROM transactions AS t LEFT JOIN accounts AS a ON a.account_id = t.sender_id
                    WHERE t.receiver_id = ? GROUP BY t.sender_id, a.entity_name
                    ORDER BY SUM(t.amount) DESC LIMIT 25
                """, conn, params=(selected_entity,))
                outgoing_flows = pd.read_sql_query("""
                    SELECT t.receiver_id AS "Other account", a.entity_name AS "Entity name",
                           COUNT(*) AS Transfers, SUM(t.amount) AS "Total flow (INR)",
                           MAX(t.timestamp) AS "Latest transfer"
                    FROM transactions AS t LEFT JOIN accounts AS a ON a.account_id = t.receiver_id
                    WHERE t.sender_id = ? GROUP BY t.receiver_id, a.entity_name
                    ORDER BY SUM(t.amount) DESC LIMIT 25
                """, conn, params=(selected_entity,))

        if not profile:
            st.error("The selected account could not be loaded.")
        else:
            st.markdown(f"### {profile['entity_name']}")
            st.caption(f"Account {profile['account_id']} · {profile['account_type'].title()}")
            if profile.get("profile_source") == "SYNTHETIC_DEMO":
                st.caption("Profile source: fictional ARGUS demo data · not independently verified")
            elif not profile.get("profile_source"):
                st.info("No profile record is available for this account. Missing data is not evidence of risk or clearance.")

            metrics = st.columns(4)
            metrics[0].metric("Transaction history", f"{int(flow_summary['transaction_count'] or 0):,}")
            metrics[1].metric("Incoming total", f"₹{float(flow_summary['incoming_total'] or 0):,.2f}")
            metrics[2].metric("Outgoing total", f"₹{float(flow_summary['outgoing_total'] or 0):,.2f}")
            metrics[3].metric("Saved alerts", f"{int(alerts_summary['alert_count'] or 0):,}", f"Peak score {int(alerts_summary['peak_risk'] or 0)}/100" if alerts_summary['peak_risk'] is not None else None)

            background_tab, links_tab, cases_tab, activity_tab, demo_context_tab = st.tabs(["Background", "Relationships & flows", "Case records", "Alerts", "Demo transaction context"])
            with background_tab:
                if profile.get("profile_source"):
                    c1, c2, c3 = st.columns(3)
                    c1.markdown("**Occupation**")
                    c1.write(profile["occupation"])
                    c1.markdown("**Work status**")
                    c1.write(profile["employment_status"])
                    c2.markdown("**Industry**")
                    c2.write(profile["industry"])
                    c2.markdown("**Location**")
                    c2.write(profile["city"])
                    c3.markdown("**Experience**")
                    c3.write(f"{int(profile['years_active'])} year(s)")
                    c3.markdown("**Income / turnover band**")
                    c3.write(profile["income_or_turnover_band"])
                    if profile["entity_kind"] == "Business":
                        st.write(f"**Business name:** {profile['business_name'] or profile['entity_name']}")
                        st.write(f"**Demo registration status:** {profile['business_registration_status']}")
                    elif profile["employer_account_id"]:
                        st.write(f"**Employer:** {profile['employer_name']} · {profile['employer_account_id']}")
                    st.caption("Occupation, location, income, and registration details are fictional examples, not verified facts.")
                else:
                    st.info("No synthetic profile has been generated for this account yet. Run the entity data generator to populate the demo profiles.")
                st.write({"Opening balance (INR)": profile["opening_balance"], "Current balance (INR)": profile["balance"], "Account created": profile["created_at"]})

            with links_tab:
                st.markdown("#### Declared demo relationships")
                if relationships.empty:
                    st.info("No synthetic employment or ownership links are recorded for this account.")
                else:
                    rel_view = relationships.copy()
                    rel_view["Direction"] = rel_view.apply(lambda r: "This account → related entity" if r["account_id"] == selected_entity else "Related entity → this account", axis=1)
                    rel_view["Related account"] = rel_view.apply(lambda r: r["related_account_id"] if r["account_id"] == selected_entity else r["account_id"], axis=1)
                    rel_view["Related entity"] = rel_view.apply(lambda r: r["related_name"] if r["account_id"] == selected_entity else r["account_name"], axis=1)
                    st.dataframe(rel_view[["relationship_type", "Direction", "Related account", "Related entity", "start_year", "evidence_note"]].rename(columns={"relationship_type":"Relationship", "start_year":"Since (demo)", "evidence_note":"Source note"}), use_container_width=True, hide_index=True)
                st.markdown("#### Observed incoming transfers")
                st.caption("These are account-to-account transaction links, not proof of personal or business relationships.")
                st.dataframe(incoming_flows, use_container_width=True, hide_index=True)
                st.markdown("#### Observed outgoing transfers")
                st.dataframe(outgoing_flows, use_container_width=True, hide_index=True)

            with cases_tab:
                st.caption("Fictional sample records only. ARGUS has no connection to real police or court records. Case status applies only to the individual demo case.")
                if case_records.empty:
                    st.info("No fictional demo case record is attached to this account. This does not mean a real-world background check was performed.")
                else:
                    for _, case_row in case_records.iterrows():
                        with st.expander(f"{case_row['case_category']} · {case_row['case_status']} · {case_row['reference_number']}"):
                            st.write({"Allegation": case_row["allegation"], "Status": case_row["case_status"], "Court / authority": case_row["court_or_authority"], "Filed": case_row["filed_date"], "Outcome date": case_row["outcome_date"], "Outcome": case_row["outcome_summary"], "Source": case_row["record_source"]})
                    st.caption("An acquittal is shown as an outcome for that specific fictional case; it is not described as proof of innocence in other matters. A pending case is not a finding of guilt.")

            with activity_tab:
                st.caption("Transfer-pattern scores and synthetic entity context are separate evidence. Fictional case records do not increase or decrease transaction risk scores.")
                if recent_alerts.empty:
                    st.info("No saved alert records are linked to this account.")
                else:
                    st.dataframe(recent_alerts, use_container_width=True, hide_index=True)

            with demo_context_tab:
                st.caption("Generated device, region, channel, purpose, and invoice values are fictional demo data. ARGUS uses available values as review signals only.")
                if demo_account_context:
                    st.write({
                        "Usual demo region": demo_account_context["usual_region"],
                        "Usual demo device": demo_account_context["primary_device_id"],
                        "Context source": demo_account_context["context_source"],
                    })
                else:
                    st.info("No usual device or region is stored for this account.")
                if demo_transaction_context.empty:
                    st.info("No generated transaction context is available for this account's stored transfers.")
                else:
                    st.dataframe(demo_transaction_context, use_container_width=True, hide_index=True)


elif page == "Reports":

    st.subheader("Account review reports")
    st.caption("Create a clear report for one flagged account, including why it was flagged and which connected accounts ARGUS checked.")
    st.warning("Entity profiles and case records in this demo are fictional. ARGUS has no connection to a bank, police department, court, sanctions list, or government registry. The send buttons below are demonstrations only and never transmit data.")
    report_source_choice = st.radio(
        "Report source",
        ["Live session alerts", "Saved historical alerts"],
        horizontal=True,
        key="argus_report_source_choice",
    )

    live_report_events = [dict(row) for row in st.session_state.get("argus_live_alerts", [])]
    saved_report_events = []
    report_names = {}
    report_name_ids = {str(value) for row in live_report_events
                       for value in (row.get("account_id"), row.get("Sender"), row.get("Receiver")) if value}
    with get_connection() as conn:
        if report_source_choice == "Saved historical alerts":
            saved_report_events = [dict(row) for row in conn.execute("""
                SELECT al.alert_id, al.transaction_id, al.account_id, al.risk_score, al.reason,
                       al.status, al.created_at,
                       t.sender_id AS Sender, t.receiver_id AS Receiver,
                       t.amount AS Amount, t.timestamp AS Timestamp, t.description AS Description,
                       dc.device_id AS demo_device_id, dc.origin_region AS demo_origin_region,
                       dc.payment_channel AS demo_payment_channel, dc.payment_purpose AS demo_payment_purpose,
                       dc.merchant_category AS demo_merchant_category, dc.invoice_reference AS demo_invoice_reference
                FROM alerts al LEFT JOIN transactions t ON t.transaction_id = al.transaction_id
                LEFT JOIN demo_transaction_context dc ON dc.transaction_id = al.transaction_id
                ORDER BY al.created_at DESC
                LIMIT 5000
            """)]
            report_name_ids.update(
                str(value) for row in saved_report_events
                for value in (row.get("account_id"), row.get("Sender"), row.get("Receiver")) if value
            )
        for offset in range(0, len(report_name_ids), 400):
            group = sorted(report_name_ids)[offset:offset + 400]
            marks = ",".join("?" for _ in group)
            for row in conn.execute(f"SELECT account_id, entity_name FROM accounts WHERE account_id IN ({marks})", group):
                report_names[str(row["account_id"])] = str(row["entity_name"] or row["account_id"])

    def render_alert_report(source_label, source_events, live_source=False):
        by_account = {}
        for row in source_events:
            account_id = row.get("account_id") if not live_source else None
            if not account_id:
                account_id = row.get("Sender") or row.get("Receiver")
            if account_id:
                by_account.setdefault(str(account_id), []).append(row)
            if live_source:
                for endpoint in (row.get("Sender"), row.get("Receiver")):
                    if endpoint:
                        by_account.setdefault(str(endpoint), []).append(row)
        if not by_account:
            st.info("There are no alert records available for this report source yet.")
            return
        account_options = sorted(by_account)
        selected_account = st.selectbox(
            "Choose an alerted account",
            account_options,
            format_func=lambda account_id: f"{report_names.get(account_id, account_id)} · {account_id}",
            key=f"report_{source_label.lower().replace(' ', '_')}_account",
        )
        selected_rows = by_account[selected_account]
        unique_events = {}
        for row in selected_rows:
            tx_id = str(row.get("Transaction ID") or row.get("transaction_id") or f"{row.get('Timestamp')}|{row.get('Sender')}|{row.get('Receiver')}|{row.get('Amount (INR)', row.get('Amount'))}")
            unique_events[tx_id] = row
        events = sorted(unique_events.items(), key=lambda item: str(item[1].get("Timestamp") or item[1].get("created_at") or ""), reverse=True)
        party_ids = sorted({str(value) for _, row in events for value in (row.get("Sender"), row.get("Receiver")) if value})
        report_depth = st.select_slider("Transfer branch depth for this report", options=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10], value=5, key=f"report_{source_label.lower().replace(' ', '_')}_depth", help="Follows incoming and outgoing transfers from the suspect, then from associated accounts. The report lists the coverage limits it reaches.")
        report_cutoff = None
        if live_source:
            st.caption("Live report branches use the latest 5,000 simulation events kept in this session. The report can follow up to 10 steps and 5,000 transfer links/accounts in both directions.")
        else:
            report_days = st.selectbox("Historical branch period", [30, 90, 365, 0], format_func=lambda value: "All available history" if value == 0 else f"Last {value} days", index=1, key=f"report_{source_label.lower().replace(' ', '_')}_period")
            if report_days:
                from datetime import timedelta
                report_cutoff = (datetime.now() - timedelta(days=int(report_days))).strftime("%Y-%m-%d %H:%M:%S")
            st.caption("Historical reports can follow up to 10 steps and 5,000 distinct transfer links/accounts in both directions.")

        # Keep report building bounded on the five-million-transaction dataset.
        max_report_branch_links = 5000
        max_report_accounts = 5000
        report_limit_reached = False
        report_branch_links = {}
        report_account_depth = {str(selected_account): 0}
        report_frontier = {str(selected_account)}
        report_visited = set(report_frontier)
        if live_source:
            live_pool = [dict(row) for row in st.session_state.get("argus_sim_events", [])]
            for branch_step in range(int(report_depth)):
                if len(report_branch_links) >= max_report_branch_links or len(report_visited) >= max_report_accounts:
                    report_limit_reached = True
                    break
                next_accounts = set()
                for row in live_pool:
                    sender = str(row.get("Sender") or "")
                    receiver = str(row.get("Receiver") or "")
                    if sender in report_frontier or receiver in report_frontier:
                        pair_key = f"{sender}|{receiver}"
                        if pair_key not in report_branch_links and len(report_branch_links) >= max_report_branch_links:
                            report_limit_reached = True
                            continue
                        aggregate = report_branch_links.setdefault(pair_key, {"Sender": sender, "Receiver": receiver, "Transfers": 0, "Flow (INR)": 0.0, "Alert count": 0, "Peak score": 0, "Last seen": "", "Reasons": []})
                        aggregate["Transfers"] += 1
                        try:
                            aggregate["Flow (INR)"] += float(row.get("Amount (INR)", row.get("Amount", 0)) or 0)
                        except (TypeError, ValueError):
                            pass
                        score_value = int(row.get("risk_score", 0) or 0)
                        aggregate["Peak score"] = max(aggregate["Peak score"], score_value)
                        if score_value >= int(row.get("alert_threshold", ALERT_THRESHOLD)):
                            aggregate["Alert count"] += 1
                            reason_text = str(row.get("risk_reasons", "") or "")
                            if reason_text:
                                aggregate["Reasons"].append(reason_text)
                        aggregate["Last seen"] = max(str(aggregate["Last seen"]), str(row.get("Timestamp", "")))
                        for endpoint in (sender, receiver):
                            if endpoint and endpoint not in report_visited:
                                if len(report_visited) + len(next_accounts) < max_report_accounts:
                                    next_accounts.add(endpoint)
                                else:
                                    report_limit_reached = True
                report_visited.update(next_accounts)
                for account_id in next_accounts:
                    report_account_depth.setdefault(account_id, branch_step + 1)
                report_frontier = next_accounts
                if not report_frontier:
                    break
        else:
            with get_connection() as conn:
                for _branch_step in range(int(report_depth)):
                    remaining_links = max_report_branch_links - len(report_branch_links)
                    if remaining_links <= 0 or len(report_visited) >= max_report_accounts:
                        report_limit_reached = True
                        break
                    next_accounts = set()
                    frontier_ids = sorted(report_frontier)
                    for offset in range(0, len(frontier_ids), 350):
                        remaining_links = max_report_branch_links - len(report_branch_links)
                        if remaining_links <= 0:
                            report_limit_reached = True
                            break
                        group = frontier_ids[offset:offset + 350]
                        marks = ",".join("?" for _ in group)
                        date_sql = " AND timestamp >= ?" if report_cutoff else ""
                        params = group + group + ([report_cutoff] if report_cutoff else [])
                        for row in conn.execute(f"""
                            SELECT sender_id AS Sender, receiver_id AS Receiver,
                                   COUNT(*) AS Transfers, SUM(amount) AS "Flow (INR)",
                                   MAX(timestamp) AS "Last seen",
                                   0 AS "Alert count", NULL AS "Peak score"
                            FROM transactions
                            WHERE (sender_id IN ({marks}) OR receiver_id IN ({marks})){date_sql}
                            GROUP BY sender_id, receiver_id
                            ORDER BY MAX(timestamp) DESC, sender_id, receiver_id
                            LIMIT ?
                        """, params + [remaining_links]):
                            item = dict(row)
                            pair_key = f"{item['Sender']}|{item['Receiver']}"
                            if pair_key not in report_branch_links and len(report_branch_links) >= max_report_branch_links:
                                report_limit_reached = True
                                continue
                            report_branch_links.setdefault(pair_key, {**item, "Reasons": []})
                            for endpoint in (str(item["Sender"]), str(item["Receiver"])):
                                if endpoint and endpoint not in report_visited:
                                    if len(report_visited) + len(next_accounts) < max_report_accounts:
                                        next_accounts.add(endpoint)
                                    else:
                                        report_limit_reached = True
                    report_visited.update(next_accounts)
                    for account_id in next_accounts:
                        report_account_depth.setdefault(account_id, _branch_step + 1)
                    report_frontier = next_accounts
                    if not report_frontier:
                        break

                report_account_ids = sorted(report_visited)
                for offset in range(0, len(report_account_ids), 350):
                    group = report_account_ids[offset:offset + 350]
                    marks = ",".join("?" for _ in group)
                    for row in conn.execute(f"""
                        SELECT t.sender_id AS Sender, t.receiver_id AS Receiver,
                               COUNT(*) AS "Alert count", MAX(a.risk_score) AS "Peak score",
                               GROUP_CONCAT(DISTINCT a.reason) AS Reasons
                        FROM alerts a JOIN transactions t ON t.transaction_id = a.transaction_id
                        WHERE (t.sender_id IN ({marks}) OR t.receiver_id IN ({marks}))
                        {"AND t.timestamp >= ?" if report_cutoff else ""}
                        GROUP BY t.sender_id, t.receiver_id
                    """, group + group + ([report_cutoff] if report_cutoff else [])):
                        pair_key = f"{row['Sender']}|{row['Receiver']}"
                        if pair_key in report_branch_links:
                            branch = report_branch_links[pair_key]
                            branch["Alert count"] = max(int(branch.get("Alert count", 0) or 0), int(row["Alert count"] or 0))
                            branch["Peak score"] = max(float(branch.get("Peak score", 0) or 0), float(row["Peak score"] or 0))
                            branch["Reasons"] = [str(row["Reasons"])] if row["Reasons"] else []

        context_account_ids = sorted(report_visited)
        profiles = {}
        relationships = []
        cases_by_account = {}
        flow_stats = {}
        with get_connection() as conn:
            for offset in range(0, len(context_account_ids), 350):
                group = context_account_ids[offset:offset + 350]
                marks = ",".join("?" for _ in group)
                for row in conn.execute(f"""
                    SELECT a.account_id, a.entity_name, a.account_type,
                           p.entity_kind, p.occupation, p.employment_status, p.industry,
                           p.employer_account_id, p.employer_name, p.business_name,
                           p.business_registration_status, p.city, p.years_active,
                           p.income_or_turnover_band, p.profile_source
                    FROM accounts a LEFT JOIN entity_profiles p ON p.account_id = a.account_id
                    WHERE a.account_id IN ({marks})
                """, group):
                    profiles[str(row["account_id"])] = dict(row)
                for row in conn.execute(f"""
                    SELECT r.account_id, a.entity_name AS account_name,
                           r.related_account_id, b.entity_name AS related_name,
                           r.relationship_type, r.start_year, r.relationship_source, r.evidence_note
                    FROM entity_relationships r
                    LEFT JOIN accounts a ON a.account_id = r.account_id
                    LEFT JOIN accounts b ON b.account_id = r.related_account_id
                    WHERE r.account_id IN ({marks}) OR r.related_account_id IN ({marks})
                """, group + group):
                    relationships.append(dict(row))
                for row in conn.execute(f"""
                    SELECT account_id, case_category, allegation, case_status,
                           court_or_authority, reference_number, filed_date,
                           outcome_date, outcome_summary, record_source
                    FROM entity_case_records WHERE account_id IN ({marks}) ORDER BY filed_date DESC
                """, group):
                    cases_by_account.setdefault(str(row["account_id"]), []).append(dict(row))
                for row in conn.execute(f"""
                    SELECT sender_id AS account_id, COUNT(*) AS outgoing_count,
                           SUM(amount) AS outgoing_total, COUNT(DISTINCT receiver_id) AS outgoing_contacts
                    FROM transactions WHERE sender_id IN ({marks}) GROUP BY sender_id
                """, group):
                    flow_stats.setdefault(str(row["account_id"]), {}).update(dict(row))
                for row in conn.execute(f"""
                    SELECT receiver_id AS account_id, COUNT(*) AS incoming_count,
                           SUM(amount) AS incoming_total, COUNT(DISTINCT sender_id) AS incoming_contacts
                    FROM transactions WHERE receiver_id IN ({marks}) GROUP BY receiver_id
                """, group):
                    flow_stats.setdefault(str(row["account_id"]), {}).update(dict(row))

            # Follow declared entity links beyond the transaction graph and load
            # each linked account's available background, relationships, history,
            # and fictional case records for the downloadable narrative.
            entity_seen = set(context_account_ids)
            entity_frontier = {
                str(row.get("related_account_id")) for row in relationships
                if row.get("related_account_id") and str(row.get("related_account_id")) not in entity_seen
            } | {
                str(row.get("account_id")) for row in relationships
                if row.get("account_id") and str(row.get("account_id")) not in entity_seen
            }
            relationship_keys = {
                (str(row.get("account_id")), str(row.get("related_account_id")), str(row.get("relationship_type")))
                for row in relationships
            }
            for _entity_step in range(2):
                current_entities = sorted(entity_frontier - entity_seen)
                if not current_entities:
                    break
                remaining_accounts = max(0, max_report_accounts - len(entity_seen))
                if len(current_entities) > remaining_accounts:
                    current_entities = current_entities[:remaining_accounts]
                    report_limit_reached = True
                if not current_entities:
                    break
                entity_seen.update(current_entities)
                next_entities = set()
                for offset in range(0, len(current_entities), 350):
                    group = current_entities[offset:offset + 350]
                    marks = ",".join("?" for _ in group)
                    for row in conn.execute(f"""
                        SELECT a.account_id, a.entity_name, a.account_type,
                               p.entity_kind, p.occupation, p.employment_status, p.industry,
                               p.employer_account_id, p.employer_name, p.business_name,
                               p.business_registration_status, p.city, p.years_active,
                               p.income_or_turnover_band, p.profile_source
                        FROM accounts a LEFT JOIN entity_profiles p ON p.account_id = a.account_id
                        WHERE a.account_id IN ({marks})
                    """, group):
                        profiles[str(row["account_id"])] = dict(row)
                    for row in conn.execute(f"""
                        SELECT account_id, case_category, allegation, case_status,
                               court_or_authority, reference_number, filed_date,
                               outcome_date, outcome_summary, record_source
                        FROM entity_case_records WHERE account_id IN ({marks}) ORDER BY filed_date DESC
                    """, group):
                        cases_by_account.setdefault(str(row["account_id"]), []).append(dict(row))
                    for row in conn.execute(f"""
                        SELECT sender_id AS account_id, COUNT(*) AS outgoing_count,
                               SUM(amount) AS outgoing_total, COUNT(DISTINCT receiver_id) AS outgoing_contacts
                        FROM transactions WHERE sender_id IN ({marks}) GROUP BY sender_id
                    """, group):
                        flow_stats.setdefault(str(row["account_id"]), {}).update(dict(row))
                    for row in conn.execute(f"""
                        SELECT receiver_id AS account_id, COUNT(*) AS incoming_count,
                               SUM(amount) AS incoming_total, COUNT(DISTINCT sender_id) AS incoming_contacts
                        FROM transactions WHERE receiver_id IN ({marks}) GROUP BY receiver_id
                    """, group):
                        flow_stats.setdefault(str(row["account_id"]), {}).update(dict(row))
                    rel_sql = f"""
                        SELECT r.account_id, a.entity_name AS account_name,
                               r.related_account_id, b.entity_name AS related_name,
                               r.relationship_type, r.start_year, r.relationship_source, r.evidence_note
                        FROM entity_relationships r
                        LEFT JOIN accounts a ON a.account_id = r.account_id
                        LEFT JOIN accounts b ON b.account_id = r.related_account_id
                        WHERE r.account_id IN ({marks}) OR r.related_account_id IN ({marks})
                    """
                    for row in conn.execute(rel_sql, group + group):
                        item = dict(row)
                        key = (str(item.get("account_id")), str(item.get("related_account_id")), str(item.get("relationship_type")))
                        if key not in relationship_keys:
                            relationship_keys.add(key)
                            relationships.append(item)
                        for endpoint in (str(item.get("account_id") or ""), str(item.get("related_account_id") or "")):
                            if endpoint and endpoint not in entity_seen and len(entity_seen) + len(next_entities) < max_report_accounts:
                                next_entities.add(endpoint)
                            elif endpoint and endpoint not in entity_seen:
                                report_limit_reached = True
                entity_frontier = next_entities
            for account_id in entity_seen - set(context_account_ids):
                report_account_depth.setdefault(account_id, "Entity link")
            context_account_ids = sorted(entity_seen)

        relationships_by_account = {}
        for relationship in relationships:
            for endpoint in (str(relationship.get("account_id") or ""), str(relationship.get("related_account_id") or "")):
                if endpoint:
                    relationships_by_account.setdefault(endpoint, []).append(relationship)
        branch_links_by_account = {}
        for branch in report_branch_links.values():
            for endpoint in (str(branch.get("Sender") or ""), str(branch.get("Receiver") or "")):
                if endpoint:
                    branch_links_by_account.setdefault(endpoint, []).append(branch)

        report_transfer_rows = []
        report_pattern_summary = {}
        total_alert_amount = 0.0
        all_scores = []
        for tx_id, row in events:
            sender = str(row.get("Sender") or "Unknown sender")
            receiver = str(row.get("Receiver") or "Unknown receiver")
            amount = row.get("Amount (INR)", row.get("Amount", 0))
            try:
                amount = float(amount or 0)
            except (TypeError, ValueError):
                amount = 0.0
            score = row.get("risk_score", row.get("risk_score", ""))
            if score not in (None, ""):
                try:
                    all_scores.append(int(float(score)))
                except (TypeError, ValueError):
                    pass
            total_alert_amount += amount
            reasons = row.get("risk_reasons", row.get("reason", "No saved reason text"))
            if isinstance(reasons, list):
                reasons = "; ".join(str(item) for item in reasons)
            patterns = row.get("risk_patterns", [])
            pattern_text = "\n".join(
                f"  - {_plain_alert_reason(item.get('pattern', 'Pattern'))}: {_plain_alert_reason(item.get('evidence', ''))}"
                for item in patterns if isinstance(item, dict)
            ) if isinstance(patterns, list) else ""
            if isinstance(patterns, list) and patterns:
                for pattern_item in patterns:
                    if not isinstance(pattern_item, dict):
                        continue
                    pattern_name = str(pattern_item.get("pattern") or pattern_item.get("code") or "Pattern")
                    pattern_key = str(pattern_item.get("code") or pattern_name)
                    summary_item = report_pattern_summary.setdefault(pattern_key, {
                        "Pattern": pattern_name,
                        "Evidence": str(pattern_item.get("evidence") or "Evidence not stored"),
                        "Transfers with this pattern": 0,
                    })
                    summary_item["Transfers with this pattern"] += 1
            elif reasons:
                # Older saved alerts keep plain reason text rather than structured pattern fields.
                for reason_part in dict.fromkeys(part.strip() for part in str(reasons).split(";") if part.strip()):
                    summary_item = report_pattern_summary.setdefault(f"saved:{reason_part}", {
                        "Pattern": _plain_alert_reason(reason_part),
                        "Evidence": "Saved alert reason; structured pattern details were not stored for this record.",
                        "Transfers with this pattern": 0,
                    })
                    summary_item["Transfers with this pattern"] += 1
            report_transfer_rows.append({
                "Transaction ID": tx_id,
                "Time": str(row.get("Timestamp") or row.get("created_at") or ""),
                "From account": sender,
                "From name": report_names.get(sender, sender),
                "To account": receiver,
                "To name": report_names.get(receiver, receiver),
                "Amount (INR)": amount,
                "Risk score": score,
                "Risk level": row.get("risk_level", ""),
                "Why flagged": _plain_alert_reason(str(reasons or "")),
                "Pattern evidence": pattern_text,
                "Entity context": str(row.get("entity_context", "")),
                "Demo device": str(row.get("demo_device_id", "") or ""),
                "Demo origin": str(row.get("demo_origin_region", "") or ""),
                "Demo channel": str(row.get("demo_payment_channel", "") or ""),
                "Demo purpose": str(row.get("demo_payment_purpose", "") or ""),
                "Demo merchant category": str(row.get("demo_merchant_category", "") or ""),
                "Demo invoice": str(row.get("demo_invoice_reference", "") or ""),
                "Alert status": row.get("status", "Current session" if live_source else "Open"),
            })

        # Recheck the selected account's alert-linked transfers against the
        # complete pattern catalogue. The report states this scope explicitly:
        # unflagged surrounding transactions are not included in this check.
        report_check_events = [
            {
                "Sender": item["From account"],
                "Receiver": item["To account"],
                "Amount (INR)": item["Amount (INR)"],
                "Timestamp": item["Time"],
                "demo_device_id": item["Demo device"],
                "demo_origin_region": item["Demo origin"],
                "demo_payment_channel": item["Demo channel"],
                "demo_payment_purpose": item["Demo purpose"],
                "demo_merchant_category": item["Demo merchant category"],
                "demo_invoice_reference": item["Demo invoice"],
            }
            for item in report_transfer_rows
        ]
        checked_patterns = evaluate_pattern_checks(report_check_events)
        catalog_match_counts = Counter()
        catalog_evidence = {}
        for event_patterns in checked_patterns.values():
            for pattern_item in event_patterns:
                code = pattern_item["code"]
                catalog_match_counts[code] += 1
                catalog_evidence.setdefault(code, pattern_item["evidence"])
        catalog_report_rows = []
        for item in PATTERN_CATALOG:
            matched_count = catalog_match_counts.get(item["code"], 0)
            catalog_report_rows.append({
                "Code": item["code"],
                "Pattern check": item["name"],
                "Result on selected alerted transfers": (
                    f"Matched on {matched_count} transfer(s)"
                    if matched_count else "No match in these transfers"
                ),
                "What was checked": item["description"],
                "Example evidence": catalog_evidence.get(item["code"], "—"),
            })

        created_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        top_score = max(all_scores) if all_scores else 0
        report_lines = [
            "ARGUS ALERT INVESTIGATION REPORT",
            "DEMO REVIEW DOCUMENT — NOT A FINDING OF WRONGDOING",
            f"Generated: {created_text}",
            f"Source: {source_label}",
            f"Selected alerted account: {report_names.get(selected_account, selected_account)} ({selected_account})",
            f"Unique alerted transfers included: {len(report_transfer_rows)}",
            f"Combined value of these alerted transfers: INR {total_alert_amount:,.2f}",
            f"Highest available risk score: {top_score}/100",
            f"Transfer-network accounts examined: {len(context_account_ids):,} through up to {report_depth} step(s), in both incoming and outgoing directions",
            f"Distinct account-to-account links examined: {len(report_branch_links):,}",
            f"Did the review reach its limit?: {'Yes; more connected transfers may remain' if report_limit_reached else 'No'}",
            f"Distinct triggered pattern/reason types included: {len(report_pattern_summary):,}",
            f"Additional checks: {len(PATTERN_CATALOG)} checks looked at {len(report_check_events):,} selected flagged transfer(s); {len(catalog_match_counts):,} found a match.",
            "",
            "WHAT THIS REPORT COVERS",
            "This report uses local ARGUS transactions, alert reasons, synthetic profiles, synthetic relationships, and fictional demo case records. No external bank, police, court, employment, sanctions, or government sources were checked. Missing information means unknown, not cleared. A review score or fictional case record is not proof of wrongdoing.",
            f"The {len(PATTERN_CATALOG)} catalogue check results below apply only to the selected account's alert-linked transfers included in this report. 'No match' does not mean the account or its other activity is cleared. Device, location, invoice, profile, relationship, and case values in this demo are fictional.",
            "",
            "ACCOUNT BACKGROUND AND ACTIVITY",
            "GENERATED TRANSACTION CONTEXT",
            "Device, region, channel, purpose, and invoice fields below are fictional demo values and are used only as review signals.",
        ]
        for item in report_transfer_rows:
            context_bits = [
                f"device {item['Demo device']}" if item["Demo device"] else "",
                f"origin {item['Demo origin']}" if item["Demo origin"] else "",
                f"channel {item['Demo channel']}" if item["Demo channel"] else "",
                f"purpose {item['Demo purpose']}" if item["Demo purpose"] else "",
                f"merchant category {item['Demo merchant category']}" if item["Demo merchant category"] else "",
                f"invoice {item['Demo invoice']}" if item["Demo invoice"] else "",
            ]
            if any(context_bits):
                report_lines.append(f"- {item['Transaction ID']}: " + "; ".join(bit for bit in context_bits if bit))
        for account_id in context_account_ids:
            profile = profiles.get(account_id, {})
            stats = flow_stats.get(account_id, {})
            case_records = cases_by_account.get(account_id, [])
            role = profile.get("occupation") or profile.get("entity_kind") or "not listed"
            industry = profile.get("industry") or "not listed"
            profile_source = profile.get("profile_source") or "no profile record"
            report_lines.append(
                f"- {profile.get('entity_name') or report_names.get(account_id, account_id)} ({account_id}): {role}; {industry}; profile source: {profile_source}. "
                f"Stored history: {int(stats.get('incoming_count') or 0):,} incoming transfers totaling INR {float(stats.get('incoming_total') or 0):,.2f} from {int(stats.get('incoming_contacts') or 0):,} accounts; "
                f"{int(stats.get('outgoing_count') or 0):,} outgoing transfers totaling INR {float(stats.get('outgoing_total') or 0):,.2f} to {int(stats.get('outgoing_contacts') or 0):,} accounts; "
                f"{len(case_records)} fictional case record(s)."
            )
            if profile:
                report_lines.append(
                    f"  Work status: {profile.get('employment_status') or 'not listed'}; location: {profile.get('city') or 'not listed'}; "
                    f"years active: {profile.get('years_active') if profile.get('years_active') is not None else 'not listed'}; "
                    f"income/turnover band: {profile.get('income_or_turnover_band') or 'not listed'}; "
                    f"employer account: {profile.get('employer_account_id') or 'not listed'} ({profile.get('employer_name') or 'name unavailable'}); "
                    f"business: {profile.get('business_name') or 'not listed'}; demo registration: {profile.get('business_registration_status') or 'not listed'}."
                )
            for relationship in relationships_by_account.get(account_id, []):
                    other_id = str(relationship.get("related_account_id") if str(relationship.get("account_id")) == account_id else relationship.get("account_id"))
                    other_name = relationship.get("related_name") if str(relationship.get("account_id")) == account_id else relationship.get("account_name")
                    report_lines.append(f"  Declared demo link: {relationship.get('relationship_type')} with {other_name or other_id} ({other_id}); source {relationship.get('relationship_source') or 'not stated'}.")
            for case_record in case_records:
                report_lines.append(f"  Fictional case record: {case_record.get('case_category')} · {case_record.get('case_status')} · {case_record.get('outcome_summary')} · source {case_record.get('record_source')}.")

        report_lines.extend(["", "OTHER ACCOUNTS AND THEIR TRANSFERS"])
        root_contacts = {
            str(link.get("Receiver") if str(link.get("Sender")) == str(selected_account) else link.get("Sender"))
            for link in branch_links_by_account.get(str(selected_account), [])
        }
        for account_id in context_account_ids:
            if account_id == str(selected_account):
                continue
            profile = profiles.get(account_id, {})
            stats = flow_stats.get(account_id, {})
            adjacent = branch_links_by_account.get(account_id, [])
            contacts = {
                str(link.get("Receiver") if str(link.get("Sender")) == account_id else link.get("Sender"))
                for link in adjacent
            } - {account_id}
            shared_contacts = contacts & root_contacts
            case_count = len(cases_by_account.get(account_id, []))
            report_lines.append(
                f"- Step {report_account_depth.get(account_id, '?')}: {profile.get('entity_name') or report_names.get(account_id, account_id)} ({account_id}), "
                f"{profile.get('occupation') or profile.get('entity_kind') or 'role not listed'} · {profile.get('industry') or 'industry not listed'}; "
                f"{len(contacts):,} connected transfer contact(s), {len(shared_contacts):,} shared directly with the selected account; "
                f"historical incoming/outgoing totals INR {float(stats.get('incoming_total') or 0):,.2f} / INR {float(stats.get('outgoing_total') or 0):,.2f}; "
                f"{case_count} fictional case record(s)."
            )
            if shared_contacts:
                report_lines.append("  Shared direct contact IDs: " + ", ".join(sorted(shared_contacts)) + ".")
            for link in adjacent:
                if str(link.get("Sender")) != account_id:
                    continue
                other_id = str(link.get("Receiver") if str(link.get("Sender")) == account_id else link.get("Sender"))
                other_profile = profiles.get(other_id, {})
                relation = "shared with the selected account" if other_id in root_contacts else ""
                alert_text = f"; {int(link.get('Alert count', 0) or 0)} linked alert(s), peak score {link.get('Peak score') or 'not available'}" if int(link.get("Alert count", 0) or 0) else "; no saved/live alert on this account pair"
                why = "; reasons: " + ", ".join(link.get("Reasons", [])[:3]) if link.get("Reasons") else ""
                report_lines.append(
                    f"  Outgoing transfer contact: {report_names.get(other_id, other_id)} ({other_id}) · "
                    f"{other_profile.get('occupation') or 'role not listed'} / {other_profile.get('industry') or 'industry not listed'}; "
                    f"{int(link.get('Transfers', 0) or 0):,} transfer(s), INR {float(link.get('Flow (INR)', 0) or 0):,.2f}{alert_text}{why}. {relation}"
                )
            account_relationships = relationships_by_account.get(account_id, [])
            for relationship in account_relationships:
                if str(relationship.get("account_id")) != account_id:
                    continue
                linked_id = str(relationship.get("related_account_id") if str(relationship.get("account_id")) == account_id else relationship.get("account_id"))
                linked_profile = profiles.get(linked_id, {})
                report_lines.append(
                    f"  Declared entity link: {relationship.get('relationship_type')} with {report_names.get(linked_id, linked_id)} ({linked_id}); "
                    f"linked role/industry: {linked_profile.get('occupation') or 'not listed'} / {linked_profile.get('industry') or 'not listed'}; "
                    f"link source: {relationship.get('relationship_source') or 'not stated'}; this link is demo data."
                )

        report_lines.extend(["", "FLAGGED TRANSFERS AND REASONS"])
        for index, item in enumerate(report_transfer_rows, start=1):
            report_lines.extend([
                f"{index}. {item['Time']} · {item['Transaction ID']}",
                f"   {item['From name']} ({item['From account']}) → {item['To name']} ({item['To account']}) · INR {item['Amount (INR)']:,.2f}",
                f"   Review score/level: {item['Risk score']} / {item['Risk level']} · alert status: {item['Alert status']}",
                f"   Reason for review: {_plain_alert_reason(item['Why flagged'])}",
            ])
            demo_bits = [
                f"device {item['Demo device']}" if item["Demo device"] else "",
                f"origin {item['Demo origin']}" if item["Demo origin"] else "",
                f"channel {item['Demo channel']}" if item["Demo channel"] else "",
                f"purpose {item['Demo purpose']}" if item["Demo purpose"] else "",
                f"merchant {item['Demo merchant category']}" if item["Demo merchant category"] else "",
                f"invoice {item['Demo invoice']}" if item["Demo invoice"] else "",
            ]
            if any(demo_bits):
                report_lines.append("   Fictional transaction context: " + "; ".join(bit for bit in demo_bits if bit))
            if item["Pattern evidence"]:
                report_lines.append("   Pattern evidence:\n" + item["Pattern evidence"])
            if item["Entity context"]:
                report_lines.append("   Account context: " + item["Entity context"])
            report_lines.append("")
        report_lines.extend(["", "PATTERN CHECKS THAT TRIGGERED"])
        if report_pattern_summary:
            for item in sorted(report_pattern_summary.values(), key=lambda value: (-value["Transfers with this pattern"], value["Pattern"])):
                report_lines.append(
                    f"- {item['Pattern']} · appeared on {item['Transfers with this pattern']:,} alerted transfer(s): {item['Evidence']}"
                )
        else:
            report_lines.append("- No structured pattern evidence or saved alert reason was available for these records.")
        report_lines.extend(["", f"RESULTS FROM {len(PATTERN_CATALOG)} EXTRA CHECKS"])
        for item in catalog_report_rows:
            report_lines.append(
                f"- {item['Code']} · {item['Pattern check']} · {item['Result on selected alerted transfers']} · {item['What was checked']}"
                + (f" · Evidence: {item['Example evidence']}" if item["Example evidence"] != "—" else "")
            )
        report_lines.extend([
            "REVIEW NOTE",
            "The score is a rule-based review signal, not a calibrated probability. Confirm transaction and entity details with authorized, reliable records before making decisions.",
        ])
        report_text = "\n".join(report_lines)

        focus_id = str(selected_account)
        focus_profile = profiles.get(focus_id, {})
        focus_name = focus_profile.get("entity_name") or report_names.get(focus_id, focus_id)
        focus_role = focus_profile.get("occupation") or focus_profile.get("entity_kind") or "Role not listed"
        focus_industry = focus_profile.get("industry") or "Industry not listed"
        focus_stats = flow_stats.get(focus_id, {})

        st.markdown(
            f'''<section class="argus-report-cover">
                <div class="argus-report-cover-kicker">ARGUS · ACCOUNT REVIEW REPORT</div>
                <h2>{html.escape(str(focus_name))}</h2>
                <div class="argus-report-cover-meta">Account {html.escape(focus_id)} <span>·</span> {html.escape(source_label)} <span>·</span> Prepared {html.escape(created_text)}</div>
                <div class="argus-report-cover-tag">DEMO REVIEW · NOT A FINDING OF WRONGDOING</div>
            </section>''',
            unsafe_allow_html=True,
        )

        metrics = st.columns(4)
        metrics[0].metric("Transfers flagged", f"{len(report_transfer_rows):,}")
        metrics[1].metric("Combined flagged value", f"₹{total_alert_amount:,.2f}")
        metrics[2].metric("Connected accounts checked", f"{max(0, len(context_account_ids) - 1):,}")
        metrics[3].metric("Highest review score", f"{top_score}/100")

        st.markdown("#### Summary")
        profile_source_text = "fictional demo profile" if focus_profile.get("profile_source") == "SYNTHETIC_DEMO" else "no profile available"
        st.write(
            f"The selected report account appears in **{len(report_transfer_rows):,} flagged transfer(s)** "
            f"with a combined value of **₹{total_alert_amount:,.2f}**. Its highest attached review score is **{top_score}/100**. "
            f"The available profile lists **{focus_role}** in **{focus_industry}** ({profile_source_text}). "
            f"ARGUS examined {len(report_branch_links):,} connected transfer link(s) across the selected branch depth of {report_depth} step(s), "
            f"and checked available background details for {max(0, len(context_account_ids) - 1):,} connected account(s)."
        )
        if report_limit_reached:
            st.warning(f"Branch review reached its limit of {max_report_branch_links:,} transfer links or {max_report_accounts:,} accounts. The downloadable report identifies this limit; it does not imply that unexamined branches are clear.")
        st.info("This is a review signal, not a finding of wrongdoing. Profiles and case records are fictional demo data; no outside bank, police, court, employment, or government records were checked.")

        st.markdown("#### Why the transfers were flagged")
        reason_items = list(dict.fromkeys(
            reason
            for item in report_transfer_rows
            for reason in _plain_reason_items(item.get("Why flagged") or "No reason text stored")
        ))
        if reason_items:
            for reason in reason_items[:12]:
                st.write("• " + reason)
            if len(reason_items) > 12:
                st.caption(f"{len(reason_items) - 12:,} additional unique reason(s) are included in the downloaded report.")
        else:
            st.write("No alert reason text is attached to the selected records.")

        st.markdown("#### What ARGUS noticed")
        if report_pattern_summary:
            pattern_display = pd.DataFrame(sorted(
                report_pattern_summary.values(),
                key=lambda value: (-value["Transfers with this pattern"], value["Pattern"]),
            )).rename(columns={
                "Pattern": "What ARGUS noticed",
                "Evidence": "Simple explanation",
                "Transfers with this pattern": "Flagged transfers with this sign",
            })
            pattern_display["What ARGUS noticed"] = pattern_display["What ARGUS noticed"].map(_plain_alert_reason)
            pattern_display["Simple explanation"] = pattern_display["Simple explanation"].map(_plain_alert_reason)
            st.dataframe(
                pattern_display,
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("No structured pattern evidence is attached to these alert records.")
        st.caption(
            f"ARGUS also checked {len(PATTERN_CATALOG)} possible signs against the {len(report_check_events):,} flagged transfer(s) listed here. "
            f"{len(catalog_match_counts):,} signs matched. A sign that did not match only describes these transfers; it does not clear other activity."
        )
        with st.expander(f"See all {len(PATTERN_CATALOG)} checks and results"):
            catalog_display = pd.DataFrame(catalog_report_rows).rename(columns={
                "Code": "Check ID",
                "Pattern check": "What ARGUS checked",
                "Result on selected alerted transfers": "Result for these flagged transfers",
                "What was checked": "What this means",
                "Example evidence": "Example details",
            })
            catalog_display = catalog_display.drop(columns=["Check ID"], errors="ignore")
            st.caption("‘Matched’ means the check found this sign in the selected flagged transfers. ‘No match’ means it did not find that sign in those transfers.")
            st.dataframe(catalog_display, use_container_width=True, hide_index=True)

        st.markdown("#### Selected account background")
        focus_cols = st.columns(4)
        focus_cols[0].markdown("**Profession / role**")
        focus_cols[0].write(focus_role)
        focus_cols[1].markdown("**Industry**")
        focus_cols[1].write(focus_industry)
        focus_cols[2].markdown("**Location**")
        focus_cols[2].write(focus_profile.get("city") or "Not listed")
        focus_cols[3].markdown("**Profile source**")
        focus_cols[3].write("Fictional demo" if focus_profile.get("profile_source") == "SYNTHETIC_DEMO" else "Not available")
        st.caption(
            f"Stored history: {int(focus_stats.get('incoming_count') or 0):,} incoming transfer(s) "
            f"(₹{float(focus_stats.get('incoming_total') or 0):,.2f}) from {int(focus_stats.get('incoming_contacts') or 0):,} account(s); "
            f"{int(focus_stats.get('outgoing_count') or 0):,} outgoing transfer(s) "
            f"(₹{float(focus_stats.get('outgoing_total') or 0):,.2f}) to {int(focus_stats.get('outgoing_contacts') or 0):,} account(s). "
            "Stored-history totals may cover a wider period than the selected alert period."
        )

        st.markdown("#### Connected transfer branches")
        branch_rows = []
        for branch in report_branch_links.values():
            sender = str(branch.get("Sender") or "")
            receiver = str(branch.get("Receiver") or "")
            if not sender or not receiver:
                continue
            sender_depth = report_account_depth.get(sender, report_depth + 1)
            receiver_depth = report_account_depth.get(receiver, report_depth + 1)
            branch_step = min(sender_depth if isinstance(sender_depth, int) else report_depth + 1,
                              receiver_depth if isinstance(receiver_depth, int) else report_depth + 1)
            branch_rows.append({
                "Step": f"{branch_step}",
                "Flow": f"{report_names.get(sender, sender)} ({sender}) → {report_names.get(receiver, receiver)} ({receiver})",
                "Transfers": int(branch.get("Transfers", 0) or 0),
                "Total value (INR)": float(branch.get("Flow (INR)", 0) or 0),
                "Alert count": int(branch.get("Alert count", 0) or 0),
                "Peak score": branch.get("Peak score", "—"),
                "Latest transfer": str(branch.get("Last seen") or "Not available"),
                "Reason": "; ".join(branch.get("Reasons", [])[:2]) if isinstance(branch.get("Reasons"), list) else str(branch.get("Reasons") or ""),
            })
        branch_df = pd.DataFrame(branch_rows)
        # Build a compact, self-contained SVG so the report image works offline
        # in the downloadable HTML without external services or image packages.
        map_width, map_height = 1200, 960
        map_cx, map_cy = map_width / 2, 450
        ordered_map_edges = sorted(
            [dict(branch) for branch in report_branch_links.values()
             if branch.get("Sender") and branch.get("Receiver")],
            key=lambda edge: (
                0 if focus_id in {str(edge.get("Sender")), str(edge.get("Receiver"))} else 1,
                -int(edge.get("Alert count", 0) or 0),
                -float(edge.get("Flow (INR)", 0) or 0),
                str(edge.get("Sender")), str(edge.get("Receiver")),
            ),
        )
        map_edges = []
        map_account_ids = {focus_id}
        for edge in ordered_map_edges:
            endpoints = {str(edge["Sender"]), str(edge["Receiver"])}
            if len(map_account_ids | endpoints) > 33:
                continue
            map_edges.append(edge)
            map_account_ids.update(endpoints)
        ring_accounts = sorted(
            map_account_ids - {focus_id},
            key=lambda account_id: (
                report_account_depth.get(account_id, report_depth + 1)
                if isinstance(report_account_depth.get(account_id, report_depth + 1), int)
                else report_depth + 1,
                account_id,
            ),
        )
        map_positions = {focus_id: (map_cx, map_cy)}
        for offset, account_id in enumerate(ring_accounts):
            ring = offset // 11
            within_ring = offset % 11
            ring_size = min(11, len(ring_accounts) - ring * 11)
            angle = (-math.pi / 2) + (2 * math.pi * within_ring / max(1, ring_size))
            radius = 185 + ring * 115
            map_positions[account_id] = (
                map_cx + radius * math.cos(angle),
                map_cy + radius * math.sin(angle),
            )

        svg_parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{map_width}" height="{map_height}" viewBox="0 0 {map_width} {map_height}" style="width:100%;height:auto" role="img" aria-label="Transfer map centered on {html.escape(focus_id, quote=True)}">',
            '<defs><marker id="argus-arrow" markerWidth="9" markerHeight="9" refX="7" refY="4.5" orient="auto"><path d="M0,0 L9,4.5 L0,9 z" fill="#86a7bd"/></marker></defs>',
            '<rect width="100%" height="100%" rx="20" fill="#0b1522"/>',
            '<text x="28" y="38" fill="#9bb2c6" font-family="Arial,sans-serif" font-size="13" font-weight="700" letter-spacing="1.4">TRANSFER FLOWS · ARROWS SHOW MONEY DIRECTION</text>',
        ]
        for edge in map_edges:
            sender, receiver = str(edge["Sender"]), str(edge["Receiver"])
            if sender not in map_positions or receiver not in map_positions:
                continue
            x1, y1 = map_positions[sender]
            x2, y2 = map_positions[receiver]
            color = "#f1b56e" if receiver == focus_id else "#5edacb" if sender == focus_id else "#829bb1"
            width = min(4.0, 1.5 + math.log1p(max(0, int(edge.get("Transfers", 0) or 0))))
            edge_title = html.escape(
                f"{report_names.get(sender, sender)} to {report_names.get(receiver, receiver)} · {int(edge.get('Transfers', 0) or 0)} transfer(s) · INR {float(edge.get('Flow (INR)', 0) or 0):,.2f}",
                quote=True,
            )
            svg_parts.append(
                f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{color}" stroke-width="{width:.1f}" stroke-opacity=".74" marker-end="url(#argus-arrow)"><title>{edge_title}</title></line>'
            )
        for account_id, (x, y) in map_positions.items():
            is_focus = account_id == focus_id
            radius = 34 if is_focus else 20
            color = "#f1b56e" if is_focus else "#173d4a"
            border = "#ffe0a5" if is_focus else "#62dfd0"
            svg_parts.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius}" fill="{color}" stroke="{border}" stroke-width="2.5"><title>{html.escape(report_names.get(account_id, account_id))} · {html.escape(account_id)}</title></circle>'
            )
            label = "SELECTED SUSPECT" if is_focus else str(report_names.get(account_id, account_id))[:17]
            label_color = "#ffe8bf" if is_focus else "#dbe7f1"
            id_color = "#f7d9a8" if is_focus else "#89a5ba"
            svg_parts.append(
                f'<text x="{x:.1f}" y="{y + radius + 18:.1f}" text-anchor="middle" fill="{label_color}" font-family="Arial,sans-serif" font-size="{12 if is_focus else 11}" font-weight="700">{html.escape(label)}</text>'
                f'<text x="{x:.1f}" y="{y + radius + 33:.1f}" text-anchor="middle" fill="{id_color}" font-family="Arial,sans-serif" font-size="10">{html.escape(account_id[:22])}</text>'
            )
        svg_parts.extend([
            '<circle cx="30" cy="930" r="5" fill="#f1b56e"/><text x="42" y="934" fill="#b8c7d5" font-family="Arial,sans-serif" font-size="11">Money going into suspect</text>',
            '<circle cx="220" cy="930" r="5" fill="#5edacb"/><text x="232" y="934" fill="#b8c7d5" font-family="Arial,sans-serif" font-size="11">Money sent by suspect</text>',
            '<circle cx="405" cy="930" r="5" fill="#829bb1"/><text x="417" y="934" fill="#b8c7d5" font-family="Arial,sans-serif" font-size="11">Other connected transfer links</text>',
            '</svg>',
        ])
        report_map_svg = "".join(svg_parts)
        map_scope_note = (
            f"Map view shows {len(map_account_ids) - 1:,} associated account(s) and {len(map_edges):,} link(s). "
            f"The full examined network has {max(0, len(context_account_ids) - 1):,} associated account(s) and {len(report_branch_links):,} link(s); use the full report CSV bundle for every link."
        )

        st.markdown("#### Transfer map for this account")
        st.caption("The selected account is centered. Arrows point in the direction the money moved.")
        components.html(report_map_svg, height=960, scrolling=False)
        st.caption(map_scope_note)

        if branch_df.empty:
            st.info("No connected transfer branches were found for this account in the selected source and period.")
        else:
            branch_df = branch_df.sort_values(["Alert count", "Total value (INR)"], ascending=[False, False])
            st.dataframe(branch_df.head(20), use_container_width=True, hide_index=True)
            if len(branch_df) > 20:
                with st.expander(f"Show all {len(branch_df):,} transfer links"):
                    st.dataframe(branch_df, use_container_width=True, hide_index=True)

        st.markdown("#### Associated account checks")
        root_contacts = {
            str(link.get("Receiver") if str(link.get("Sender")) == focus_id else link.get("Sender"))
            for link in branch_links_by_account.get(focus_id, [])
        }
        associate_rows = []
        associate_detail_data = {}
        for account_id in context_account_ids:
            if account_id == focus_id:
                continue
            profile = profiles.get(account_id, {})
            account_links = branch_links_by_account.get(account_id, [])
            own_contacts = {
                str(link.get("Receiver") if str(link.get("Sender")) == account_id else link.get("Sender"))
                for link in account_links
            } - {account_id}
            shared_contacts = sorted(own_contacts & root_contacts)
            account_cases = cases_by_account.get(account_id, [])
            depth_value = report_account_depth.get(account_id, "Entity link")
            associate_rows.append({
                "Associated account": account_id,
                "Name": profile.get("entity_name") or report_names.get(account_id, account_id),
                "Role": profile.get("occupation") or profile.get("entity_kind") or "Not listed",
                "Industry": profile.get("industry") or "Not listed",
                "Location": profile.get("city") or "Not listed",
                "Connection": f"Transfer branch · step {depth_value}" if isinstance(depth_value, int) else str(depth_value),
                "Shared direct contacts": len(shared_contacts),
                "Declared links": len(relationships_by_account.get(account_id, [])),
                "Fictional case records": len(account_cases),
            })
            associate_detail_data[account_id] = (profile, shared_contacts, account_cases, account_links)
        if associate_rows:
            associate_df = pd.DataFrame(associate_rows)
            st.dataframe(associate_df, use_container_width=True, hide_index=True, height=min(360, 42 + 35 * min(len(associate_df), 9)))
            detailed_associate_ids = [account_id for account_id in context_account_ids if account_id != focus_id][:50]
            with st.expander("Open detailed checks for associated accounts"):
                st.caption(f"Detailed on-screen checks are shown for up to {len(detailed_associate_ids)} accounts to keep the report responsive. The full account and branch narrative is included in the downloadable report.")
                for account_id in detailed_associate_ids:
                    profile, shared_contacts, account_cases, account_links = associate_detail_data[account_id]
                    account_name = profile.get("entity_name") or report_names.get(account_id, account_id)
                    account_role = profile.get("occupation") or profile.get("entity_kind") or "Role not listed"
                    account_industry = profile.get("industry") or "Industry not listed"
                    with st.expander(f"{account_name} · {account_id} · {report_account_depth.get(account_id, 'Entity link')}"):
                        st.write(f"**Background:** {account_role} · {account_industry} · {profile.get('city') or 'Location not listed'} · work status {profile.get('employment_status') or 'not listed'} · years active {profile.get('years_active') if profile.get('years_active') is not None else 'not listed'} · income/turnover {profile.get('income_or_turnover_band') or 'not listed'}.")
                        st.caption("Fictional demo profile; not independently verified.")
                        if shared_contacts:
                            st.write("**Contacts shared directly with the report account:** " + ", ".join(shared_contacts[:30]) + (" …" if len(shared_contacts) > 30 else ""))
                        else:
                            st.write("**Matching contacts:** no shared direct contact was found in the branch records examined.")
                        relation_items = relationships_by_account.get(account_id, [])
                        if relation_items:
                            for item in relation_items:
                                other_id = str(item.get("related_account_id") if str(item.get("account_id")) == account_id else item.get("account_id"))
                                other_name = item.get("related_name") if str(item.get("account_id")) == account_id else item.get("account_name")
                                st.write(f"**Declared link:** {str(item.get('relationship_type') or 'related').replace('_', ' ').lower()} · {other_name or report_names.get(other_id, other_id)} ({other_id}); source {item.get('relationship_source') or 'not listed'}.")
                        else:
                            st.write("**Declared links:** none found in available records.")
                        if account_cases:
                            for case in account_cases:
                                st.write(f"**Fictional case record:** {case.get('case_category')} · {case.get('case_status')} · {case.get('outcome_summary')}.")
                        else:
                            st.write("**Fictional case records:** none stored for this account. This is not an external background check.")
        else:
            st.info("No associated accounts were found in the selected branch depth.")

        st.markdown("#### Alerted transfer evidence")
        evidence_df = pd.DataFrame(report_transfer_rows)
        if evidence_df.empty:
            st.info("No transaction rows are available to show.")
        else:
            evidence_columns = ["Time", "Transaction ID", "From name", "From account", "To name", "To account", "Amount (INR)", "Risk score", "Risk level", "Why flagged", "Demo device", "Demo origin", "Demo channel", "Demo purpose", "Demo merchant category", "Demo invoice"]
            with st.expander(f"Review {len(evidence_df):,} flagged transfer(s)", expanded=False):
                st.dataframe(evidence_df[evidence_columns], use_container_width=True, hide_index=True, height=420)
                pattern_details = [
                    {"Transaction ID": item["Transaction ID"], "Pattern evidence": item["Pattern evidence"], "Account context": item["Entity context"]}
                    for item in report_transfer_rows if item["Pattern evidence"] or item["Entity context"]
                ]
                if pattern_details:
                    st.markdown("**Pattern and entity context**")
                    st.dataframe(pd.DataFrame(pattern_details), use_container_width=True, hide_index=True)

        st.markdown("#### Download or share this report")
        safe_account = re.sub(r"[^A-Za-z0-9_-]", "_", selected_account)
        source_key = source_label.lower().replace(" ", "_")
        report_meta = {
            "Report generated": created_text,
            "Alert source": source_label,
            "Selected account ID": focus_id,
            "Selected account name": focus_name,
        }

        def make_report_csv(rows, fields):
            csv_columns = list(report_meta) + list(fields)
            csv_rows = [{**report_meta, **row} for row in rows]
            csv_text = pd.DataFrame(csv_rows, columns=csv_columns).to_csv(
                index=False,
                na_rep="",
                float_format="%.2f",
                lineterminator="\r\n",
            )
            return csv_text.encode("utf-8-sig")

        def csv_number(value):
            try:
                return float(value)
            except (TypeError, ValueError):
                return ""

        transfer_csv_fields = [
            "Transaction ID", "Transaction time", "From account ID", "From account name",
            "To account ID", "To account name", "Amount INR", "Risk score", "Risk level",
            "Alert status", "Reason", "Pattern evidence", "Entity context", "Demo device",
            "Demo region", "Demo channel", "Demo purpose", "Demo merchant category", "Demo invoice reference",
        ]
        transfer_csv_rows = [{
            "Transaction ID": row["Transaction ID"], "Transaction time": row["Time"],
            "From account ID": row["From account"], "From account name": row["From name"],
            "To account ID": row["To account"], "To account name": row["To name"],
            "Amount INR": csv_number(row["Amount (INR)" ]), "Risk score": csv_number(row["Risk score"]),
            "Risk level": row["Risk level"], "Alert status": row["Alert status"],
            "Reason": row["Why flagged"], "Pattern evidence": row["Pattern evidence"],
            "Entity context": row["Entity context"], "Demo device": row["Demo device"],
            "Demo region": row["Demo origin"], "Demo channel": row["Demo channel"],
            "Demo purpose": row["Demo purpose"], "Demo merchant category": row["Demo merchant category"],
            "Demo invoice reference": row["Demo invoice"],
        } for row in report_transfer_rows]
        transfer_csv = make_report_csv(transfer_csv_rows, transfer_csv_fields)

        branch_csv_fields = [
            "Step from selected account", "From account ID", "From account name", "To account ID",
            "To account name", "Transfer count", "Total value INR", "Linked alert count",
            "Peak risk score", "Latest transfer", "Alert reasons",
        ]
        branch_csv_rows = []
        for branch in report_branch_links.values():
            sender, receiver = str(branch.get("Sender") or ""), str(branch.get("Receiver") or "")
            depths = [report_account_depth.get(node) for node in (sender, receiver)]
            valid_depths = [depth for depth in depths if isinstance(depth, int)]
            step_number = min(valid_depths) if valid_depths else "Entity link"
            branch_csv_rows.append({
                "Step from selected account": step_number,
                "From account ID": sender, "From account name": report_names.get(sender, sender),
                "To account ID": receiver, "To account name": report_names.get(receiver, receiver),
                "Transfer count": int(branch.get("Transfers", 0) or 0),
                "Total value INR": csv_number(branch.get("Flow (INR)", 0)),
                "Linked alert count": int(branch.get("Alert count", 0) or 0),
                "Peak risk score": csv_number(branch.get("Peak score")),
                "Latest transfer": branch.get("Last seen") or "",
                "Alert reasons": "; ".join(branch.get("Reasons", [])) if isinstance(branch.get("Reasons"), list) else str(branch.get("Reasons") or ""),
            })
        branch_csv = make_report_csv(branch_csv_rows, branch_csv_fields)

        account_csv_fields = [
            "Account ID", "Account name", "Relationship to selected account", "Profession or role",
            "Industry", "Work status", "Location", "Years active", "Income or turnover band",
            "Employer account ID", "Employer name", "Business name", "Profile source",
            "Incoming transfer count", "Incoming total INR", "Outgoing transfer count", "Outgoing total INR",
            "Shared direct contacts", "Declared demo relationships", "Fictional case records",
        ]
        account_csv_rows = []
        for account_id in context_account_ids:
            if account_id == focus_id:
                continue
            profile = profiles.get(account_id, {})
            stats = flow_stats.get(account_id, {})
            direct_contacts = {
                str(link.get("Receiver") if str(link.get("Sender")) == account_id else link.get("Sender"))
                for link in branch_links_by_account.get(account_id, [])
            } - {account_id}
            shared = sorted(direct_contacts & root_contacts)
            relation_texts = []
            for relation in relationships_by_account.get(account_id, []):
                other_id = str(relation.get("related_account_id") if str(relation.get("account_id")) == account_id else relation.get("account_id"))
                other_name = relation.get("related_name") if str(relation.get("account_id")) == account_id else relation.get("account_name")
                relation_texts.append(f"{relation.get('relationship_type') or 'linked'}: {other_name or other_id} ({other_id})")
            cases = cases_by_account.get(account_id, [])
            account_csv_rows.append({
                "Account ID": account_id,
                "Account name": profile.get("entity_name") or report_names.get(account_id, account_id),
                "Relationship to selected account": f"Transfer depth {report_account_depth.get(account_id)}" if isinstance(report_account_depth.get(account_id), int) else str(report_account_depth.get(account_id) or "Entity relationship"),
                "Profession or role": profile.get("occupation") or profile.get("entity_kind") or "Not listed",
                "Industry": profile.get("industry") or "Not listed",
                "Work status": profile.get("employment_status") or "Not listed",
                "Location": profile.get("city") or "Not listed",
                "Years active": profile.get("years_active") if profile.get("years_active") is not None else "",
                "Income or turnover band": profile.get("income_or_turnover_band") or "Not listed",
                "Employer account ID": profile.get("employer_account_id") or "",
                "Employer name": profile.get("employer_name") or "",
                "Business name": profile.get("business_name") or "",
                "Profile source": profile.get("profile_source") or "No profile available",
                "Incoming transfer count": int(stats.get("incoming_count") or 0),
                "Incoming total INR": csv_number(stats.get("incoming_total", 0)),
                "Outgoing transfer count": int(stats.get("outgoing_count") or 0),
                "Outgoing total INR": csv_number(stats.get("outgoing_total", 0)),
                "Shared direct contacts": "; ".join(shared),
                "Declared demo relationships": "; ".join(relation_texts),
                "Fictional case records": "; ".join(
                    f"{case.get('case_category')} · {case.get('case_status')} · {case.get('outcome_summary')}"
                    for case in cases
                ),
            })
        account_csv = make_report_csv(account_csv_rows, account_csv_fields)
        pattern_csv_fields = ["Pattern code", "Pattern check", "Result", "What was checked", "Example evidence"]
        pattern_csv_rows = [{
            "Pattern code": item["Code"], "Pattern check": item["Pattern check"],
            "Result": item["Result on selected alerted transfers"],
            "What was checked": item["What was checked"], "Example evidence": item["Example evidence"],
        } for item in catalog_report_rows]
        pattern_csv = make_report_csv(pattern_csv_rows, pattern_csv_fields)

        # Format the same complete narrative into an offline-friendly, print-ready HTML report.
        html_body = []
        list_open = False
        report_headings = {
            "WHAT THIS REPORT COVERS", "ACCOUNT BACKGROUND AND ACTIVITY", "GENERATED TRANSACTION CONTEXT",
            "OTHER ACCOUNTS AND THEIR TRANSFERS", "FLAGGED TRANSFERS AND REASONS", "PATTERN CHECKS THAT TRIGGERED",
            f"RESULTS FROM {len(PATTERN_CATALOG)} EXTRA CHECKS", "REVIEW NOTE",
        }
        for line in report_lines[2:]:
            clean_line = str(line)
            if not clean_line.strip():
                if list_open:
                    html_body.append("</ul>")
                    list_open = False
                continue
            if clean_line in report_headings:
                if list_open:
                    html_body.append("</ul>")
                    list_open = False
                html_body.append(f"<h2>{html.escape(clean_line.title())}</h2>")
            elif clean_line.startswith("- "):
                if not list_open:
                    html_body.append('<ul class="report-list">')
                    list_open = True
                html_body.append(f"<li>{html.escape(clean_line[2:])}</li>")
            elif re.match(r"^\d+\.\s", clean_line):
                if list_open:
                    html_body.append("</ul>")
                    list_open = False
                html_body.append(f'<h3 class="transfer-finding">{html.escape(clean_line)}</h3>')
            else:
                if list_open:
                    html_body.append("</ul>")
                    list_open = False
                detail_class = ' class="report-detail"' if clean_line.startswith("  ") else ""
                html_body.append(f"<p{detail_class}>{html.escape(clean_line.strip()).replace(chr(10), '<br>')}</p>")
        if list_open:
            html_body.append("</ul>")
        summary_html = (
            f"The selected account appears in <strong>{len(report_transfer_rows):,} flagged transfer(s)</strong> "
            f"with a combined value of <strong>₹{total_alert_amount:,.2f}</strong>. The highest attached review score is "
            f"<strong>{top_score}/100</strong>. ARGUS examined <strong>{len(report_branch_links):,} transfer link(s)</strong> "
            f"and available background information for <strong>{max(0, len(context_account_ids) - 1):,} connected account(s)</strong>."
        )
        report_html = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>ARGUS Account Review · {html.escape(str(focus_name))}</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;background:#eef2f6;color:#182736;font:15px/1.6 "Segoe UI",Arial,sans-serif}}.sheet{{max-width:1100px;margin:28px auto;background:#fff;box-shadow:0 12px 36px #18273618;border-radius:16px;overflow:hidden}}header{{padding:34px 42px;background:linear-gradient(115deg,#102638,#153e4a);color:#f3f8fb}}.brand{{font-size:12px;letter-spacing:2px;font-weight:800;color:#7ce3d5}}h1{{margin:10px 0 4px;font-size:30px;line-height:1.2}}.account{{color:#c0d2df}}.tag{{display:inline-block;margin-top:18px;padding:6px 10px;border:1px solid #eabf7a;border-radius:20px;color:#ffdda7;font-size:11px;letter-spacing:.7px;font-weight:800}}main{{padding:30px 42px}}.meta{{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:20px}}.meta span{{padding:7px 11px;border-radius:8px;background:#f0f4f7;color:#526579;font-size:13px}}.metrics{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:18px 0 24px}}.metric{{padding:15px;border:1px solid #dfe7ed;border-radius:11px;background:#fbfcfd}}.metric b{{display:block;font-size:22px;color:#163b4b}}.metric small{{color:#65798a}}.summary{{padding:17px 19px;border-left:4px solid #39a99c;border-radius:8px;background:#eef8f6}}h2{{margin:30px 0 12px;padding-bottom:8px;border-bottom:1px solid #e3e9ee;color:#183a4b;font-size:19px}}h3{{color:#294c5c;font-size:15px}}p{{margin:8px 0}}ul{{padding-left:22px}}li{{padding:3px 0}}.report-detail{{margin-left:18px;color:#506578}}.map{{padding:8px;border:1px solid #dfe7ed;border-radius:12px;background:#0b1522}}.map svg{{display:block;width:100%;height:auto}}.map-note{{color:#607487;font-size:12px}}.transfer-finding{{margin-top:18px;padding:11px 13px;border-radius:8px;background:#f2f6f9}}.disclaimer{{padding:14px 16px;border:1px solid #f0d6ad;border-radius:8px;background:#fff8ed;color:#624d30}}footer{{margin-top:28px;padding:18px 42px;background:#f3f6f8;color:#637789;font-size:12px}}@media(max-width:700px){{.sheet{{margin:0;border-radius:0}}header,main,footer{{padding:22px}}.metrics{{grid-template-columns:repeat(2,1fr)}}}}
</style></head><body><article class="sheet">
<header><div class="brand">ARGUS · FINANCIAL INTELLIGENCE</div><h1>{html.escape(str(focus_name))}</h1><div class="account">Account {html.escape(focus_id)} · {html.escape(source_label)}</div><div class="tag">DEMO REVIEW · NOT A FINDING OF WRONGDOING</div></header>
<main><div class="meta"><span>Prepared {html.escape(created_text)}</span><span>Role: {html.escape(str(focus_role))}</span><span>Industry: {html.escape(str(focus_industry))}</span></div>
<div class="summary">{summary_html}</div><div class="metrics"><div class="metric"><b>{len(report_transfer_rows):,}</b><small>Transfers flagged</small></div><div class="metric"><b>₹{total_alert_amount:,.2f}</b><small>Total value flagged</small></div><div class="metric"><b>{max(0, len(context_account_ids) - 1):,}</b><small>Connected accounts checked</small></div><div class="metric"><b>{top_score}/100</b><small>Highest review score</small></div></div>
<h2>Transfer map around the selected account</h2><div class="map">{report_map_svg}</div><p class="map-note">{html.escape(map_scope_note)} Arrows show money direction; orange indicates money entering the suspect account and teal indicates money leaving it.</p>
<div class="disclaimer">This is a review aid, not a finding of wrongdoing. Profiles, relationships, and case records are fictional demo data. No outside bank, police, court, employment, or government records were checked. Missing information means unknown.</div>
{''.join(html_body)}</main><footer>ARGUS offline demo · Generated {html.escape(created_text)} · Scores are rule-based review signals, not calibrated probabilities.</footer></article></body></html>'''
        html_bytes = report_html.encode("utf-8")

        bundle_buffer = io.BytesIO()
        with zipfile.ZipFile(bundle_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr(f"ARGUS_Report_{safe_account}.html", html_bytes)
            bundle.writestr("alerted_transfers.csv", transfer_csv)
            bundle.writestr("connected_branches.csv", branch_csv)
            bundle.writestr("associated_accounts.csv", account_csv)
            bundle.writestr("pattern_check_results.csv", pattern_csv)
        full_bundle = bundle_buffer.getvalue()

        st.caption("The HTML report includes the transfer map image. The CSV exports use consistent columns, numeric amounts/scores, UTF-8 encoding, and no spreadsheet index. The complete bundle includes transfers, branches, associated accounts, and pattern-check results.")
        download_cols = st.columns(4)
        download_cols[0].download_button("Download report · HTML", html_bytes, file_name=f"ARGUS_Report_{safe_account}.html", mime="text/html", key=f"report_html_{source_key}")
        download_cols[1].download_button("Alerted transfers · CSV", transfer_csv, file_name=f"ARGUS_Transfers_{safe_account}.csv", mime="text/csv", key=f"report_transfers_csv_{source_key}")
        download_cols[2].download_button("Transfer branches · CSV", branch_csv, file_name=f"ARGUS_Branches_{safe_account}.csv", mime="text/csv", key=f"report_branches_csv_{source_key}")
        download_cols[3].download_button("Full report data · ZIP", full_bundle, file_name=f"ARGUS_Report_Data_{safe_account}.zip", mime="application/zip", key=f"report_bundle_{source_key}")
        bank_col, police_col, _ = st.columns([1, 1, 2])
        if bank_col.button("Send to bank · demo", key=f"report_bank_{source_key}"):
            st.info("Demo only: no report was sent, uploaded, or saved outside ARGUS.")
        if police_col.button("Send to police · demo", key=f"report_police_{source_key}"):
            st.info("Demo only: no report was sent, uploaded, or saved outside ARGUS.")

    if report_source_choice == "Live session alerts":
        render_alert_report("Live simulation", live_report_events, live_source=True)
    else:
        render_alert_report("Saved historical", saved_report_events, live_source=False)





# --------------------------------------------------

# FOOTER

# --------------------------------------------------



st.divider()



st.caption(

    "ARGUS • Offline Financial Intelligence • "

    "Scalable Development Build 0.4"

)



st.caption(

    "Investigative aid only: unusual activity requires human review "

    "and does not establish wrongdoing."

)



