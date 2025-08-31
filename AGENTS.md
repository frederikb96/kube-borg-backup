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
There are two environments, by default you are in the cloud environment, which is explained below. But you might be in a local one, where things are different.

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
- Always use temporary files for the resources you create, so you can delete them again easily and you encounter less issues with bash and EOF syntax problems.
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

### Local Environment
Here you have directly access to the kubernetes via `KUBECONFIG="$HOME/.kube/kmini.yaml" kubectl`. Only use this config and not the default one! The tests are the same as described above.
Regarding python code, always make sure that you have the active virtual environment which is installed at `.venv` in the root of the repository.

## Documentation

When developing new features or fixes, also take a look at the `docs*/` folders for more detailed documentation. Especially interesting is also the `docs-logs/` folder which contains markdown files on old feature or bug fix implementations which are sometimes super useful when implementing similar features or fixes to already know where to look for or how to approach a task. But mind that those might be outdated!

One always active task which should not be neglected is to keep the `AGENTS.md` files up to date. If you encounter issues when using functions or the cli and found a fix on how to use it, please directly document it. Basically for every issue you encounter or where you have to iterate to figure out the correct approach, just document it. The main goal of the `AGENTS.md` files is to reduce the time it takes to contribute to this repository and reduce the iterations needed to figure out nice workflows or how things work and are structured or how to approach new tasks. So document issues and solutions which you come across your way. This will help you and others to not run into the same issues again and again.

The second always active task is to create or update a markdown file in the `docs-logs/` folder for every feature or bug fix you implement. A focus is here to include always two things. 1. A description of the feature or bug fix, what it does, how it works and why it is needed. 2. A detailed description of the implementation, how you implemented it, what you learned, what you had to look up and where you found the information and which relevant files in this repo you changed or are needed to understand the implementation. This way you can look up the implementation later on and do not have to figure out everything again. Also it helps others to understand the implementation and how it works, so they can build on top of it or fix issues in the future.
