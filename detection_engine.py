

"""ARGUS temporal and relationship risk engine.

Live simulation scores and entity context are computed in memory. Scores are
heuristic review signals, not probabilities or proof of fraud. Synthetic case
records never change transaction risk scores.
"""

from __future__ import annotations



from collections import Counter, defaultdict
from bisect import bisect_left, bisect_right

from datetime import datetime, timezone
from statistics import median

import uuid
import sqlite3
import random



from database import get_connection
from pattern_catalog import evaluate_pattern_checks



ALERT_THRESHOLD = 20
REVIEW_THRESHOLD = 35

HISTORY_LIMIT = 10000



WINDOWS_SECONDS = {

    "10-second": 10,

    "1-minute": 60,

    "1-hour": 3600,

    "24-hour": 86400,

    "7-day": 604800,

    "30-day": 2592000,

}

# Rule weights are deliberately bounded review signals. They are not
# calibrated probabilities and should be validated against labeled data before
# operational use.
MAX_PATTERN_POINTS = 70
PATTERN_SCORE_CAP = 100





def _risk_level(score: int) -> str:

    if score >= 80:

        return "URGENT REVIEW"

    if score >= 65:

        return "HIGH"

    if score >= REVIEW_THRESHOLD:

        return "REVIEW"

    if score >= 20:

        return "WATCH"

    return "LOW"





def _amount(event: dict) -> float:

    try:

        return max(0.0, float(event.get("Amount (INR)", 0) or 0))

    except (TypeError, ValueError):

        return 0.0





def _parse_time(event: dict):

    """Return a UTC Unix timestamp, or None when unavailable."""

    value = next(

        (

            event.get(key)

            for key in (

                "Timestamp", "timestamp", "Date", "datetime", "Created At"

            )

            if event.get(key) not in (None, "")

        ),

        None,

    )

    if value is None:

        return None



    if isinstance(value, datetime):

        parsed = value

    else:

        try:

            parsed = datetime.fromisoformat(

                str(value).strip().replace("Z", "+00:00")

            )

        except (ValueError, TypeError):

            parsed = None

            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):

                try:

                    parsed = datetime.strptime(str(value), fmt)

                    break

                except ValueError:

                    continue

            if parsed is None:

                return None



    if parsed.tzinfo is None:

        parsed = parsed.replace(tzinfo=timezone.utc)



    return parsed.astimezone(timezone.utc).timestamp()





def _graph(events):

    outgoing = defaultdict(set)

    incoming = defaultdict(set)

    pairs = Counter()



    for event in events:

        sender = event.get("Sender")

        receiver = event.get("Receiver")

        if sender and receiver:

            outgoing[sender].add(receiver)

            incoming[receiver].add(sender)

            pairs[(sender, receiver)] += 1



    return outgoing, incoming, pairs





def _has_path(start, target, outgoing, max_hops=3):

    """Find a short directed path without revisiting nodes."""

    if not start or not target:

        return False



    frontier = {start}

    visited = set()



    for _ in range(max_hops):

        if target in frontier:

            return True



        visited |= frontier

        frontier = {

            neighbor

            for node in frontier

            for neighbor in outgoing.get(node, ())

            if neighbor not in visited

        }



    return target in frontier





def _temporal_evidence(events):

    """Build event-time-aware, group-level temporal evidence.



    A two-pointer scan evaluates rolling windows after sorting each node's

    transfers by timestamp. Every event in a qualifying fan-in/fan-out group

    receives supporting evidence, regardless of input order.

    """

    reasons = defaultdict(set)

    timed = [

        (index, _parse_time(event), event)

        for index, event in enumerate(events)

    ]

    timed = [

        item for item in timed

        if item[1] is not None

        and item[2].get("Sender")

        and item[2].get("Receiver")

    ]



    incoming = defaultdict(list)

    outgoing = defaultdict(list)

    pair_rows = defaultdict(list)



    for index, timestamp, event in timed:

        sender = event["Sender"]

        receiver = event["Receiver"]



        outgoing[sender].append((timestamp, index, event))

        incoming[receiver].append((timestamp, index, event))

        pair_rows[(sender, receiver)].append((timestamp, index, event))



    # Group-level fan-in and fan-out.

    for groups, direction in (

        (incoming, "fan-in"),

        (outgoing, "fan-out"),

    ):

        for _, rows in groups.items():

            rows.sort(key=lambda row: (row[0], row[1]))



            for label, seconds in WINDOWS_SECONDS.items():

                left = 0

                counts = Counter()



                for right, (timestamp, _, event) in enumerate(rows):

                    identity = (

                        event["Sender"]

                        if direction == "fan-in"

                        else event["Receiver"]

                    )

                    counts[identity] += 1



                    while rows[left][0] < timestamp - seconds:

                        old_event = rows[left][2]

                        old_identity = (

                            old_event["Sender"]

                            if direction == "fan-in"

                            else old_event["Receiver"]

                        )

                        counts[old_identity] -= 1

                        if counts[old_identity] <= 0:

                            del counts[old_identity]

                        left += 1



                    # Five distinct counterparties is a heuristic trigger,

                    # not a conclusion that the activity is fraudulent.

                    if len(counts) >= 5:

                        count = len(counts)

                        counterparties = (

                            "senders" if direction == "fan-in"

                            else "recipients"

                        )

                        reason = (

                            (
                                f"Money came from {count} different accounts within {label}"
                                if direction == "fan-in"
                                else f"Money was sent to {count} different accounts within {label}"
                            )

                        )

                        reasons[rows[right][1]].add(reason)



    # Repeated sender/receiver pairs, including patterns spanning days.

    for rows in pair_rows.values():

        rows.sort(key=lambda row: (row[0], row[1]))



        for label, seconds in WINDOWS_SECONDS.items():

            left = 0



            for right, (timestamp, _, _) in enumerate(rows):

                while rows[left][0] < timestamp - seconds:

                    left += 1



                count = right - left + 1

                if count >= 3:

                    reason = (

                        f"The same two accounts transferred money {count} times within {label}"

                    )

                    reasons[rows[right][1]].add(reason)



    # Pass-through candidate: funds arrive at the sender and are sent onward

    # within one hour, with amounts within 30%. This is only a heuristic.

    incoming_by_receiver = defaultdict(list)

    for index, timestamp, event in timed:

        incoming_by_receiver[event["Receiver"]].append(

            (timestamp, index, event)

        )

    for rows in incoming_by_receiver.values():
        rows.sort(key=lambda row: (row[0], row[1]))
    incoming_times_by_receiver = {
        account: [row[0] for row in rows]
        for account, rows in incoming_by_receiver.items()
    }



    for index, timestamp, event in timed:

        sender = event["Sender"]

        receiver = event["Receiver"]

        amount = _amount(event)



        if sender == receiver or amount <= 0:

            continue



        candidate_rows = incoming_by_receiver.get(sender, ())
        candidate_times = incoming_times_by_receiver.get(sender, ())
        start = bisect_left(candidate_times, timestamp - 3600)
        end = bisect_right(candidate_times, timestamp)
        for incoming_time, _, incoming_event in candidate_rows[max(start, end - 250):end]:

            elapsed = timestamp - incoming_time



            if not 0 <= elapsed <= 3600:

                continue



            incoming_amount = _amount(incoming_event)

            if incoming_amount <= 0:

                continue



            ratio = amount / incoming_amount

            if 0.70 <= ratio <= 1.30:

                reasons[index].add(

                    "This account received a similar amount and sent it onward within 1 hour"

                )

                break



    return reasons





