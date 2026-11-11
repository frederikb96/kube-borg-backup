# Development

## Checks

The pull request checks run these from the repository root; the chart lint runs when `charts/` changes.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r apps/requirements-dev.txt -r apps/controller/requirements.txt

ruff check apps/ scripts/
mypy --config-file mypy.ini
pytest
helm lint charts/kube-borg-backup
```

`pytest` also checks the documentation: every file in `docs/` is linked from the README and every relative link resolves. A change under `charts/` has to bump the chart `version`; chart-testing fails the pull request otherwise. The required check on `main` is `CI ok`.

Code style is Ruff with a line length of 120, strict mypy and modern type hints (`dict[str, Any]`, `str | None`). Errors go to stderr with a non-zero exit.

## Layout

- `apps/common/`: library shared by the controllers and the CLI (hooks, pod monitoring, archive naming).
- `apps/controller/`: the snapshot and backup controllers.
- `apps/backup-runner/`: the Borg scripts the runner image starts (`backup.py`, `list.py`, `restore.py`).
- `apps/cli/`: `kbb`; `apps/setup.py` packages it with `common`.
- `apps/tests/`: unit tests, no cluster needed.
- `charts/kube-borg-backup/`: the Helm chart; `example/values.yaml` mirrors its structure.
- `scripts/`: the release checks below.

The chart values, `example/values.yaml` and any test values file keep the same structure. Configuration is documented as comments in the chart's `values.yaml` and nowhere else.

## Releasing

The version is written in five places that have to agree: `version` and `appVersion` in `charts/kube-borg-backup/Chart.yaml`, `apps/controller/VERSION`, `apps/backup-runner/VERSION` and `version` in `apps/setup.py`. `scripts/check_release_versions.py` compares them and checks that `CHANGELOG.md` has a section for the version; it runs in every pull request, and with the tag as argument at the start of the release pipeline.

- Add changes to `CHANGELOG.md` under `[Unreleased]` as they are made.
- A release is a pull request that moves them into a dated section, bumps the five places and is merged with a rebase.
- The tag `vX.Y.Z` (or a `-rc`, `-beta` or `-alpha` variant) goes on the merge commit. It starts the release pipeline: version check, the lint, type, test and chart gates, then the chart to GitHub Pages, both images for amd64 and arm64 to GHCR, and a GitHub Release whose notes are that version's `CHANGELOG.md` section. `latest` moves only for stable tags, and tags with a suffix are marked as pre-releases.
- Pull requests that change only workflows or dependencies that never reach an image need no release.
