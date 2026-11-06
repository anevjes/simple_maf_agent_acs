from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Annotated, TypedDict, cast

from agent_control_spec import ActivatedPolicy, AsyncAcsInterceptor
from agent_framework import (
    SKIP_PARSING,
    Agent,
    BaseChatClient,
    ChatOptions,
    FunctionTool,
    create_agent_hooks_middleware,
    tool,
)
from agent_hooks import ApprovalResolver, InterceptionRecord, Interceptor
from pydantic import Field

MANIFEST_PATH = Path(__file__).parent / "policies" / "manifest.yaml"
INITIAL_BALANCE_CENTS = 500_000
logger = logging.getLogger("banking.acs")


class Balance(TypedDict):
    account_id: str
    currency: str
    balance_cents: int


class TransferReceipt(TypedDict):
    request_id: str
    account_id: str
    payee_id: str
    amount_cents: int
    balance_cents: int
    simulated: bool


class BankingLedger:
    """Single synthetic customer; balances are process-local, not a banking backend."""

    def __init__(self) -> None:
        self._balances = {"checking-001": INITIAL_BALANCE_CENTS, "savings-001": 1_000_000}
        self._receipts: dict[str, TransferReceipt] = {}
        self._lock = Lock()
        self.tool_calls: list[str] = []

    def balance(self, account_id: str) -> Balance:
        with self._lock:
            self._require_owned_account(account_id)
            return {
                "account_id": account_id,
                "currency": "USD",
                "balance_cents": self._balances[account_id],
            }

    def transfer(
        self, account_id: str, payee_id: str, amount_cents: int, request_id: str
    ) -> TransferReceipt:
        with self._lock:
            self._require_owned_account(account_id)
            if payee_id not in {"utility-company", "landlord"}:
                raise ValueError("The payee is not registered for this demo customer.")
            if type(amount_cents) is not int or amount_cents <= 0:
                raise ValueError("amount_cents must be a positive integer.")
            if not request_id.strip():
                raise ValueError("A nonempty request_id is required for idempotency.")
            previous = self._receipts.get(request_id)
            if previous is not None:
                if (
                    previous["account_id"] != account_id
                    or previous["payee_id"] != payee_id
                    or previous["amount_cents"] != amount_cents
                ):
                    raise ValueError(
                        "The request_id was already used with different transfer details."
                    )
                return previous.copy()
            if amount_cents > self._balances[account_id]:
                raise ValueError("Insufficient simulated funds.")
            self._balances[account_id] -= amount_cents
            receipt: TransferReceipt = {
                "request_id": request_id,
                "account_id": account_id,
                "payee_id": payee_id,
                "amount_cents": amount_cents,
                "balance_cents": self._balances[account_id],
                "simulated": True,
            }
            self._receipts[request_id] = receipt
            return receipt.copy()

    def _require_owned_account(self, account_id: str) -> None:
        if account_id not in self._balances:
            raise PermissionError("The account does not belong to the fixed demo customer.")


def banking_tools(ledger: BankingLedger) -> list[FunctionTool]:
    @tool(approval_mode="never_require", result_parser=SKIP_PARSING)
    def get_balance(
        account_id: Annotated[str, Field(description="checking-001 or savings-001.")],
    ) -> Balance:
        """Read a simulated account balance in integer USD cents."""
        ledger.tool_calls.append("get_balance")
        return ledger.balance(account_id)

    @tool(approval_mode="never_require", result_parser=SKIP_PARSING)
    def transfer_money(
        account_id: Annotated[str, Field(description="The source demo account.")],
        payee_id: Annotated[str, Field(description="utility-company or landlord.")],
        amount_cents: Annotated[
            int, Field(strict=True, description="Transfer amount in integer USD cents.")
        ],
        request_id: Annotated[str, Field(description="Unique idempotency key for this transfer.")],
    ) -> TransferReceipt:
        """Debit simulated funds. ACS, not a model argument, controls approval."""
        ledger.tool_calls.append("transfer_money")
        return ledger.transfer(account_id, payee_id, amount_cents, request_id)

    @tool(approval_mode="never_require", result_parser=SKIP_PARSING)
    def get_account_profile(account_id: str) -> dict[str, str]:
        """Return synthetic profile data to demonstrate post-tool redaction."""
        ledger.tool_calls.append("get_account_profile")
        ledger.balance(account_id)
        return {"account_id": account_id, "account_number": "ACCT-123456789012"}

    @tool(approval_mode="never_require")
    def export_all_accounts() -> str:
        """Inert forbidden capability included to prove ACS blocks tool execution."""
        ledger.tool_calls.append("export_all_accounts")
        raise PermissionError("Bulk exports are never implemented in this simulated bank.")

    return [get_balance, transfer_money, get_account_profile, export_all_accounts]


@dataclass
class AuditTrail:
    retain_records: bool = False
    records: list[InterceptionRecord] = field(default_factory=list)

    def record(self, record: InterceptionRecord) -> None:
        if self.retain_records:
            self.records.append(record)
        logger.info(
            "%s",
            json.dumps(
                {
                    "event": "acs_decision",
                    "session_id": record.session_id,
                    "sequence": record.sequence,
                    "point": record.interception_point.value,
                    "decision": record.verdict.decision.value,
                    "reasons": [v.reason for v in record.verdicts if v.reason],
                    "warnings": [w.reason for w in record.verdict.warnings],
                    "approval_required": record.verdict.approval is not None,
                    "resolved_by": record.resolved_by,
                }
            ),
        )


@asynccontextmanager
async def banking_agent(
    client: BaseChatClient[ChatOptions],
    *,
    ledger: BankingLedger,
    audit: AuditTrail,
    resolver: ApprovalResolver | None = None,
    policy: ActivatedPolicy | None = None,
) -> AsyncIterator[Agent]:
    active_policy = policy if policy is not None else ActivatedPolicy.activate(str(MANIFEST_PATH))
    async with AsyncAcsInterceptor(active_policy) as acs:
        # The preview SDK protocol omits awaitable returns from its declared type.
        native_controls = create_agent_hooks_middleware(
            {"banking-acs": cast(Interceptor, acs)},
            mode="enforce",
            resolver=resolver,
            record_sink=audit.record,
        )
        yield Agent(
            client=client,
            name="banking-acs-demo",
            instructions=(
                "You are a SIMULATED banking assistant for a fixed demo customer. "
                "There is no real money. Use tools for balances and transfers; never invent success. "
                "The customer's accounts are checking-001 and savings-001. "
                "Registered payees are utility-company and landlord. Amounts are integer USD cents. "
                "Use a unique request_id per intended transfer and reuse it only when retrying it. "
                "ACS policies are authoritative. Never claim that a blocked transfer completed, "
                "grant approval yourself, or reveal an unmasked account number. "
                "If approval is required, explain that a trusted operator must approve it."
            ),
            tools=banking_tools(ledger),
            middleware=[native_controls],
            default_options={"store": False},
        )
