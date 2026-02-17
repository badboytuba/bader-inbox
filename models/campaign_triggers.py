# Copyright 2026 Bader Business
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

"""
Campaign Trigger System
========================
Hooks into Odoo model events to automatically fire campaigns.

Architecture:
- A central dispatcher checks all active triggered campaigns
  whenever a relevant Odoo event occurs.
- Each trigger subtype has a matching method that evaluates
  whether the contact matches the campaign's audience.
- Cron-based triggers (abandoned cart, overdue invoice, date events)
  run periodically and scan for matching records.
"""

import json
import logging
from datetime import timedelta
from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)


class BaderCampaignTriggerDispatcher(models.AbstractModel):
    """Central dispatcher for campaign triggers.
    Other models call dispatch_trigger() when events occur."""

    _name = "bader.inbox.campaign.trigger.dispatcher"
    _description = "Campaign Trigger Dispatcher"

    @api.model
    def dispatch_trigger(self, trigger_subtype, partner, context_data=None):
        """Check all active campaigns for this trigger type and enqueue matching contacts.

        Args:
            trigger_subtype: e.g. 'lead_created', 'order_confirmed'
            partner: res.partner record
            context_data: dict with extra data (product_id, order_id, etc.)
        """
        if not partner:
            return

        phone = partner.mobile or partner.phone
        if not phone:
            return

        # Find active campaigns with matching trigger
        Campaign = self.env["bader.inbox.campaign"]
        campaigns = Campaign.search([
            ("status", "=", "active"),
            ("campaign_type", "=", "triggered"),
        ])

        for campaign in campaigns:
            if not campaign.flow_id:
                continue

            # Find trigger nodes matching this subtype
            trigger_nodes = campaign.flow_id.node_ids.filtered(
                lambda n: n.node_type == "trigger" and n.node_subtype == trigger_subtype
            )
            if not trigger_nodes:
                continue

            # Check trigger config (e.g., specific stage, product, etc.)
            for trigger_node in trigger_nodes:
                config = trigger_node.get_config()
                if not self._matches_trigger_config(trigger_subtype, config, context_data):
                    continue

                # Check if partner is in audience
                if not self._partner_in_audience(campaign, partner):
                    continue

                # Check if already running for this partner
                existing = self.env["bader.inbox.campaign.execution"].search([
                    ("campaign_id", "=", campaign.id),
                    ("partner_id", "=", partner.id),
                    ("status", "in", ["queued", "running", "waiting"]),
                ], limit=1)
                if existing:
                    continue

                # Enqueue execution
                self._create_execution(campaign, partner, trigger_node, context_data)

    def _matches_trigger_config(self, subtype, config, context_data):
        """Check if event data matches trigger node configuration"""
        context_data = context_data or {}

        if subtype == "lead_stage_changed":
            target_stage = config.get("stage_id")
            if target_stage and context_data.get("stage_id") != target_stage:
                return False

        elif subtype == "cart_abandoned":
            min_minutes = config.get("min_minutes", 60)
            if context_data.get("abandoned_minutes", 0) < min_minutes:
                return False

        elif subtype == "invoice_overdue":
            min_days = config.get("min_days_overdue", 1)
            if context_data.get("days_overdue", 0) < min_days:
                return False

        elif subtype == "tag_added":
            target_tag = config.get("tag_id")
            if target_tag and context_data.get("tag_id") != target_tag:
                return False

        elif subtype == "product_viewed":
            target_product = config.get("product_id")
            if target_product and context_data.get("product_id") != target_product:
                return False

        return True

    def _partner_in_audience(self, campaign, partner):
        """Check if partner belongs to any of the campaign's audiences"""
        if not campaign.audience_ids:
            return True  # No audience filter = all contacts

        for audience in campaign.audience_ids:
            contacts = audience._get_contacts()
            if partner.id in contacts.ids:
                return True
        return False

    def _create_execution(self, campaign, partner, trigger_node, context_data):
        """Create a new campaign execution for this partner"""
        Execution = self.env["bader.inbox.campaign.execution"]

        variables = context_data or {}
        # Add partner info
        variables.update({
            "partner_name": partner.name,
            "partner_email": partner.email or "",
            "partner_phone": partner.mobile or partner.phone or "",
        })

        execution = Execution.create({
            "campaign_id": campaign.id,
            "partner_id": partner.id,
            "current_node_id": trigger_node.id,
            "status": "queued",
            "next_action_at": fields.Datetime.now(),
            "variables": json.dumps(variables, default=str),
        })

        _logger.info(
            f"Campaign trigger: enqueued {partner.name} "
            f"for campaign '{campaign.name}' "
            f"(trigger: {trigger_node.node_subtype})"
        )
        return execution


