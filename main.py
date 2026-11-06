from __future__ import annotations

import argparse
import asyncio
import logging
import os
from typing import TYPE_CHECKING

from remote_demo import RemoteDemoError, run_remote_demo, validate_remote_options
from scenarios import SCENARIOS_BY_NAME

if TYPE_CHECKING:
    from agent_framework import BaseChatClient, ChatOptions


async def serve(client: BaseChatClient[ChatOptions]) -> None:
    from agent_framework_foundry_hosting import ResponsesHostServer

    from banking_agent import AuditTrail, BankingLedger, banking_agent

    async with client:
        async with banking_agent(client, ledger=BankingLedger(), audit=AuditTrail()) as agent:
            server = ResponsesHostServer(agent, history_source="agent_server")
            await server.run_async()


async def run(
    mode: str,
    *,
    demo: bool,
    scenario: str | None,
    agent_name: str = "banking-acs-demo",
    environment: str | None = None,
    version: str | None = None,
    timeout: int = 180,
) -> int:
    if mode == "remote":
        if not demo:
            raise ValueError("Remote mode requires --demo.")
        return await asyncio.to_thread(
            run_remote_demo,
            scenario,
            agent_name=agent_name,
            environment=environment,
            version=version,
            timeout=timeout,
        )
    if demo:
        if mode != "demo":
            raise ValueError("Use --mode remote --demo to call the deployed hosted agent.")
        from demo import run_demo

        await run_demo(scenario)
        return 0
    if mode == "demo":
        from demo import ScriptedBankingClient

        logging.getLogger("banking").warning(
            "SCRIPTED DEMO MODE: no model calls, no real money, and no operator approval resolver."
        )
        await serve(ScriptedBankingClient())
        return 0
    from agent_framework.foundry import FoundryChatClient
    from azure.identity import DefaultAzureCredential

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
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Simulated banking agent with native MAF ACS policies."
    )
    parser.add_argument(
        "--mode",
        choices=("demo", "foundry", "remote"),
        help="demo: offline; foundry: local server with real model; remote: hosted-agent demo.",
    )
    parser.add_argument("--demo", action="store_true", help="Run CLI scenarios, then exit.")
    parser.add_argument(
        "--scenario", choices=tuple(SCENARIOS_BY_NAME), help="Select one CLI scenario."
    )
    parser.add_argument(
        "--environment", help="Remote azd environment; otherwise use azd's default."
    )
    parser.add_argument(
        "--agent-name", help="Remote azure.yaml service (default: banking-acs-demo)."
    )
    parser.add_argument(
        "--version", help="Remote deployed version; otherwise use the azd environment."
    )
    parser.add_argument(
        "--timeout", type=int, help="Remote per-command timeout in seconds (default: 180)."
    )
    args = parser.parse_args()
    mode = args.mode or os.getenv("BANKING_MODE", "demo")
    if mode != "remote":
        from dotenv import load_dotenv

        load_dotenv()
        mode = args.mode or os.getenv("BANKING_MODE", "demo")
    if mode not in {"demo", "foundry", "remote"}:
        parser.error("BANKING_MODE must be demo, foundry or remote.")
    if args.scenario and not args.demo:
        parser.error("--scenario requires --demo.")
    if mode == "remote" and not args.demo:
        parser.error("--mode remote requires --demo.")
    if args.demo and mode == "foundry":
        parser.error(
            "--mode foundry starts a local server; use --mode remote --demo for remote calls."
        )
    if mode != "remote" and any(
        value is not None
        for value in (args.environment, args.agent_name, args.version, args.timeout)
    ):
        parser.error("--environment, --agent-name, --version and --timeout require --mode remote.")
    agent_name = args.agent_name if args.agent_name is not None else "banking-acs-demo"
    timeout = args.timeout if args.timeout is not None else 180
    if mode == "remote":
        try:
            validate_remote_options(
                agent_name=agent_name,
                environment=args.environment,
                version=args.version,
                timeout=timeout,
            )
        except RemoteDemoError as exc:
            parser.error(str(exc))
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        status = asyncio.run(
            run(
                mode,
                demo=args.demo,
                scenario=args.scenario,
                agent_name=agent_name,
                environment=args.environment,
                version=args.version,
                timeout=timeout,
            )
        )
    except RemoteDemoError as exc:
        parser.exit(1, f"Remote demo failed: {exc}\n")
    raise SystemExit(status)


if __name__ == "__main__":
    main()
