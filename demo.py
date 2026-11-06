from __future__ import annotations

import json
from collections.abc import AsyncIterable, Awaitable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from agent_framework import (
    BaseChatClient,
    ChatMiddlewareLayer,
    ChatOptions,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    FunctionInvocationLayer,
    Message,
    ResponseStream,
)
from agent_hooks import (
    ALLOW,
    ApprovalOutcome,
    ApprovalRequest,
    ApprovalResolution,
    Decision,
    InterceptionBlocked,
    InterceptionPoint,
    Verdict,
)

from banking_agent import INITIAL_BALANCE_CENTS, AuditTrail, BankingLedger, banking_agent
from scenarios import SCENARIOS as SCENARIOS
from scenarios import SCENARIOS_BY_NAME as SCENARIOS_BY_NAME
from scenarios import Scenario as Scenario
from scenarios import transfer_arguments as transfer_arguments


class ScriptedBankingClient(
    FunctionInvocationLayer[ChatOptions],
    ChatMiddlewareLayer[ChatOptions],
    BaseChatClient[ChatOptions],
):
    """Only the model is scripted. MAF tool invocation and ACS are real."""

    def __init__(self) -> None:
        super().__init__(middleware=[])
        self.call_count = 0
        self.seen_tool_results: list[str] = []

    def _response(self, messages: Sequence[Message]) -> ChatResponse:
        self.call_count += 1
        user_indices = [i for i, message in enumerate(messages) if message.role == "user"]
        if not user_indices:
            return self._text("Offline demo: send /demo balance.")
        user_index = user_indices[-1]
        prompt = messages[user_index].text
        scenario = next((case for case in SCENARIOS if case.prompt == prompt), None)
        if scenario is None:
            names = ", ".join(SCENARIOS_BY_NAME)
            return self._text(f"Scripted offline mode. Send /demo <scenario>. Scenarios: {names}")
        results = [
            content.result
            for message in messages[user_index + 1 :]
            for content in message.contents
            if content.type == "function_result"
        ]
        if results:
            text = json.dumps(results[-1], default=str)
            self.seen_tool_results.append(text)
            return self._text(f"Simulated bank tool result: {text}")
        if scenario.tool_name is not None:
            return ChatResponse(
                messages=[
                    Message(
                        role="assistant",
                        contents=[
                            Content.from_function_call(
                                call_id=f"call_{uuid4().hex}",
                                name=scenario.tool_name,
                                arguments=scenario.arguments,
                            )
                        ],
                    )
                ]
            )
        return self._text(scenario.final_text or "This is a simulated banking demonstration.")

    @staticmethod
    def _text(text: str) -> ChatResponse:
        return ChatResponse(messages=[Message(role="assistant", contents=[text])])

    def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        stream: bool,
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> Awaitable[ChatResponse] | ResponseStream[ChatResponseUpdate, ChatResponse]:
        if stream:
            return ResponseStream(
                self._stream(messages, options),
                finalizer=lambda updates: ChatResponse.from_updates(updates),
            )

        async def respond() -> ChatResponse:
            await self._validate_options(options)
            return self._response(messages)

        return respond()

    async def _stream(
        self, messages: Sequence[Message], options: Mapping[str, Any]
    ) -> AsyncIterable[ChatResponseUpdate]:
        await self._validate_options(options)
        response = self._response(messages)
        for message in response.messages:
            is_tool_call = any(content.type == "function_call" for content in message.contents)
            yield ChatResponseUpdate(
                role="assistant",
                contents=message.contents,
                finish_reason="tool_calls" if is_tool_call else "stop",
            )


class SimulatedOperator:
    """CLI/test-only approval, never installed on the hosted agent."""

    def __init__(self, *, approve: bool) -> None:
        self.approve = approve
        self.requests: list[ApprovalRequest] = []

    def resolve(self, request: ApprovalRequest) -> ApprovalResolution:
        self.requests.append(request)
        permitted = (
            self.approve
            and request.interception_point == InterceptionPoint.PRE_TOOL_CALL
            and request.verdict.reason == "large_transfer_requires_approval"
        )
        return ApprovalResolution(
            outcome=ApprovalOutcome.APPROVE if permitted else ApprovalOutcome.REJECT,
            context_identity=request.context_identity,
            verdict=ALLOW if permitted else Verdict(Decision.DENY, reason="operator_rejected"),
        )


