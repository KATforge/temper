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

Imp discovers `workspace.yaml` by walking up from the working directory, so one
feature can span several repositories and integrate them in dependency order.
Hearth owns everything from trunk onward: the local runtime, credentials,
releases, and deployment.
