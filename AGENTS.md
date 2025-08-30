Project conventions and guardrails

- Python Style: Follow PEP 8 for formatting and PEP 257 for docstrings. Use type hints on public functions. Group imports: stdlib, third‑party, local.
- Error Handling: Keep it simple but explicit. Print human‑readable errors to stderr and exit non‑zero. Prefer library exceptions (e.g., Kubernetes `ApiException`) over custom wrappers; handle common cases like 404 cleanly.
- Design Approach: Prefer pragmatism and clarity over premature architecture. Start with small, well‑named functions in a single module; avoid scattering code across many files early. As complexity grows, incrementally extract modules. Rely on established libraries instead of reinventing behavior.
- Output Philosophy: Default to minimal, useful output. Keep logs concise and actionable; add verbosity only when requested (e.g., via flags). Avoid noisy, multi‑line dumps unless they provide clear value.
- KISS over cleverness: rely on well‑tested libraries instead of rolling our own.
- Structure: Compose code from single‑purpose functions that are easy to test and reuse. Keep public surfaces small; document intent with short docstrings and a few targeted comments.
- Kubernetes Init: Prefer in‑cluster configuration; fall back to local kubeconfig for development, then construct `client.CoreV1Api()`.
- Dependencies: List required libraries in `requirements.txt`.
- Update docs when behavior changes and keep related files in the repository in sync and up to date.

Codex Web Environment

- Preinstalled Python: The hosted Codex environment includes common Python tooling and project requirements
- No Kubernetes access: There is no kubeconfig or cluster connectivity. Kubernetes calls cannot be executed end‑to‑end.
- Testing focus: Prefer unit‑style checks for pure code (config parsing, templating). Avoid network/cluster‑dependent checks.
- Shell Scripts: start with `#!/bin/sh` and `set -euo pipefail`. Keep them POSIX
  compatible and add short comments for clarity.
