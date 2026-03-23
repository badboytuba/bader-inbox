# Copyright 2026 Bader Business
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import logging
from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class BaderInboxGroupMember(models.Model):
    """Member of a WhatsApp group conversation"""

    _name = "bader.inbox.group.member"
    _description = "Group Member"
    _order = "is_superadmin desc, is_admin desc, name"
    _rec_name = "display_name"

    conversation_id = fields.Many2one(
        "bader.inbox.conversation", string="Group",
        required=True, ondelete="cascade", index=True,
    )
    phone = fields.Char(string="Phone", required=True, index=True)
    name = fields.Char(string="Name")
    display_name = fields.Char(compute="_compute_display_name", store=True, string="Display Name")
    is_admin = fields.Boolean(string="Admin", default=False)
    is_superadmin = fields.Boolean(string="Super Admin", default=False)

    # Link to existing partner/conversation
    partner_id = fields.Many2one("res.partner", string="Contact", compute="_compute_partner", store=True)
    individual_conversation_id = fields.Many2one(
        "bader.inbox.conversation", string="Individual Chat",
        compute="_compute_individual_conversation",
    )

    _sql_constraints = [
        ('unique_group_phone',
         'UNIQUE(conversation_id, phone)',
         'A member with this phone already exists in this group.'),
    ]

    @api.depends("name", "phone")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = rec.name or rec.phone or "Unknown"

    @api.depends("phone")
    def _compute_partner(self):
        Partner = self.env["res.partner"]
        for rec in self:
            if not rec.phone:
                rec.partner_id = False
                continue
            partner = Partner.search([
                "|",
                ("phone", "like", rec.phone[-9:]),
                ("mobile", "like", rec.phone[-9:]),
            ], limit=1)
            rec.partner_id = partner.id if partner else False

    def _compute_individual_conversation(self):
        Conv = self.env["bader.inbox.conversation"].sudo()
        for rec in self:
            if not rec.phone:
                rec.individual_conversation_id = False
                continue
            conv = Conv.search([
                ("phone", "=", rec.phone),
                ("is_group", "!=", True),
                ("channel_id", "=", rec.conversation_id.channel_id.id),
            ], limit=1)
            rec.individual_conversation_id = conv.id if conv else False
