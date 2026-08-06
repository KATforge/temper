# Temper

Temper coordinates related Imp worktrees, isolated local runtimes, exact cross-repository tests, and immutable delivery.

Use Imp for one repository. Use Temper when repositories must move or run together.

## Install

```bash
uv tool install katforge-temper
temper --version
```

Create a workspace once. `temper.yaml` keeps portable topology; absolute repository paths stay under `~/.config/temper/`.

```bash
temper workspace init storefront \
  --repository api=/workspace/api \
  --repository web=/workspace/web
```

## Workflow

```bash
temper change start checkout-redesign --services api,web
temper lease start checkout-redesign --profile test
temper lease test checkout-redesign-test
temper ship checkout-redesign --to qa
temper promote --from qa --to prod
```

Temper is designed for people and parallel AI agents. It never invokes Git directly. Imp owns source state. Hearth executes KATforge deployments.

Deployable services fail closed unless their artifact build emits an exact `sha256:<digest>` file. Temper binds that digest to the approved plan, publishes the built artifact, and gives Hearth only `image@sha256:<digest>`.

[Documentation](https://docs.katforge.com/packages/temper)

## Development

```bash
uv sync --extra dev
uv run ruff check .
uv run pytest
```