def _load_entity_context(events):
    """Load profiles, relationships and case context for this event batch.

    Case records are returned only to explain the review context. They are
    never inputs to transaction risk scoring.
    """
    account_ids = set()
    for event in events:
        for key in ("Sender", "Receiver"):
            value = event.get(key)
            if value not in (None, ""):
                account_ids.add(str(value))
    profiles = {}
    relationships = set()
    cases = defaultdict(list)
    if not account_ids:
        return profiles, relationships, cases
    ids = sorted(account_ids)
    try:
        with get_connection() as conn:
            for start in range(0, len(ids), 400):
                chunk = ids[start:start + 400]
                marks = ",".join("?" for _ in chunk)
                for row in conn.execute(
                    f"SELECT account_id, entity_kind, occupation, employment_status, industry, employer_account_id, business_name, profile_source FROM entity_profiles WHERE account_id IN ({marks})",
                    chunk,
                ):
                    profiles[str(row["account_id"])] = dict(row)
                for row in conn.execute(
                    f"SELECT account_id, related_account_id, relationship_type FROM entity_relationships WHERE account_id IN ({marks}) OR related_account_id IN ({marks})",
                    chunk + chunk,
                ):
                    relationships.add((str(row["account_id"]), str(row["related_account_id"]), str(row["relationship_type"])))
                for row in conn.execute(
                    f"SELECT account_id, case_category, case_status, outcome_summary, record_source FROM entity_case_records WHERE account_id IN ({marks}) ORDER BY filed_date DESC",
                    chunk,
                ):
                    cases[str(row["account_id"])].append(dict(row))
    except sqlite3.OperationalError:
        # Continue safely if entity enrichment has not yet been installed/populated.
        return {}, set(), defaultdict(list)
    return profiles, relationships, cases


DEMO_REGIONS = (
    "Demo City North", "Demo City West", "Demo City Central",
    "Demo City South", "Demo City East",
)
DEMO_CHANNELS = ("UPI", "IMPS", "NEFT", "Card", "Wallet")
DEMO_CHANNEL_WEIGHTS = (42, 18, 20, 12, 8)
DEMO_CONTEXT_SEED = 6_281_047


def _demo_id_number(value):
    digits = "".join(character for character in str(value or "") if character.isdigit())
    return int(digits or 0)


