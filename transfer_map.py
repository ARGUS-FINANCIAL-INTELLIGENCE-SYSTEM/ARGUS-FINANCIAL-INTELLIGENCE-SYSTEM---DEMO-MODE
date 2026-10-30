
"""ARGUS Transfer Map data access.

Historical transaction queries only.
Simulator events are not treated as historical transfers.
No database writes are performed by this module.
"""

import pandas as pd
import streamlit as st

from database import get_connection


DEFAULT_LIMIT = 1000
MAX_LIMIT = 5000
MAX_BRANCH_DEPTH = 10
MAX_BRANCH_EDGES = 5000
DEFAULT_ACCOUNT_SEARCH_LIMIT = 100


TRANSFER_COLUMNS = [
    "transaction_id",
    "timestamp",
    "sender_id",
    "sender_name",
    "sender_type",
    "receiver_id",
    "receiver_name",
    "receiver_type",
    "amount",
    "currency",
    "description",
    "is_flagged",
    "alert_reasons",
    "alert_score",
]


def _bounded_int(value, minimum, maximum, default):
    """Convert an input to an integer within safe bounds."""
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default

    return max(minimum, min(value, maximum))


def _alert_join_sql():
    """Return indexed, per-transaction alert lookups without scanning all alerts."""
    # This compatibility helper is retained for callers. Alert values are now
    # selected with indexed correlated lookups in _transfer_select_sql.
    return ""


def _transfer_select_sql():
    """Shared historical transaction columns and alert status."""
    return """
        SELECT
            t.transaction_id AS transaction_id,
            t.timestamp AS timestamp,
            t.sender_id AS sender_id,
            s.entity_name AS sender_name,
            s.account_type AS sender_type,
            t.receiver_id AS receiver_id,
            r.entity_name AS receiver_name,
            r.account_type AS receiver_type,
            t.amount AS amount,
            t.currency AS currency,
            t.description AS description,

            CASE WHEN EXISTS (
                SELECT 1 FROM alerts AS flagged
                WHERE flagged.transaction_id = t.transaction_id
            ) THEN 1 ELSE 0 END AS is_flagged,

            COALESCE((
                SELECT GROUP_CONCAT(DISTINCT linked.reason)
                FROM alerts AS linked
                WHERE linked.transaction_id = t.transaction_id
            ), '') AS alert_reasons,
            (
                SELECT MAX(linked.risk_score)
                FROM alerts AS linked
                WHERE linked.transaction_id = t.transaction_id
            ) AS alert_score

        FROM transactions AS t

        LEFT JOIN accounts AS s
            ON s.account_id = t.sender_id

        LEFT JOIN accounts AS r
            ON r.account_id = t.receiver_id

    """


@st.cache_data(ttl=45, show_spinner=False)
def load_transfer_network(
    search_term="",
    start_time=None,
    end_time=None,
    limit=DEFAULT_LIMIT,
):
    """Load a bounded set of historical transfers.

    Search can match sender/receiver IDs or entity names.
    The result includes transaction-linked alert status.
    """
    limit = _bounded_int(limit, 1, MAX_LIMIT, DEFAULT_LIMIT)
    search_term = (search_term or "").strip()

    conditions = []
    params = []

    if start_time:
        conditions.append("t.timestamp >= ?")
        params.append(start_time)

    if end_time:
        conditions.append("t.timestamp <= ?")
        params.append(end_time)

    if search_term:
        conditions.append(
            """
            (
                t.sender_id LIKE ?
                OR t.receiver_id LIKE ?
                OR s.entity_name LIKE ?
                OR r.entity_name LIKE ?
            )
            """
        )
        pattern = "%" + search_term + "%"
        params.extend([pattern, pattern, pattern, pattern])

    where_sql = (
        "WHERE " + " AND ".join(conditions)
        if conditions
        else ""
    )

    query = (
        _transfer_select_sql()
        + "\n"
        + where_sql
        + """
        ORDER BY t.timestamp DESC, t.transaction_id DESC
        LIMIT ?
        """
    )

    params.append(limit)

    with get_connection() as conn:
        return pd.read_sql_query(query, conn, params=params)


@st.cache_data(ttl=45, show_spinner=False)
def search_accounts(search_term, limit=DEFAULT_ACCOUNT_SEARCH_LIMIT):
    """Search accounts across the database, not just visible map nodes.

    This supports searching a large account table without attempting
    to render every account in one graph.
    """
    search_term = (search_term or "").strip()
    limit = _bounded_int(
        limit, 1, MAX_LIMIT, DEFAULT_ACCOUNT_SEARCH_LIMIT
    )

    if not search_term:
        return pd.DataFrame(
            columns=[
                "account_id",
                "entity_name",
                "account_type",
            ]
        )

    pattern = "%" + search_term + "%"

    query = """
        SELECT
            account_id,
            entity_name,
            account_type
        FROM accounts
        WHERE account_id LIKE ?
           OR entity_name LIKE ?
           OR account_type LIKE ?
        ORDER BY
            CASE WHEN account_id = ? THEN 0 ELSE 1 END,
            entity_name,
            account_id
        LIMIT ?
    """

    with get_connection() as conn:
        return pd.read_sql_query(
            query,
            conn,
            params=[
                pattern,
                pattern,
                pattern,
                search_term,
                limit,
            ],
        )


