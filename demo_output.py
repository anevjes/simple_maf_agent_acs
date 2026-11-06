from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence

from scenarios import Scenario

_ACCOUNT_NUMBER = re.compile(r"ACCT-[0-9]{12}")
_STEPS = {
    "agent_startup": "Agent startup",
    "input": "Inspect incoming request",
    "pre_model_call": "Before model invocation",
    "post_model_call": "Inspect model response",
    "pre_tool_call": "Before tool invocation",
    "post_tool_call": "Inspect tool result",
    "output": "Inspect final reply",
    "agent_shutdown": "Agent shutdown",
}


class DemoAuditConsoleFilter(logging.Filter):
    """Hide duplicate INFO JSON only on the offline demo's console handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name != "banking.acs" or record.levelno >= logging.WARNING


def console_text(text: str) -> str:
    text = _ACCOUNT_NUMBER.sub("[SYNTHETIC-ACCOUNT-MASKED]", text)
    return "".join(
        character
        if character.isprintable() or character in "\n\t"
        else f"\\u{ord(character):04x}"
        for character in text
    )


def print_text_block(title: str, text: str) -> None:
    print(f"\n{console_text(title)}", flush=True)
    lines = console_text(text).splitlines() or ["(no text returned)"]
    for line in lines:
        print(f"  {line}", flush=True)
    if console_text(text) != text:
        print("  [Console-only masking/escaping; the actual request or reply is unchanged.]")


def print_scenario_start(
    scenario: Scenario, prompt: str | None, *, scripted: bool = False
) -> None:
    print(f"\n{'=' * 88}", flush=True)
    print(f"SCENARIO: {console_text(scenario.name)}", flush=True)
    expected = f"{scenario.point} {scenario.decision.upper()}"
    if scenario.reason:
        expected += f" | {scenario.reason}"
    print(f"EXPECTED NATIVE ACS: {console_text(expected)}", flush=True)
    if prompt is None:
        print("\nINPUT: not sent (scenario skipped)", flush=True)
        return
    print_text_block("INPUT (prepared request)", prompt)
    if scripted and scenario.tool_name:
        print_text_block(
            "SCRIPTED TOOL REQUEST (planned, not execution evidence)",
            f"{scenario.tool_name}\n{json.dumps(scenario.arguments, indent=2)}",
        )
    if scripted and scenario.final_text:
        print_text_block("SCRIPTED MODEL OUTPUT TARGET (synthetic)", scenario.final_text)


def print_trace_header(*, complete: bool = True) -> None:
    label = "" if complete else " [PARTIAL - NOT VERIFIED]"
    print(f"\nNATIVE ACS TIMELINE{label}", flush=True)
    print("  Seq  ACS        Lifecycle point    Observed agent step", flush=True)
    print(f"  {'-' * 82}", flush=True)


def print_acs_decision(
    *,
    sequence: int,
    point: str,
    decision: str,
    reasons: Sequence[str],
    warnings: Sequence[str | None],
    approval_required: bool | None,
    resolved_by: str | None,
) -> None:
    print(
        f"  {sequence:02d}   {console_text(decision).upper():9} "
        f"{console_text(point):18} {_STEPS.get(point, 'Lifecycle event')}",
        flush=True,
    )
    if reasons:
        print(f"       reason: {console_text(', '.join(reasons))}", flush=True)
    if warnings:
        warning_text = ", ".join(
            warning if warning is not None else "(native warning reason not provided)"
            for warning in warnings
        )
        print(f"       [WARN] {console_text(warning_text)}", flush=True)
    if approval_required or resolved_by is not None:
        resolver = console_text(resolved_by) if resolved_by is not None else "(none)"
        print(
            f"       approval_required={approval_required}; resolved_by={resolver}",
            flush=True,
        )
    elif approval_required is None and point == "pre_tool_call":
        print("       approval metadata: not recorded", flush=True)


def print_agent_response(
    text: str, *, status: str | None = None, scripted_tool_result: bool = False
) -> None:
    prefix = "Simulated bank tool result: "
    if scripted_tool_result and text.startswith(prefix):
        value = json.loads(text.removeprefix(prefix))
        payload = value if isinstance(value, str) else json.dumps(value, indent=2)
        text = f"Simulated bank tool result:\n{payload}"
    title = "AGENT RESPONSE" if status is None else f"AGENT RESPONSE ({status})"
    print_text_block(title, text)
