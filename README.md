# Skills

**Repository:** https://github.com/corradodebari/skills/
**Version:** 0.1.0

Collection of skills implemented to support developers on Oracle platforms related projects. Each included guide has actionable examples, best practices, common pitfalls, sources, and explicit Oracle version compatibility notes.


| Directory | Description |
|-----------|-------------|
| [`graphrag-oracle-builder`](./graphrag-oracle-builder/SKILL.md) | Build a GraphRAG knowledge base into an Oracle Property Graph (23ai–26ai) with embeddings from documents. [`Guide`](./graphrag-oracle-builder/CODEX_SKILL_GRAPH_RAG_GUIDE.md).
| [`microtx-workflows`](./microtx-workflows/SKILL.md) | Manage Oracle MicroTx Workflow Server workflows, connectors, agentic AI profiles, workflow executions, and human-task approvals against the Conductor-based REST API. [`Skill`](./microtx-workflows/SKILL.md).

## Installation

Clone this repository locally:

```bash
git clone https://github.com/corradodebari/skills.git
cd skills
```

Install a skill by copying its directory into the target assistant's skills folder.

## microtx-workflows

### Claude
From project root:

```bash
mkdir -p ~/.claude/skills
cp -R microtx-workflows ~/.claude/skills/
```

#### Claude permissions

To speedup the sessions, merge the following permissions into `~/.claude/settings.json` if you want the skill to read its bundled references and call a local MicroTx Workflow Server without repeated prompts:

```json
{
  "permissions": {
    "allow": [
      "Bash(curl http://127.0.0.1/workflow-server/api*)",
      "Bash(curl -s http://127.0.0.1/workflow-server/api*)",
      "Bash(curl -X GET http://127.0.0.1/workflow-server/api*)",
      "Bash(curl -s -X GET http://127.0.0.1/workflow-server/api*)",
      "Read([YOUR_HOME_DIR]/.claude/skills/microtx-workflows/**/*.md)",
      "Bash(python3 *),
      "Bash(rg:*)"
    ]
  }
}
```

Adjust the `Read(...)` path if you installed the skill somewhere other than `[YOUR_HOME_DIR]/.claude/skills/microtx-workflows`.

### Codex
From project root:

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
cp -R microtx-workflows "${CODEX_HOME:-$HOME/.codex}/skills/"
```

#### Codex permissions

To speedup the execution as well for Codex, filesystem and network permissions belong in `${CODEX_HOME:-$HOME/.codex}/config.toml`. Add a permission profile like this:

```toml
default_permissions = "microtx-workflows"

[permissions.microtx-workflows]
approval_policy = "on-request"

[permissions.microtx-workflows.filesystem]
":minimal" = "read"
"~/.codex/skills/microtx-workflows" = "read"
":workspace_roots" = "write"

[permissions.microtx-workflows.network]
enabled = true
mode = "limited"

[permissions.microtx-workflows.network.domains]
"127.0.0.1" = "allow"
"localhost" = "allow"
```

Adjust the skill path if `CODEX_HOME` is not `~/.codex`, or if you installed the skill with a symlink to another checkout.

Codex command allow rules live separately in `${CODEX_HOME:-$HOME/.codex}/rules/default.rules`. Add these only if you want Codex to skip approval prompts for the broad command prefixes:

```python
prefix_rule(
    pattern = ["curl"],
    decision = "allow",
    justification = "Allow MicroTx Workflow Server API calls; the Codex network profile restricts destinations to localhost.",
)

prefix_rule(
    pattern = ["python3"],
    decision = "allow",
    justification = "Allow local Python helpers for JSON formatting and OpenAPI inspection.",
)
```


---