@dataclass
class ScenarioResult:
    scenario: Scenario
    text: str
    ledger: BankingLedger
    audit: AuditTrail
    client: ScriptedBankingClient
    operator: SimulatedOperator | None

    def verify(self) -> None:
        expected = self.scenario
        matches = [
            record
            for record in self.audit.records
            if record.interception_point.value == expected.point
            and record.verdict.decision.value == expected.decision
            and (
                expected.reason is None
                or any(verdict.reason == expected.reason for verdict in record.verdicts)
            )
        ]
        if not matches:
            raise AssertionError(
                f"{expected.name}: the expected native ACS verdict was not recorded."
            )
        if self.ledger.balance("checking-001")["balance_cents"] != (
            INITIAL_BALANCE_CENTS - expected.debit_cents
        ):
            raise AssertionError(f"{expected.name}: unexpected ledger mutation.")
        if expected.decision == "deny" and self.ledger.tool_calls:
            raise AssertionError(f"{expected.name}: a blocked tool was executed.")
        if expected.name == "prompt-injection" and self.client.call_count:
            raise AssertionError("Blocked input reached the model.")
        if expected.tool_name and expected.decision != "deny":
            if self.ledger.tool_calls != [expected.tool_name]:
                raise AssertionError(f"{expected.name}: expected exactly one tool invocation.")
        if expected.name == "warning" and not any(r.verdict.warnings for r in matches):
            raise AssertionError("The warning was not preserved.")
        if expected.name == "approval-granted" and not any(
            r.resolved_by == "approval" for r in matches
        ):
            raise AssertionError("The transfer did not pass through the native approval seam.")
        if expected.decision == "transform":
            if "ACCT-123456789012" in self.text or "[ACCOUNT-REDACTED]" not in self.text:
                raise AssertionError(f"{expected.name}: unredacted data escaped.")
        failures = [
            verdict.reason
            for record in self.audit.records
            for verdict in record.verdicts
            if verdict.reason and verdict.reason.startswith(("host_error:", "runtime_error:"))
        ]
        if failures:
            raise AssertionError(f"{expected.name}: enforcement failed: {failures}")


async def run_scenario(scenario: Scenario, *, streaming: bool = False) -> ScenarioResult:
    ledger = BankingLedger()
    audit = AuditTrail(retain_records=True)
    operator = None if scenario.approval is None else SimulatedOperator(approve=scenario.approval)
    async with ScriptedBankingClient() as client:
        async with banking_agent(client, ledger=ledger, audit=audit, resolver=operator) as agent:
            try:
                if streaming:
                    stream = agent.run(scenario.prompt, stream=True)
                    updates = [update async for update in stream]
                    text = "".join(update.text for update in updates)
                else:
                    response = await agent.run(scenario.prompt)
                    text = response.text
            except InterceptionBlocked as exc:
                text = f"BLOCKED by native ACS: {exc}"
    return ScenarioResult(scenario, text, ledger, audit, client, operator)


async def run_demo(scenario_name: str | None = None) -> None:
    cases = SCENARIOS if scenario_name is None else (SCENARIOS_BY_NAME[scenario_name],)
    print("SIMULATED BANK | scripted model | REAL native MAF + ACS enforcement\n")
    for scenario in cases:
        result = await run_scenario(scenario)
        result.verify()
        print(f"PASS {scenario.name:20} {scenario.point:15} {scenario.decision.upper()}")
        print(f"     tool calls: {result.ledger.tool_calls or 'NONE'}")
        print(
            f"     checking balance: {result.ledger.balance('checking-001')['balance_cents']} cents"
        )
        print(f"     {result.text}")
    print(f"\n{len(cases)}/{len(cases)} scenarios verified.")
