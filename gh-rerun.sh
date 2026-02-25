#!/usr/bin/env bash
set -euo pipefail

ORG="withzetalabs"

gh repo list "$ORG" --json name --jq '.[].name' |
while read -r repo; do
  echo "→ $ORG/$repo"

  # Get the most recent workflow run ID (any status)
  RUN_ID=$(gh run list \
    -R "$ORG/$repo" \
    --limit 1 \
    --json databaseId \
    --jq '.[0].databaseId' 2>/dev/null || true)

  if [[ -z "$RUN_ID" ]]; then
    echo "  ⚠️  No workflow runs found"
    continue
  fi

  echo "  ↻ Re-running workflow run ID: $RUN_ID"
  gh run rerun "$RUN_ID" -R "$ORG/$repo" --failed=false
done
