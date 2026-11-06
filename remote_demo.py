from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from demo_output import (
    console_text,
    print_acs_decision,
    print_agent_response,
    print_scenario_start,
    print_trace_header,
)
from scenarios import SCENARIOS, SCENARIOS_BY_NAME, Scenario

ROOT = Path(__file__).resolve().parent
HOST_APPROVAL_SCENARIOS = {"approval-granted", "approval-rejected"}


class RemoteDemoError(RuntimeError):
    pass


def validate_remote_options(
    *,
    agent_name: str,
    environment: str | None,
    version: str | None,
    timeout: int,
) -> None:
    for option, value in (
        ("--agent-name", agent_name),
        ("--environment", environment),
        ("--version", version),
    ):
        if value is not None and not value.strip():
            raise RemoteDemoError(f"{option} must not be blank.")
    if type(timeout) is not int or timeout <= 0:
        raise RemoteDemoError("--timeout must be a positive integer.")


@dataclass(frozen=True)
class NativeDecision:
    session_id: str
    sequence: int
    point: str
    decision: str
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    approval_required: bool | None = None
    resolved_by: str | None = None

    @classmethod
    def parse(cls, value: dict[str, object]) -> NativeDecision:
        session_id = value.get("session_id")
        sequence = value.get("sequence")
        point = value.get("point")
        decision = value.get("decision")
        reasons = value.get("reasons")
        warnings = value.get("warnings")
        approval_required = value.get("approval_required")
        resolved_by = value.get("resolved_by")
        if (
            not isinstance(session_id, str)
            or not session_id
            or type(sequence) is not int
            or sequence < 0
            or not isinstance(point, str)
            or decision not in ("allow", "deny", "transform")
            or not isinstance(reasons, list)
            or not all(isinstance(reason, str) for reason in reasons)
            or not isinstance(warnings, list)
            or not all(isinstance(warning, str) for warning in warnings)
            or (approval_required is not None and type(approval_required) is not bool)
            or ("approval_required" in value and approval_required is None)
            or (resolved_by is not None and not isinstance(resolved_by, str))
        ):
            raise RemoteDemoError("Malformed native acs_decision metadata.")
        return cls(
            session_id,
            sequence,
            point,
            str(decision),
            tuple(reasons),
            tuple(warnings),
            approval_required,
            resolved_by,
        )


def audit_decisions(text: str) -> list[NativeDecision]:
    records: list[NativeDecision] = []
    decoder = json.JSONDecoder()

    def collect(value: object) -> None:
        if isinstance(value, dict):
            if value.get("event") == "acs_decision":
                records.append(NativeDecision.parse(value))
            else:
                for item in value.values():
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, str) and "acs_decision" in value:
            scan(value)

    def scan(line: str) -> None:
        before = len(records)
        position = 0
        while (start := line.find("{", position)) >= 0:
            try:
                value, position = decoder.raw_decode(line, start)
            except json.JSONDecodeError:
                position = start + 1
            else:
                collect(value)
        if "acs_decision" in line and len(records) == before:
            raise RemoteDemoError("An acs_decision log could not be decoded.")

    for line in text.splitlines():
        scan(line)
    return sorted(set(records), key=lambda record: record.sequence)


def complete_trace(records: Sequence[NativeDecision]) -> bool:
    return bool(
        records
        and len({record.session_id for record in records}) == 1
        and records[0].point == "agent_startup"
        and records[-1].point == "agent_shutdown"
        and [record.sequence for record in records] == list(range(len(records)))
    )


def expected_decisions(
    scenario: Scenario, records: Sequence[NativeDecision]
) -> list[NativeDecision]:
    failures = [
        reason
        for record in records
        for reason in record.reasons
        if reason.startswith(("host_error:", "runtime_error:"))
    ]
    if failures:
        raise RemoteDemoError(f"Native enforcement reported errors: {', '.join(failures)}")
    return [
        record
        for record in records
        if record.point == scenario.point
        and record.decision == scenario.decision
        and (scenario.reason is None or scenario.reason in record.reasons)
        and (scenario.name != "warning" or "transfer_near_approval_limit" in record.warnings)
    ]


@dataclass(frozen=True)
class EndpointResponse:
    status: str
    text: str