def _live_demo_transaction_context(event, sender_context, receiver_profile):
    """Create reproducible, clearly fictional context for in-memory SIM events."""
    transaction_id = str(event.get("Transaction ID") or event.get("transaction_id") or "")
    rng = random.Random(DEMO_CONTEXT_SEED + _demo_id_number(transaction_id))
    usual_region = str(sender_context.get("usual_region") or "Demo region not listed")
    region = usual_region
    if rng.random() < 0.02:
        alternatives = [item for item in DEMO_REGIONS if item != region]
        if alternatives:
            region = rng.choice(alternatives)
    device = str(sender_context.get("primary_device_id") or "Demo device not listed")
    if rng.random() < 0.01:
        device = f"DEMO-ALT-DEVICE-{_demo_id_number(transaction_id) % 100000:05d}"

    description = str(event.get("Description", event.get("description", "")) or "").lower()
    if any(word in description for word in ("salary", "payroll", "wage")):
        purpose = "Salary or payroll (inferred from demo description)"
    elif any(word in description for word in ("invoice", "supplier", "business", "service")):
        purpose = "Business payment (inferred from demo description)"
    elif any(word in description for word in ("retail", "purchase", "order", "product")):
        purpose = "Purchase (inferred from demo description)"
    elif any(word in description for word in ("utility", "electric", "water", "bill")):
        purpose = "Bill payment (inferred from demo description)"
    else:
        purpose = rng.choice((
            "Personal transfer (demo label)", "General payment (demo label)",
            "Purpose not stated in demo record",
        ))

    receiver_kind = str((receiver_profile or {}).get("entity_kind") or "").lower()
    receiver_industry = str((receiver_profile or {}).get("industry") or "")
    merchant_category = receiver_industry if receiver_kind == "business" else "Not applicable"
    invoice_reference = None
    business_words = ("business", "supplier", "invoice", "service", "purchase", "retail")
    if any(word in description for word in business_words) and rng.random() < 0.65:
        invoice_reference = f"DEMO-INV-{_demo_id_number(transaction_id):012d}"
    channel = rng.choices(DEMO_CHANNELS, weights=DEMO_CHANNEL_WEIGHTS, k=1)[0]
    return {
        "device_id": device,
        "origin_region": region,
        "payment_channel": channel,
        "payment_purpose": purpose,
        "merchant_category": merchant_category,
        "invoice_reference": invoice_reference,
        "context_source": "SYNTHETIC_DEMO_LIVE",
    }


def _load_demo_transaction_context(events, profiles=None):
    """Attach generated context to event copies without writing to SQLite."""
    profiles = profiles or {}
    needs_context = [
        event for event in events
        if not any(event.get(key) for key in (
            "demo_context_source", "demo_device_id", "demo_origin_region",
            "demo_payment_channel", "demo_payment_purpose",
        ))
    ]
    transaction_ids = sorted({
        str(event.get("Transaction ID") or event.get("transaction_id") or "")
        for event in needs_context
        if event.get("Transaction ID") or event.get("transaction_id")
    })
    account_ids = sorted({
        str(event.get(key) or "")
        for event in needs_context for key in ("Sender", "Receiver") if event.get(key)
    })
    transaction_context = {}
    account_context = {}
    if transaction_ids or account_ids:
        try:
            with get_connection() as conn:
                for start in range(0, len(transaction_ids), 400):
                    chunk = transaction_ids[start:start + 400]
                    marks = ",".join("?" for _ in chunk)
                    for row in conn.execute(
                        f"SELECT transaction_id, device_id, origin_region, payment_channel, payment_purpose, merchant_category, invoice_reference, context_source FROM demo_transaction_context WHERE transaction_id IN ({marks})",
                        chunk,
                    ):
                        transaction_context[str(row["transaction_id"])] = dict(row)
                for start in range(0, len(account_ids), 400):
                    chunk = account_ids[start:start + 400]
                    marks = ",".join("?" for _ in chunk)
                    for row in conn.execute(
                        f"SELECT account_id, usual_region, primary_device_id, context_source FROM demo_account_context WHERE account_id IN ({marks})",
                        chunk,
                    ):
                        account_context[str(row["account_id"])] = dict(row)
        except sqlite3.OperationalError:
            transaction_context = {}
            account_context = {}

    enriched = []
    for original in events:
        event = dict(original)
        sender = str(event.get("Sender") or "")
        receiver = str(event.get("Receiver") or "")
        transaction_id = str(event.get("Transaction ID") or event.get("transaction_id") or "")
        sender_context = account_context.get(sender, {})
        receiver_context = account_context.get(receiver, {})
        context = transaction_context.get(transaction_id)
        if context is None and transaction_id.upper().startswith("SIM"):
            context = _live_demo_transaction_context(event, sender_context, profiles.get(receiver, {}))
        context = context or {}
        event.update({
            "demo_device_id": context.get("device_id", ""),
            "demo_origin_region": context.get("origin_region", ""),
            "demo_payment_channel": context.get("payment_channel", ""),
            "demo_payment_purpose": context.get("payment_purpose", ""),
            "demo_merchant_category": context.get("merchant_category", ""),
            "demo_invoice_reference": context.get("invoice_reference") or "",
            "demo_sender_usual_region": sender_context.get("usual_region", ""),
            "demo_sender_primary_device": sender_context.get("primary_device_id", ""),
            "demo_receiver_usual_region": receiver_context.get("usual_region", ""),
            "demo_context_source": context.get("context_source", "NOT_AVAILABLE"),
        })
        enriched.append(event)
    return enriched


