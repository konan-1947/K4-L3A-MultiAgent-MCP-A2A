from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case


class FakeGateway:
    def __init__(self, failed: set[str] | None = None) -> None:
        self.failed = failed or set()
        self.called: list[str] = []

    async def list_tools(self) -> list[str]:
        return [
            "get_order",
            "get_order_items",
            "get_order_payments",
            "get_payment_timeline",
            "get_refund_timeline",
            "get_shipment_summary",
            "get_sellers",
            "get_product_context",
            "get_policy",
        ]

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.called.append(tool_name)
        if tool_name in self.failed:
            raise RuntimeError("unavailable")
        domain = {
            "get_order": "order",
            "get_order_items": "item",
            "get_order_payments": "payment",
            "get_payment_timeline": "payment",
            "get_refund_timeline": "refund",
            "get_shipment_summary": "shipment",
            "get_sellers": "seller",
            "get_product_context": "product",
            "get_policy": "policy",
        }[tool_name]
        data = (
            {"order_id": arguments["order_id"]}
            if tool_name == "get_order"
            else {"rows": [{"case_id": case_id}]}
        )
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{tool_name}_{'a' * 20}",
            "result_hash": f"sha256:{'b' * 64}",
            "domain": domain,
            "data": data,
        }


@pytest.mark.parametrize("failed_tools", [set(), {"get_sellers"}])
def test_solve_case_routes_tools_and_emits_valid_contracts(
    tmp_path: Path, failed_tools: set[str]
) -> None:
    async def run() -> tuple[dict[str, Any], FakeGateway, Path]:
        root = Path(__file__).resolve().parents[1]
        contracts = Contracts(root / "contracts" / "schemas")
        trace_path = tmp_path / "trace.jsonl"
        trace = TraceWriter(trace_path, contracts)
        gateway = FakeGateway(failed_tools)
        case = {
            "case_id": "CASE_001",
            "policy_version": "POLICY_V1",
            "customer_request": {
                "claimed_order_id": "order-001",
                "claims": [{"claim_id": "claim-001", "topic": "late_delivery_seller"}],
            },
        }
        output = await solve_case(case, gateway, trace)
        contracts.validate_output(output, "test output")
        trace_events = [
            json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()
        ]
        for event in trace_events:
            contracts.validate_trace(event, "test trace")
        event_types = [event["event_type"] for event in trace_events]
        assert event_types.index("tool_result_consumed") < event_types.index("handoff")
        assert event_types.index("policy_decided") < event_types.index("verification_completed")
        return output, gateway, trace_path

    output, gateway, trace_path = asyncio.run(run())
    assert set(gateway.called) == {
        "get_order",
        "get_order_items",
        "get_shipment_summary",
        "get_sellers",
        "get_policy",
    }
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["claim_assessments"][0]["verdict"] == "insufficient_evidence"
    if "get_sellers" in failed_tools:
        trace_lines = trace_path.read_text(encoding="utf-8").splitlines()
        assert any(json.loads(line).get("decision_code") == "MCP_TOOL_FAILED" for line in trace_lines)
