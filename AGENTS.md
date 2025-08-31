## Project conventions and guardrails

- Python Style: Follow PEP 8 for formatting and PEP 257 for docstrings. Use type hints on public functions. Group imports: stdlib, third‑party, local.
- Error Handling: Keep it simple but explicit. Print human‑readable errors to stderr and exit non‑zero. Prefer library exceptions (e.g., Kubernetes `ApiException`) over custom wrappers; handle common cases like 404 cleanly.
- Design Approach: Prefer pragmatism and clarity over premature architecture. Start with small, well‑named functions in a single module; avoid scattering code across many files early. As complexity grows, incrementally extract modules. Rely on established libraries instead of reinventing behavior.
- Output Philosophy: Default to minimal, useful output. Keep logs concise and actionable; add verbosity only when requested (e.g., via flags). Avoid noisy, multi‑line dumps unless they provide clear value.
- KISS over cleverness: rely on well‑tested libraries instead of rolling our own.
- Structure: Compose code from single‑purpose functions that are easy to test and reuse. Keep public surfaces small; document intent with short docstrings and a few targeted comments.
- Kubernetes Init: Prefer in‑cluster configuration; fall back to local kubeconfig for development, then construct `client.CoreV1Api()`.
- Dependencies: List required libraries in `requirements.txt`.
- Update docs when behavior changes and keep related files in the repository in sync and up to date.

## Environment and Tests
Extensive testings are always key! Always report the results of your tests! Iterate over your code changes if tests fail due to your changes. Report issues with the environment if you cannot get your tests to work.

When implementing features that require a kubernetes cluster, you can test them within your development environment.
- Within your environment, you can access a kubernetes test cluster, e.g. like this:
```sh
~/.local/bin/kubectl --insecure-skip-tls-verify=true get nodes
```
- The binary might be not on path and must be accessed at `~/.local/bin/kubectl`.
- The config is available during your session at `~/.kube/config`.
- Create a namespace for your tests:
```sh
kubectl create namespace test-XXXX # replace XXXX with random chars
```
- Use that namespace for your tests.
- You have a default storage class and a default snapshot class available.
- You can create, pvcs, snapshots, pods which mount those and create data in there, etc.
- When you are done with your tests, delete the namespace again:
```sh
kubectl delete namespace test-XXXX # replace XXXX with the name you used before
```

When implementing python code, you can install the depndencies directly via pip, since you are already in a virtual environment. Just run:
```sh
cd apps/<app-name>
pip install -r requirements.txt
```
- Then execute and debug your code. If it depends on kubernetes, make sure you have access to the kubernetes test cluster as described above and use a dedicated namespace for your tests.
