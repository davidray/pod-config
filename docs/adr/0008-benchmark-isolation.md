# ADR 0008: Benchmark isolation and comparability

Status: accepted (2026-09-23)

## Decision
- **Workspaces.** Each trial gets a fresh temp workspace. Git sources are cloned `--no-hardlinks` and checked out at a required pinned `ref`. Plain-directory fixtures are copied and committed with a fixed author, committer and date, so identical content yields an identical starting SHA. Sources are never mutated, and tests assert this.
- **Setup** runs before the agent with network access and scrubbed secrets. The agent and validation run in the no-network sandbox.
- **Validation** is harness-owned:
  - `restore` resets protected paths, such as the tests the agent must not weaken
  - `overlay` adds hidden tests
  - afterwards the workspace is reset to the agent's result tree, so the recorded diff and any retry never include hidden tests
- **Case verification.** `qwenbench bench verify-cases` proves each case discriminates: the base fails and `reference.patch` passes. It caught a weak reference test during development through the mutation check.
- **Comparability.** Model revision, image, argv, generation, agent SHA plus a dirty flag, prompt hashes and starting commits go into `run.json`. `compare` lists every difference instead of silently comparing.

## Consequences
The agent sandbox has no network, so repositories whose tests need services
must start them in setup or be adapted. Diffs are captured as git trees, so
nothing depends on the agent's cooperation.