def endpoint_response(raw: str) -> EndpointResponse:
    normalized = raw.replace("\r\n", "\n")
    headers, separator, body = normalized.partition("\n\n")
    status_line = headers.splitlines()[0] if headers else ""
    match = re.fullmatch(r"HTTP/\S+ ([0-9]{3})(?: .*)?", status_line)
    if not separator or match is None:
        raise RemoteDemoError("azd did not return the documented raw HTTP response.")
    status_code = int(match.group(1))
    if not 200 <= status_code < 300:
        raise RemoteDemoError(f"The hosted endpoint returned HTTP {status_code}.")
    try:
        if body.lstrip().startswith("{"):
            payload: object = json.loads(body)
        else:
            terminal: list[object] = []
            for block in body.split("\n\n"):
                data = "\n".join(
                    line[5:].lstrip(" ") for line in block.splitlines() if line.startswith("data:")
                )
                if not data or data == "[DONE]":
                    continue
                event = json.loads(data)
                if isinstance(event, dict) and event.get("type") in {
                    "response.completed",
                    "response.failed",
                    "response.incomplete",
                }:
                    terminal.append(event.get("response"))
            if len(terminal) != 1:
                raise RemoteDemoError("The Responses stream has no unique terminal response.")
            payload = terminal[0]
    except json.JSONDecodeError as exc:
        raise RemoteDemoError("The endpoint returned malformed Responses JSON.") from exc
    if not isinstance(payload, dict) or payload.get("status") not in {
        "completed",
        "failed",
        "incomplete",
    }:
        raise RemoteDemoError("The endpoint returned an unsupported Responses status.")
    output = payload.get("output")
    if not isinstance(output, list):
        raise RemoteDemoError("The endpoint returned an invalid Responses output.")
    texts: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            raise RemoteDemoError("The endpoint returned invalid message content.")
        for part in content:
            if isinstance(part, dict) and part.get("type") in {"output_text", "refusal"}:
                text = part.get("text") if part["type"] == "output_text" else part.get("refusal")
                if not isinstance(text, str):
                    raise RemoteDemoError("The endpoint returned a non-text message.")
                texts.append(text)
    text = "\n".join(texts)
    if re.search(r"ACCT-[0-9]{12}", text):
        raise RemoteDemoError(
            "An unredacted synthetic account number escaped; response suppressed."
        )
    return EndpointResponse(str(payload["status"]), text)


