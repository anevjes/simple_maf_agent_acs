from __future__ import annotations

import io
import json
import logging

import pytest

from demo_output import (
    DemoAuditConsoleFilter,
    console_text,
    print_acs_decision,
    print_agent_response,
    print_scenario_start,
)
from scenarios import SCENARIOS, SCENARIOS_BY_NAME, Scenario


def test_input_display_preserves_the_prepared_request(capsys) -> None:
    prompt = SCENARIOS_BY_NAME["large-transfer"].remote_prompt
    print_scenario_start(SCENARIOS_BY_NAME["large-transfer"], prompt)
    output = capsys.readouterr().out
    assert prompt in output
    assert "SCENARIO: large-transfer" in output
    assert "EXPECTED NATIVE ACS: pre_tool_call DENY" in output
    assert output.index("EXPECTED NATIVE ACS") < output.index("INPUT")


def test_console_masks_synthetic_numbers_without_changing_input(capsys) -> None:
    prompt = "Repeat ACCT-123456789012."
    print_scenario_start(SCENARIOS_BY_NAME["output-redaction"], prompt)
    output = capsys.readouterr().out
    assert "ACCT-123456789012" not in output
    assert "[SYNTHETIC-ACCOUNT-MASKED]" in output
    assert "actual request or reply is unchanged" in output
    assert prompt == "Repeat ACCT-123456789012."


def test_console_escapes_terminal_controls_but_preserves_readable_lines() -> None:
    displayed = console_text("hello\x1b[31m\rworld\nnext\tcolumn")
    assert "\x1b" not in displayed
    assert "\r" not in displayed
    assert "\\u001b[31m\\u000d" in displayed
    assert "\nnext\tcolumn" in displayed


@pytest.mark.parametrize(
    "point",
    [
        "agent_startup",
        "input",
        "pre_model_call",
        "post_model_call",
        "pre_tool_call",
        "post_tool_call",
        "output",
        "agent_shutdown",
    ],
)
def test_all_native_lifecycle_points_have_readable_rows(point: str, capsys) -> None:
    print_acs_decision(
        sequence=0,
        point=point,
        decision="allow",
        reasons=(),
        warnings=(),
        approval_required=False,
        resolved_by=None,
    )
    output = capsys.readouterr().out
    assert "00" in output
    assert point in output
    assert "ALLOW" in output
    assert "Lifecycle event" not in output


def test_warning_is_displayed_without_changing_the_allow_verdict(capsys) -> None:
    print_acs_decision(
        sequence=4,
        point="pre_tool_call",
        decision="allow",
        reasons=(),
        warnings=("transfer_near_approval_limit",),
        approval_required=False,
        resolved_by=None,
    )
    output = capsys.readouterr().out
    assert "ALLOW" in output
    assert "[WARN] transfer_near_approval_limit" in output
    assert "DENY" not in output


def test_a_native_warning_without_a_reason_is_not_silently_dropped(capsys) -> None:
    print_acs_decision(
        sequence=4,
        point="pre_tool_call",
        decision="allow",
        reasons=(),
        warnings=(None,),
        approval_required=False,
        resolved_by=None,
    )
    output = capsys.readouterr().out
    assert "ALLOW" in output
    assert "[WARN] (native warning reason not provided)" in output


def test_approval_resolution_and_rule_reason_are_displayed(capsys) -> None:
    print_acs_decision(
        sequence=4,
        point="pre_tool_call",
        decision="allow",
        reasons=("large_transfer_requires_approval",),
        warnings=(),
        approval_required=False,
        resolved_by="approval",
    )
    output = capsys.readouterr().out
    assert "reason: large_transfer_requires_approval" in output
    assert "resolved_by=approval" in output


def test_agent_response_is_separate_and_scripted_json_is_readable(capsys) -> None:
    text = "Simulated bank tool result: " + json.dumps(json.dumps({"error": "native blocked"}))
    print_agent_response(text, scripted_tool_result=True)
    output = capsys.readouterr().out
    assert "AGENT RESPONSE" in output
    assert '"error": "native blocked"' in output
    assert '\\"error\\"' not in output


def test_empty_response_is_explicit(capsys) -> None:
    print_agent_response("", status="failed")
    output = capsys.readouterr().out
    assert "AGENT RESPONSE (failed)" in output
    assert "(no text returned)" in output


@pytest.mark.parametrize(
    ("name", "level", "visible"),
    [
        ("banking.acs", logging.INFO, False),
        ("banking.acs", logging.WARNING, True),
        ("banking.acs", logging.ERROR, True),
        ("other.logger", logging.INFO, True),
    ],
)
def test_console_filter_keeps_errors_and_other_logging(
    name: str, level: int, visible: bool
) -> None:
    record = logging.LogRecord(name, level, "", 0, "metadata", (), None)
    assert DemoAuditConsoleFilter().filter(record) is visible


def test_a_separate_audit_sink_still_receives_the_original_json() -> None:
    console_output = io.StringIO()
    audit_output = io.StringIO()
    console = logging.StreamHandler(console_output)
    console.addFilter(DemoAuditConsoleFilter())
    audit = logging.StreamHandler(audit_output)
    message = '{"event":"acs_decision","point":"input","decision":"allow"}'
    record = logging.LogRecord("banking.acs", logging.INFO, "", 0, message, (), None)
    console.handle(record)
    audit.handle(record)
    assert console_output.getvalue() == ""
    assert audit_output.getvalue().strip() == message


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda scenario: scenario.name)
async def test_offline_demo_shows_input_full_trace_response_and_result(
    scenario: Scenario, capsys
) -> None:
    from demo import run_demo

    await run_demo(scenario.name)
    output = capsys.readouterr().out
    assert f"SCENARIO: {scenario.name}" in output
    assert scenario.prompt in output
    assert output.index("INPUT") < output.index("NATIVE ACS TIMELINE")
    assert output.index("agent_startup") < output.index("agent_shutdown")
    assert output.index("agent_shutdown") < output.index("AGENT RESPONSE")
    assert output.index("AGENT RESPONSE") < output.index(f"PASS {scenario.name}")
    assert "1/1 scenarios verified." in output
    assert "ACCT-123456789012" not in output
    assert "Executed tools:" in output
    if scenario.name == "warning":
        assert "[WARN] transfer_near_approval_limit" in output
    if scenario.name == "approval-granted":
        assert "resolved_by=approval" in output
        assert "Executed tools: transfer_money" in output
    if scenario.name == "prompt-injection":
        assert "Scripted model calls: 0" in output
        assert "Executed tools: NONE" in output
    if scenario.name == "large-transfer":
        assert "approval_required=True; resolved_by=(none)" in output
        assert "Executed tools: NONE" in output
        assert "USD 5,000.00 (500000 cents)" in output


async def test_human_output_does_not_add_banking_payloads_to_native_audit(caplog) -> None:
    from demo import run_demo

    caplog.set_level(logging.INFO, logger="banking.acs")
    await run_demo("tool-redaction")
    messages = [
        record.getMessage() for record in caplog.records if record.name == "banking.acs"
    ]
    assert messages
    for message in messages:
        metadata = json.loads(message)
        assert metadata["event"] == "acs_decision"
        assert "checking-001" not in message
        assert "ACCT-123456789012" not in message
        assert not {"input", "arguments", "tool_result", "output"} & metadata.keys()
