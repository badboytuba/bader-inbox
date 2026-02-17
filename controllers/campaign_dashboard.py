# Copyright 2026 Bader Business
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

"""
Campaign Dashboard Controller
Provides JSON endpoints for the OWL campaign dashboard component.
"""

import json
import logging
from datetime import datetime, timedelta
from odoo import http, fields
from odoo.http import request

_logger = logging.getLogger(__name__)


class CampaignDashboardController(http.Controller):

    @http.route("/bader-inbox/campaign/dashboard", type="json", auth="user")
    def get_dashboard_data(self, **kw):
        """Return all dashboard data in a single call"""
        Campaign = request.env["bader.inbox.campaign"]
        Execution = request.env["bader.inbox.campaign.execution"]
        ExecLog = request.env["bader.inbox.campaign.exec.log"]
        Throttle = request.env["bader.inbox.campaign.throttle"]
        OptOut = request.env["bader.inbox.campaign.optout"]

        now = fields.Datetime.now()
        today = fields.Date.today()
        week_ago = now - timedelta(days=7)
        month_ago = now - timedelta(days=30)

        # ── Global KPIs ──
        all_campaigns = Campaign.search([])
        active_campaigns = Campaign.search([("status", "=", "active")])

        total_sent = sum(c.stat_sent for c in all_campaigns)
        total_delivered = sum(c.stat_delivered for c in all_campaigns)
        total_read = sum(c.stat_read for c in all_campaigns)
        total_replied = sum(c.stat_replied for c in all_campaigns)
        total_failed = sum(c.stat_failed for c in all_campaigns)
        total_opted_out = sum(c.stat_opted_out for c in all_campaigns)

        delivery_rate = (total_delivered / total_sent * 100) if total_sent else 0
        read_rate = (total_read / total_sent * 100) if total_sent else 0
        reply_rate = (total_replied / total_sent * 100) if total_sent else 0
        fail_rate = (total_failed / total_sent * 100) if total_sent else 0

        kpis = {
            "total_campaigns": len(all_campaigns),
            "active_campaigns": len(active_campaigns),
            "total_sent": total_sent,
            "total_delivered": total_delivered,
            "total_read": total_read,
            "total_replied": total_replied,
            "total_failed": total_failed,
            "total_opted_out": total_opted_out,
            "delivery_rate": round(delivery_rate, 1),
            "read_rate": round(read_rate, 1),
            "reply_rate": round(reply_rate, 1),
            "fail_rate": round(fail_rate, 1),
        }

        # ── Campaign Cards ──
        campaign_cards = []
        for c in all_campaigns.sorted(key=lambda x: x.create_date, reverse=True)[:20]:
            card_sent = c.stat_sent or 0
            card = {
                "id": c.id,
                "name": c.name,
                "status": c.status,
                "type": c.campaign_type,
                "sent": card_sent,
                "delivered": c.stat_delivered,
                "read": c.stat_read,
                "replied": c.stat_replied,
                "failed": c.stat_failed,
                "opted_out": c.stat_opted_out,
                "delivery_rate": round((c.stat_delivered / card_sent * 100) if card_sent else 0, 1),
                "read_rate": round((c.stat_read / card_sent * 100) if card_sent else 0, 1),
                "reply_rate": round((c.stat_replied / card_sent * 100) if card_sent else 0, 1),
                "start_date": c.start_date.isoformat() if c.start_date else None,
                "ab_enabled": c.ab_enabled,
                "user": c.user_id.name if c.user_id else "",
                "audience_count": c.total_audience,
            }
            campaign_cards.append(card)

        # ── Channel Health (Number Status) ──
        channels = request.env["bader.inbox.channel"].search([])
        channel_health = []
        for ch in channels:
            # Count sends today for this channel
            today_logs = ExecLog.search_count([
                ("channel_id", "=", ch.id),
                ("executed_at", ">=", today),
                ("action", "in", ["send_text", "send_media", "send_template"]),
            ])
            failed_logs = ExecLog.search_count([
                ("channel_id", "=", ch.id),
                ("executed_at", ">=", today),
                ("result", "=", "failed"),
            ])

            health_score = 100
            if today_logs > 0:
                health_score = max(0, 100 - (failed_logs / today_logs * 100))

            channel_health.append({
                "id": ch.id,
                "name": ch.name,
                "phone": ch.phone_number if hasattr(ch, "phone_number") else "",
                "status": ch.status if hasattr(ch, "status") else "connected",
                "sent_today": today_logs,
                "failed_today": failed_logs,
                "health_score": round(health_score, 0),
            })

        # ── Daily Send Volume (last 7 days) ──
        daily_volume = []
        for i in range(6, -1, -1):
            day = today - timedelta(days=i)
            day_start = datetime.combine(day, datetime.min.time())
            day_end = datetime.combine(day, datetime.max.time())

            sent_count = ExecLog.search_count([
                ("executed_at", ">=", day_start),
                ("executed_at", "<=", day_end),
                ("action", "in", ["send_text", "send_media", "send_template"]),
            ])
            failed_count = ExecLog.search_count([
                ("executed_at", ">=", day_start),
                ("executed_at", "<=", day_end),
                ("result", "=", "failed"),
            ])

            daily_volume.append({
                "date": day.strftime("%d/%m"),
                "day_name": day.strftime("%a"),
                "sent": sent_count,
                "failed": failed_count,
            })

        # ── Opt-out trend ──
        optout_count = OptOut.search_count([])
        optout_recent = OptOut.search_count([("opted_out_at", ">=", week_ago)])

        # ── Active Executions ──
        active_execs = Execution.search_count([("status", "in", ["queued", "running", "waiting"])])
        completed_execs = Execution.search_count([("status", "=", "completed")])

        return {
            "kpis": kpis,
            "campaigns": campaign_cards,
            "channel_health": channel_health,
            "daily_volume": daily_volume,
            "optout": {
                "total": optout_count,
                "recent_week": optout_recent,
            },
            "executions": {
                "active": active_execs,
                "completed": completed_execs,
            },
        }

    @http.route("/bader-inbox/campaign/action", type="json", auth="user")
    def campaign_action(self, campaign_id, action, **kw):
        """Execute campaign actions from the dashboard"""
        campaign = request.env["bader.inbox.campaign"].browse(campaign_id)
        if not campaign.exists():
            return {"error": "Campanha não encontrada"}

        if action == "activate":
            campaign.action_activate()
        elif action == "pause":
            campaign.action_pause()
        elif action == "complete":
            campaign.action_complete()
        elif action == "cancel":
            campaign.action_cancel()
        elif action == "duplicate":
            new_campaign = campaign.copy({"name": f"{campaign.name} (Cópia)"})
            return {"success": True, "new_id": new_campaign.id}

        return {"success": True}
