# Copyright 2026 Bader Business
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import logging

from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)


class BaderInboxMessage(models.Model):
    """Individual chat message"""
    
    _name = "bader.inbox.message"
    _description = "Bader Inbox Message"
    _order = "create_date asc"

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

    @api.model_create_multi
    def create(self, vals_list):
        messages = super().create(vals_list)
        
        for message in messages:
            # Update conversation
            conv = message.conversation_id
            conv.write({
                "last_message": message.content[:100] if message.content else message.message_type,
                "last_message_date": fields.Datetime.now(),
            })
            
            # Increment unread for incoming (atomic to avoid race conditions)
            if message.direction == "in":
                self.env.cr.execute(
                    "UPDATE bader_inbox_conversation SET unread_count = unread_count + 1 WHERE id = %s",
                    [conv.id]
                )
                conv.invalidate_recordset(['unread_count'])
        
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
        
        # Create message record
        message_vals = {
            "conversation_id": conversation_id,
            "direction": "out",
            "message_type": msg_type,
            "content": content,
            "status": "pending",
            "author_id": self.env.user.id,
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
        
        return message
