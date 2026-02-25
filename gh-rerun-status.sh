ORG="withzetalabs"

gh repo list "$ORG" --json name --jq '.[].name' |
while read -r repo; do
  gh run list \
    -R "$ORG/$repo" \
    --status in_progress,queued \
    --json workflowName,status,createdAt \
    --jq '.[] | "'$repo' | \(.workflowName) | \(.status) | \(.createdAt)"'
done
