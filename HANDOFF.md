# DK InfraEdge AI Ops Agent — Deploy & Test

## Context
Pre-packaged files in ~/dk-ops-agent/.
This is an AI-powered operations agent that checks pipeline health, revenue, onboarding, and content status daily using the DK InfraEdge MCP server + OpenAI.

## Steps

### 1. Test locally first
```bash
cd ~/dk-ops-agent
pip install httpx --break-system-packages

# Quick test — pipeline check only (no OpenAI needed)
MCP_BASE_URL=https://dk-infraedge-mcp.home.arpa \
MCP_AUTH_TOKEN=***REMOVED-CREDENTIAL*** \
python3 agent.py --mode pipeline-check -v

# Full report with AI recommendations
MCP_BASE_URL=https://dk-infraedge-mcp.home.arpa \
MCP_AUTH_TOKEN=***REMOVED-CREDENTIAL*** \
OPENAI_API_KEY=$(kubectl -n logsight get secret logsight-secrets -o jsonpath='{.data.OPENAI_API_KEY}' | base64 -d) \
python3 agent.py --mode full-report --output /tmp/ops-report.md -v

cat /tmp/ops-report.md
```

### 2. Create k8s secret
```bash
kubectl -n logsight create secret generic dk-ops-agent-secrets \
  --from-literal=MCP_AUTH_TOKEN=***REMOVED-CREDENTIAL*** \
  --dry-run=client -o yaml | kubectl apply -f -
```

### 3. Build and deploy as CronJob
```bash
cd ~/dk-ops-agent
docker build -t dk-ops-agent:latest .
docker save dk-ops-agent:latest -o /tmp/dk-ops-agent.tar

for node in k3s-cp-01 k3s-wk-01 k3s-wk-02; do
  scp /tmp/dk-ops-agent.tar $node:/tmp/
  ssh $node "sudo ctr -n k8s.io images import /tmp/dk-ops-agent.tar && rm /tmp/dk-ops-agent.tar"
done

kubectl apply -f ~/dk-ops-agent/k8s/cronjob.yaml
```

### 4. Test the CronJob manually
```bash
kubectl -n logsight create job --from=cronjob/dk-ops-agent dk-ops-test
kubectl -n logsight wait --for=condition=complete job/dk-ops-test --timeout=60s
kubectl -n logsight logs job/dk-ops-test
kubectl -n logsight delete job dk-ops-test
```

### 5. Push to GitHub
```bash
cd ~/dk-ops-agent
git init && git add -A
git commit -m "feat: DK InfraEdge AI ops agent — daily pipeline/revenue/onboarding briefings"
gh repo create eched1/dk-ops-agent --public --source . --push
```

## Expected output
The daily briefing should show:
- Pipeline: 9 deals, ~$118K potential revenue, 7 in Idea stage, 2 Building
- Revenue: current invoice totals
- Onboarding: any active client onboardings
- AI recommendations for each section
