# Copyright 2026 Bader Business
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo import fields, models


class BaderInboxNote(models.Model):
    """Internal notes for conversations (team-only, not sent to customer)"""

    _name = "bader.inbox.note"
    _description = "Internal Note"
    _order = "create_date desc"

    conversation_id = fields.Many2one(
        "bader.inbox.conversation", required=True, ondelete="cascade", index=True
    )
    content = fields.Text(required=True)
    author_id = fields.Many2one(
        "res.users", string="Author", default=lambda s: s.env.user, readonly=True
    )
