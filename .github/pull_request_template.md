<!--
  KubeCommerce pull request template.
  Keep PRs small and tied to one phase/issue. CI (`.github/workflows/ci.yml`)
  must be green and every required check below must pass before merge.
-->

## What & why

<!-- One paragraph: what changed and the problem it solves. Link the phase/issue. -->

- Phase / issue:
- Services or libs touched:

## Type of change

- [ ] Application code (`services/**`, `libs/**`)
- [ ] Tests
- [ ] CI/CD (`.github/workflows/**`, `.github/dependabot.yml`)
- [ ] Docs / runbook / security
- [ ] Compose / local tooling

## How it was validated

<!-- Exact commands and observed results. Do not claim checks you did not run. -->

```bash
# e.g. for a service:
cd services/<name>
uv sync --locked
uv run ruff check . && uv run ruff format --check .
uv run mypy app
uv run pytest -q --cov=app --cov-fail-under=80
```

## CI gate checklist

- [ ] `lint` passes (ruff check + format)
- [ ] `typecheck` passes (mypy)
- [ ] `unit-test` passes (coverage >= 80% per project)
- [ ] `integration-test` passes (compose up + integration/failure suites)
- [ ] `dependency-security` passes (PRs only, dependency review)
- [ ] `code-security` passes (CodeQL for Python)
- [ ] `container-config-security` passes (Trivy fs/config)
- [ ] `compose-validation` passes (compose.yaml + `bash -n` scripts)

## Supply chain / security

- [ ] No secrets, tokens or private keys added to tracked files
- [ ] New third-party Actions pinned to a full commit SHA with a `# vX.Y.Z` comment
- [ ] No `latest` image tag introduced
- [ ] Any Trivy/CodeQL suppression is narrow and documented (see `docs/security.md`)

## Notes for reviewers

<!-- Migrations, breaking changes, rollout/rollback notes, follow-ups. -->