def _entity_context_note(event, profiles, relationships, cases, reasons):
    """Add cautious context; synthetic or allegation records never reduce/increase risk."""
    sender = str(event.get("Sender", "") or "")
    receiver = str(event.get("Receiver", "") or "")
    sender_profile = profiles.get(sender)
    receiver_profile = profiles.get(receiver)
    description = str(event.get("Description", event.get("description", "")) or "").lower()
    notes = []
    fan_in = any("money came from" in reason.lower() or "receiver got money from" in reason.lower() or "multiple senders" in reason.lower() for reason in reasons)
    receiver_occupation = str((receiver_profile or {}).get("occupation", "")).lower()
    receiver_is_shop_or_owner = receiver_occupation in {
        "shopkeeper", "food vendor", "small business owner", "business owner / operator"
    }
    receiver_is_matching_business = (
        receiver_profile
        and receiver_profile.get("entity_kind") == "Business"
        and receiver_profile.get("industry") in {"Retail", "Food and beverage", "Wholesale trade", "Hospitality"}
    )
    if receiver_profile and fan_in and (receiver_is_shop_or_owner or receiver_is_matching_business):
        notes.append(f"Possible business explanation: this demo profile describes the receiving account as {receiver_profile['industry']}; customer payments may explain some incoming transfers, but the profile is fictional.")
    if sender_profile and sender_profile.get("entity_kind") == "Business" and any(word in description for word in ("invoice", "supplier", "business", "service")):
        notes.append(f"Possible business context: sender has a synthetic {sender_profile.get('industry', 'business')} profile and the transfer description is business-related.")
    if (receiver, sender, "EMPLOYED_BY") in relationships and any(word in description for word in ("salary", "payroll", "wage")):
        notes.append("Possible payroll context: the recipient has a synthetic employment link to the sending account.")
    if (sender, receiver, "OWNED_BY") in relationships or (receiver, sender, "OWNED_BY") in relationships:
        notes.append("ARGUS found a fictional ownership link between these accounts and includes it as context, not as proof of a real-world relationship.")
    for label, profile in (("Sender", sender_profile), ("Receiver", receiver_profile)):
        if profile:
            notes.append(f"{label} demo profile: {profile.get('occupation', 'role not stated')} · {profile.get('industry', 'industry not stated')}.")
    demo_context_fields = (
        ("device", event.get("demo_device_id")),
        ("origin region", event.get("demo_origin_region")),
        ("payment channel", event.get("demo_payment_channel")),
        ("payment purpose", event.get("demo_payment_purpose")),
        ("merchant category", event.get("demo_merchant_category")),
        ("invoice reference", event.get("demo_invoice_reference")),
    )
    available_demo_context = [f"{label}: {value}" for label, value in demo_context_fields if value]
    if available_demo_context:
        notes.append("Fictional transaction context: " + "; ".join(available_demo_context) + ".")
        notes.append("These generated device, region, channel, purpose, and invoice details are demo signals, not verified facts.")
    for label, account_id in (("Sender", sender), ("Receiver", receiver)):
        links = sorted(row for row in relationships if account_id in row[:2])
        for related_account, relationship in [
            (row[1] if row[0] == account_id else row[0], row[2])
            for row in links[:3]
        ]:
            notes.append(
                f"{label} synthetic relationship: {relationship.replace('_', ' ').lower()} "
                f"linked to account {related_account}; the Investigation report checks available profile, transfer, relationship, and demo case context for linked accounts."
            )
    for label, account_id in (("Sender", sender), ("Receiver", receiver)):
        records = cases.get(account_id, ())
        if records:
            synthetic_count = sum(
                str(record.get("record_source", "")).upper() == "SYNTHETIC_DEMO"
                for record in records
            )
            if synthetic_count:
                case_summary = "; ".join(
                    f"{record.get('case_category', 'demo case')} — {record.get('case_status', 'status unavailable')}"
                    for record in records[:3]
                    if str(record.get("record_source", "")).upper() == "SYNTHETIC_DEMO"
                )
                notes.append(
                    f"{label} has {synthetic_count} fictional demo case record(s); "
                    f"examples: {case_summary}. These are not real background checks and are excluded from risk scoring."
                )
    if not notes:
        if not sender_profile and not receiver_profile:
            return "No synthetic profile context is available for these accounts."
        return "No role or relationship match was found for this transfer."
    return " ".join(dict.fromkeys(notes))


def _demo_context_adjustment(event, profiles, relationships, reasons, patterns, pair_history_count):
    """Apply a small, explainable demo-only offset for strongly fitting activity.

    The generated profiles are fictional. This feature is useful to demonstrate
    how business/payroll context can reduce a noisy fan-in/fan-out signal, but
    it must never be presented as a real-world clearance or background check.
    Serious flow patterns block the adjustment.
    """
    sender = str(event.get("Sender", "") or "")
    receiver = str(event.get("Receiver", "") or "")
    source_profile = profiles.get(sender, {})
    destination_profile = profiles.get(receiver, {})
    description = str(event.get("Description", event.get("description", "")) or "").lower()
    reason_text = " ".join(str(reason).lower() for reason in reasons)
    pattern_codes = {str(pattern.get("code", "")) for pattern in patterns}
    hard_signals = {"rapid_pass_through", "recipient_forwarding", "reciprocal_flow", "multi_hop_amount_chain"}
    if pattern_codes & hard_signals or "cycle" in reason_text or "moving back through" in reason_text or "pass-through" in reason_text:
        return 0, ""

    amount = _amount(event)
    business_terms = ("business payment", "supplier", "invoice", "sale", "customer", "merchant", "order", "product", "pos")
    payroll_terms = ("salary", "payroll", "wage", "employee")
    description_support = any(term in description for term in business_terms)
    fan_in = "money came from" in reason_text or "receiver got money from" in reason_text or "multiple senders" in reason_text
    fan_out = "money was sent to" in reason_text or "sender paid" in reason_text or "multiple recipients" in reason_text

    merchant_industries = {"Retail", "Food and beverage", "Wholesale trade", "Hospitality"}
    destination_occupation = str(destination_profile.get("occupation", "")).lower()
    receiver_is_merchant = (
        (
            destination_profile.get("entity_kind") == "Business"
            and destination_profile.get("industry") in merchant_industries
        )
        or destination_occupation in {
            "shopkeeper", "food vendor", "small business owner", "business owner / operator"
        }
    )
    if receiver_is_merchant and fan_in and description_support and amount <= 250_000:
        return -8, "The fictional shop/business profile fits the incoming payment pattern and the transfer description looks business-related."

    if (receiver, sender, "EMPLOYED_BY") in relationships and (
        any(term in description for term in payroll_terms) or pair_history_count >= 2
    ):
        return -8, "The fictional employer link and payroll wording or repeated transfers fit a possible salary payment."

    if (sender, receiver, "OWNED_BY") in relationships or (receiver, sender, "OWNED_BY") in relationships:
        if any(term in description for term in ("owner", "capital", "dividend", "draw", "business payment")) and pair_history_count >= 1:
            return -5, "The fictional ownership link and repeated business-like transfers may explain this payment."

    sender_is_business = source_profile.get("entity_kind") == "Business"
    if sender_is_business and fan_out and description_support and pair_history_count >= 1:
        return -4, "The fictional business profile and repeated business-like payments may explain this outgoing transfer."

    return 0, ""


