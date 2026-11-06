from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from agent_control_spec import ActivatedPolicy
from agent_hooks import AgentContextBuilder, Decision

from banking_agent import INITIAL_BALANCE_CENTS, MANIFEST_PATH, BankingLedger
from demo import SCENARIOS, SCENARIOS_BY_NAME, Scenario, run_scenario


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda scenario: scenario.name)
async def test_real_native_acs_scenarios(scenario: Scenario) -> None:
    result = await run_scenario(scenario)
    result.verify()


@pytest.mark.parametrize("amount", [True, False, 1.5, "25000", None])
async def test_invalid_amount_types_are_denied_before_tool_execution(
    amount: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = replace(
        SCENARIOS_BY_NAME["invalid-amount"],
        arguments={
            "account_id": "checking-001",
            "payee_id": "utility-company",
            "amount_cents": amount,
            "request_id": "malformed-tool-amount",
        },
    )
    monkeypatch.setattr("demo.SCENARIOS", (scenario,))
    result = await run_scenario(scenario)
    result.verify()
    assert result.ledger.tool_calls == []


@pytest.mark.parametrize(
    "name", ["small-transfer", "large-transfer", "tool-redaction", "output-redaction"]
)
async def test_streaming_is_also_enforced(name: str) -> None:
    result = await run_scenario(SCENARIOS_BY_NAME[name], streaming=True)
    result.verify()


async def test_all_eight_lifecycle_points_are_emitted() -> None:
    result = await run_scenario(SCENARIOS_BY_NAME["balance"])
    points = [record.interception_point.value for record in result.audit.records]
    assert points == [
        "agent_startup",
        "input",
        "pre_model_call",
        "post_model_call",
        "pre_tool_call",
        "post_tool_call",
        "pre_model_call",
        "post_model_call",
        "output",
        "agent_shutdown",
    ]
    assert [record.sequence for record in result.audit.records] == list(range(len(points)))
    assert len({record.session_id for record in result.audit.records}) == 1


async def test_redacted_tool_result_is_what_the_model_receives() -> None:
    result = await run_scenario(SCENARIOS_BY_NAME["tool-redaction"])
    assert result.client.seen_tool_results
    assert "ACCT-123456789012" not in json.dumps(result.client.seen_tool_results)
    assert "[ACCOUNT-REDACTED]" in json.dumps(result.client.seen_tool_results)


async def test_operator_cannot_override_a_hard_deny() -> None:
    result = await run_scenario(SCENARIOS_BY_NAME["hard-limit"])
    result.verify()
    assert result.operator is not None
    assert result.operator.requests == []


async def test_approval_is_bound_to_the_actual_context() -> None:
    result = await run_scenario(SCENARIOS_BY_NAME["approval-granted"])
    result.verify()
    assert result.operator is not None
    assert len(result.operator.requests) == 1
    assert result.operator.requests[0].context_identity


async def test_no_resolver_means_no_large_transfer() -> None:
    result = await run_scenario(SCENARIOS_BY_NAME["large-transfer"])
    result.verify()
    assert result.operator is None
    assert result.ledger.tool_calls == []


@pytest.fixture(scope="module")
def policy() -> ActivatedPolicy:
    return ActivatedPolicy.activate(str(MANIFEST_PATH))


@pytest.mark.parametrize(
    ("amount", "decision", "warning"),
    [
        (1, "allow", False),
        (49_999, "allow", False),
        (50_000, "allow", True),
        (100_000, "allow", True),
        (100_001, "deny", False),
        (500_000, "deny", False),
        (500_001, "deny", False),
        (0, "deny", False),
        (-1, "deny", False),
        (1.5, "deny", False),
        (True, "deny", False),
        ("25000", "deny", False),
    ],
)
def test_exact_transfer_policy_boundaries(
    policy: ActivatedPolicy, amount: object, decision: str, warning: bool
) -> None:
    builder = AgentContextBuilder(
        agent_id="banking-demo", framework="agent-framework", session_id="boundary-test"
    )
    context = builder.pre_tool_call(
        call_id="boundary",
        name="transfer_money",
        args={
            "account_id": "checking-001",
            "payee_id": "utility-company",
            "amount_cents": amount,
            "request_id": "boundary",
        },
    )
    verdict = policy.evaluate("pre_tool_call", context)
    assert verdict.decision.value == decision
    assert bool(verdict.warnings) is warning
    requires_approval = isinstance(amount, int) and 100_000 < amount <= 500_000
    assert (verdict.approval is not None) is requires_approval


@pytest.mark.parametrize("field", ["account_id", "payee_id", "amount_cents", "request_id"])
def test_missing_transfer_fields_fail_closed(policy: ActivatedPolicy, field: str) -> None:
    args = {
        "account_id": "checking-001",
        "payee_id": "utility-company",
        "amount_cents": 25_000,
        "request_id": "missing-field",
    }
    del args[field]
    context = AgentContextBuilder(
        agent_id="banking-demo", framework="agent-framework", session_id="missing-test"
    ).pre_tool_call(call_id="missing", name="transfer_money", args=args)
    assert policy.evaluate("pre_tool_call", context).decision is Decision.DENY


def test_manifest_errors_are_not_replaced_with_an_allow_policy(tmp_path: Path) -> None:
    invalid = tmp_path / "manifest.yaml"
    invalid.write_text("agent_control_specification_version: invalid", encoding="utf-8")
    with pytest.raises(ValueError):
        ActivatedPolicy.activate(str(invalid))


def test_ledger_idempotency() -> None:
    ledger = BankingLedger()
    first = ledger.transfer("checking-001", "utility-company", 25_000, "same-request")
    second = ledger.transfer("checking-001", "utility-company", 25_000, "same-request")
    assert first == second
    assert ledger.balance("checking-001")["balance_cents"] == INITIAL_BALANCE_CENTS - 25_000
    second["balance_cents"] = 0
    assert ledger.transfer("checking-001", "utility-company", 25_000, "same-request") == first


def test_idempotency_key_cannot_change_transfer_details() -> None:
    ledger = BankingLedger()
    ledger.transfer("checking-001", "utility-company", 25_000, "same-request")
    with pytest.raises(ValueError, match="different transfer details"):
        ledger.transfer("checking-001", "landlord", 25_000, "same-request")


@pytest.mark.parametrize("amount", [0, -1, True, 1.5])
def test_ledger_rejects_invalid_amounts(amount) -> None:
    ledger = BankingLedger()
    with pytest.raises(ValueError, match="positive integer"):
        ledger.transfer("checking-001", "utility-company", amount, "invalid")
    assert ledger.balance("checking-001")["balance_cents"] == INITIAL_BALANCE_CENTS


def test_ledger_checks_ownership_even_without_acs() -> None:
    with pytest.raises(PermissionError):
        BankingLedger().balance("checking-999")


def test_insufficient_funds_do_not_mutate_the_ledger() -> None:
    ledger = BankingLedger()
    with pytest.raises(ValueError, match="Insufficient"):
        ledger.transfer("checking-001", "utility-company", INITIAL_BALANCE_CENTS + 1, "overdraw")
    assert ledger.balance("checking-001")["balance_cents"] == INITIAL_BALANCE_CENTS


async def test_every_run_has_an_independent_control_session() -> None:
    first = await run_scenario(SCENARIOS_BY_NAME["balance"])
    second = await run_scenario(SCENARIOS_BY_NAME["balance"])
    assert {r.session_id for r in first.audit.records}.isdisjoint(
        r.session_id for r in second.audit.records
    )
