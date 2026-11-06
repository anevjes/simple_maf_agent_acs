from __future__ import annotations

import argparse
import asyncio
import logging
import os

from agent_framework import BaseChatClient, ChatOptions
from agent_framework.foundry import FoundryChatClient
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

from banking_agent import AuditTrail, BankingLedger, banking_agent
from demo import SCENARIOS_BY_NAME, ScriptedBankingClient, run_demo


async def serve(client: BaseChatClient[ChatOptions]) -> None:
    async with client:
        async with banking_agent(client, ledger=BankingLedger(), audit=AuditTrail()) as agent:
            server = ResponsesHostServer(agent, history_source="agent_server")
            await server.run_async()


async def run(mode: str, *, demo: bool, scenario: str | None) -> None:
    if demo:
        await run_demo(scenario)
        return
    if mode == "demo":
        logging.getLogger("banking").warning(
            "SCRIPTED DEMO MODE: no model calls, no real money, and no operator approval resolver."
        )
        await serve(ScriptedBankingClient())
        return
    endpoint = os.getenv("FOUNDRY_PROJECT_ENDPOINT", "").strip()
    deployment = os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME", "").strip()
    if not endpoint or not deployment:
        raise ValueError(
            "Foundry mode requires FOUNDRY_PROJECT_ENDPOINT and AZURE_AI_MODEL_DEPLOYMENT_NAME. "
            "Set them in .env or the azd environment; use --mode demo for offline testing."
        )
    with DefaultAzureCredential() as credential:
        await serve(
            FoundryChatClient(
                project_endpoint=endpoint,
                model=deployment,
                credential=credential,
            )
        )


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Simulated banking agent with native MAF ACS policies."
    )
    parser.add_argument(
        "--mode", choices=("demo", "foundry"), default=os.getenv("BANKING_MODE", "demo")
    )
    parser.add_argument(
        "--demo", action="store_true", help="Run and verify all CLI scenarios, then exit."
    )
    parser.add_argument(
        "--scenario", choices=tuple(SCENARIOS_BY_NAME), help="Select one CLI scenario."
    )
    args = parser.parse_args()
    if args.mode not in {"demo", "foundry"}:
        parser.error("BANKING_MODE must be demo or foundry.")
    if args.scenario and not args.demo:
        parser.error("--scenario requires --demo.")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(run(args.mode, demo=args.demo, scenario=args.scenario))


if __name__ == "__main__":
    main()