def _behavioral_patterns(events, target_start, target_end):
    """Return explainable behavior patterns for target events in a batch.

    This intentionally works on the bounded live window rather than the full
    five-million-row history. It has no external watchlist or legal-threshold
    dependency. Missing timestamps simply skip time-based patterns.
    """
    result = defaultdict(list)
    timed = []
    for index, event in enumerate(events):
        timestamp = _parse_time(event)
        sender, receiver = event.get("Sender"), event.get("Receiver")
        if timestamp is not None and sender and receiver and sender != receiver:
            timed.append((timestamp, index, event))

    by_sender = defaultdict(list)
    by_receiver = defaultdict(list)
    for timestamp, index, event in timed:
        by_sender[event["Sender"]].append((timestamp, index, event))
        by_receiver[event["Receiver"]].append((timestamp, index, event))
    timed.sort(key=lambda row: (row[0], row[1]))
    for rows in by_sender.values():
        rows.sort(key=lambda row: (row[0], row[1]))
    for rows in by_receiver.values():
        rows.sort(key=lambda row: (row[0], row[1]))
    sender_times = {
        account: [row[0] for row in rows]
        for account, rows in by_sender.items()
    }

    def add(index, code, label, points, evidence):
        if target_start <= index < target_end and not any(
            item["code"] == code for item in result[index]
        ):
            result[index].append({
                "code": code, "pattern": label, "points": points,
                "evidence": evidence,
            })

    # Repeated precise values, possible splitting or scripted activity.
    amount_rows = defaultdict(list)
    for timestamp, index, event in timed:
        amount = _amount(event)
        if amount > 0:
            amount_rows[(event["Sender"], round(amount, 2))].append((timestamp, index))
    for (sender, amount), rows in amount_rows.items():
        if len(rows) < 4:
            continue
        rows.sort()
        left = 0
        for right, (timestamp, _) in enumerate(rows):
            while rows[left][0] < timestamp - 3600:
                left += 1
            count = right - left + 1
            if count >= 4:
                add(rows[right][1], "repeated_amount_burst", "Same amount sent repeatedly", 7,
                    f"{count} payments of ₹{amount:,.2f} were sent by one account within 1 hour; this can also happen with regular bills or payroll.")

    # Rapid pass-through: an account receives and forwards a similar amount.
    for account, outgoing_rows in by_sender.items():
        incoming_rows = sorted(by_receiver.get(account, ()), key=lambda row: row[0])
        in_times = [row[0] for row in incoming_rows]
        for timestamp, index, event in outgoing_rows:
            amount = _amount(event)
            if amount <= 0:
                continue
            lo = bisect_left(in_times, timestamp - 1800)
            hi = bisect_right(in_times, timestamp)
            matched = False
            for incoming_time, _, incoming_event in incoming_rows[max(lo, hi - 250):hi]:
                incoming_amount = _amount(incoming_event)
                if incoming_amount and 0.8 <= amount / incoming_amount <= 1.2:
                    matched = True
                    break
            if matched:
                add(index, "rapid_pass_through", "Money received and quickly sent onward", 10,
                    "A similar amount came into this account and was sent onward within 30 minutes; check the reason for both transfers.")

    # Flag the incoming leg too when its recipient quickly forwards similar
    # funds. This makes the transfer chain visible from either side.
    for timestamp, index, event in timed:
        amount = _amount(event)
        if amount <= 0:
            continue
        follow_rows = by_sender.get(event["Receiver"], ())
        follow_times = sender_times.get(event["Receiver"], ())
        lo = bisect_right(follow_times, timestamp)
        hi = bisect_right(follow_times, timestamp + 1800)
        if any(
            0.8 <= _amount(follow_event) / amount <= 1.2
            for _, _, follow_event in follow_rows[lo:hi][-250:]
            if _amount(follow_event) > 0
        ):
            add(index, "recipient_forwarding", "Recipient quickly sent a similar amount onward", 8,
                "The receiving account sent a similar amount to another account within 30 minutes; check the full path and purpose.")

    # Short multi-hop chains can hide the branch after the first recipient.
    # Limit each search to recent candidates so dense simulation bursts stay bounded.
    for timestamp, index, event in timed:
        if not (target_start <= index < target_end):
            continue
        base_amount = _amount(event)
        if base_amount <= 0:
            continue
        first_rows = by_sender.get(event["Receiver"], ())
        first_times = sender_times.get(event["Receiver"], ())
        first_lo = bisect_right(first_times, timestamp)
        first_hi = bisect_right(first_times, timestamp + 6 * 3600)
        found_chain = False
        for first_time, _, first_event in first_rows[first_lo:min(first_hi, first_lo + 50)]:
            first_amount = _amount(first_event)
            intermediate = first_event.get("Receiver")
            if not first_amount or not intermediate or intermediate in {event["Sender"], event["Receiver"]}:
                continue
            if not 0.65 <= first_amount / base_amount <= 1.35:
                continue
            second_rows = by_sender.get(intermediate, ())
            second_times = sender_times.get(intermediate, ())
            second_lo = bisect_right(second_times, first_time)
            second_hi = bisect_right(second_times, timestamp + 12 * 3600)
            for _, _, second_event in second_rows[second_lo:min(second_hi, second_lo + 50)]:
                final_amount = _amount(second_event)
                if (
                    second_event.get("Receiver") not in {event["Sender"], event["Receiver"], intermediate}
                    and final_amount > 0
                    and 0.65 <= final_amount / base_amount <= 1.35
                ):
                    add(index, "multi_hop_amount_chain", "Similar amount moved through several accounts", 10,
                        "A similar amount moved through two more accounts within 12 hours; check the full route and business purpose.")
                    found_chain = True
                    break
            if found_chain:
                break

    # New links matter most when an account already has an established live
    # baseline. A first-ever simulated transfer is not treated as suspicious.
    seen_pairs = set()
    sender_activity = Counter()
    for _, index, event in timed:
        pair = (event["Sender"], event["Receiver"])
        if target_start <= index < target_end:
            if sender_activity[event["Sender"]] >= 5 and pair not in seen_pairs:
                add(index, "new_counterparty", "First transfer to a new account after prior activity", 3,
                    "This sender has earlier activity, but no earlier payment to this recipient appears in the available records.")
        seen_pairs.add(pair)
        sender_activity[event["Sender"]] += 1

    # Reactivation following a long observed quiet period can be notable, but
    # only when the sender already had an established baseline in this window.
    for account, rows in by_sender.items():
        prior_count = 0
        prior_time = None
        for timestamp, index, _ in rows:
            if prior_count >= 3 and prior_time is not None and timestamp - prior_time >= 7 * 86400:
                add(index, "dormant_reactivation", "Transfers resumed after a week or more of quiet", 6,
                    "Earlier outgoing transfers were followed by at least seven days with no outgoing transfer in the available records.")
            prior_count += 1
            prior_time = timestamp

    # Emerging counterparties and rapid fan behavior across practical windows.
    for account, rows in list(by_sender.items()) + list(by_receiver.items()):
        rows.sort(key=lambda row: (row[0], row[1]))
        direction = "outgoing" if account in by_sender and rows is by_sender.get(account) else "incoming"
        identity_key = "Receiver" if direction == "outgoing" else "Sender"
        for window, minimum in ((60, 6), (3600, 10), (86400, 18)):
            left = 0
            peers = Counter()
            for right, (timestamp, _, event) in enumerate(rows):
                peers[event[identity_key]] += 1
                while rows[left][0] < timestamp - window:
                    old = rows[left][2][identity_key]
                    peers[old] -= 1
                    if not peers[old]:
                        del peers[old]
                    left += 1
                if len(peers) >= minimum:
                    label_window = {60: "1 minute", 3600: "1 hour", 86400: "24 hours"}[window]
                    add(rows[right][1], f"{direction}_velocity_{window}",
                        ("Many different accounts sent money in a short time" if direction == "incoming" else "Money was sent to many different accounts in a short time"), 8,
                        f"{len(peers)} different accounts within {label_window}.")

    # Multi-payment accumulation: several transfers to multiple destinations
    # can form a material burst even when no individual payment is large.
    for sender, rows in by_sender.items():
        for window, minimum_total in ((600, 25_000), (3600, 50_000), (86400, 100_000)):
            left = 0
            total = 0.0
            peer_counts = Counter()
            for right, (timestamp, _, event) in enumerate(rows):
                total += _amount(event)
                peer_counts[event["Receiver"]] += 1
                while rows[left][0] < timestamp - window:
                    total -= _amount(rows[left][2])
                    old_peer = rows[left][2]["Receiver"]
                    peer_counts[old_peer] -= 1
                    if peer_counts[old_peer] <= 0:
                        del peer_counts[old_peer]
                    left += 1
                group_size = right - left + 1
                if group_size >= 4 and len(peer_counts) >= 2 and total >= minimum_total:
                    label_window = {600: "10 minutes", 3600: "1 hour", 86400: "24 hours"}[window]
                    add(rows[right][1], f"multi_payment_burst_{window}",
                        "Several payments added up quickly", 6,
                        f"{group_size} payments totaling ₹{total:,.2f} went to {len(peer_counts)} accounts within {label_window}; check whether this matches normal business or salary payments.")

    # A hub that both receives from many accounts and rapidly pays many others
    # can be a legitimate payroll/merchant account or a pass-through hub.
    for account in set(by_sender) & set(by_receiver):
        incoming_rows = by_receiver[account]
        outgoing_rows = by_sender[account]
        for out_time, out_index, _ in outgoing_rows:
            in_times = [row[0] for row in incoming_rows]
            left = bisect_left(in_times, out_time - 3600)
            right = bisect_right(in_times, out_time)
            incoming_candidates = incoming_rows[max(left, right - 500):right]
            incoming_peers = {row[2]["Sender"] for row in incoming_candidates}
            out_times = sender_times[account]
            out_left = bisect_left(out_times, out_time)
            out_right = bisect_right(out_times, out_time + 3600)
            outgoing_peers = {
                row[2]["Receiver"]
                for row in outgoing_rows[out_left:min(out_right, out_left + 500)]
            }
            if len(incoming_peers) >= 3 and len(outgoing_peers) >= 3:
                add(out_index, "multi_leg_hub", "Account received and paid many accounts within one hour", 7,
                    f"This account received money from {len(incoming_peers)} accounts and paid {len(outgoing_peers)} accounts within one hour. Check this account's stated work and whether the transfers fit its usual activity.")

    # Fast reciprocal movement between a pair is separate from simple repeat
    # volume and may indicate refunds, settlement, or circular movement.
    directed_rows = defaultdict(list)
    for timestamp, index, event in timed:
        directed_rows[(event["Sender"], event["Receiver"])].append((timestamp, index, event))
    for (sender, receiver), rows in directed_rows.items():
        reverse = directed_rows.get((receiver, sender), ())
        reverse_times = [row[0] for row in reverse]
        for timestamp, index, event in rows:
            lo = bisect_left(reverse_times, timestamp - 86400)
            hi = bisect_right(reverse_times, timestamp + 86400)
            if hi > lo and _amount(event) > 0:
                add(index, "reciprocal_flow", "Money moved both ways between the same two accounts", 5,
                    "Both accounts sent money to each other within 24 hours; this could be a refund, repayment, or another type of transfer.")

    # Round-number concentration can indicate batching but is not suspicious alone.
    round_groups = defaultdict(list)
    for timestamp, index, event in timed:
        amount = _amount(event)
        if amount >= 1000 and amount % 1000 == 0:
            round_groups[event["Sender"]].append((timestamp, index, amount))
    for sender, rows in round_groups.items():
        rows.sort()
        for pos, (timestamp, _, _) in enumerate(rows):
            recent = [(i, a) for t, i, a in rows[max(0, pos - 7):pos + 1] if timestamp - t <= 3600]
            if len(recent) >= 5:
                add(recent[-1][0], "round_amount_cluster", "Several round-number payments within one hour", 4,
                    f"{len(recent)} payments were round amounts within one hour; check whether they match regular sales, bills, or payroll.")

    return result