# ═══════════════════════════════════════════════════════════════════════════
#  ODOO MODEL OVERRIDES — Event Listeners
# ═══════════════════════════════════════════════════════════════════════════


class CrmLeadCampaignTrigger(models.Model):
    """CRM Lead/Opportunity triggers"""
    _inherit = "crm.lead"

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        dispatcher = self.env["bader.inbox.campaign.trigger.dispatcher"]
        for lead in records:
            partner = lead.partner_id
            if partner:
                trigger_type = "lead_created" if lead.type == "lead" else "lead_created"
                dispatcher.dispatch_trigger(trigger_type, partner, {
                    "lead_id": lead.id,
                    "lead_name": lead.name,
                    "lead_type": lead.type,
                    "stage_id": lead.stage_id.id if lead.stage_id else False,
                })
        return records

    def write(self, vals):
        old_stages = {rec.id: rec.stage_id.id for rec in self}
        old_active = {rec.id: rec.active for rec in self}
        result = super().write(vals)

        dispatcher = self.env["bader.inbox.campaign.trigger.dispatcher"]

        for lead in self:
            partner = lead.partner_id
            if not partner:
                continue

            # Stage changed
            if "stage_id" in vals:
                old_stage = old_stages.get(lead.id)
                if old_stage != lead.stage_id.id:
                    dispatcher.dispatch_trigger("lead_stage_changed", partner, {
                        "lead_id": lead.id,
                        "lead_name": lead.name,
                        "stage_id": lead.stage_id.id,
                        "old_stage_id": old_stage,
                    })

            # Won/Lost
            if "probability" in vals:
                if lead.probability == 100:
                    dispatcher.dispatch_trigger("opportunity_won", partner, {
                        "lead_id": lead.id,
                        "lead_name": lead.name,
                        "expected_revenue": lead.expected_revenue,
                    })
                elif lead.probability == 0 and not lead.active:
                    dispatcher.dispatch_trigger("opportunity_lost", partner, {
                        "lead_id": lead.id,
                        "lead_name": lead.name,
                    })

        return result


class SaleOrderCampaignTrigger(models.Model):
    """eCommerce / Sale Order triggers"""
    _inherit = "sale.order"

    def action_confirm(self):
        result = super().action_confirm()
        dispatcher = self.env["bader.inbox.campaign.trigger.dispatcher"]
        for order in self:
            partner = order.partner_id
            if partner:
                products = []
                for line in order.order_line:
                    if line.product_id:
                        products.append({
                            "product_id": line.product_id.id,
                            "product_name": line.product_id.name,
                            "qty": line.product_uom_qty,
                            "price": line.price_total,
                        })
                dispatcher.dispatch_trigger("order_confirmed", partner, {
                    "order_id": order.id,
                    "order_name": order.name,
                    "amount_total": order.amount_total,
                    "products": products,
                })
        return result


class StockPickingCampaignTrigger(models.Model):
    """Inventory / Shipping triggers"""
    _inherit = "stock.picking"

    def button_validate(self):
        result = super().button_validate()
        dispatcher = self.env["bader.inbox.campaign.trigger.dispatcher"]
        for picking in self:
            if picking.picking_type_code != "outgoing":
                continue
            partner = picking.partner_id
            if partner:
                dispatcher.dispatch_trigger("order_shipped", partner, {
                    "picking_id": picking.id,
                    "picking_name": picking.name,
                    "origin": picking.origin or "",
                })
        return result


