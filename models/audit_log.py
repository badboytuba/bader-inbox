# Copyright 2026 Bader Business
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo import models, fields, api
import logging

_logger = logging.getLogger(__name__)


class BaderInboxAuditLog(models.Model):
    _name = "bader.inbox.audit.log"
    _description = "Bader Inbox Audit Log"
    _order = "create_date desc"
    _rec_name = "action"

    user_id = fields.Many2one("res.users", string="User", default=lambda self: self.env.uid, readonly=True)
    conversation_id = fields.Many2one("bader.inbox.conversation", string="Conversation", ondelete="set null")
    action = fields.Selection([
        ("message_sent", "Message Sent"),
        ("message_deleted", "Message Deleted"),
        ("conversation_assigned", "Conversation Assigned"),
        ("conversation_resolved", "Conversation Resolved"),
        ("tag_added", "Tag Added"),
        ("tag_removed", "Tag Removed"),
        ("opportunity_created", "Opportunity Created"),
        ("quotation_created", "Quotation Created"),
        ("task_created", "Task Created"),
        ("export_csv", "Export CSV"),
        ("ai_toggled", "AI Toggled"),
        ("note_added", "Note Added"),
        ("note_deleted", "Note Deleted"),
    ], string="Action", required=True)
    details = fields.Text(string="Details")
    ip_address = fields.Char(string="IP Address")

    @api.model
    def log_action(self, action, conversation_id=None, details=None):
        """Create an audit log entry."""
        try:
            vals = {
                "action": action,
                "details": details,
            }
            if conversation_id:
                vals["conversation_id"] = conversation_id
            self.sudo().create(vals)
        except Exception as e:
            _logger.warning("Failed to create audit log: %s", e)
