# Copyright 2026 Bader Business
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import logging
from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class BaderInboxScheduledMessage(models.Model):
    """Scheduled messages to be sent at a future time"""

    _name = "bader.inbox.scheduled.message"
    _description = "Scheduled Message"
    _order = "scheduled_datetime asc"

    conversation_id = fields.Many2one(
        "bader.inbox.conversation", required=True, ondelete="cascade", index=True
    )
    content = fields.Text(required=True)
    msg_type = fields.Selection(
        [("text", "Text"), ("image", "Image"), ("document", "Document")],
        default="text",
    )
    media_data = fields.Text(string="Media Data (base64)")
    media_filename = fields.Char()
    scheduled_datetime = fields.Datetime(required=True, index=True)
    status = fields.Selection(
        [("pending", "Pending"), ("sent", "Sent"), ("failed", "Failed"), ("cancelled", "Cancelled")],
        default="pending",
        index=True,
    )
    author_id = fields.Many2one(
        "res.users", string="Scheduled By", default=lambda s: s.env.user, readonly=True
    )
    error_message = fields.Text(readonly=True)

    @api.model
    def _cron_send_scheduled(self):
        """Send all messages whose scheduled_datetime has passed"""
        now = fields.Datetime.now()
        messages = self.search([
            ("status", "=", "pending"),
            ("scheduled_datetime", "<=", now),
        ])
        _logger.info(f"Scheduled messages cron: {len(messages)} messages to send")
        Message = self.env["bader.inbox.message"]
        for sm in messages:
            try:
                Message.send_message(
                    sm.conversation_id.id,
                    sm.content,
                    sm.msg_type,
                )
                sm.status = "sent"
                _logger.info(f"Scheduled message {sm.id} sent to conversation {sm.conversation_id.id}")
            except Exception as e:
                sm.status = "failed"
                sm.error_message = str(e)[:500]
                _logger.error(f"Scheduled message {sm.id} failed: {e}")
