"""ARGUS's explainable transaction-pattern checks.

These checks use only account IDs, amounts, and timestamps available in local
transactions. They are review signals, not proof of wrongdoing. Profile,
relationship, and case data are deliberately not treated as verified evidence.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from bisect import bisect_left, bisect_right
from datetime import datetime, timezone


WINDOWS = (
    ("1m", 60, "1 minute"),
    ("10m", 600, "10 minutes"),
    ("1h", 3600, "1 hour"),
    ("24h", 86400, "24 hours"),
    ("7d", 604800, "7 days"),
)

CONCENTRATION_WINDOWS = (
    ("10m", 600, "10 minutes"),
    ("1h", 3600, "1 hour"),
    ("24h", 86400, "24 hours"),
    ("7d", 604800, "7 days"),
)


def _make_catalog():
    checks = []
    # 1–20: unusually many different senders/recipients in a time window.
    for direction in ("incoming", "outgoing"):
        for window_code, seconds, label in WINDOWS:
            for minimum in (5, 10):
                checks.append({
                    "code": f"P{len(checks) + 1:03d}", "family": "velocity",
                    "direction": direction, "seconds": seconds,
                    "window": label, "minimum": minimum,
                    "name": ("Many senders in a short time" if direction == "incoming"
                             else "Payments to many accounts in a short time"),
                    "description": f"At least {minimum} different account(s) sent money to or received money from one account within {label}.",
                })
    # 21–40: the total money entering or leaving within a time window.
    for direction in ("incoming", "outgoing"):
        for window_code, seconds, label in WINDOWS:
            for minimum in (50_000, 250_000):
                checks.append({
                    "code": f"P{len(checks) + 1:03d}", "family": "value",
                    "direction": direction, "seconds": seconds,
                    "window": label, "minimum": minimum,
                    "name": ("Large incoming total in a short time" if direction == "incoming"
                             else "Large outgoing total in a short time"),
                    "description": f"Money moving {direction} totals at least INR {minimum:,.0f} within {label}.",
                })
    # 41–60: repeat transfers between the same two accounts.
    for window_code, seconds, label in WINDOWS:
        for minimum in (3, 5, 10, 20):
            checks.append({
                "code": f"P{len(checks) + 1:03d}", "family": "repeat_pair",
                "seconds": seconds, "window": label, "minimum": minimum,
                "name": "Repeated transfers between the same two accounts",
                "description": f"The same pair transferred money at least {minimum} times within {label}.",
            })
    # 61–80: repeated matching payment amounts in a short time.
    for direction in ("incoming", "outgoing"):
        for window_code, seconds, label in WINDOWS:
            for minimum in (3, 5):
                checks.append({
                    "code": f"P{len(checks) + 1:03d}", "family": "same_amount",
                    "direction": direction, "seconds": seconds,
                    "window": label, "minimum": minimum,
                    "name": "Repeated payments of the same amount",
                    "description": f"At least {minimum} matching amounts moved {direction} within {label}.",
                })
    # 81–100: similar money received and sent onward within a time window.
    for window_code, seconds, label in WINDOWS:
        for tolerance in (0.10, 0.20, 0.30, 0.50):
            checks.append({
                "code": f"P{len(checks) + 1:03d}", "family": "pass_through",
                "seconds": seconds, "window": label,
                "tolerance": tolerance, "minimum": 1,
                "name": "Similar money received and sent onward",
                "description": f"A similar amount was received and sent onward within {label} (difference up to {tolerance:.0%}).",
            })

    # 101–140: repeated smaller outgoing payments in a short period.
    # These are review signals only; ordinary bills and payroll can also match.
    for window_code, seconds, label in WINDOWS:
        for minimum in (3, 4, 5, 6, 8, 10, 12, 15):
            checks.append({
                "code": f"P{len(checks) + 1:03d}", "family": "split_outgoing",
                "seconds": seconds, "window": label, "minimum": minimum,
                "name": "Several smaller payments sent close together",
                "description": f"At least {minimum} outgoing payments of INR 100,000 or less were sent within {label}; review their purpose and recipients.",
            })

    # 141–180: repeated round-number outgoing payments.
    for window_code, seconds, label in WINDOWS:
        for minimum in (3, 4, 5, 6, 8, 10, 12, 15):
            checks.append({
                "code": f"P{len(checks) + 1:03d}", "family": "round_amount",
                "seconds": seconds, "window": label, "minimum": minimum,
                "name": "Cluster of round-number payments",
                "description": f"At least {minimum} outgoing payments of INR 1,000 or more, each in whole INR 1,000 units, occurred within {label}.",
            })

    # 181–220: most of an account's incoming or outgoing value is concentrated
    # with one counterparty over a time window.
    for direction in ("incoming", "outgoing"):
        for window_code, seconds, label in CONCENTRATION_WINDOWS:
            for share in (0.70, 0.75, 0.80, 0.90, 0.95):
                checks.append({
                    "code": f"P{len(checks) + 1:03d}", "family": "counterparty_concentration",
                    "direction": direction, "seconds": seconds, "window": label,
                    "share": share, "name": "Most money linked to one account",
                    "description": f"One account represented at least {share:.0%} of the {direction} value within {label}; check whether this fits the account's normal activity.",
                })

    # 221–260: the same two accounts transfer in both directions repeatedly.
    for window_code, seconds, label in WINDOWS:
        for minimum in (2, 3, 4, 5, 8, 10, 15, 20):
            checks.append({
                "code": f"P{len(checks) + 1:03d}", "family": "reciprocal_pair",
                "seconds": seconds, "window": label, "minimum": minimum,
                "name": "Repeated transfers in both directions",
                "description": f"The same two accounts each sent at least {minimum} transfers to the other within {label}.",
            })

# 261–300: a transfer is much larger than the account's earlier normal
    # amount. History thresholds avoid treating a new account as having a
    # known baseline.
    for direction in ("incoming", "outgoing"):
        for factor in (3, 4, 5, 10, 20):
            for history_minimum in (3, 5, 10, 20):
                checks.append({
                    "code": f"P{len(checks) + 1:03d}", "family": "baseline_spike",
                    "direction": direction, "factor": factor,
                    "history_minimum": history_minimum,
                    "name": "Payment far above the account's earlier usual amount",
                    "description": f"This {direction} payment is at least {factor} times the account's median earlier payment, based on at least {history_minimum} earlier transfers.",
                })

    # Demo-only transaction context checks. The input values are generated for
    # ARGUS demonstrations and are never treated as verified identity data.
    demo_context_checks = (
        ("demo_device_mismatch", "Transfer used a different demo device", "The transaction device differs from the sender account's usual demo device."),
        ("demo_shared_device", "Demo device appears across several accounts", "The same fictional device is linked to several different sender accounts in the examined events."),
        ("demo_region_mismatch", "Transfer region differs from usual demo region", "The transaction's fictional origin region differs from the sender account's usual demo region."),
        ("demo_region_hopping", "Several demo regions used close together", "The sender's transactions use multiple fictional regions within a short period."),
        ("demo_channel_mix", "Several payment channels used close together", "The sender uses multiple fictional payment channels within a short period."),
        ("demo_invoice_reuse", "Demo invoice reference appears across recipients", "The same fictional invoice reference is attached to transfers to different recipients."),
        ("demo_business_invoice_missing", "Business-like demo payment has no invoice reference", "The demo purpose is business-related but the generated transaction context has no invoice reference."),
        ("demo_purpose_description_mismatch", "Demo purpose differs from transfer description", "The generated payment-purpose label does not fit the transaction description."),
    )
    for family, name, description in demo_context_checks:
        checks.append({
            "code": f"P{len(checks) + 1:03d}", "family": family,
            "name": name, "description": description,
        })

    # Additional checks describe combined flow, history, and context behavior.
    # These are one check per behavior, not several window-size variations.
    advanced_checks = (
        ("collection_then_payout", "Collection followed by a broad payout", "One account collects from several senders and then pays several recipients."),
        ("converging_splitting_branch", "Transfer branches converge and split", "Several transfer paths meet at an account and its outgoing activity then branches to multiple accounts."),
        ("same_destination_independent_paths", "Separate transfer paths reach one destination", "Different first-step accounts reach the same destination through separate intermediaries."),
        ("multi_hop_same_amount_chain", "Similar amount moves through several accounts", "A similar amount can be followed through at least three transfer steps."),
        ("return_to_origin_three_step", "Transfer path returns to its starting account", "Money travels through at least two other accounts before returning to the originating account."),
        ("fixed_fee_pass_through", "Repeated fixed amount kept during forwarding", "Forwarded amounts repeatedly differ from the incoming amount by the same fixed amount."),
        ("proportional_fee_pass_through", "Repeated percentage kept during forwarding", "Forwarded amounts repeatedly retain a similar percentage of incoming amounts."),
        ("incoming_sum_paid_out", "Several incoming payments are combined into one payout", "An outgoing transfer is close to the combined value of several recent incoming payments."),
        ("incoming_amount_distributed", "One incoming amount is split across recipients", "Several outgoing transfers together are close to a recent incoming amount."),
        ("new_counterparty_burst", "Several new contacts appear together", "An account starts paying several recipients not seen in its earlier supplied history."),
        ("shared_contact_cluster", "Two accounts share an unusual contact group", "Two accounts have several of the same transfer contacts in the examined network."),
        ("long_gap_counterparty_return", "Transfer resumes with a long-quiet contact", "A payment pair resumes after a long gap in the supplied transaction history."),
        ("dormant_account_reactivation", "Quiet account becomes active again", "An account resumes transfers after an extended quiet period."),
        ("unusual_hour_for_account", "Transfer occurs at an unusual hour for this account", "The transfer hour differs from the account's earlier observed activity hours."),
        ("burst_after_quiet_period", "A transfer burst follows a quiet period", "An account starts a concentrated burst of transfers after a long period without activity."),
        ("alternating_incoming_outgoing", "Account rapidly switches between receiving and sending", "Recent transfer direction alternates repeatedly for the same account."),
        ("coordinated_branch_timing", "Several accounts pay one destination close together", "Different senders make transfers to the same destination within a narrow time span."),
        ("reverse_transfer_path", "A multi-account transfer path is reversed", "Accounts in a short transfer path later send money back along the same links in reverse order."),
        ("device_change_before_burst", "Device change appears before a transfer burst", "A different generated device is followed by several outgoing transfers."),
        ("region_shift_with_forwarding", "Region change coincides with onward transfers", "A transfer from a different generated region is followed by onward movement of a similar amount."),
        ("region_inconsistent_connected_path", "Connected transfers show different demo regions", "A short transfer path carries different generated origin regions."),
        ("channel_change_before_burst", "Payment channel changes before a transfer burst", "A new generated payment channel appears as outgoing activity expands to new contacts."),
        ("purpose_shift_in_transfer_chain", "Generated payment purpose changes along a chain", "A similar amount moves through a short chain while its generated purpose labels change."),
        ("merchant_category_shift", "Merchant category changes along a payment chain", "Connected business-like transfers point to different generated merchant categories."),
        ("invoice_amount_inconsistency", "Same demo invoice has different transfer amounts", "Transfers sharing a generated invoice reference have noticeably different amounts."),
        ("context_and_amount_change", "Transaction context and amount change together", "A sender's generated device, region, or channel changes while its amount is far above its own earlier activity."),
        ("purpose_profile_mismatch", "Generated payment purpose differs from recipient business context", "The generated payment purpose and merchant category do not agree with the recipient's available demo business context."),
        ("relationship_purpose_mismatch", "Transfer purpose does not fit a declared demo relationship", "The sender and receiver have a generated employment or ownership link, but the transfer purpose or description does not fit that link."),
    )
    for family, name, description in advanced_checks:
        checks.append({
            "code": f"P{len(checks) + 1:03d}", "family": f"advanced_{family}",
            "name": name, "description": description,
        })
    return tuple(checks)


PATTERN_CATALOG = _make_catalog()
PATTERN_BY_CODE = {item["code"]: item for item in PATTERN_CATALOG}
PATTERN_CODE_INDEX = {
    (item["family"], tuple(sorted(
        (key, value) for key, value in item.items()
        if key not in {"code", "family", "name", "description"}
    ))): item["code"]
    for item in PATTERN_CATALOG
}


def _pattern_code(family, **parameters):
    return PATTERN_CODE_INDEX[(family, tuple(sorted(parameters.items())))]


def _timestamp(event):
    value = event.get("Timestamp", event.get("timestamp", event.get("Date")))
    if value in (None, ""):
        return None
    try:
        from datetime import datetime, timezone
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def evaluate_pattern_checks(events, target_start=0, target_end=None):
    """Return matched pattern checks for target event indexes.

    Historical events may supply context, while only indexes in the requested
    target range receive results. Missing timestamps/account IDs skip only the
    checks that need them.
    """
    rows = []
    for index, event in enumerate(events):
        sender = str(event.get("Sender") or "")
        receiver = str(event.get("Receiver") or "")
        try:
            amount = max(0.0, float(event.get("Amount (INR)", event.get("Amount", 0)) or 0))
        except (TypeError, ValueError):
            amount = 0.0
        stamp = _timestamp(event)
        if sender and receiver and sender != receiver and stamp is not None:
            rows.append((stamp, index, sender, receiver, amount))
    if target_end is None:
        target_end = len(events)
    results = defaultdict(list)

    def add(index, code, evidence):
        if target_start <= index < target_end:
            item = PATTERN_BY_CODE[code]
            results[index].append({
                "code": code,
                "family": item["family"],
                "pattern": item["name"],
                "points": 0,
                "evidence": evidence,
            })

    # Directed account activity, grouped in time order.
    activity = {"incoming": defaultdict(list), "outgoing": defaultdict(list)}
    pairs = defaultdict(list)
    exact_amounts = {"incoming": defaultdict(list), "outgoing": defaultdict(list)}
    for stamp, index, sender, receiver, amount in rows:
        activity["outgoing"][sender].append((stamp, index, receiver, amount))
        activity["incoming"][receiver].append((stamp, index, sender, amount))
        pairs[(sender, receiver)].append((stamp, index))
        if amount > 0:
            exact_amounts["outgoing"][(sender, round(amount, 2))].append((stamp, index))
            exact_amounts["incoming"][(receiver, round(amount, 2))].append((stamp, index))

    for direction in ("incoming", "outgoing"):
        for account, group in activity[direction].items():
            group.sort()
            for _, seconds, label in WINDOWS:
                for minimum in (5, 10):
                    left = 0
                    peer_counts = Counter()
                    for right, (stamp, index, peer, _) in enumerate(group):
                        peer_counts[peer] += 1
                        while group[left][0] < stamp - seconds:
                            old_peer = group[left][2]
                            peer_counts[old_peer] -= 1
                            if peer_counts[old_peer] == 0:
                                del peer_counts[old_peer]
                            left += 1
                        if len(peer_counts) >= minimum:
                            code = _pattern_code("velocity", direction=direction, seconds=seconds, window=label, minimum=minimum)
                            add(index, code, f"{len(peer_counts)} different account(s) within {label}.")

                for _, seconds, label in WINDOWS:
                    for minimum in (50_000, 250_000):
                        left = 0
                        total = 0.0
                        for right, (stamp, index, _, amount) in enumerate(group):
                            total += amount
                            while group[left][0] < stamp - seconds:
                                total -= group[left][3]
                                left += 1
                            if total >= minimum:
                                code = _pattern_code("value", direction=direction, seconds=seconds, window=label, minimum=minimum)
                                add(index, code, f"INR {total:,.2f} moved {direction} within {label}.")

    for (sender, receiver), group in pairs.items():
        group.sort()
        for _, seconds, label in WINDOWS:
            left = 0
            for right, (stamp, index) in enumerate(group):
                while group[left][0] < stamp - seconds:
                    left += 1
                count = right - left + 1
                for minimum in (3, 5, 10, 20):
                    if count >= minimum:
                        code = _pattern_code("repeat_pair", seconds=seconds, window=label, minimum=minimum)
                        add(index, code, f"{sender} and {receiver} transferred {count} times within {label}.")

    for direction in ("incoming", "outgoing"):
        for (account, amount), group in exact_amounts[direction].items():
            group.sort()
            for _, seconds, label in WINDOWS:
                left = 0
                for right, (stamp, index) in enumerate(group):
                    while group[left][0] < stamp - seconds:
                        left += 1
                    count = right - left + 1
                    for minimum in (3, 5):
                        if count >= minimum:
                            code = _pattern_code("same_amount", direction=direction, seconds=seconds, window=label, minimum=minimum)
                            add(index, code, f"{count} payments of INR {amount:,.2f} moved {direction} within {label}.")

    incoming = defaultdict(list)
    outgoing = defaultdict(list)
    for row in rows:
        stamp, index, sender, receiver, amount = row
        incoming[receiver].append(row)
        outgoing[sender].append(row)
    for account, out_rows in outgoing.items():
        in_rows = incoming.get(account, [])
        in_rows.sort()
        in_times = [item[0] for item in in_rows]
        for stamp, index, sender, receiver, amount in out_rows:
            if amount <= 0:
                continue
            for _, seconds, label in WINDOWS:
                lo = bisect_left(in_times, stamp - seconds)
                hi = bisect_right(in_times, stamp)
                if hi <= lo:
                    continue
                # Bound comparison work for accounts with exceptionally dense history.
                candidates = in_rows[max(lo, hi - 250):hi]
                matched = None
                for in_stamp, _, in_sender, _, in_amount in reversed(candidates):
                    if in_amount <= 0:
                        continue
                    difference = abs(amount - in_amount) / in_amount
                    if difference <= 0.50:
                        matched = (in_sender, in_amount, in_stamp)
                        break
                if matched:
                    in_sender, in_amount, _ = matched
                    for tolerance in (0.10, 0.20, 0.30, 0.50):
                        if abs(amount - in_amount) / in_amount <= tolerance:
                            code = _pattern_code("pass_through", seconds=seconds, window=label, tolerance=tolerance, minimum=1)
                            add(index, code, f"INR {in_amount:,.2f} from {in_sender} was followed by INR {amount:,.2f} to {receiver} within {label}.")

    # Additional transaction-only families. All calculations are bounded to
    # the supplied history/batch, and only requested target indexes are added.
    for account, group in activity["outgoing"].items():
        group.sort()
        for _, seconds, label in WINDOWS:
            left = 0
            split_count = 0
            round_count = 0
            for right, (stamp, index, peer, amount) in enumerate(group):
                if 0 < amount <= 100_000:
                    split_count += 1
                is_round = amount >= 1000 and abs(amount / 1000 - round(amount / 1000)) < 1e-9
                if is_round:
                    round_count += 1
                while group[left][0] < stamp - seconds:
                    old_amount = group[left][3]
                    if 0 < old_amount <= 100_000:
                        split_count -= 1
                    if old_amount >= 1000 and abs(old_amount / 1000 - round(old_amount / 1000)) < 1e-9:
                        round_count -= 1
                    left += 1
                if target_start <= index < target_end:
                    for minimum in (3, 4, 5, 6, 8, 10, 12, 15):
                        if split_count >= minimum:
                            code = _pattern_code("split_outgoing", seconds=seconds, window=label, minimum=minimum)
                            add(index, code, f"{split_count} outgoing payments of INR 100,000 or less fell within {label}.")
                    for minimum in (3, 4, 5, 6, 8, 10, 12, 15):
                        if round_count >= minimum:
                            code = _pattern_code("round_amount", seconds=seconds, window=label, minimum=minimum)
                            add(index, code, f"{round_count} outgoing payments were whole INR 1,000 amounts within {label}.")

    for direction in ("incoming", "outgoing"):
        for account, group in activity[direction].items():
            group.sort()
            for _, seconds, label in CONCENTRATION_WINDOWS:
                left = 0
                peer_totals = defaultdict(float)
                total = 0.0
                for right, (stamp, index, peer, amount) in enumerate(group):
                    total += amount
                    peer_totals[peer] += amount
                    while group[left][0] < stamp - seconds:
                        old_peer, old_amount = group[left][2], group[left][3]
                        peer_totals[old_peer] -= old_amount
                        total -= old_amount
                        if peer_totals[old_peer] <= 1e-9:
                            peer_totals.pop(old_peer, None)
                        left += 1
                    if not (target_start <= index < target_end) or total <= 0:
                        continue
                    largest_peer_total = max(peer_totals.values(), default=0.0)
                    share = largest_peer_total / total
                    for threshold in (0.70, 0.75, 0.80, 0.90, 0.95):
                        if share >= threshold:
                            code = _pattern_code(
                                "counterparty_concentration", direction=direction,
                                seconds=seconds, window=label, share=threshold,
                            )
                            add(index, code, f"One account represented {share:.0%} of INR {total:,.2f} {direction} within {label}.")

    # Two-way payment checks use unordered pairs while retaining direction.
    pair_directions = defaultdict(list)
    for stamp, index, sender, receiver, amount in rows:
        pair_directions[tuple(sorted((sender, receiver)))].append((stamp, index, sender, receiver))
    for pair, group in pair_directions.items():
        group.sort()
        for _, seconds, label in WINDOWS:
            left = 0
            counts = Counter()
            for right, (stamp, index, sender, receiver) in enumerate(group):
                counts[(sender, receiver)] += 1
                while group[left][0] < stamp - seconds:
                    old_sender, old_receiver = group[left][2], group[left][3]
                    counts[(old_sender, old_receiver)] -= 1
                    left += 1
                if not (target_start <= index < target_end):
                    continue
                each_way = min(counts.get((pair[0], pair[1]), 0), counts.get((pair[1], pair[0]), 0))
                for minimum in (2, 3, 4, 5, 8, 10, 15, 20):
                    if each_way >= minimum:
                        code = _pattern_code("reciprocal_pair", seconds=seconds, window=label, minimum=minimum)
                        add(index, code, f"Both accounts sent at least {each_way} transfers to each other within {label}.")

    # Compare a target transfer with earlier amounts for that same account and
    # direction. This deliberately does not invent a baseline for new accounts.
    for direction in ("incoming", "outgoing"):
        for account, group in activity[direction].items():
            group.sort()
            earlier_amounts = []
            for stamp, index, peer, amount in group:
                if index < target_start and amount > 0:
                    earlier_amounts.append(amount)
                    continue
                if not (target_start <= index < target_end) or amount <= 0:
                    continue
                for history_minimum in (3, 5, 10, 20):
                    if len(earlier_amounts) < history_minimum:
                        continue
                    baseline = sorted(earlier_amounts)[len(earlier_amounts) // 2]
                    if baseline <= 0:
                        continue
                    factor = amount / baseline
                    for threshold in (3, 4, 5, 10, 20):
                        if factor >= threshold:
                            code = _pattern_code(
                                "baseline_spike", direction=direction,
                                factor=threshold, history_minimum=history_minimum,
                            )
                            add(index, code, f"INR {amount:,.2f} was {factor:.1f} times the earlier median INR {baseline:,.2f} from {len(earlier_amounts)} prior {direction} transfer(s).")

    # Evaluate synthetic context only when it is present on an event. These
    # fields help demonstrate investigative evidence, not verified background.
    demo_device_accounts = defaultdict(set)
    demo_invoices = defaultdict(set)
    events_by_sender = defaultdict(list)
    for index, event in enumerate(events):
        sender = str(event.get("Sender") or "")
        receiver = str(event.get("Receiver") or "")
        device = str(event.get("demo_device_id") or "").strip()
        invoice = str(event.get("demo_invoice_reference") or "").strip()
        if device and sender:
            demo_device_accounts[device].add(sender)
        if invoice and receiver:
            demo_invoices[invoice].add(receiver)
        stamp = _timestamp(event)
        if sender and stamp is not None:
            events_by_sender[sender].append((stamp, index, event))

    context_code_by_family = {
        PATTERN_BY_CODE[item["code"]]["family"]: item["code"]
        for item in PATTERN_CATALOG if item["family"].startswith("demo_")
    }
    for sender, sender_rows in events_by_sender.items():
        sender_rows.sort(key=lambda row: (row[0], row[1]))
        recent_left = 0
        recent_region_counts = Counter()
        recent_channel_counts = Counter()
        for position, (stamp, index, event) in enumerate(sender_rows):
            current_region = str(event.get("demo_origin_region") or "").strip()
            current_channel = str(event.get("demo_payment_channel") or "").strip()
            if current_region:
                recent_region_counts[current_region] += 1
            if current_channel:
                recent_channel_counts[current_channel] += 1
            while sender_rows[recent_left][0] < stamp - 86400:
                old_event = sender_rows[recent_left][2]
                old_region = str(old_event.get("demo_origin_region") or "").strip()
                old_channel = str(old_event.get("demo_payment_channel") or "").strip()
                if old_region:
                    recent_region_counts[old_region] -= 1
                    if recent_region_counts[old_region] <= 0:
                        del recent_region_counts[old_region]
                if old_channel:
                    recent_channel_counts[old_channel] -= 1
                    if recent_channel_counts[old_channel] <= 0:
                        del recent_channel_counts[old_channel]
                recent_left += 1
            if not (target_start <= index < target_end):
                continue
            device = str(event.get("demo_device_id") or "").strip()
            usual_device = str(event.get("demo_sender_primary_device") or "").strip()
            region = str(event.get("demo_origin_region") or "").strip()
            usual_region = str(event.get("demo_sender_usual_region") or "").strip()
            channel = str(event.get("demo_payment_channel") or "").strip()
            purpose = str(event.get("demo_payment_purpose") or "").lower()
            invoice = str(event.get("demo_invoice_reference") or "").strip()
            description = str(event.get("Description", event.get("description", "")) or "").lower()

            if device and usual_device and device != usual_device:
                add(index, context_code_by_family["demo_device_mismatch"], f"Transaction device {device} differs from the sender's usual demo device {usual_device}.")
            linked_accounts = len(demo_device_accounts.get(device, ())) if device else 0
            if linked_accounts >= 3:
                add(index, context_code_by_family["demo_shared_device"], f"Fictional device {device} is linked to {linked_accounts} sender accounts in this examined event set.")
            if region and usual_region and region != usual_region:
                add(index, context_code_by_family["demo_region_mismatch"], f"Transaction origin {region} differs from the sender's usual demo region {usual_region}.")

            recent_regions = set(recent_region_counts)
            recent_channels = set(recent_channel_counts)
            if len(recent_regions) >= 2:
                add(index, context_code_by_family["demo_region_hopping"], f"The sender used {len(recent_regions)} demo regions within 24 hours: {', '.join(sorted(recent_regions))}.")
            if len(recent_channels) >= 3:
                add(index, context_code_by_family["demo_channel_mix"], f"The sender used {len(recent_channels)} demo payment channels within 24 hours: {', '.join(sorted(recent_channels))}.")

            if invoice and len(demo_invoices.get(invoice, ())) >= 2:
                add(index, context_code_by_family["demo_invoice_reuse"], f"Demo invoice {invoice} is linked to {len(demo_invoices[invoice])} different recipients.")
            business_purpose = any(word in purpose for word in ("business", "invoice", "supplier", "merchant"))
            if business_purpose and not invoice:
                add(index, context_code_by_family["demo_business_invoice_missing"], "The generated context labels this as a business payment but has no invoice reference.")
            mismatch_terms = {
                "salary": ("salary", "payroll", "wage"),
                "payroll": ("salary", "payroll", "wage"),
                "purchase": ("retail", "purchase", "order", "product"),
                "bill": ("utility", "electric", "water", "bill"),
                "business": ("invoice", "supplier", "business", "service"),
            }
            purpose_key = next((key for key in mismatch_terms if key in purpose), None)
            if purpose_key and not any(term in description for term in mismatch_terms[purpose_key]):
                add(index, context_code_by_family["demo_purpose_description_mismatch"], f"Demo purpose '{event.get('demo_payment_purpose')}' does not match description '{event.get('Description', event.get('description', ''))}'.")

    # Distinct combined flow/history/context checks. Process in timestamp order
    # and only compare with earlier transfers, so a live event never borrows
    # evidence from a later transaction. Per-account comparisons are bounded.
    advanced_codes = {
        item["family"]: item["code"] for item in PATTERN_CATALOG
        if item["family"].startswith("advanced_")
    }
    ordered_rows = sorted(rows, key=lambda item: (item[0], item[1]))
    prior_rows = []
    account_prior = defaultdict(list)
    pair_prior = defaultdict(list)
    contacts = defaultdict(set)
    invoice_amounts = defaultdict(list)
    prior_out_accounts = defaultdict(list)
    prior_two_hop_routes = defaultdict(list)
    prior_out_latest = {}

    def advanced_add(index, family, evidence):
        add(index, advanced_codes[family], evidence)

    def similar(left, right, tolerance=0.15):
        return left > 0 and right > 0 and abs(left - right) / max(left, right) <= tolerance

    for stamp, index, sender, receiver, amount in ordered_rows:
        event = events[index]
        # Limit expensive comparisons without discarding the normal short-term history.
        sender_history = account_prior[sender][-400:]
        receiver_history = account_prior[receiver][-400:]
        pair_history = pair_prior[(sender, receiver)]
        last_sender_event = account_prior[sender][-1] if account_prior[sender] else None
        last_receiver_event = account_prior[receiver][-1] if account_prior[receiver] else None
        recent_sender = [item for item in sender_history if stamp - item[0] <= 86400]
        recent_receiver = [item for item in receiver_history if stamp - item[0] <= 86400]
        if target_start <= index < target_end:
            # A branch account receives from several distinct accounts and then
            # sends to several others. Fire on each relevant edge in that branch.
            for node in (sender, receiver):
                history = account_prior[node][-400:]
                incoming = {item[2] for item in history if item[3] == node and stamp - item[0] <= 86400}
                outgoing = {item[3] for item in history if item[2] == node and stamp - item[0] <= 86400}
                if node == sender:
                    outgoing.add(receiver)
                else:
                    incoming.add(sender)
                if len(incoming) >= 3 and len(outgoing) >= 3:
                    advanced_add(index, "advanced_collection_then_payout", f"Account {node} received from {len(incoming)} accounts and sent to {len(outgoing)} accounts within 24 hours.")
                    advanced_add(index, "advanced_converging_splitting_branch", f"Transfer paths converge at {node} from {len(incoming)} accounts and branch to {len(outgoing)} accounts.")

            # New recipients compared with the same sender's earlier contacts.
            last_outgoing = [item for item in sender_history if item[2] == sender][-5:]
            earlier_recipients = {item[3] for item in sender_history[:-len(last_outgoing)] if item[2] == sender} if last_outgoing else set()
            recent_recipients = {item[3] for item in last_outgoing}
            recent_recipients.add(receiver)
            new_in_last_five = recent_recipients - earlier_recipients
            if len(earlier_recipients) >= 3 and len(new_in_last_five) >= 3:
                advanced_add(index, "advanced_new_counterparty_burst", f"At least {len(new_in_last_five)} of the sender's latest five outgoing contacts are new compared with earlier supplied history.")

            if pair_history and stamp - pair_history[-1][0] >= 30 * 86400:
                advanced_add(index, "advanced_long_gap_counterparty_return", f"This account pair resumed after a {int((stamp - pair_history[-1][0]) / 86400)}-day gap.")
            if last_sender_event and stamp - last_sender_event[0] >= 30 * 86400:
                advanced_add(index, "advanced_dormant_account_reactivation", f"Sender activity resumed after a {int((stamp - last_sender_event[0]) / 86400)}-day quiet period in the supplied history.")

            earlier_sender = [item for item in sender_history if item[2] == sender]
            if len(earlier_sender) >= 5:
                hour_counts = Counter(datetime.fromtimestamp(item[0], timezone.utc).hour for item in earlier_sender)
                current_hour = datetime.fromtimestamp(stamp, timezone.utc).hour
                if hour_counts[current_hour] == 0 or hour_counts[current_hour] / len(earlier_sender) <= 0.05:
                    advanced_add(index, "advanced_unusual_hour_for_account", f"Hour {current_hour:02d}:00 is uncommon in the sender's {len(earlier_sender)} earlier supplied outgoing transfers.")

            outgoing_24h = [item for item in recent_sender if item[2] == sender] + [(stamp, index, sender, receiver, amount, event)]
            if len(outgoing_24h) >= 3:
                first_burst = outgoing_24h[0]
                earlier_for_burst = [item for item in account_prior[sender] if item[0] < first_burst[0]]
                if earlier_for_burst and first_burst[0] - earlier_for_burst[-1][0] >= 7 * 86400:
                    quiet_days = int((first_burst[0] - earlier_for_burst[-1][0]) / 86400)
                    advanced_add(index, "advanced_burst_after_quiet_period", f"Outgoing activity resumed after a {quiet_days}-day quiet gap and reached {len(outgoing_24h)} transfers in 24 hours.")

            incident = sorted(sender_history, key=lambda item: (item[0], item[1]))[-6:]
            directions = ["out" if item[2] == sender else "in" if item[3] == sender else "other" for item in incident]
            directions.append("out")
            if len(directions) >= 5 and all(directions[pos] != directions[pos - 1] for pos in range(1, len(directions))):
                advanced_add(index, "advanced_alternating_incoming_outgoing", "The account's latest transfer directions alternate between receiving and sending across five or more events.")

            recent_coordinated = [item for item in prior_rows[-400:] if item[3] == receiver and stamp - item[0] <= 300 and item[2] != sender]
            if len({item[2] for item in recent_coordinated} | {sender}) >= 3:
                advanced_add(index, "advanced_coordinated_branch_timing", f"At least {len({item[2] for item in recent_coordinated} | {sender})} different senders paid {receiver} within five minutes.")

            # Same-value multi-step movement and a three-link return to origin.
            incoming_to_sender = [item for item in sender_history if item[3] == sender and stamp - item[0] <= 86400]
            if any(similar(prior[4], amount) for prior in incoming_to_sender):
                for prior in incoming_to_sender:
                    if not similar(prior[4], amount):
                        continue
                    earlier = [item for item in account_prior[prior[2]][-100:] if item[3] == prior[2] and stamp - item[0] <= 86400]
                    if any(similar(item[4], amount) for item in earlier):
                        advanced_add(index, "advanced_multi_hop_same_amount_chain", f"A similar amount can be followed through at least three earlier/current transfer steps ending at {receiver}.")
                        break
            # Search at most two previous links from current receiver back to sender.
            found_return = any(
                middle != sender
                and first_stamp <= prior_out_latest.get((middle, sender), 0) <= stamp
                and stamp - first_stamp <= 30 * 86400
                for first_stamp, middle in prior_out_accounts.get(receiver, ())[-400:]
                if stamp - first_stamp <= 30 * 86400
            )
            if found_return:
                advanced_add(index, "advanced_return_to_origin_three_step", f"This transfer closes a path from {sender} through at least two other accounts and back to {sender}.")

            # Multiple inbound payments approximately combined into a payout.
            incoming_values = [item[4] for item in incoming_to_sender[-6:] if item[4] > 0]
            if len(incoming_values) >= 2 and any(similar(sum(incoming_values[start:end]), amount, 0.10)
                                                 for start in range(len(incoming_values))
                                                 for end in range(start + 2, len(incoming_values) + 1)):
                advanced_add(index, "advanced_incoming_sum_paid_out", "This outgoing amount is close to the combined value of several recent incoming payments.")
            # Current plus earlier outgoing totals compared with the latest incoming transfer.
            prior_out_values = [item[4] for item in recent_sender if item[2] == sender and item[4] > 0]
            prior_in_values = [item[4] for item in sender_history if item[3] == sender and stamp - item[0] <= 86400 and item[4] > 0]
            if prior_in_values and similar(sum(prior_out_values) + amount, prior_in_values[-1], 0.10) and len(prior_out_values) >= 1:
                advanced_add(index, "advanced_incoming_amount_distributed", "Recent outgoing payments together are close to one recent incoming transfer.")

            # Repeatedly similar difference/retained percentage for pass-throughs.
            pass_pairs = []
            current_edge = (stamp, index, sender, receiver, amount, event)
            for incoming_event in incoming_to_sender[-20:]:
                for outgoing_event in recent_sender[-19:] + [current_edge]:
                    if outgoing_event[2] == sender and outgoing_event[0] >= incoming_event[0] and incoming_event[4] > 0:
                        pass_pairs.append((incoming_event[4] - outgoing_event[4], outgoing_event[4] / incoming_event[4]))
            if len(pass_pairs) >= 2:
                diffs = [item[0] for item in pass_pairs]
                ratios = [item[1] for item in pass_pairs]
                if any(abs(a - b) <= max(1.0, 0.02 * max(abs(a), abs(b))) for pos, a in enumerate(diffs) for b in diffs[pos + 1:]):
                    advanced_add(index, "advanced_fixed_fee_pass_through", "Several incoming/outgoing pairs retain a similar fixed amount during forwarding.")
                if any(abs(a - b) <= 0.02 for pos, a in enumerate(ratios) for b in ratios[pos + 1:]):
                    advanced_add(index, "advanced_proportional_fee_pass_through", "Several incoming/outgoing pairs retain a similar percentage during forwarding.")

            # Shared contact group and separate paths converging on current destination.
            shared = contacts[sender] & contacts[receiver]
            if len(shared) >= 3:
                advanced_add(index, "advanced_shared_contact_cluster", f"These endpoints share {len(shared)} transfer contacts in the supplied history.")
            two_hop_sources = {
                (source, middle) for route_stamp, source, middle in prior_two_hop_routes.get(receiver, ())[-400:]
                if stamp - route_stamp <= 30 * 86400
            }
            two_hop_sources.update(
                (source, sender) for source_stamp, source in prior_out_accounts.get(sender, ())[-50:]
                if stamp - source_stamp <= 30 * 86400 and source not in (sender, receiver)
            )
            distinct_sources = {source for source, _ in two_hop_sources}
            distinct_intermediaries = {mid for _, mid in two_hop_sources}
            if len(distinct_sources) >= 2 and len(distinct_intermediaries) >= 2:
                advanced_add(index, "advanced_same_destination_independent_paths", f"At least two separate two-step paths reach destination {receiver} through different intermediaries.")

            # Reverse of an earlier two-link path (A→B→C followed by C→B→A).
            prior_ab = any(item[2] == receiver and item[3] == sender for item in sender_history)
            prior_bc = any(item[2] == sender and item[3] != receiver for item in sender_history)
            if prior_ab and prior_bc:
                advanced_add(index, "advanced_reverse_transfer_path", "This transfer reverses a previously observed two-link route through the same accounts.")

            # Context changes are checked against the sender's earlier context and
            # connected inbound edge. Generated values remain demo-only evidence.
            device = str(event.get("demo_device_id") or "")
            usual_device = str(event.get("demo_sender_primary_device") or "")
            region = str(event.get("demo_origin_region") or "")
            usual_region = str(event.get("demo_sender_usual_region") or "")
            channel = str(event.get("demo_payment_channel") or "")
            purpose = str(event.get("demo_payment_purpose") or "").lower()
            merchant = str(event.get("demo_merchant_category") or "").lower()
            if device and usual_device and device != usual_device and len([item for item in recent_sender if item[2] == sender]) >= 2:
                advanced_add(index, "advanced_device_change_before_burst", "A different generated device appears with at least two other recent outgoing transfers.")
            if region and usual_region and region != usual_region and amount > 0:
                if any(similar(item[4], amount) for item in incoming_to_sender):
                    advanced_add(index, "advanced_region_shift_with_forwarding", "A transfer from a different usual demo region forwards an amount similar to recent incoming funds.")
            prior_regions = {str(item[5].get("demo_origin_region") or "") for item in sender_history if item[5].get("demo_origin_region")}
            if region and prior_regions and any(previous != region for previous in prior_regions):
                advanced_add(index, "advanced_region_inconsistent_connected_path", f"Connected activity includes different generated regions: {', '.join(sorted(prior_regions | {region}))}.")
            prior_channels = [str(item[5].get("demo_payment_channel") or "") for item in sender_history if item[2] == sender and item[5].get("demo_payment_channel")]
            if channel and prior_channels and channel != Counter(prior_channels).most_common(1)[0][0] and len({item[3] for item in recent_sender if item[2] == sender} | {receiver}) >= 3:
                advanced_add(index, "advanced_channel_change_before_burst", "The generated channel differs from the sender's earlier common channel as its recipient group expands.")
            prior_purposes = [str(item[5].get("demo_payment_purpose") or "").lower() for item in incoming_to_sender]
            if purpose and any(previous and previous != purpose and similar(item[4], amount) for item, previous in zip(incoming_to_sender, prior_purposes)):
                advanced_add(index, "advanced_purpose_shift_in_transfer_chain", "A similar amount is forwarded with a different generated payment-purpose label.")
            prior_merchants = [str(item[5].get("demo_merchant_category") or "").lower() for item in incoming_to_sender]
            if merchant and any(previous and previous != merchant and similar(item[4], amount) for item, previous in zip(incoming_to_sender, prior_merchants)):
                advanced_add(index, "advanced_merchant_category_shift", "A similar amount moves along the chain while its generated merchant category changes.")
            invoice = str(event.get("demo_invoice_reference") or "").strip()
            if invoice:
                for prior_amount, prior_index in invoice_amounts[invoice]:
                    if not similar(prior_amount, amount, 0.20):
                        advanced_add(index, "advanced_invoice_amount_inconsistency", f"Generated invoice {invoice} is attached to amounts differing by more than 20%.")
                        break
            context_changed = bool((device and usual_device and device != usual_device) or (region and usual_region and region != usual_region))
            if context_changed and len(earlier_sender) >= 3:
                baseline_values = [item[4] for item in earlier_sender[-20:] if item[4] > 0]
                if baseline_values and amount >= 3 * sorted(baseline_values)[len(baseline_values) // 2]:
                    advanced_add(index, "advanced_context_and_amount_change", "A generated device/region change coincides with a payment at least three times the sender's recent median amount.")
            if merchant and any(word in purpose for word in ("salary", "payroll", "wage")):
                advanced_add(index, "advanced_purpose_profile_mismatch", "Generated salary-like purpose conflicts with the recipient's generated merchant category.")
            relationship_types = {str(value).upper() for value in (event.get("demo_relationship_types") or [])}
            if relationship_types and not any(word in purpose or word in description for word in ("salary", "payroll", "wage", "business", "invoice", "supplier", "service", "owner", "capital", "dividend")):
                advanced_add(index, "advanced_relationship_purpose_mismatch", "The endpoints have a generated employment/ownership link, but the transfer has no matching generated purpose or description.")

        prior_rows.append((stamp, index, sender, receiver, amount, event))
        account_prior[sender].append((stamp, index, sender, receiver, amount, event))
        account_prior[receiver].append((stamp, index, sender, receiver, amount, event))
        pair_prior[(sender, receiver)].append((stamp, index))
        for source_stamp, source in prior_out_accounts.get(sender, ())[-50:]:
            if stamp - source_stamp <= 30 * 86400 and source not in (sender, receiver):
                prior_two_hop_routes[receiver].append((stamp, source, sender))
        prior_out_accounts[sender].append((stamp, receiver))
        prior_out_latest[(sender, receiver)] = stamp
        contacts[sender].add(receiver)
        contacts[receiver].add(sender)
        invoice = str(event.get("demo_invoice_reference") or "").strip()
        if invoice:
            invoice_amounts[invoice].append((amount, index))


    for index in results:
        results[index].sort(key=lambda item: item["code"])
    return results


def catalog_status(events):
    """Describe check coverage without implying missing evidence is clear."""
    usable = sum(
        bool(event.get("Sender") and event.get("Receiver") and _timestamp(event) is not None)
        for event in events
    )
    return {
        "total_checks": len(PATTERN_CATALOG),
        "transaction_rows_with_required_fields": usable,
        "transaction_rows_seen": len(events),
        "external_sources_checked": False,
    }
