# Copyright 2026 Bader Business
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import logging
from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)


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
    mentioned_user_ids = fields.Many2many(
        "res.users", "bader_inbox_note_mention_rel",
        "note_id", "user_id", string="Mentioned Users"
    )

    @api.model
    def create_mention_note(self, conversation_id, content, mentioned_user_ids=None):
        """Create an internal note with @mentions.

        - Creates the note
        - Assigns the first mentioned user to the conversation
        - Sends Odoo notification to all mentioned users
        - Returns note data for frontend display
        """
        conversation = self.env["bader.inbox.conversation"].browse(conversation_id)
        if not conversation.exists():
            return {"error": "Conversation not found"}

        vals = {
            "conversation_id": conversation_id,
            "content": content,
        }
        if mentioned_user_ids:
            vals["mentioned_user_ids"] = [(6, 0, mentioned_user_ids)]

        note = self.create(vals)

        # Assign the first mentioned user to the conversation
        if mentioned_user_ids:
            first_user = self.env["res.users"].browse(mentioned_user_ids[0])
            if first_user.exists():
                conversation.assigned_user_id = first_user

            # Notify all mentioned users via Odoo mail
            for uid in mentioned_user_ids:
                user = self.env["res.users"].browse(uid)
                if not user.exists() or user.id == self.env.user.id:
                    continue

                # Build notification message
                conv_name = conversation.contact_name or conversation.phone or "Unknown"
                author_name = self.env.user.name

                # Post to conversation chatter (mail.thread)
                conversation.message_post(
                    body=_(
                        '<strong>%(author)s</strong> te mencionó en la conversación con '
                        '<strong>%(contact)s</strong>:<br/>'
                        '<em>"%(content)s"</em>',
                        author=author_name,
                        contact=conv_name,
                        content=content[:200],
                    ),
                    message_type="notification",
                    subtype_xmlid="mail.mt_note",
                    partner_ids=[user.partner_id.id],
                )

                _logger.info(
                    "Mention notification sent: %s -> %s (conv %s)",
                    author_name, user.name, conversation_id
                )

        return {
            "id": note.id,
            "content": note.content,
            "author_id": [note.author_id.id, note.author_id.name],
            "create_date": fields.Datetime.to_string(note.create_date),
            "mentioned_user_ids": [
                [u.id, u.name] for u in note.mentioned_user_ids
            ],
        }
