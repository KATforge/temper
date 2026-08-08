# Changelog

All notable changes to this project are documented here.

## [Unreleased]

- Added `temper.error.v1` machine-readable failure envelopes under `--json`
- Added stale-lock reclamation, owner-checked lock release, and collision-free plan numbering
- Changed Imp failures to surface the message from Imp's own error envelope
- Removed dead snapshot-candidate, archive, and topology-shim code paths
- Added coordinated Imp changes across repositories
- Added composed plans, interactive selection, and stable JSON output
- Added one warm shared runtime with exclusive live or snapshot-backed leases
- Added recursive versioned service dependencies in `temper.yaml`
- Added workspace-scoped local state and registered repository discovery
- Added source-only services and Docker-backed end-to-end runtime coverage
- Added stable active-source maps with explicit trunk selection and automatic stale-pointer repair
- Added waitable exclusive runtime leases, inferred Compose mounts, immutable test snapshots, and runtime health receipts
- Added optional runtime preparation and private process-environment loading
- Added repository deduplication when several service names share one source repository
- Changed common change actions to top-level commands
- Changed KATforge topology ownership from Hearth to `temper.yaml`
- Changed workspace initialization to omit unused runtime and environment placeholders
- Changed new plan metadata and Docker labels to use Temper-owned names
- Fixed workspace diagnostics, process exit codes, and clean CLI errors
- Fixed partial multi-repository completion so the same plan resumes only pending repositories
- Removed deployment, promotion, and rollback from Temper