def summarize_network(transfers):
    """Return one row per visible account with activity summaries."""
    columns = [
        "account_id",
        "account_name",
        "account_type",
        "outgoing_count",
        "incoming_count",
        "distinct_recipients",
        "distinct_senders",
        "outgoing_amount",
        "incoming_amount",
    ]

    if transfers.empty:
        return pd.DataFrame(columns=columns)

    outgoing = transfers.groupby("sender_id").agg(
        outgoing_count=("transaction_id", "count"),
        distinct_recipients=("receiver_id", "nunique"),
        outgoing_amount=("amount", "sum"),
    )

    incoming = transfers.groupby("receiver_id").agg(
        incoming_count=("transaction_id", "count"),
        distinct_senders=("sender_id", "nunique"),
        incoming_amount=("amount", "sum"),
    )

    accounts = {}

    for row in transfers.itertuples(index=False):
        accounts[row.sender_id] = (
            row.sender_name or row.sender_id,
            row.sender_type or "unknown",
        )
        accounts[row.receiver_id] = (
            row.receiver_name or row.receiver_id,
            row.receiver_type or "unknown",
        )

    result = pd.DataFrame(index=sorted(accounts))
    result.index.name = "account_id"

    result["account_name"] = [
        accounts[account_id][0] for account_id in result.index
    ]
    result["account_type"] = [
        accounts[account_id][1] for account_id in result.index
    ]

    result = result.join(outgoing, how="left")
    result = result.join(incoming, how="left")

    numeric_types = {
        "outgoing_count": int,
        "incoming_count": int,
        "distinct_recipients": int,
        "distinct_senders": int,
        "outgoing_amount": float,
        "incoming_amount": float,
    }

    numeric_columns = list(numeric_types.keys())
    result[numeric_columns] = result[numeric_columns].fillna(0)
    result[numeric_columns] = result[numeric_columns].astype(
        numeric_types
    )

    return result.reset_index()[columns]


@st.cache_data(ttl=45, show_spinner=False)
def load_account_branches(
    account_id,
    depth=3,
    max_edges=300,
):
    """Trace historical transfers connected to an account.

    Expands outward for up to `depth` hops and returns at most
    `max_edges` distinct transactions.

    This is a bounded investigation view, not a complete network
    export. It performs no database writes.
    """
    account_id = (account_id or "").strip()

    if not account_id:
        return pd.DataFrame(columns=TRANSFER_COLUMNS)

    depth = _bounded_int(depth, 1, MAX_BRANCH_DEPTH, 3)
    max_edges = _bounded_int(
        max_edges, 1, MAX_BRANCH_EDGES, 300
    )

    discovered_edges = set()
    visited = {account_id}
    frontier = {account_id}

    with get_connection() as conn:
        for _ in range(depth):
            if not frontier or len(discovered_edges) >= max_edges:
                break

            ids = sorted(frontier)
            next_frontier = set()
            # Stay below SQLite's bound parameter limit even when a large
            # branch produces many accounts at the next step.
            for start in range(0, len(ids), 300):
                chunk = ids[start:start + 300]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    """
                    SELECT transaction_id, sender_id, receiver_id
                    FROM transactions
                    WHERE sender_id IN (""" + placeholders + """)
                       OR receiver_id IN (""" + placeholders + """)
                    ORDER BY timestamp DESC, transaction_id DESC
                    LIMIT ?
                    """,
                    chunk + chunk + [max_edges],
                ).fetchall()

                for row in rows:
                    if row["transaction_id"] not in discovered_edges:
                        if len(discovered_edges) >= max_edges:
                            break
                        discovered_edges.add(row["transaction_id"])

                    for node in (row["sender_id"], row["receiver_id"]):
                        if node not in visited:
                            visited.add(node)
                            next_frontier.add(node)

                if len(discovered_edges) >= max_edges:
                    break

            frontier = next_frontier

        if not discovered_edges:
            return pd.DataFrame(columns=TRANSFER_COLUMNS)

        # Keep each IN list below SQLite's bound-parameter limit. Large
        # branch searches can contain thousands of distinct transactions.
        edge_ids = sorted(discovered_edges)
        frames = []
        for start in range(0, len(edge_ids), 500):
            chunk = edge_ids[start:start + 500]
            placeholders = ",".join("?" for _ in chunk)
            query = (
                _transfer_select_sql()
                + """
                WHERE t.transaction_id IN ("""
                + placeholders
                + """)
                ORDER BY t.timestamp DESC, t.transaction_id DESC
                """
            )
            frames.append(pd.read_sql_query(query, conn, params=chunk))

        result = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=TRANSFER_COLUMNS)
        if not result.empty and "timestamp" in result:
            result = result.sort_values(["timestamp", "transaction_id"], ascending=[False, False])
        return result.reset_index(drop=True)
