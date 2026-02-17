# Copyright 2026 Bader Business
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo import api, fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    bader_inbox_conversation_ids = fields.One2many(
        "bader.inbox.conversation", "partner_id", string="WhatsApp Conversations"
    )
    bader_inbox_conversation_count = fields.Integer(
        compute="_compute_bader_inbox_count", string="Conversations"
    )

    @api.depends("bader_inbox_conversation_ids")
    def _compute_bader_inbox_count(self):
        for partner in self:
            partner.bader_inbox_conversation_count = len(partner.bader_inbox_conversation_ids)

    # AI Agent memory - persists between conversation sessions
    ai_memory = fields.Text(string="AI Memory",
        help="JSON data with customer preferences, interests, and notes collected by the AI agent")


class CrmLead(models.Model):
    _inherit = "crm.lead"

    bader_inbox_conversation_ids = fields.One2many(
        "bader.inbox.conversation", "lead_id", string="WhatsApp Conversations"
    )
