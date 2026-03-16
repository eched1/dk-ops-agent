#!/usr/bin/env python3
"""DK InfraEdge AI Ops Agent — automates routine business operations.

Runs daily or on-demand to:
1. Check pipeline health and flag stalled deals
2. Chase overdue invoices
3. Track onboarding progress and flag blockers
4. Generate daily briefing
5. Suggest follow-up actions

Uses the DK InfraEdge MCP server (Notion-backed) for all data access.
Uses OpenAI for intelligent recommendations.

Usage:
    python3 agent.py --mode daily-briefing
    python3 agent.py --mode pipeline-check
    python3 agent.py --mode invoice-chase
    python3 agent.py --mode onboarding-check
    python3 agent.py --mode full-report
"""

import argparse
import datetime
import json
import logging
import os
import sys
from typing import Optional

import httpx

__version__ = "0.1.0"
LOG = logging.getLogger("dk-ops-agent")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MCP_BASE_URL = os.getenv("MCP_BASE_URL", "https://dk-infraedge-mcp.home.arpa")
MCP_AUTH_TOKEN = os.getenv("MCP_AUTH_TOKEN", "***REMOVED-CREDENTIAL***")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")  # optional


# ---------------------------------------------------------------------------
# MCP Client — calls DK InfraEdge MCP tools
# ---------------------------------------------------------------------------

class MCPClient:
    """Minimal client for the DK InfraEdge MCP server (StreamableHTTP transport)."""

    def __init__(self, base_url: str, auth_token: str):
        self.base_url = base_url.rstrip("/")
        self.auth_token = auth_token
        self.session_id: Optional[str] = None
        self.client = httpx.Client(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {auth_token}",
                "Accept": "application/json, text/event-stream",
            },
            verify=False,
            timeout=30,
        )
        self._initialize()

    def _initialize(self):
        """Initialize the MCP session (required by StreamableHTTPServerTransport)."""
        payload = {
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "dk-ops-agent", "version": __version__},
            },
            "id": 1,
        }
        try:
            resp = self.client.post("/mcp", json=payload)
            resp.raise_for_status()
            self.session_id = resp.headers.get("mcp-session-id")
            LOG.info("MCP session initialized: %s", self.session_id)
        except Exception as e:
            LOG.error("MCP init failed: %s", e)

    def _parse_sse(self, text: str) -> dict:
        """Parse SSE response (event: message\\ndata: {...})."""
        for line in text.strip().split("\n"):
            if line.startswith("data: "):
                return json.loads(line[6:])
        # Fallback: try parsing as plain JSON
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"error": f"Unparseable response: {text[:200]}"}

    def call_tool(self, tool_name: str, args: dict = None) -> dict:
        """Call an MCP tool and return the result."""
        payload = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": args or {},
            },
            "id": 1,
        }
        headers = {}
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        try:
            resp = self.client.post("/mcp", json=payload, headers=headers)
            resp.raise_for_status()
            data = self._parse_sse(resp.text)
            if "result" in data:
                result = data["result"]
                # MCP tools return content as [{type: "text", text: "..."}]
                if isinstance(result, dict) and "content" in result:
                    for item in result["content"]:
                        if item.get("type") == "text":
                            try:
                                return json.loads(item["text"])
                            except (json.JSONDecodeError, TypeError):
                                return {"text": item["text"]}
                return result
            if "error" in data:
                LOG.error("MCP error: %s", data["error"])
                return {"error": data["error"]}
            return data
        except Exception as e:
            LOG.error("MCP call failed (%s): %s", tool_name, e)
            return {"error": str(e)}

    # --- Convenience methods ---

    def pipeline_overview(self) -> dict:
        return self.call_tool("pipeline_overview")

    def crm_search(self, **kwargs) -> dict:
        return self.call_tool("crm_search", kwargs)

    def revenue_report(self) -> dict:
        return self.call_tool("revenue_report")

    def onboarding_status(self) -> dict:
        return self.call_tool("onboarding_status")

    def content_status(self) -> dict:
        return self.call_tool("content_status")

    def crm_update_stage(self, contact_id: str, new_stage: str, notes: str = "") -> dict:
        return self.call_tool("crm_update_stage", {
            "contact_id": contact_id,
            "new_stage": new_stage,
            "notes": notes,
        })

    def invoice_create(self, **kwargs) -> dict:
        return self.call_tool("invoice_create", kwargs)


