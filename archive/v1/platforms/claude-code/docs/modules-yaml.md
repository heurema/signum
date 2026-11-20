# modules.yaml — Module Lifecycle Manifest

Machine-readable declaration of which code modules are current, deprecated, or superseded. Designed for AI agent consumption at session start.

## Format

Place `modules.yaml` at the project root (next to `CLAUDE.md`).

```yaml
# modules.yaml — module lifecycle status
# Signum reads this during CONTRACT phase to warn about superseded code.
# Agents read this at session start to avoid building on obsolete modules.

modules:
  - path: src/auth/jwt.rs
    lifecycle: active
    description: JWT token generation and validation

  - path: src/auth/session.rs
    lifecycle: superseded
    supersededBy: src/auth/jwt.rs
    reason: "Replaced by JWT-based auth in Q1 2026"
    supersededAt: "2026-01-15"

  - path: src/api/v1/
    lifecycle: deprecated
    reason: "v2 API preferred. Remove after 2026-06-01"
    removeAfter: "2026-06-01"

  - path: src/experiments/cache_v2.rs
    lifecycle: experimental
    description: "Experimental cache layer, may be removed"
```

## Fields

| Field | Required | Values | Description |
|-------|----------|--------|-------------|
| `path` | yes | string | File or directory path relative to project root |
| `lifecycle` | yes | `active` / `deprecated` / `superseded` / `experimental` | Current status |
| `description` | no | string | What this module does |
| `supersededBy` | when superseded | string | Path to the replacement module |
| `reason` | when deprecated/superseded | string | Why this module is no longer current |
| `supersededAt` | no | date | When supersession happened |
| `removeAfter` | no | date | Deadline for removal (feature flag time-bomb) |

## Lifecycle States

```
experimental → active → deprecated → superseded → (removed from manifest)
```

- **active** — current, correct implementation. Agents should use this.
- **experimental** — may change or be removed. Agents should note instability.
- **deprecated** — still works but a replacement exists or is planned. Agents should prefer alternatives.
- **superseded** — replaced by another module. Agents must NOT build on this. Use `supersededBy` instead.

## Integration

### Signum (CONTRACT phase)
`modules-check.sh` reads `modules.yaml` during Step 1.2.7 and emits warnings for any inScope files that are superseded or deprecated.

### Session-start hook (any agent)
```bash
# Example: inject module warnings into agent context
if [ -f modules.yaml ]; then
  python3 -c "
import yaml, json
data = yaml.safe_load(open('modules.yaml'))
warnings = [m for m in data.get('modules', []) if m.get('lifecycle') in ('superseded', 'deprecated')]
if warnings:
    print('MODULE WARNINGS:')
    for w in warnings:
        print(f'  {w[\"path\"]}: {w[\"lifecycle\"]} — {w.get(\"reason\", \"\")}')
"
fi
```

### CLAUDE.md reference
Add to CLAUDE.md:
```
@modules.yaml
```
This ensures the manifest is loaded into agent context.

## Design Rationale

Based on research (2026-03-20):
- **Backstage catalog-info.yaml** — `spec.lifecycle` vocabulary (active/deprecated/experimental)
- **Ansible meta/runtime.yml** — tombstone pattern with routing to replacement
- **Kubernetes Finalizers** — cleanup is a blocking contract, not optional
- **Codified Context** (arXiv:2602.20478) — session-start hook for context injection
