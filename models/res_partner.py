# Copyright 2026 Bader Business
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo import fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    bader_inbox_conversation_ids = fields.One2many(
        "bader.inbox.conversation", "partner_id", string="WhatsApp Conversations"
    )
    bader_inbox_conversation_count = fields.Integer(
        compute="_compute_bader_inbox_count", string="Conversations"
    )

    def _compute_bader_inbox_count(self):
        for partner in self:
            partner.bader_inbox_conversation_count = len(partner.bader_inbox_conversation_ids)


class CrmLead(models.Model):
    _inherit = "crm.lead"

    bader_inbox_conversation_ids = fields.One2many(
        "bader.inbox.conversation", "lead_id", string="WhatsApp Conversations"
    )
