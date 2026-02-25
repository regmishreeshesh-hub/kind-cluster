# 1. Get the crumb first
CRUMB=$(curl -s -u "zetalabsserver:1166ba67d11d865f782acbf2e8578a8936" \
  --cookie-jar /tmp/cookies \
  "http://192.168.1.72:8090/crumbIssuer/api/json" | jq -r '.crumb')

# 2. Trigger the build (with crumb + cookie)
curl -X POST \
  -u "your-username:your-api-token" \
  --cookie /tmp/cookies \
  -H "Jenkins-Crumb: $CRUMB" \
  "http://192.168.1.72:8090/job/Development/job/embedder-dev/build?token=buildtoken"

  # trigger github pipeline 
  curl -X POST -H "Authorization: token $GITHUB_TOKEN" https://api.github.com/repos/your-repo/Dispatches