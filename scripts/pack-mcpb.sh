#!/usr/bin/env bash
# Build a Claude Desktop .mcpb bundle of the current source tree.
#
# Reads the version from manifest.json and writes
# .mcpb-cache/multi-mail-<version>.mcpb.
#
# Requires: node/npx (uses npx -y @anthropic-ai/mcpb, no global install needed).

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

if ! command -v npx >/dev/null 2>&1; then
  echo "error: npx not on PATH — install Node.js first" >&2
  exit 1
fi

manifest_version="$(grep -E '"manifest_version"' manifest.json | head -1 | sed -E 's/.*"([^"]+)".*/\1/' || true)"
plugin_version="$(grep -E '^[[:space:]]*"version"' manifest.json | head -1 | sed -E 's/.*"version"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/')"
plugin_marketplace_version="$(grep -E '"version"' .claude-plugin/plugin.json | head -1 | sed -E 's/.*"version"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/')"
pyproject_version="$(grep -E '^version[[:space:]]*=' pyproject.toml | head -1 | sed -E 's/.*"([^"]+)".*/\1/')"

if [[ "$plugin_version" != "$plugin_marketplace_version" ]] \
  || [[ "$plugin_version" != "$pyproject_version" ]]; then
  echo "error: version mismatch — keep all three in sync before packing:" >&2
  echo "         manifest.json:              $plugin_version" >&2
  echo "         .claude-plugin/plugin.json: $plugin_marketplace_version" >&2
  echo "         pyproject.toml:             $pyproject_version" >&2
  exit 1
fi

mkdir -p .mcpb-cache
output=".mcpb-cache/multi-mail-${plugin_version}.mcpb"

echo "Packing multi-mail v${plugin_version} (manifest_version ${manifest_version:-unknown})"
npx -y @anthropic-ai/mcpb pack . "$output"

echo
echo "Bundle: $repo_root/$output"
echo "Install: drag it onto Claude Desktop → Customize → Personal plugins"
echo "         (or use the ⋮ menu on the existing entry to replace)"
