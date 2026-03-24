# Contributing to ai-mem

Thank you for your interest in contributing.

---

## Local development setup

### Prerequisites

- Python 3.11+
- Azure CLI
- PowerShell 7+ (for deployment scripts)
- A personal Azure subscription (for testing against a live Function App)

### Install the client package in editable mode

```bash
pip install -e client/
```

This installs the `ai-mem` and `ai-mem-mcp` entry points from `client/src/ai_mem/`.

### Install dev dependencies

```bash
pip install ruff pytest
```

---

## Running tests

### Python tests

```bash
pytest tests/
```

There are currently no Python unit tests in the `tests/` directory, but the scaffold exists for adding them. New contributions should include tests where practical.

### PowerShell integration tests

The PowerShell test suite at `tests/CodexMemoryAzure.Tests.ps1` uses Pester and validates structural invariants (entry points, module references, API surface presence). To run it:

```powershell
Invoke-Pester tests/CodexMemoryAzure.Tests.ps1
```

Pester 5.x is required. Install it with:

```powershell
Install-Module Pester -Force -Scope CurrentUser
```

---

## Linting

```bash
pip install ruff
ruff check client/src/
```

All Python code in `client/src/ai_mem/` should pass `ruff check` without errors before submitting a PR.

---

## Deploying a dev Function App for testing

To test changes to `api/memory_api/run.py` or `api/embedding_worker/run.py` against a live Azure backend:

1. Provision a personal dev resource group and Function App:

   ```powershell
   .\scripts\azure\Invoke-CodexMemoryAzureBuild.ps1 `
     -Location "<your-azure-region>" `
     -ResourceGroupName "rg-ai-mem-dev" `
     -UserId "you@example.com" `
     -WorkspaceId "dev"
   ```

2. Deploy updated Function code only (faster iteration):

   ```powershell
   .\scripts\azure\Invoke-CodexMemoryFunctionDeploy.ps1 `
     -ResourceGroupName "rg-ai-mem-dev" `
     -FunctionAppName "<your-dev-function-app>" `
     -AllowDegradedEmbedding   # if Azure OpenAI is not configured for dev
   ```

3. Verify with the CLI:

   ```bash
   ai-mem doctor
   ai-mem search --query "test"
   ```

---

## PR guidelines

- Keep PRs focused. Separate refactoring from feature changes.
- Update relevant docs (`docs/`) if the change affects architecture, API surface, or configuration.
- Add or update PowerShell tests in `tests/CodexMemoryAzure.Tests.ps1` for structural changes to the client package or scripts.
- Do not include personal resource names, usernames, or email addresses in any file.
- Run `ruff check client/src/` before opening a PR.
- Commit messages should describe the *why*, not just the *what*.

---

## Code style

Python code follows the existing patterns in `client/src/ai_mem/`:

- Standard library imports first, then third-party, then local.
- Type annotations on all public functions.
- `dataclass` for configuration objects.
- No external dependencies beyond what is already in `client/pyproject.toml` unless discussed first.
- Keep the MCP server (`mcp_server.py`) and CLI (`cli.py`) thin; put logic in `client.py`.

PowerShell scripts follow the existing patterns in `scripts/azure/`:

- `Set-StrictMode -Version Latest` and `$ErrorActionPreference = "Stop"` at the top.
- Wrap `az` calls in helper functions that check `$LASTEXITCODE`.
- No hardcoded resource names, usernames, or regions as non-parameter defaults.

---

## Questions

Open a GitHub issue if you have questions about the architecture or want to discuss a proposed change before implementing it.