class ResPartnerCampaignTrigger(models.Model):
    """Contact tag triggers"""
    _inherit = "res.partner"

    def write(self, vals):
        old_tags = {rec.id: set(rec.category_id.ids) for rec in self}
        result = super().write(vals)

        if "category_id" in vals:
            dispatcher = self.env["bader.inbox.campaign.trigger.dispatcher"]
            for partner in self:
                new_tags = set(partner.category_id.ids)
                added_tags = new_tags - old_tags.get(partner.id, set())
                for tag_id in added_tags:
                    tag = self.env["res.partner.category"].browse(tag_id)
                    dispatcher.dispatch_trigger("tag_added", partner, {
                        "tag_id": tag_id,
                        "tag_name": tag.name if tag.exists() else "",
                    })

        return result


# ═══════════════════════════════════════════════════════════════════════════
#  CRON-BASED TRIGGERS
# ═══════════════════════════════════════════════════════════════════════════


class BaderCampaignCronTriggers(models.Model):
    """Cron-based triggers that scan periodically for matching records"""

    _inherit = "bader.inbox.campaign"

    @api.model
    def _cron_trigger_abandoned_carts(self):
        """Find abandoned carts (draft sale orders) and trigger campaigns"""
        dispatcher = self.env["bader.inbox.campaign.trigger.dispatcher"]

        # Find campaigns with cart_abandoned trigger
        campaigns = self.search([
            ("status", "=", "active"),
            ("campaign_type", "=", "triggered"),
        ])

        for campaign in campaigns:
            if not campaign.flow_id:
                continue
            triggers = campaign.flow_id.node_ids.filtered(
                lambda n: n.node_type == "trigger" and n.node_subtype == "cart_abandoned"
            )
            if not triggers:
                continue

            for trigger in triggers:
                config = trigger.get_config()
                min_minutes = config.get("min_minutes", 60)
                max_minutes = config.get("max_minutes", 1440)  # 24h

                threshold_min = fields.Datetime.now() - timedelta(minutes=max_minutes)
                threshold_max = fields.Datetime.now() - timedelta(minutes=min_minutes)

                # Find draft orders (carts) in the time window
                carts = self.env["sale.order"].search([
                    ("state", "=", "draft"),
                    ("date_order", ">=", threshold_min),
                    ("date_order", "<=", threshold_max),
                    ("partner_id", "!=", False),
                ])

                for cart in carts:
                    abandoned_min = (fields.Datetime.now() - cart.date_order).total_seconds() / 60
                    products = []
                    for line in cart.order_line:
                        if line.product_id:
                            products.append({
                                "product_id": line.product_id.id,
                                "product_name": line.product_id.name,
                                "price": line.price_total,
                            })

                    dispatcher.dispatch_trigger("cart_abandoned", cart.partner_id, {
                        "order_id": cart.id,
                        "order_name": cart.name,
                        "amount_total": cart.amount_total,
                        "abandoned_minutes": abandoned_min,
                        "products": products,
                    })

        _logger.info("Cron: abandoned cart trigger scan complete")

    @api.model
    def _cron_trigger_overdue_invoices(self):
        """Find overdue invoices and trigger campaigns"""
        dispatcher = self.env["bader.inbox.campaign.trigger.dispatcher"]

        campaigns = self.search([
            ("status", "=", "active"),
            ("campaign_type", "=", "triggered"),
        ])

        for campaign in campaigns:
            if not campaign.flow_id:
                continue
            triggers = campaign.flow_id.node_ids.filtered(
                lambda n: n.node_type == "trigger" and n.node_subtype == "invoice_overdue"
            )
            if not triggers:
                continue

            for trigger in triggers:
                config = trigger.get_config()
                min_days = config.get("min_days_overdue", 1)
                max_days = config.get("max_days_overdue", 90)

                threshold_min = fields.Date.today() - timedelta(days=max_days)
                threshold_max = fields.Date.today() - timedelta(days=min_days)

                invoices = self.env["account.move"].search([
                    ("move_type", "=", "out_invoice"),
                    ("payment_state", "in", ["not_paid", "partial"]),
                    ("invoice_date_due", ">=", threshold_min),
                    ("invoice_date_due", "<=", threshold_max),
                    ("partner_id", "!=", False),
                ])

                for inv in invoices:
                    days_overdue = (fields.Date.today() - inv.invoice_date_due).days
                    dispatcher.dispatch_trigger("invoice_overdue", inv.partner_id, {
                        "invoice_id": inv.id,
                        "invoice_name": inv.name,
                        "amount_total": inv.amount_total,
                        "amount_residual": inv.amount_residual,
                        "days_overdue": days_overdue,
                    })

        _logger.info("Cron: overdue invoice trigger scan complete")

    @api.model
    def _cron_trigger_date_events(self):
        """Trigger campaigns based on partner date fields (birthday, anniversary, etc.)"""
        dispatcher = self.env["bader.inbox.campaign.trigger.dispatcher"]

        campaigns = self.search([
            ("status", "=", "active"),
            ("campaign_type", "=", "triggered"),
        ])

        today = fields.Date.today()

        for campaign in campaigns:
            if not campaign.flow_id:
                continue
            triggers = campaign.flow_id.node_ids.filtered(
                lambda n: n.node_type == "trigger" and n.node_subtype == "date_trigger"
            )
            if not triggers:
                continue

            for trigger in triggers:
                config = trigger.get_config()
                date_field = config.get("date_field", "")
                days_before = config.get("days_before", 0)

                if not date_field:
                    continue

                target_date = today + timedelta(days=days_before)

                # Search partners where date field matches
                try:
                    partners = self.env["res.partner"].search([
                        (date_field, "!=", False),
                    ])
                    for partner in partners:
                        field_value = getattr(partner, date_field, None)
                        if field_value and hasattr(field_value, "month") and hasattr(field_value, "day"):
                            if field_value.month == target_date.month and field_value.day == target_date.day:
                                dispatcher.dispatch_trigger("date_trigger", partner, {
                                    "date_field": date_field,
                                    "date_value": str(field_value),
                                    "event": "birthday" if date_field == "birthday" else "anniversary",
                                })
                except Exception as e:
                    _logger.warning(f"Date trigger error for field '{date_field}': {e}")

        _logger.info("Cron: date event trigger scan complete")

    @api.model
    def _cron_trigger_manual_segments(self):
        """Process manual/scheduled segment campaigns"""
        now = fields.Datetime.now()

        campaigns = self.search([
            ("status", "=", "active"),
            ("campaign_type", "=", "one_shot"),
            ("start_date", "<=", now),
        ])

        for campaign in campaigns:
            if not campaign.flow_id or not campaign.audience_ids:
                continue

            # Check if already processed
            existing_count = self.env["bader.inbox.campaign.execution"].search_count([
                ("campaign_id", "=", campaign.id),
            ])
            if existing_count > 0:
                continue  # Already launched

            # Get all contacts from audiences
            all_contacts = self.env["res.partner"]
            for audience in campaign.audience_ids:
                all_contacts |= audience._get_contacts()

            trigger_node = campaign.flow_id.node_ids.filtered(
                lambda n: n.node_type == "trigger"
            )
            if not trigger_node:
                continue
            trigger_node = trigger_node[0]

            created = 0
            for partner in all_contacts:
                phone = partner.mobile or partner.phone
                if not phone:
                    continue

                self.env["bader.inbox.campaign.execution"].create({
                    "campaign_id": campaign.id,
                    "partner_id": partner.id,
                    "current_node_id": trigger_node.id,
                    "status": "queued",
                    "next_action_at": now,
                    "variables": json.dumps({
                        "partner_name": partner.name,
                        "partner_email": partner.email or "",
                        "partner_phone": phone,
                    }),
                })
                created += 1

            _logger.info(
                f"Manual segment: enqueued {created} contacts "
                f"for campaign '{campaign.name}'"
            )
