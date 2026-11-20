#!/usr/bin/env bash
# modules-check.sh — read modules.yaml and emit supersession warnings
# Usage: modules-check.sh [--project-root <path>]
# Output: JSON to stdout with module lifecycle status and warnings
# Exit 0: check complete
# Exit 1: modules.yaml not found (graceful, not an error)

set -euo pipefail

PROJECT_ROOT="."
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-root) PROJECT_ROOT="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

MODULES_FILE="$PROJECT_ROOT/modules.yaml"

if [ ! -f "$MODULES_FILE" ]; then
  echo '{"status":"no_manifest","modules":[],"warnings":[]}'
  exit 0
fi

# Parse modules.yaml and emit warnings for deprecated/superseded modules
python3 -c "
import sys, json

try:
    import yaml
except ImportError:
    # Fallback: minimal YAML parser for flat module entries
    import re

    class MinimalYAML:
        @staticmethod
        def safe_load(text):
            # Parse simple modules.yaml format
            result = {'modules': []}
            current = None
            for line in text.split('\n'):
                stripped = line.strip()
                if not stripped or stripped.startswith('#'):
                    continue
                if line.startswith('  ') and current is not None:
                    if ':' in stripped:
                        k, _, v = stripped.partition(':')
                        current[k.strip()] = v.strip().strip('\"').strip(\"'\")
                elif stripped.startswith('- '):
                    # New module entry under modules:
                    if current is not None:
                        result['modules'].append(current)
                    current = {}
                    rest = stripped[2:].strip()
                    if ':' in rest:
                        k, _, v = rest.partition(':')
                        current[k.strip()] = v.strip().strip('\"').strip(\"'\")
                elif stripped == 'modules:':
                    pass
            if current is not None:
                result['modules'].append(current)
            return result

    yaml = MinimalYAML()

with open('$MODULES_FILE') as f:
    data = yaml.safe_load(f.read())

modules = data.get('modules', [])
warnings = []

for mod in modules:
    path = mod.get('path', '')
    lifecycle = mod.get('lifecycle', 'active')
    superseded_by = mod.get('supersededBy', '')
    reason = mod.get('reason', '')

    if lifecycle == 'superseded' and superseded_by:
        warnings.append({
            'level': 'error',
            'path': path,
            'message': f'SUPERSEDED by {superseded_by}: {reason}. Do NOT build on this module.',
        })
    elif lifecycle == 'deprecated':
        warnings.append({
            'level': 'warn',
            'path': path,
            'message': f'DEPRECATED: {reason}. Prefer the replacement if available.',
        })
    elif lifecycle == 'experimental':
        warnings.append({
            'level': 'info',
            'path': path,
            'message': f'EXPERIMENTAL: may change or be removed.',
        })

result = {
    'status': 'ok',
    'modules_total': len(modules),
    'active': len([m for m in modules if m.get('lifecycle', 'active') == 'active']),
    'superseded': len([m for m in modules if m.get('lifecycle') == 'superseded']),
    'deprecated': len([m for m in modules if m.get('lifecycle') == 'deprecated']),
    'warnings': warnings,
}
print(json.dumps(result, indent=2))
" 2>/dev/null || echo '{"status":"parse_error","modules":[],"warnings":[]}'
