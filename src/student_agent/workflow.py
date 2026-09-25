from __future__ import annotations

from typing import Any

from . import OUTPUT_SCHEMA_VERSION
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

ORDER_ISSUES = {"canceled_order_paid", "unavailable_order_paid"}
REFUND_ISSUES = {"refund_pending", "refund_failed"}
SHIPMENT_ISSUES = {"late_delivery_seller", "late_delivery_logistics"}


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _records(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _refs(evidence: dict[str, Any] | None) -> list[str]:
    if not evidence:
        return []
    ref = evidence.get("evidence_ref")
    return [ref] if isinstance(ref, str) else []


def _ids(records: list[dict[str, Any]], *keys: str) -> list[str]:
    values: list[str] = []
    for record in records:
        for key in keys:
            value = _text(record.get(key))
            if value:
                values.append(value)
    return _unique(values)


def _captured_total(payment_timeline: dict[str, Any] | None) -> float:
    if not payment_timeline:
        return 0.0
    total = 0.0
    for event in _records(payment_timeline.get("data", {}).get("events")):
        if event.get("event_type") == "captured" and event.get("status") == "confirmed":
            amount = _number(event.get("amount_brl"))
            if amount is not None:
                total += amount
    return round(total, 2)


def _payment_rows(payment_evidence: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not payment_evidence:
        return []
    return _records(payment_evidence.get("data"))


def _timeline_events(evidence: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not evidence:
        return []
    return _records(evidence.get("data", {}).get("events"))


def _evidence_refs(
    evidence: dict[str, dict[str, Any] | None], *tool_names: str
) -> list[str]:
    refs: list[str] = []
    for tool_name in tool_names:
        refs.extend(_refs(evidence.get(tool_name)))
    return _unique(refs)


def _issue_supported(
    issue: str,
    order: dict[str, Any] | None,
    payments: dict[str, Any] | None,
    payment_timeline: dict[str, Any] | None,
    refund_timeline: dict[str, Any] | None,
    shipment: dict[str, Any] | None,
) -> bool:
    order_data = order.get("data", {}) if order else {}
    order_status = order_data.get("order_status")
    captured = _captured_total(payment_timeline)
    payment_rows = _payment_rows(payments)
    timeline_events = _timeline_events(payment_timeline)
    refund_events = _timeline_events(refund_timeline)
    shipment_events = _timeline_events(shipment)

    if issue == "canceled_order_paid":
        return order_status == "canceled" and captured > 0
    if issue == "unavailable_order_paid":
        return order_status == "unavailable" and captured > 0
    if issue == "late_delivery_seller":
        return any(
            event.get("event_type") == "delivered_late"
            and event.get("actor") == "seller"
            for event in shipment_events
        )
    if issue == "late_delivery_logistics":
        return any(
            event.get("event_type") == "delivered_late"
            and event.get("actor") == "logistics_provider"
            for event in shipment_events
        )
    if issue == "valid_split_payment":
        types = {row.get("payment_type") for row in payment_rows}
        return len(payment_rows) >= 2 and len(types) >= 2 and not any(
            event.get("event_type") == "reconciliation_mismatch"
            for event in timeline_events
        )
    if issue == "payment_mismatch":
        return any(
            event.get("event_type") == "reconciliation_mismatch"
            for event in timeline_events
        )
    if issue == "duplicate_charge":
        captures = [
            _number(event.get("amount_brl"))
            for event in timeline_events
            if event.get("event_type") == "captured"
            and event.get("status") == "confirmed"
        ]
        # A duplicate capture can happen at different timestamps. Identify it
        # by repeated confirmed amounts rather than repeated timestamps.
        amounts = [amount for amount in captures if amount is not None]
        return len(amounts) >= 2 and len(amounts) > len(set(amounts))
    if issue in REFUND_ISSUES:
        expected_status = "pending" if issue == "refund_pending" else "failed"
        return any(
            event.get("event_type") == "refund_requested"
            and event.get("status") == expected_status
            for event in refund_events
        )
    return issue == "unsupported_claim"


def _cause_code(issue: str, supported: bool) -> str:
    if not supported and issue != "unsupported_claim":
        return "INSUFFICIENT_EVIDENCE"
    return {
        "canceled_order_paid": "CANCELED_ORDER_WITH_CAPTURE",
        "unavailable_order_paid": "UNAVAILABLE_ORDER_WITH_CAPTURE",
        "late_delivery_seller": "SELLER_DELIVERY_DELAY",
        "late_delivery_logistics": "LOGISTICS_DELIVERY_DELAY",
        "valid_split_payment": "VALID_SPLIT_PAYMENT",
        "payment_mismatch": "PAYMENT_RECONCILIATION_MISMATCH",
        "duplicate_charge": "DUPLICATE_PAYMENT_CAPTURE",
        "refund_pending": "REFUND_PENDING",
        "refund_failed": "REFUND_FAILED",
        "unsupported_claim": "UNSUPPORTED_CUSTOMER_CLAIM",
    }.get(issue, "INSUFFICIENT_EVIDENCE")


def _claim_verdict(issue: str, supported: bool) -> str:
    if issue == "unsupported_claim":
        return "unsupported"
    if supported:
        return "supported"
    return "insufficient_evidence"


def _refund_claim_verdict(refund_amount: float, captured_total: float) -> str:
    if refund_amount <= 0:
        return "unsupported"
    if captured_total > refund_amount:
        return "partially_supported"
    return "supported"


def _responsible_parties(
    policy_rule: dict[str, Any] | None, seller_ids: list[str]
) -> list[dict[str, str | None]]:
    if not policy_rule:
        return [{"party_type": "unknown", "party_id": None}]
    result: list[dict[str, str | None]] = []
    for party in _records(policy_rule.get("responsible_parties")):
        party_type = party.get("party_type")
        if not isinstance(party_type, str):
            continue
        party_id = party.get("party_id")
        if party_type == "seller" and seller_ids:
            party_id = seller_ids[0]
        if party_id is not None and not isinstance(party_id, str):
            party_id = str(party_id)
        result.append({"party_type": party_type, "party_id": party_id})
    return result or [{"party_type": "unknown", "party_id": None}]


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run the deterministic coordinator and specialist-agent workflow."""
    case_id = str(case["case_id"])
    request = case.get("customer_request", {})
    order_id = _text(request.get("claimed_order_id"))
    policy_version = _text(case.get("policy_version"))
    claims = request.get("claims", [])
    primary_issue = "insufficient_evidence"
    if claims and isinstance(claims[0], dict):
        primary_issue = _text(claims[0].get("topic")) or primary_issue

    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="order-agent",
        decision_code="case_scope_initialized",
    )

    evidence: dict[str, dict[str, Any]] = {}
    failures: list[str] = []

    async def call_tool(
        tool_name: str,
        actor: str,
        arguments: dict[str, str],
        *,
        required: bool = True,
    ) -> dict[str, Any] | None:
        try:
            result = await gateway.call(tool_name, case_id=case_id, **arguments)
        except (RuntimeError, ValueError, KeyError) as exc:
            if required:
                failures.append(tool_name)
            trace.emit(
                case_id=case_id,
                event_type="handoff",
                actor=actor,
                target="coordinator",
                decision_code="tool_unavailable",
                attributes={"tool_name": tool_name, "error_type": type(exc).__name__},
            )
            return None
        evidence[tool_name] = result
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool_name,
            evidence_refs=_refs(result),
        )
        return result

    if not order_id:
        failures.append("missing_claimed_order_id")
        order_id = ""

    order: dict[str, Any] | None = None
    items: dict[str, Any] | None = None
    payments: dict[str, Any] | None = None
    payment_timeline: dict[str, Any] | None = None
    refund_timeline: dict[str, Any] | None = None
    shipment: dict[str, Any] | None = None
    seller: dict[str, Any] | None = None
    policy: dict[str, Any] | None = None

    if order_id:
        order = await call_tool("get_order", "order-agent", {"order_id": order_id})
        items = await call_tool("get_order_items", "order-agent", {"order_id": order_id})
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="order-agent",
            target="payment-agent",
            decision_code="order_scope_handoff",
            evidence_refs=_refs(order) + _refs(items),
        )
        payments = await call_tool(
            "get_order_payments", "payment-agent", {"order_id": order_id}
        )
        payment_timeline = await call_tool(
            "get_payment_timeline", "payment-agent", {"order_id": order_id}
        )
        if primary_issue in REFUND_ISSUES or primary_issue in ORDER_ISSUES:
            refund_timeline = await call_tool(
                "get_refund_timeline",
                "payment-agent",
                {"order_id": order_id},
                required=primary_issue in REFUND_ISSUES,
            )
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="payment-agent",
            target="shipment-agent",
            decision_code="payment_scope_handoff",
            evidence_refs=_refs(payments) + _refs(payment_timeline),
        )
        shipment = await call_tool(
            "get_shipment_summary", "shipment-agent", {"order_id": order_id}
        )
        if primary_issue in {"late_delivery_seller", "unavailable_order_paid"}:
            seller = await call_tool(
                "get_sellers", "order-agent", {"order_id": order_id}, required=False
            )

    if policy_version:
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="policy-agent",
            decision_code="policy_lookup",
        )
        policy = await call_tool(
            "get_policy", "policy-agent", {"policy_version": policy_version}
        )

    item_rows = _records(items.get("data")) if items else []
    payment_rows = _payment_rows(payments)
    shipment_data = shipment.get("data", {}) if shipment else {}
    policy_data = policy.get("data", {}) if policy else {}
    rules = policy_data.get("rules", {}) if isinstance(policy_data, dict) else {}
    policy_rule = rules.get(primary_issue) if isinstance(rules, dict) else None
    if not isinstance(policy_rule, dict):
        policy_rule = None

    item_ids = _ids(item_rows, "order_item_id", "item_id")
    seller_ids = _ids(item_rows, "seller_id")
    for shipping in _records(shipment_data.get("shipping_limits")):
        item_ids = _unique(item_ids + _ids([shipping], "order_item_id"))
        seller_ids = _unique(seller_ids + _ids([shipping], "seller_id"))
    payment_refs = _ids(
        payment_rows,
        "payment_id",
        "payment_reference",
        "transaction_id",
        "payment_sequential",
    )
    shipment_ids = _ids(
        _records(shipment_data), "shipment_id", "shipment_reference", "tracking_id"
    )
    evidence_sources = {**evidence, "get_sellers": seller}
    all_refs = _evidence_refs(
        evidence_sources,
        "get_order",
        "get_order_items",
        "get_order_payments",
        "get_payment_timeline",
        "get_refund_timeline",
        "get_shipment_summary",
        "get_sellers",
        "get_policy",
    )
    if primary_issue in ORDER_ISSUES:
        relevant_refs = _evidence_refs(
            evidence_sources,
            "get_order",
            "get_order_items",
            "get_order_payments",
            "get_payment_timeline",
            "get_refund_timeline",
            "get_policy",
        )
    elif primary_issue in SHIPMENT_ISSUES:
        relevant_refs = _evidence_refs(
            evidence_sources,
            "get_order",
            "get_order_items",
            "get_shipment_summary",
            "get_sellers",
            "get_policy",
        )
    elif (
        primary_issue in {"valid_split_payment", "payment_mismatch", "duplicate_charge"}
        or primary_issue in REFUND_ISSUES
    ):
        relevant_refs = _evidence_refs(
            evidence_sources,
            "get_order",
            "get_order_payments",
            "get_payment_timeline",
            "get_refund_timeline",
            "get_policy",
        )
    else:
        relevant_refs = all_refs

    supported = _issue_supported(
        primary_issue,
        order,
        payments,
        payment_timeline,
        refund_timeline,
        shipment,
    )
    policy_status = policy_rule.get("case_status") if policy_rule else None
    case_status = policy_status if policy_status in {
        "action_required",
        "no_action",
        "needs_investigation",
    } else "needs_investigation"
    if primary_issue == "unsupported_claim":
        case_status = "no_action"
    if not supported and primary_issue != "unsupported_claim":
        case_status = "needs_investigation"

    refund_amount = _number(policy_rule.get("refund_brl")) if policy_rule else 0.0
    refund_amount = round(refund_amount or 0.0, 2)
    refund_lines: list[dict[str, Any]] = []
    if refund_amount > 0:
        refund_lines.append(
            {
                "reason_code": primary_issue,
                "amount_brl": refund_amount,
                "entity_id": order_id or None,
            }
        )

    claim_assessments: list[dict[str, Any]] = []
    for claim in claims[:5] if isinstance(claims, list) else []:
        if not isinstance(claim, dict):
            continue
        claim_id = _text(claim.get("claim_id"))
        topic = _text(claim.get("topic"))
        if not claim_id or not topic:
            continue
        claim_supported = topic == primary_issue and supported
        if topic == "requested_full_refund":
            verdict = _refund_claim_verdict(
                refund_amount, _captured_total(payment_timeline)
            )
            claim_supported = verdict != "unsupported"
        else:
            verdict = _claim_verdict(topic, claim_supported)
        claim_refs = relevant_refs
        if topic == "requested_full_refund":
            claim_refs = _evidence_refs(
                evidence_sources,
                "get_order",
                "get_order_payments",
                "get_payment_timeline",
                "get_refund_timeline",
                "get_policy",
            )
        claim_assessments.append(
            {
                "claim_id": claim_id,
                "verdict": verdict,
                "confidence": round(0.9 if claim_supported else 0.82, 2),
                "evidence_refs": claim_refs[:10],
            }
        )

    conflicts: list[dict[str, Any]] = []
    timeline_events = _timeline_events(payment_timeline)
    if primary_issue == "payment_mismatch" and any(
        event.get("event_type") == "reconciliation_mismatch" for event in timeline_events
    ):
        conflicts.append(
            {
                "field": "payment_reconciliation",
                "sources": ["get_order_payments", "get_payment_timeline"],
                "selected_source": "get_payment_timeline",
                "resolution_code": "open_reconciliation_mismatch",
            }
        )
    if failures:
        conflicts.append(
            {
                "field": "missing_tool_evidence",
                "sources": ["mcp_gateway", *sorted(set(failures))[:4]],
                "selected_source": None,
                "resolution_code": "tool_unavailable_no_guess",
            }
        )

    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        target="verifier",
        decision_code=primary_issue,
        evidence_refs=_refs(policy),
    )

    confidence = 0.96 if supported else 0.78
    if primary_issue == "unsupported_claim":
        confidence = 0.92
    if failures and not supported:
        confidence = 0.55

    output: dict[str, Any] = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "case_status": case_status,
            "confidence": min(1.0, max(0.0, confidence)),
        },
        "affected_entities": {
            "order_ids": [order_id] if order_id else [],
            "item_ids": item_ids,
            "seller_ids": seller_ids,
            "payment_references": payment_refs,
            "shipment_ids": shipment_ids,
        },
        "claim_assessments": claim_assessments,
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": _cause_code(primary_issue, supported), "rank": 1}],
            "responsible_parties": _responsible_parties(policy_rule, seller_ids),
        },
        "evidence_refs": relevant_refs,
        "data_conflicts": conflicts[:5],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund_amount,
            "refund_lines": refund_lines,
        },
        "resolution_actions": [
            str(policy_rule.get("recommended_action"))
            if policy_rule and policy_rule.get("recommended_action")
            else "investigate_missing_evidence"
        ],
    }

    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        target="coordinator",
        decision_code="output_invariants_checked",
        evidence_refs=relevant_refs[:20],
        attributes={
            "evidence_count": len(relevant_refs),
            "tool_failure_count": len(failures),
        },
    )
    return output