def score_live_batch(

    events: list[dict],

    previous_events: list[dict],

) -> list[dict]:

    """Score a batch while preserving the app's existing function interface."""

    history = [dict(event) for event in previous_events[-HISTORY_LIMIT:]]

    batch = [dict(event) for event in events]

    combined = history + batch

    entity_profiles, entity_relationships, entity_cases = _load_entity_context(batch)
    combined = _load_demo_transaction_context(combined, entity_profiles)
    history = combined[:len(history)]
    batch = combined[len(history):]
    relationship_types_by_pair = defaultdict(set)
    for left, right, relation_type in entity_relationships:
        relationship_types_by_pair[frozenset((left, right))].add(relation_type)
    for event in combined:
        sender = str(event.get("Sender") or "")
        receiver = str(event.get("Receiver") or "")
        event["demo_sender_occupation"] = entity_profiles.get(sender, {}).get("occupation", "")
        event["demo_sender_industry"] = entity_profiles.get(sender, {}).get("industry", "")
        event["demo_receiver_occupation"] = entity_profiles.get(receiver, {}).get("occupation", "")
        event["demo_receiver_industry"] = entity_profiles.get(receiver, {}).get("industry", "")
        event["demo_relationship_types"] = sorted(relationship_types_by_pair.get(frozenset((sender, receiver)), ()))

    temporal = _temporal_evidence(combined)
    behavioral = _behavioral_patterns(combined, len(history), len(combined))
    catalog_patterns = evaluate_pattern_checks(combined, len(history), len(combined))

    all_out, all_in, _ = _graph(combined)

    prior_out, _, _ = _graph(history)



    sender_counts = Counter(event.get("Sender") for event in history)

    receiver_counts = Counter(event.get("Receiver") for event in history)

    pair_counts = Counter(

        (event.get("Sender"), event.get("Receiver"))

        for event in history

    )



    sender_amounts = defaultdict(list)

    for event in history:

        sender = event.get("Sender")

        if sender:

            sender_amounts[sender].append(_amount(event))



    scored = []

    offset = len(history)



    for batch_index, event in enumerate(batch):

        combined_index = offset + batch_index

        sender = event.get("Sender")

        receiver = event.get("Receiver")

        amount = _amount(event)

        score = 0

        reasons = []



        if amount >= 200_000:

            score += 25

            reasons.append(f"Large payment amount: ₹{amount:,.2f}")

        elif amount >= 100_000:

            score += 18

            reasons.append(f"Payment amount is high: ₹{amount:,.2f}")



        if sender_counts[sender] >= 8:

            score += 12

            reasons.append("Sender has made many recent payments")



        if receiver_counts[receiver] >= 8:

            score += 12

            reasons.append("Receiver has received many recent payments")



        if pair_counts[(sender, receiver)] >= 2:

            score += 10

            reasons.append("These two accounts have transferred money several times")



        amounts = sender_amounts.get(sender, [])

        if len(amounts) >= 5:

            baseline = median(amounts)

            if baseline > 0 and amount >= max(baseline * 4, 10_000):

                score += 12

                reasons.append(
                    f"This payment is at least 4 times the sender's usual payment (about ₹{baseline:,.2f})"
                )



        out_degree = len(all_out.get(sender, ()))

        in_degree = len(all_in.get(receiver, ()))



        if out_degree >= 12:

            score += 12

            reasons.append(f"Sender paid {out_degree} different accounts in the recent activity")

        elif out_degree >= 6:

            score += 7

            reasons.append(f"Sender paid {out_degree} different accounts in the recent activity")



        if in_degree >= 12:

            score += 12

            reasons.append(f"Receiver got money from {in_degree} different accounts in the recent activity")

        elif in_degree >= 6:

            score += 7

            reasons.append(f"Receiver got money from {in_degree} different accounts in the recent activity")



        # Cycle check uses historical graph only, not the current transfer.

        if (

            sender != receiver

            and _has_path(receiver, sender, prior_out, max_hops=3)

        ):

            score += 15

            reasons.append("Money may be moving back through a recent transfer path")



        scored_temporal_families = set()
        for reason in sorted(temporal.get(combined_index, ())):
            if reason not in reasons:
                if reason.startswith("Money came from "):
                    family = "incoming_many_accounts"
                elif reason.startswith("Money was sent to "):
                    family = "outgoing_many_accounts"
                elif reason.startswith("The same two accounts transferred"):
                    family = "repeat_between_accounts"
                else:
                    family = reason.split(":", 1)[0]
                if family not in scored_temporal_families:
                    score += 6
                    scored_temporal_families.add(family)
                reasons.append(reason)

        patterns = behavioral.get(combined_index, [])
        catalog_hits = catalog_patterns.get(combined_index, [])
        # Add a bounded score for catalogue evidence. Thresholds within one
        # family count once so matching several nearby thresholds does not
        # multiply the same underlying behavior.
        catalog_family_points = {}
        for hit in catalog_hits:
            catalog_family_points[hit["family"]] = max(
                catalog_family_points.get(hit["family"], 0), 2
            )
        score += min(18, sum(catalog_family_points.values()))
        patterns.extend(catalog_hits)
        catalog_families = {hit.get("family") for hit in catalog_hits}
        score += min(
            MAX_PATTERN_POINTS,
            sum(int(pattern.get("points", 0)) for pattern in patterns
                if pattern.get("family") != "catalog"),
        )
        reasons.extend(
            f"{pattern['pattern']}: {pattern['evidence']}"
            for pattern in patterns
            if pattern.get("family") != "velocity"
            and pattern.get("family") != "value"
            and pattern.get("family") != "repeat_pair"
            and pattern.get("family") != "same_amount"
            and pattern.get("family") != "pass_through"
            and pattern.get("family") not in catalog_families
        )
        for family in sorted(catalog_families):
            family_hit = next((item for item in catalog_hits if item.get("family") == family), None)
            if family_hit:
                reasons.append(f"{family_hit['pattern']}: {family_hit['evidence']}")

        represented = {
            f"{pattern.get('pattern', '')}: {pattern.get('evidence', '')}"
            for pattern in patterns
        }
        for reason in reasons:
            if reason not in represented:
                label = reason.split(":", 1)[0]
                patterns.append({
                    "code": "rule_evidence",
                    "pattern": label,
                    "points": 0,
                    "evidence": reason,
                })
                represented.add(reason)

        context_adjustment, context_basis = _demo_context_adjustment(
            event, entity_profiles, entity_relationships, reasons,
            patterns, pair_counts[(sender, receiver)],
        )
        if context_adjustment:
            score = max(0, score + context_adjustment)
            reasons.append(
                f"Possible explanation changed the demo score by {context_adjustment} points: {context_basis}"
            )
            patterns.append({
                "code": "demo_context_adjustment",
                "pattern": "Possible shop, salary, or ownership explanation",
                "points": context_adjustment,
                "evidence": context_basis,
            })



        score = min(100, score)

        event.update({

            "risk_score": score,

            "risk_level": _risk_level(score),

            "risk_reasons": (

                "; ".join(dict.fromkeys(reasons))

                if reasons else "No unusual pattern was found by the current checks"

            ),

            "event_source": "LIVE_SIMULATION",

            "alert_threshold": ALERT_THRESHOLD,

            "risk_patterns": patterns,

            "entity_context": _entity_context_note(
                event, entity_profiles, entity_relationships, entity_cases, reasons
            ) + (f" Illustrative demo scoring adjustment: {context_basis}" if context_adjustment else ""),

            "context_adjustment": context_adjustment,

        })

        scored.append(event)



        # Maintain prior-history counters for the next event in this batch.

        sender_counts[sender] += 1

        receiver_counts[receiver] += 1

        pair_counts[(sender, receiver)] += 1

        sender_amounts[sender].append(amount)



    return scored