def _azd(
    arguments: Sequence[str], *, environment: str | None, timeout: int
) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("azd")
    if executable is None:
        raise RemoteDemoError("Install azd and its azure.ai.agents extension before remote mode.")
    command = [executable, "ai", "agent", *arguments, "--no-prompt"]
    if environment:
        command.extend(("--environment", environment))
    try:
        return subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RemoteDemoError("The azd executable is unavailable.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RemoteDemoError(f"azd {arguments[0]} timed out after {timeout} seconds.") from exc


def _check_command(result: subprocess.CompletedProcess[str], operation: str) -> None:
    if result.returncode:
        stdout = result.stdout.strip()
        if stdout.startswith(("HTTP/", "event:", "data:")):
            status = stdout.splitlines()[0] if stdout.startswith("HTTP/") else "Endpoint event stream"
            stdout = f"{status}\nRaw endpoint/log payload suppressed."
        detail = "\n".join(
            f"{stream}:\n{output.strip()}"
            for stream, output in (("stdout", stdout), ("stderr", result.stderr))
            if output.strip()
        ) or "No diagnostic message was returned."
        raise RemoteDemoError(f"azd {operation} failed (exit {result.returncode}):\n{detail}")


def _print_native_trace(records: Sequence[NativeDecision], *, complete: bool = True) -> None:
    print_trace_header(complete=complete)
    if not records:
        print("  No native ACS decisions were retrieved.", flush=True)
    for record in records:
        print_acs_decision(
            sequence=record.sequence,
            point=record.point,
            decision=record.decision,
            reasons=record.reasons,
            warnings=record.warnings,
            approval_required=record.approval_required,
            resolved_by=record.resolved_by,
        )


def _remote_case(
    scenario: Scenario,
    *,
    agent_name: str,
    environment: str | None,
    version: str | None,
    timeout: int,
) -> tuple[EndpointResponse, list[NativeDecision], str]:
    prompt = scenario.remote_prompt
    print_scenario_start(scenario, prompt)
    if shutil.which("azd") is None:
        raise RemoteDemoError("Install azd and its azure.ai.agents extension before remote mode.")
    session_id = f"banking-demo-{uuid4().hex}"
    create = ["sessions", "create", "--agent-name", agent_name, "--session-id", session_id]
    if version:
        create.extend(("--version", version))
    # A caller-selected ID isolates audit evidence and cleanup from existing sessions.
    print(f"\nHOST ACTIVITY\n  [RUN] Creating isolated session {session_id}", flush=True)
    try:
        _check_command(_azd(create, environment=environment, timeout=timeout), "session creation")
        print("  [OK] Hosted session created.\n  [RUN] Sending the prepared input.", flush=True)
        invocation = _azd(
            [
                "invoke",
                agent_name,
                prompt,
                "--session-id",
                session_id,
                "--new-conversation",
                "--protocol",
                "responses",
                "--output",
                "raw",
                "--timeout",
                str(timeout),
            ],
            environment=environment,
            timeout=timeout,
        )
        _check_command(invocation, "invocation")
        response = endpoint_response(invocation.stdout)
        print(
            f"  [OK] Responses request returned: {response.status}.\n"
            "  [RUN] Retrieving native ACS decisions.",
            flush=True,
        )
        records: list[NativeDecision] = []
        for attempt in range(3):
            logs = _azd(
                ["monitor", agent_name, "--session-id", session_id, "--tail", "300", "--raw"],
                environment=environment,
                timeout=timeout,
            )
            _check_command(logs, "audit retrieval")
            records = audit_decisions(logs.stdout)
            if complete_trace(records):
                print(f"  [OK] Retrieved {len(records)} native lifecycle decisions.", flush=True)
                _print_native_trace(records)
                print_agent_response(response.text, status=response.status)
                return response, records, session_id
            if attempt < 2:
                print("  [WAIT] Native audit is not complete; retrying.", flush=True)
                time.sleep(1)
        _print_native_trace(records, complete=False)
        raise RemoteDemoError(
            "Native audit is missing, truncated, or ambiguous; no ACS verdict can be verified."
        )
    finally:
        original_error = sys.exception()
        try:
            print("\nHOST CLEANUP\n  [RUN] Stopping the demo-owned hosted session.", flush=True)
            stopped = _azd(
                ["sessions", "stop", session_id, "--agent-name", agent_name],
                environment=environment,
                timeout=timeout,
            )
            _check_command(stopped, f"cleanup of demo-owned session {session_id}")
            print("  [OK] Demo-owned session stopped.", flush=True)
        except RemoteDemoError as exc:
            if original_error is None:
                raise
            print(f"ERROR cleanup of {session_id}: {exc}", file=sys.stderr)


def run_remote_demo(
    scenario_name: str | None = None,
    *,
    agent_name: str = "banking-acs-demo",
    environment: str | None = None,
    version: str | None = None,
    timeout: int = 180,
) -> int:
    validate_remote_options(
        agent_name=agent_name, environment=environment, version=version, timeout=timeout
    )
    cases = SCENARIOS if scenario_name is None else (SCENARIOS_BY_NAME[scenario_name],)
    print(
        console_text(
            f"SIMULATED BANK | REMOTE hosted agent: {agent_name} "
            f"| azd env: {environment or 'default'}"
        ),
        flush=True,
    )
    print("Checking native hook evidence, not the last shutdown verdict or model claims.")
    print("ACS verdicts apply per lifecycle point; a later ALLOW does not undo an earlier DENY.")
    observed = missing = skipped = 0
    for scenario in cases:
        if scenario.name in HOST_APPROVAL_SCENARIOS:
            skipped += 1
            print_scenario_start(scenario, None)
            print(f"SKIP {scenario.name}: hosted demo has no trusted operator resolver.")
            continue
        response, records, session_id = _remote_case(
            scenario,
            agent_name=agent_name,
            environment=environment,
            version=version,
            timeout=timeout,
        )
        matches = expected_decisions(scenario, records)
        if response.status != "completed" and not (
            response.status == "failed" and scenario.decision == "deny" and matches
        ):
            raise RemoteDemoError(f"{scenario.name}: Responses request ended as {response.status}.")
        print("\nRESULT")
        if matches:
            observed += 1
            print(
                f"OBSERVED {scenario.name:20} {scenario.point} {scenario.decision.upper()}"
                f" {scenario.reason or ''}"
            )
        else:
            missing += 1
            print(
                f"NOT_OBSERVED {scenario.name}: expected {scenario.point} "
                f"{scenario.decision.upper()} {scenario.reason or ''}."
            )
            print("     A model refusal or different tool arguments are not ACS evidence.")
        print(f"     hosted session: {session_id}; native session: {records[0].session_id}")
    print(
        f"\n{'=' * 88}\nSUMMARY: {observed} observed; {missing} not observed; {skipped} skipped."
    )
    print("Remote audit does not expose executed tool names/arguments or ledger mutations.")
    print("Lifecycle rows are observed boundaries, not private model reasoning.")
    return 1 if missing else (0 if observed else 2)
