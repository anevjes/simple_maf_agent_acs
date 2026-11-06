# Banking ACS demo

This project was built with the microsoft-foundry skill. Before working on or
answering questions about Foundry agents, read the microsoft-foundry skill first.
If you are in VS Code, read the vscode-microsoft-foundry skill first.

- Keep all accounts, identities, and money simulated.
- Use MAF's native `create_agent_hooks_middleware` bundle for ACS enforcement.
  Do not replace it with prompt instructions or a Python imitation of the policy engine.
- Keep all eight lifecycle points bound in the ACS manifest. Errors must fail closed.
- Approval is host-owned. Never accept approval as a model-supplied tool argument.
- Run the native integration tests in Linux x64; the pinned ACS release has no Windows ARM64 wheel.
- Never include `.env`, credentials, or raw banking content in deployment packages or audit logs.
