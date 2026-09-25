from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


TOOL_ACTORS = {
    "get_order": ("order-agent", "VERIFY_ORDER"),
    "get_order_items": ("item-agent", "VERIFY_ORDER_ITEMS"),
    "get_order_payments": ("payment-agent", "VERIFY_ORDER_PAYMENTS"),
    "get_payment_timeline": ("payment-agent", "VERIFY_PAYMENT_TIMELINE"),
    "get_refund_timeline": ("payment-agent", "VERIFY_REFUND_TIMELINE"),
    "get_shipment_summary": ("shipment-agent", "VERIFY_SHIPMENT"),
    "get_sellers": ("seller-agent", "VERIFY_SELLERS"),
    "get_product_context": ("item-agent", "VERIFY_PRODUCT_CONTEXT"),
    "get_policy": ("policy-agent", "VERIFY_POLICY"),
}

TOPIC_TO_TOOLS = {
    "canceled_order_paid": {"get_order_payments", "get_payment_timeline"},
    "unavailable_order_paid": {"get_order_items", "get_order_payments"},
    "late_delivery_seller": {"get_shipment_summary", "get_order_items", "get_sellers"},
    "late_delivery_logistics": {"get_shipment_summary", "get_order_items"},
    "valid_split_payment": {"get_order_payments", "get_payment_timeline"},
    "payment_mismatch": {"get_order_payments", "get_payment_timeline"},
    "duplicate_charge": {"get_order_payments", "get_payment_timeline"},
    "refund_pending": {"get_refund_timeline", "get_payment_timeline"},
    "refund_failed": {"get_refund_timeline", "get_payment_timeline"},
    "unsupported_claim": {
        "get_order_items",
        "get_order_payments",
        "get_payment_timeline",
        "get_refund_timeline",
        "get_shipment_summary",
        "get_sellers",
        "get_product_context",
    },
}

TOOL_DOMAINS = {
    "get_order": "order",
    "get_order_items": "item",
    "get_order_payments": "payment",
    "get_payment_timeline": "payment",
    "get_refund_timeline": "refund",
    "get_shipment_summary": "shipment",
    "get_sellers": "seller",
    "get_product_context": "product",
    "get_policy": "policy",
}

REFUND_REQUEST_TOOLS = {
    "get_policy",
    "get_order_payments",
    "get_payment_timeline",
    "get_refund_timeline",
}
PRIMARY_ISSUE_TOPICS = set(TOPIC_TO_TOOLS)


def _rows(data: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict) and isinstance(data.get(key), list):
        return [row for row in data[key] if isinstance(row, dict)]
    return []