# ---------------------------------------------------------------------------
# AI Advisor — generates recommendations
# ---------------------------------------------------------------------------

class AIAdvisor:
    """Uses OpenAI to generate actionable recommendations from business data."""

    def __init__(self, api_key: str, model: str = "gpt-4o-mini"):
        self.enabled = bool(api_key)
        self.api_key = api_key
        self.model = model
        self.client = httpx.Client(timeout=30) if self.enabled else None

    def advise(self, context: str, question: str) -> str:
        if not self.enabled:
            return "[AI advisor unavailable — set OPENAI_API_KEY]"

        try:
            resp = self.client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": (
                            "You are the AI operations advisor for DK InfraEdge, "
                            "a Texas-based infrastructure consulting company. "
                            "Give concise, actionable recommendations. "
                            "Focus on revenue impact and next steps. "
                            "Use bullet points. Keep it under 200 words."
                        )},
                        {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}"},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 500,
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            LOG.error("AI advisor error: %s", e)
            return f"[AI advisor error: {e}]"


# ---------------------------------------------------------------------------
# Operations checks
# ---------------------------------------------------------------------------

def check_pipeline(mcp: MCPClient, advisor: AIAdvisor) -> dict:
    """Check pipeline health — stalled deals, missing follow-ups."""
    overview = mcp.pipeline_overview()
    overdue = mcp.crm_search(overdue_only=True)

    report = {
        "section": "Pipeline Health",
        "total_deals": overview.get("totalDeals", 0),
        "total_revenue": overview.get("totalPotentialRevenue", 0),
        "by_stage": overview.get("byStage", {}),
        "overdue_followups": [],
        "deals": overview.get("deals", []),
    }

    if isinstance(overdue, dict) and "contacts" in overdue:
        report["overdue_followups"] = overdue["contacts"]

    # AI recommendations
    context = json.dumps(report, indent=2, default=str)
    report["ai_recommendation"] = advisor.advise(
        context,
        "Which deals need immediate attention? What's the biggest revenue risk? "
        "What specific outreach should happen this week?"
    )

    return report


def check_revenue(mcp: MCPClient, advisor: AIAdvisor) -> dict:
    """Check revenue — outstanding invoices, overdue payments."""
    rev = mcp.revenue_report()

    report = {
        "section": "Revenue & Invoicing",
        "data": rev,
    }

    context = json.dumps(rev, indent=2, default=str)
    report["ai_recommendation"] = advisor.advise(
        context,
        "Are there overdue invoices? What's the cash flow outlook? "
        "Should we send payment reminders?"
    )

    return report


def check_onboarding(mcp: MCPClient, advisor: AIAdvisor) -> dict:
    """Check active onboardings for blockers."""
    status = mcp.onboarding_status()

    report = {
        "section": "Client Onboarding",
        "data": status,
    }

    context = json.dumps(status, indent=2, default=str)
    report["ai_recommendation"] = advisor.advise(
        context,
        "Are any onboardings stalled or behind schedule? "
        "What milestones need attention this week?"
    )

    return report


def check_content(mcp: MCPClient, advisor: AIAdvisor) -> dict:
    """Check content pipeline status."""
    status = mcp.content_status()

    report = {
        "section": "Content Pipeline",
        "data": status,
    }

    context = json.dumps(status, indent=2, default=str)
    report["ai_recommendation"] = advisor.advise(
        context,
        "What content should be published this week? "
        "Are there any bottlenecks in the pipeline?"
    )

    return report


# ---------------------------------------------------------------------------
# Report formatter
# ---------------------------------------------------------------------------

