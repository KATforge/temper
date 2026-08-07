<p align="center">
   <img src="logo.png" alt="Temper" width="160">
</p>

<h1 align="center">Temper</h1>
<p align="center"><strong>Exact multi-repository delivery for people and parallel AI agents.</strong></p>

Temper coordinates related Imp worktrees, one warm shared runtime, exact cross-repository tests, and immutable delivery.

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
temper lease start --profile test
temper lease test
temper lease stop
temper change review
temper change done
temper ship --to qa
temper promote --from qa --to prod
```

Omitting an existing change or lease opens a picker. Review asks whether to mark every exact candidate after displaying them.

Temper is designed for people and parallel AI agents. It never invokes Git directly. Imp owns source state. Hearth executes KATforge deployments.

Deployable services fail closed unless their artifact build emits an exact `sha256:<digest>` file. Temper binds that digest to the approved plan, publishes the built artifact, and gives Hearth only `image@sha256:<digest>`.

[Documentation](https://docs.katforge.com/packages/temper/)

## Development

```bash
uv sync --extra dev
uv run ruff check .
uv run pytest
```
