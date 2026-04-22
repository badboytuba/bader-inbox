# Copyright 2026 Bader Business
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import logging

from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)


class BaderInboxMessage(models.Model):
    """Individual chat message"""
    
    _name = "bader.inbox.message"
    _description = "Bader Inbox Message"
    _order = "create_date asc, id asc"

    conversation_id = fields.Many2one(
        "bader.inbox.conversation", string="Conversation",
        required=True, ondelete="cascade", index=True
    )
    
    direction = fields.Selection([
        ("in", "Incoming"),
        ("out", "Outgoing"),
    ], required=True, string="Direction")
    
    message_type = fields.Selection([
        ("text", "Text"),
        ("image", "Image"),
        ("audio", "Audio"),
        ("video", "Video"),
        ("document", "Document"),
        ("sticker", "Sticker"),
        ("location", "Location"),
        ("contact", "Contact"),
        ("reaction", "Reaction"),
    ], default="text", string="Type")
    
    content = fields.Text(string="Content")

    # Group message sender identification
    sender_name = fields.Char(string="Sender Name",
        help="Name of the participant who sent this message in a group")
    sender_phone = fields.Char(string="Sender Phone",
        help="Phone number of the participant who sent this message in a group")
    
    # Media
    media_data = fields.Binary(string="Media Data", attachment=True)
    media_filename = fields.Char(string="Filename")
    media_mimetype = fields.Char(string="MIME Type")
    media_url = fields.Char(string="Media URL")
    # Raw webhook data for deferred media download via API
    whatsapp_key_json = fields.Text(string="WA Key JSON")
    whatsapp_content_json = fields.Text(string="WA Content JSON")
    
    # Location
    latitude = fields.Float(string="Latitude")
    longitude = fields.Float(string="Longitude")
    location_name = fields.Char(string="Location Name")
    
    # Status
    status = fields.Selection([
        ("pending", "Pending"),
        ("sent", "Sent"),
        ("delivered", "Delivered"),
        ("read", "Read"),
        ("failed", "Failed"),
    ], default="pending", string="Status")
    
    status_timestamp = fields.Datetime(string="Status Updated")
    
    # WhatsApp
    whatsapp_message_id = fields.Char(string="WA Message ID", index=True)
    
    # Author (for outgoing)
    author_id = fields.Many2one("res.users", string="Sent By")

    # Link Preview (Phase 3)
    link_preview = fields.Text(string="Link Preview JSON")

    # Multi-language (Phase 3)
    detected_language = fields.Char(string="Detected Language", size=10)
    translated_content = fields.Text(string="Translated Content")

    # Quoted / Reply
    quoted_message_id = fields.Char(string="Quoted WA Message ID",
        help="WhatsApp ID of the message being replied to")
    quoted_text = fields.Text(string="Quoted Text",
        help="Text content of the quoted message")
    quoted_participant = fields.Char(string="Quoted Participant",
        help="Phone/JID of who sent the quoted message")

    # Edit tracking
    is_edited = fields.Boolean(string="Edited", default=False)

    @api.model_create_multi
    def create(self, vals_list):
        messages = super().create(vals_list)
        
        for message in messages:
            # Update conversation metadata atomically (prevents serialization failures
            # when multiple webhook messages arrive simultaneously for the same conversation)
            conv = message.conversation_id
            last_msg = (message.content[:100] if message.content else message.message_type) or ''
            
            if message.direction == "in":
                # Incoming: update last_message + last_message_date + increment unread_count
                self.env.cr.execute("""
                    UPDATE bader_inbox_conversation
                    SET last_message = %s,
                        last_message_date = NOW() AT TIME ZONE 'UTC',
                        unread_count = unread_count + 1,
                        write_date = NOW() AT TIME ZONE 'UTC'
                    WHERE id = %s
                """, [last_msg, conv.id])
            else:
                # Outgoing: update last_message + last_message_date only
                self.env.cr.execute("""
                    UPDATE bader_inbox_conversation
                    SET last_message = %s,
                        last_message_date = NOW() AT TIME ZONE 'UTC',
                        write_date = NOW() AT TIME ZONE 'UTC'
                    WHERE id = %s
                """, [last_msg, conv.id])
            
            # Invalidate ORM cache so subsequent reads get fresh data
            conv.invalidate_recordset(['last_message', 'last_message_date', 'unread_count'])
        
        return messages

    @api.model
    def send_message(self, conversation_id, content, msg_type="text", media_data=None, media_filename=None):
        """Send message via Evolution API"""
        conversation = self.env["bader.inbox.conversation"].browse(conversation_id)
        if not conversation.exists():
            return False
        
        channel = conversation.channel_id
        if channel.state != "connected":
            return False

        # Credit automated sends (cron, chatbot, AI agent, campaign) to the
        # NancyAI user instead of the generic system/OdooBot so the chat UI
        # shows "NancyAI" as the author. Manual sends keep the real operator
        # identity.
        author = self.env.user
        if author.id == self.env.ref("base.user_root").id or (hasattr(author, "_is_system") and author._is_system()):
            nancy = self.env.ref("bader_inbox.user_nancy_ai", raise_if_not_found=False)
            if nancy and nancy.active:
                author = nancy

        # Create message record
        message_vals = {
            "conversation_id": conversation_id,
            "direction": "out",
            "message_type": msg_type,
            "content": content,
            "status": "pending",
            "author_id": author.id,
        }
        
        if media_data:
            message_vals["media_data"] = media_data
            message_vals["media_filename"] = media_filename
        
        message = self.create(message_vals)
        
        # Send via API
        try:
            api = self.env["bader.inbox.evolution_api"]
            phone = conversation.phone
            
            if msg_type == "text":
                result = api.send_text(channel.evolution_instance_name, phone, content)
            else:
                result = api.send_media(
                    channel.evolution_instance_name, 
                    phone, 
                    msg_type,
                    media_data=media_data,  # Pass base64 data
                    media_url=None,         # No URL for direct upload
                    filename=media_filename, 
                    caption=content
                )
            
            if result.get("success"):
                message.status = "sent"
                message.whatsapp_message_id = result.get("message_id")
            else:
                message.status = "failed"
                
        except Exception as e:
            _logger.error(f"Send message error: {e}")
            message.status = "failed"
        
        # F25: Audit log
        self.env["bader.inbox.audit.log"].log_action(
            "message_sent",
            conversation_id=conversation_id,
            details=f"Type: {msg_type}, Preview: {(content or '')[:50]}"
        )

        return message

    @api.model
    def send_reaction(self, message_id, emoji):
        """Send emoji reaction to a WhatsApp message via Evolution API.

        F16: Returns {"success": bool}
        """
        msg = self.browse(message_id)
        if not msg.exists():
            return {"success": False, "error": "Message not found"}

        conv = msg.conversation_id
        channel = conv.channel_id
        if not channel or not channel.evolution_instance_name:
            return {"success": False, "error": "No channel"}

        # WhatsApp message ID needed for reaction (stored in wa_message_id field if available)
        wa_msg_id = getattr(msg, "wa_message_id", None) or msg.id
        api = self.env["bader.inbox.evolution_api"]
        result = api.send_reaction(
            channel.evolution_instance_name,
            conv.phone,
            str(wa_msg_id),
            emoji,
        )
        return result