def persist_live_alerts(events: list[dict]) -> int:

    """Persist alert metadata only; never persist synthetic transactions."""

    rows = []



    for event in events:

        if int(event.get("risk_score", 0)) < ALERT_THRESHOLD:

            continue



        reason = (

            "LIVE_SIMULATION | "

            f"event={event.get('Transaction ID', 'unknown')} | "

            f"level={event.get('risk_level', 'UNKNOWN')} | "

            f"{event.get('risk_reasons', '')}"

        )

        rows.append((

            f"ALRT{uuid.uuid4().hex[:20].upper()}",

            None,

            event.get("Sender"),

            float(event.get("risk_score", 0)),

            reason,

            "Open",

        ))



    if not rows:

        return 0



    with get_connection() as conn:

        conn.executemany(

            """INSERT INTO alerts

               (alert_id, transaction_id, account_id,

                risk_score, reason, status)

               VALUES (?, ?, ?, ?, ?, ?)""",

            rows,

        )



    return len(rows)





def load_recent_live_alerts(limit: int = 100):

    with get_connection() as conn:

        return conn.execute(

            """SELECT alert_id, account_id, risk_score,

                      reason, status, created_at

               FROM alerts

               WHERE reason LIKE 'LIVE_SIMULATION%'

               ORDER BY created_at DESC

               LIMIT ?""",

            (max(1, min(int(limit), 500)),),

        ).fetchall()
