<h1 align="center">Temper</h1>
<p align="center"><strong>Retired. Its work now lives in Imp and Hearth.</strong></p>

Temper coordinated multi-repository source and local runtimes. Both halves moved to
the tools that already owned their domain:

| Temper                          | Replacement                                                  |
| ------------------------------- | ------------------------------------------------------------ |
| `temper change start`           | `imp start <name> --repo <alias> --repo <alias>`              |
| `temper review`                 | `imp review <name>`                                           |
| `temper done`                   | `imp done <name>`, dependency-first across every member       |
| `temper status`                 | `imp status`                                                  |
| `temper services`               | `hearth topology`                                             |
| `temper lease start` / `stop`   | `hearth setup --start`                                        |
| `temper use`                    | Nothing. The runtime always resolves to trunk                 |
| `temper.yaml`                   | `workspace.yaml`, schema `katforge.workspace.v1`              |

<<<<<<< HEAD
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

## Workspace fleet

When concurrent work has left several managed features across the workspace, consolidate all of them locally:

```bash
temper fleet --plan
temper fleet --apply plan:fleet:workspace:1 --yes
```

Each repository uses its native trunk unless `--into` overrides it. The default `squash` strategy integrates each feature, then removes its clean local worktree and branch. Dirty, missing, foreign-claimed, unmanaged, conflicted, or unreviewed state blocks the exact plan without discarding work.

To preserve the feature worktrees and publish one pull request per feature instead:

```bash
temper push --pr --plan
temper push --apply plan:push:workspace:1 --yes
```

Both operations are resumable across repository failures. Neither operation releases or deploys software.

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
=======
Imp discovers `workspace.yaml` by walking up from the working directory, so one
feature can span several repositories and integrate them in dependency order.
Hearth owns everything from trunk onward: the local runtime, credentials,
releases, and deployment.
>>>>>>> master
