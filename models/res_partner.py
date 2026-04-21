# Copyright 2026 Bader Business
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import logging
import re

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


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

    @api.model_create_multi
    def create(self, vals_list):
        partners = super().create(vals_list)
        partners._auto_link_inbox_conversations()
        return partners

    def write(self, vals):
        res = super().write(vals)
        if "phone" in vals or "mobile" in vals:
            self._auto_link_inbox_conversations()
        return res

    def _auto_link_inbox_conversations(self):
        """Auto-link unlinked inbox conversations when partner phone matches."""
        Conversation = self.env["bader.inbox.conversation"]
        for partner in self:
            phones = [partner.phone, partner.mobile]
            for phone in phones:
                if not phone:
                    continue
                clean = re.sub(r"[^0-9]", "", phone)
                if not clean or len(clean) < 6:
                    continue
                # Find unlinked conversations matching this phone
                for suffix_len in (0, 10, 9, 8):
                    suffix = clean if suffix_len == 0 else clean[-suffix_len:]
                    if not suffix:
                        continue
                    convos = Conversation.search([
                        ("partner_id", "=", False),
                        ("is_group", "=", False),
                        ("phone", "like", suffix),
                    ])
                    for conv in convos:
                        conv.write({"partner_id": partner.id})
                        _logger.info(
                            "Auto-linked partner %s (%s) to conversation %s (phone=%s) on partner create/write",
                            partner.id, partner.name, conv.id, conv.phone,
                        )
                    if convos:
                        break  # Found matches with this phone, skip shorter suffixes


class CrmLead(models.Model):
    _inherit = "crm.lead"

    bader_inbox_conversation_ids = fields.One2many(
        "bader.inbox.conversation", "lead_id", string="WhatsApp Conversations"
    )
