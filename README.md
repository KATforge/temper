<p align="center">
   <img src="logo.png" alt="Temper" width="160">
</p>

<h1 align="center">Temper</h1>
<p align="center"><strong>Multi-repository source and local runtime coordination.</strong></p>

Temper gives related Imp features one change, one active source map, one review, and one shared runtime lease.

Use Imp for one repository. Use Temper when repositories must move or run together.

## Install

```bash
uv tool install katforge-temper
temper --version
```

## Workspace

`temper.yaml` owns repository paths and one recursive dependency graph:

```yaml
schema: temper.workspace.v1
name: storefront
services:
  api:
    path: api
  web:
    path: web
    needs:
      api: ">=2.8.0"
```

Inspect it with:

```bash
temper services web
temper --json services web
```

## Workflow

```bash
temper change start checkout-redesign --service api --service web
temper use checkout-redesign
temper lease start checkout-redesign --profile test --wait 10m
temper lease test
temper lease stop
temper review checkout-redesign
temper done checkout-redesign
temper use trunk
```

Omitting an existing change or lease opens a picker.

When a holder dies, `temper unlock <name>` breaks its stale workspace lock and `temper lease reclaim` tears the shared runtime down; both accept `--force`.

Temper never invokes Git directly. Imp owns repository state. Deployment belongs to the consuming platform, such as Hearth for KATforge.

Runtime configuration is optional. When configured, Temper uses one stable Compose project, infers worktree mounts from the Compose file, and leases it to one change at a time.

[Documentation](https://docs.katforge.com/packages/temper/)

## Development

```bash
uv sync --extra dev
uv run ruff check .
uv run pytest
```
