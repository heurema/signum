---
title: "Claude Code Plugin Update Reliability: Known Bugs and Workarounds"
date: 2026-03-16
status: complete
depth: quick
verification: unverified
---

# Claude Code Plugin Update: Known Bugs and Reliable Workaround

## Problem Statement

`claude plugin update` не обновляет плагины надёжно. Три confirmed bugs в Claude Code CLI (все OPEN на март 2026):

1. **#29071**: `plugin update` делает `git fetch` но НЕ делает `git merge`/`git pull` — marketplace clone остаётся на старом коммите
2. **#14061**: plugin cache (`~/.claude/plugins/cache/`) не инвалидируется после update
3. **#15642**: `CLAUDE_PLUGIN_ROOT` указывает на stale cache directory после update

## Root Cause

```
claude plugin update <plugin>@<marketplace>
  │
  ├─ git fetch origin         ← works
  ├─ git merge origin/main    ← MISSING (bug #29071)
  ├─ read marketplace.json    ← reads stale local copy
  ├─ compare versions         ← "already at latest" (wrong)
  └─ invalidate cache         ← NEVER HAPPENS (bug #14061)
```

## Multi-Profile Compound Problem

С несколькими профилями проблема усугубляется:

```
~/.claude/                              ← user scope
  plugins/
    marketplaces/emporium/              ← marketplace clone (may be stale)
    cache/emporium/signum/4.1.1/        ← stale cache
  settings.json                         ← "signum@nex-devtools": true

~/.claude-profiles/work/config/         ← work profile scope
  plugins/
    marketplaces/emporium/              ← SEPARATE marketplace clone (may be stale)
    cache/emporium/signum/4.6.1/        ← different stale version
  settings.json                         ← "signum@emporium": true
```

`claude plugin update` обновляет только scope, в котором плагин установлен. Если `signum@nex-devtools` в user scope, а `signum@emporium` в work scope — нужно обновлять оба отдельно.

## Reliable Update Script

```bash
#!/usr/bin/env bash
# signum-update.sh — reliable plugin update across all scopes
# Usage: lib/signum-update.sh [version]
set -euo pipefail

VERSION="${1:-$(jq -r .version .claude-plugin/plugin.json)}"
PLUGIN_NAME="signum"
MARKETPLACE="emporium"

echo "Updating $PLUGIN_NAME to v$VERSION..."

# Step 1: Update ALL marketplace clones (fix for bug #29071)
for mp_dir in \
  "$HOME/.claude/plugins/marketplaces/$MARKETPLACE" \
  "$HOME/.claude-profiles"/*/config/plugins/marketplaces/"$MARKETPLACE"; do
  [ -d "$mp_dir/.git" ] || continue
  echo "  Pulling marketplace: $mp_dir"
  (cd "$mp_dir" && git fetch origin && git reset --hard origin/main) 2>/dev/null || true
done

# Step 2: Clear ALL stale caches (fix for bug #14061)
for cache_dir in \
  "$HOME/.claude/plugins/cache"/*/"$PLUGIN_NAME" \
  "$HOME/.claude-profiles"/*/config/plugins/cache/*/"$PLUGIN_NAME"; do
  [ -d "$cache_dir" ] || continue
  echo "  Clearing cache: $cache_dir"
  rm -rf "$cache_dir"
done

# Step 3: Sync fresh copy from source to ALL profile caches
for profile_cache in \
  "$HOME/.claude-profiles"/*/config/plugins/cache/"$MARKETPLACE"/"$PLUGIN_NAME"; do
  PROFILE_BASE=$(dirname $(dirname $(dirname "$profile_cache")))
  [ -d "$PROFILE_BASE" ] || continue
  
  TARGET="$profile_cache/$VERSION"
  mkdir -p "$TARGET"
  
  for dir in .claude-plugin agents commands lib platforms docs tests skills; do
    [ -d "$dir" ] && cp -R "$dir" "$TARGET/"
  done
  for file in README.md CHANGELOG.md QUICKSTART.md SKILL.md; do
    [ -f "$file" ] && cp "$file" "$TARGET/"
  done
  
  echo "  Synced: $TARGET"
done

# Step 4: Run CLI update (now marketplace is fresh, cache is clean)
claude plugin update "$PLUGIN_NAME@$MARKETPLACE" --scope user 2>/dev/null || true

echo ""
echo "Done. Restart Claude Code to apply v$VERSION."
```

## Recommended Integration

Add to `lib/signum-update.sh` and call from Makefile/justfile:
```makefile
update-plugin:
	lib/signum-update.sh
```

## Sources
- [#29071: plugin update doesn't fast-forward marketplace](https://github.com/anthropics/claude-code/issues/29071) — OPEN
- [#14061: plugin cache not invalidated on update](https://github.com/anthropics/claude-code/issues/14061) — OPEN
- [#15642: CLAUDE_PLUGIN_ROOT stale after update](https://github.com/anthropics/claude-code/issues/15642) — OPEN