def _date(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _verified_issue(data: dict[str, Any], claims: list[dict[str, Any]]) -> str | None:
    order = data.get("get_order", {}).get("data", {})
    payment = data.get("get_payment_timeline", {}).get("data", {})
    events = _rows(payment, "events")
    refund = data.get("get_refund_timeline", {}).get("data", {})
    refund_events = _rows(refund, "events")
    shipment = data.get("get_shipment_summary", {}).get("data", {})
    topics = {claim.get("topic") for claim in claims}
    if "canceled_order_paid" in topics and order.get("order_status") == "canceled" and any(
        event.get("event_type") == "captured" and event.get("status") == "confirmed" for event in events
    ):
        return "canceled_order_paid"
    if "unavailable_order_paid" in topics and order.get("order_status") in {"unavailable", "not_found"} and any(
        event.get("event_type") == "captured" and event.get("status") == "confirmed" for event in events
    ):
        return "unavailable_order_paid"
    if "late_delivery_seller" in topics or "late_delivery_logistics" in topics:
        delivered = _date(shipment.get("delivered_customer_at"))
        estimate = _date(shipment.get("estimated_delivery_at"))
        late_events = [event for event in _rows(shipment, "events") if event.get("event_type") == "delivered_late" and event.get("status") == "confirmed"]
        if delivered and estimate and delivered > estimate and late_events:
            actor = late_events[-1].get("actor")
            issue = "late_delivery_logistics" if actor == "logistics_provider" else "late_delivery_seller" if actor == "seller" else None
            if issue in topics:
                return issue
    if "duplicate_charge" in topics:
        confirmed = [event for event in events if event.get("event_type") == "captured" and event.get("status") == "confirmed"]
        amounts: dict[str, int] = {}
        for event in confirmed:
            amounts[str(event.get("amount_brl"))] = amounts.get(str(event.get("amount_brl")), 0) + 1
        if any(count > 1 for count in amounts.values()):
            return "duplicate_charge"
    if "payment_mismatch" in topics and any(event.get("event_type") == "reconciliation_mismatch" and event.get("status") == "open" for event in events):
        return "payment_mismatch"
    if "refund_failed" in topics and any(event.get("status") == "failed" for event in refund_events):
        return "refund_failed"
    if "refund_pending" in topics and any(event.get("status") == "pending" for event in refund_events):
        return "refund_pending"
    if "valid_split_payment" in topics:
        payments = _rows(payment, "payments")
        types = {row.get("payment_type") for row in payments}
        if len(types) > 1 and all(event.get("event_type") == "captured" and event.get("status") == "confirmed" for event in events):
            return "valid_split_payment"
    if "unsupported_claim" in topics and not any(
        issue in topics for issue in PRIMARY_ISSUE_TOPICS - {"unsupported_claim", "valid_split_payment"}
    ):
        return "unsupported_claim"
    return None


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    case_id = case["case_id"]
    request = case.get("customer_request", {})
    order_id = request.get("claimed_order_id")
    if not isinstance(order_id, str) or not order_id:
        raise ValueError(f"{case_id}: customer_request.claimed_order_id is required")
    claims = request.get("claims", [])
    if not isinstance(claims, list) or len(claims) > 5:
        raise ValueError(f"{case_id}: customer_request.claims must be an array of at most 5 claims")

    discovered = set(await gateway.list_tools())
    requested_tools = {"get_order", "get_policy"}
    for claim in claims:
        if not isinstance(claim, dict) or not isinstance(claim.get("claim_id"), str):
            raise ValueError(f"{case_id}: each claim must have a string claim_id")
        requested_tools.update(TOPIC_TO_TOOLS.get(claim.get("topic"), set()))
    tools_to_call = sorted(requested_tools & discovered)
    evidence_refs: list[str] = []
    evidence_by_tool: dict[str, dict[str, Any]] = {}
    failed_tools: list[str] = []

    for tool_name in tools_to_call:
        actor, decision_code = TOOL_ACTORS[tool_name]
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=actor,
            decision_code=decision_code,
        )
        arguments = (
            {"policy_version": case.get("policy_version", "")}
            if tool_name == "get_policy"
            else {"order_id": order_id}
        )
        try:
            evidence = await gateway.call(tool_name, case_id=case_id, **arguments)
        except RuntimeError:
            trace.emit(
                case_id=case_id,
                event_type="task_assigned",
                actor="coordinator",
                target=actor,
                decision_code="RETRY_MCP_TOOL_ONCE",
            )
            await asyncio.sleep(0.25)
            try:
                evidence = await gateway.call(tool_name, case_id=case_id, **arguments)
            except (RuntimeError, ValueError):
                failed_tools.append(tool_name)
                trace.emit(
                    case_id=case_id,
                    event_type="handoff",
                    actor=actor,
                    target="coordinator",
                    decision_code="MCP_TOOL_FAILED",
                    attributes={"tool_name": tool_name},
                )
                continue
        except ValueError:
            failed_tools.append(tool_name)
            trace.emit(
                case_id=case_id,
                event_type="handoff",
                actor=actor,
                target="coordinator",
                decision_code="MCP_TOOL_FAILED",
                attributes={"tool_name": tool_name},
            )
            continue

        expected_domain = TOOL_DOMAINS[tool_name]
        if evidence.get("domain") != expected_domain:
            failed_tools.append(tool_name)
            trace.emit(
                case_id=case_id,
                event_type="handoff",
                actor=actor,
                target="coordinator",
                decision_code="UNEXPECTED_EVIDENCE_DOMAIN",
                attributes={"tool_name": tool_name},
            )
            continue
        data = evidence.get("data")
        if tool_name == "get_order" and isinstance(data, dict):
            returned_order_id = data.get("order_id")
            if isinstance(returned_order_id, str) and returned_order_id != order_id:
                failed_tools.append(tool_name)
                trace.emit(
                    case_id=case_id,
                    event_type="handoff",
                    actor=actor,
                    target="coordinator",
                    decision_code="ORDER_ID_MISMATCH",
                    attributes={"tool_name": tool_name},
                )
                continue
        evidence_by_tool[tool_name] = evidence
        evidence_refs.append(evidence["evidence_ref"])
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool_name,
            evidence_refs=[evidence["evidence_ref"]],
        )
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=actor,
            target="coordinator",
            decision_code="EVIDENCE_READY_FOR_SYNTHESIS",
            evidence_refs=[evidence["evidence_ref"]],
        )

    primary_issue = _verified_issue(
        {name: result for name, result in evidence_by_tool.items()}, claims
    )
    policy_data = evidence_by_tool.get("get_policy", {}).get("data", {})
    policy_rule = policy_data.get("rules", {}).get(primary_issue, {}) if primary_issue else {}
    issue_refs = [
        evidence_by_tool[name]["evidence_ref"]
        for name in sorted(TOPIC_TO_TOOLS.get(primary_issue, set()) | {"get_order", "get_policy"})
        if name in evidence_by_tool
    ]
    issue_refs = list(dict.fromkeys(issue_refs))[:30]
    fully_verified = bool(primary_issue and policy_rule and issue_refs and not failed_tools)
    if primary_issue is None:
        primary_issue = "insufficient_evidence"
    if primary_issue == "unsupported_claim":
        case_status = "no_action"
    else:
        case_status = policy_rule.get("case_status", "needs_investigation")
    confidence = 0.9 if fully_verified else 0.35 if issue_refs else 0.1

    claim_assessments = []
    for claim in claims:
        topic = claim.get("topic")
        refs = list(dict.fromkeys(
            evidence_by_tool[name]["evidence_ref"]
            for name in sorted(TOPIC_TO_TOOLS.get(topic, set()) | {"get_order", "get_policy"})
            if name in evidence_by_tool
        ))[:30]
        if topic == "requested_full_refund":
            verdict = "supported" if fully_verified and float(policy_rule.get("refund_brl", 0)) > 0 else "unsupported" if primary_issue in {"valid_split_payment", "unsupported_claim"} else "insufficient_evidence"
            claim_confidence = confidence if verdict != "insufficient_evidence" else 0.2
        elif topic == primary_issue:
            verdict, claim_confidence = "supported", confidence
        elif topic in PRIMARY_ISSUE_TOPICS and primary_issue and primary_issue != "insufficient_evidence":
            verdict, claim_confidence = "unsupported", min(confidence, 0.8)
        else:
            verdict, claim_confidence = "insufficient_evidence", 0.2
        claim_assessments.append({"claim_id": claim["claim_id"], "verdict": verdict, "confidence": claim_confidence, "evidence_refs": refs})

    rules = policy_data.get("rules", {})
    financial_resolution = {"currency": "BRL", "recommended_refund_brl": 0, "refund_lines": []}
    responsible_parties: list[dict[str, Any]] = []
    resolution_actions: list[str] = []
    if fully_verified:
        refund = Decimal(str(policy_rule.get("refund_brl", 0)))
        try:
            refund_amount = float(refund.quantize(Decimal("0.01")))
        except InvalidOperation:
            refund_amount = 0
        financial_resolution["recommended_refund_brl"] = refund_amount
        if refund_amount > 0:
            financial_resolution["refund_lines"] = [{"reason_code": primary_issue, "amount_brl": refund_amount, "entity_id": order_id}]
        responsible_parties = policy_rule.get("responsible_parties", [])[:5]
        action = policy_rule.get("recommended_action")
        if isinstance(action, str):
            resolution_actions = [action]
    if primary_issue == "insufficient_evidence":
        responsible_parties = [{"party_type": "unknown", "party_id": None}]

    order_data = evidence_by_tool.get("get_order", {}).get("data", {})
    item_rows = _rows(evidence_by_tool.get("get_order_items", {}).get("data"), "items")
    payment_data = evidence_by_tool.get("get_payment_timeline", {}).get("data", {})
    shipment_data = evidence_by_tool.get("get_shipment_summary", {}).get("data", {})
    item_ids = list(dict.fromkeys(row.get("order_item_id") for row in item_rows if isinstance(row.get("order_item_id"), str)))[:20]
    seller_ids = list(dict.fromkeys(row.get("seller_id") for row in item_rows if isinstance(row.get("seller_id"), str)))[:20]
    payment_ids = list(dict.fromkeys(str(row.get("payment_sequential")) for row in _rows(payment_data, "payments") if row.get("payment_sequential") is not None))[:20]
    if fully_verified:
        trace.emit(case_id=case_id, event_type="policy_decided", actor="policy-agent", decision_code=policy_rule.get("recommended_action", primary_issue), evidence_refs=issue_refs)
        trace.emit(case_id=case_id, event_type="verification_completed", actor="verifier", decision_code="POLICY_AND_EVIDENCE_CONSISTENT", evidence_refs=issue_refs, attributes={"successful_tool_count": len(evidence_by_tool), "failed_tool_count": len(failed_tools)})
    else:
        trace.emit(case_id=case_id, event_type="policy_decided", actor="policy-agent", decision_code="POLICY_EVIDENCE_UNAVAILABLE" if "get_policy" not in evidence_by_tool else "INSUFFICIENT_VERIFIED_FACTS", evidence_refs=list(dict.fromkeys(evidence_refs))[:30])
        trace.emit(case_id=case_id, event_type="verification_completed", actor="verifier", decision_code="MISSING_REQUIRED_EVIDENCE" if failed_tools or len(evidence_by_tool) != len(tools_to_call) else "INSUFFICIENT_VERIFIED_FACTS", evidence_refs=list(dict.fromkeys(evidence_refs))[:30], attributes={"successful_tool_count": len(evidence_by_tool), "failed_tool_count": len(failed_tools)})
    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "case_status": case_status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": [order_id] if "get_order" in evidence_by_tool else [],
            "item_ids": item_ids,
            "seller_ids": seller_ids,
            "payment_references": payment_ids,
            "shipment_ids": [shipment_data["shipment_id"]] if isinstance(shipment_data, dict) and isinstance(shipment_data.get("shipment_id"), str) else [],
        },
        "claim_assessments": claim_assessments,
        "root_cause_analysis": {"ranked_causes": [{"cause_code": primary_issue.upper(), "rank": 1}] if primary_issue != "insufficient_evidence" else [], "responsible_parties": responsible_parties},
        "evidence_refs": list(dict.fromkeys(evidence_refs)),
        "data_conflicts": [],
        "financial_resolution": financial_resolution,
        "resolution_actions": resolution_actions,
    }