def format_report(sections: list[dict]) -> str:
    """Format sections into a readable daily briefing."""
    today = datetime.date.today().isoformat()
    lines = [
        f"# DK InfraEdge Daily Ops Briefing — {today}",
        "",
    ]

    for section in sections:
        name = section.get("section", "Unknown")
        lines.append(f"## {name}")
        lines.append("")

        if name == "Pipeline Health":
            lines.append(f"**Total deals:** {section.get('total_deals', 0)}")
            lines.append(f"**Pipeline value:** ${section.get('total_revenue', 0):,.0f}")
            stages = section.get("by_stage", {})
            if stages:
                lines.append(f"**By stage:** {', '.join(f'{k}: {v}' for k, v in stages.items())}")
            overdue = section.get("overdue_followups", [])
            if overdue:
                lines.append(f"\n**Overdue follow-ups ({len(overdue)}):**")
                for c in overdue[:5]:
                    lines.append(f"  - {c.get('name', 'Unknown')} ({c.get('company', '')})")

        elif name == "Revenue & Invoicing":
            data = section.get("data", {})
            lines.append(f"**Monthly revenue:** ${data.get('totalPaid', 0):,.0f}")
            lines.append(f"**Outstanding:** ${data.get('totalOutstanding', 0):,.0f}")
            lines.append(f"**Overdue:** ${data.get('totalOverdue', 0):,.0f}")

        elif name == "Client Onboarding":
            data = section.get("data", {})
            active = data.get("activeOnboardings", [])
            if isinstance(active, int):
                lines.append(f"**Active onboardings:** {active}")
            else:
                lines.append(f"**Active onboardings:** {len(active)}")
                for ob in active:
                    if isinstance(ob, dict):
                        lines.append(f"  - {ob.get('client', 'Unknown')}: {ob.get('progress', '?')}% complete")

        elif name == "Content Pipeline":
            data = section.get("data", {})
            by_status = data.get("byStatus", {})
            if by_status:
                lines.append(f"**Pipeline:** {', '.join(f'{k}: {v}' for k, v in by_status.items())}")

        # AI recommendation
        rec = section.get("ai_recommendation", "")
        if rec:
            lines.append(f"\n**AI Recommendation:**\n{rec}")

        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------

def send_slack(webhook_url: str, text: str):
    """Send report to Slack webhook."""
    if not webhook_url:
        return
    try:
        httpx.post(webhook_url, json={"text": text}, timeout=10)
        LOG.info("Slack notification sent")
    except Exception as e:
        LOG.error("Slack send failed: %s", e)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="DK InfraEdge AI Ops Agent")
    parser.add_argument("--mode", default="full-report",
                       choices=["daily-briefing", "pipeline-check", "invoice-chase",
                                "onboarding-check", "content-check", "full-report"])
    parser.add_argument("--output", help="Save report to file")
    parser.add_argument("--slack", action="store_true", help="Send to Slack")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    mcp = MCPClient(MCP_BASE_URL, MCP_AUTH_TOKEN)
    advisor = AIAdvisor(OPENAI_API_KEY, OPENAI_MODEL)

    LOG.info("DK InfraEdge Ops Agent v%s — mode: %s", __version__, args.mode)

    sections = []

    if args.mode in ("daily-briefing", "full-report", "pipeline-check"):
        sections.append(check_pipeline(mcp, advisor))

    if args.mode in ("daily-briefing", "full-report", "invoice-chase"):
        sections.append(check_revenue(mcp, advisor))

    if args.mode in ("daily-briefing", "full-report", "onboarding-check"):
        sections.append(check_onboarding(mcp, advisor))

    if args.mode in ("full-report", "content-check"):
        sections.append(check_content(mcp, advisor))

    report = format_report(sections)
    print(report)

    if args.output:
        with open(args.output, "w") as f:
            f.write(report)
        LOG.info("Report saved to %s", args.output)

    if args.slack or SLACK_WEBHOOK_URL:
        send_slack(SLACK_WEBHOOK_URL, report)


if __name__ == "__main__":
    main()
