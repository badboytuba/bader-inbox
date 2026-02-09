# Copyright 2026 Bader Business
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import logging
import json
from odoo import http, fields
from odoo.http import request

_logger = logging.getLogger(__name__)


class BaderInboxWebhook(http.Controller):
    """Webhook controller for Evolution API events"""

    @http.route(
        "/bader-inbox/webhook/<int:channel_id>",
        type="json", auth="none", methods=["POST"], csrf=False,
    )
    def webhook_handler(self, channel_id, **kwargs):
        """Handle incoming webhook from Evolution API"""
        try:
            # Odoo 16 compatibility - get JSON data from request
            data = request.get_json_data() if hasattr(request, 'get_json_data') else (request.jsonrequest if hasattr(request, 'jsonrequest') else kwargs)
            _logger.info(f"Webhook for channel {channel_id}: {json.dumps(data)[:2000]}")
            
            # DEBUG: Save full payload to file for analysis
            try:
                with open(f"/tmp/webhook_debug_{channel_id}.json", "w") as f:
                    json.dump(data, f, indent=2, default=str)
            except:
                pass
            
            channel = request.env["bader.inbox.channel"].sudo().browse(channel_id)
            if not channel.exists():
                return {"status": "error", "message": "Channel not found"}
            
            event = data.get("event", "")
            
            if event == "messages.upsert" or "message" in data:
                return self._handle_message(channel, data)
            elif event == "messages.update":
                return self._handle_message_update(channel, data)
            elif event == "connection.update":
                return self._handle_connection_update(channel, data)
            elif event == "qrcode.updated":
                return self._handle_qrcode_update(channel, data)
            
            return {"status": "ignored"}
            
        except Exception as e:
            _logger.error(f"Webhook error: {e}", exc_info=True)
            return {"status": "error", "message": str(e)}

    def _handle_message(self, channel, data):
        """Handle incoming/outgoing message"""
        try:
            # API sends message data directly in payload, not in 'data' array
            # Format: {"event": "messages.upsert", "message": {...}, "key": {...}, "pushName": "..."}
            # Or nested: {"data": [{"key": {...}, "message": {...}, "pushName": "..."}]}
            
            messages = []
            if "message" in data and "key" in data:
                # Message at root level (production API format)
                messages = [data]
            elif "data" in data:
                # Messages in data array (legacy format)
                messages = data.get("data", [])
                if not isinstance(messages, list):
                    messages = [messages]
            else:
                # Try using whole data object
                messages = [data]
            
            for msg_data in messages:
                if not msg_data:
                    continue
                
                key = msg_data.get("key", {})
                message_content = msg_data.get("message", {})
                push_name = msg_data.get("pushName", "")
                
                from_me = key.get("fromMe", False)
                direction = "out" if from_me else "in"
                
                remote_jid = key.get("remoteJid", "")
                phone = remote_jid.replace("@s.whatsapp.net", "").replace("@g.us", "")
                
                if not phone or "@" in phone:
                    continue
                
                Conversation = request.env["bader.inbox.conversation"].sudo()
                conversation = Conversation.get_or_create(
                    channel_id=channel.id,
                    phone=phone,
                    whatsapp_id=remote_jid,
                    contact_name=push_name
                )
                
                msg_type, content, media_info = self._parse_message_content(message_content)
                
                Message = request.env["bader.inbox.message"].sudo()
                existing = Message.search([("whatsapp_message_id", "=", key.get("id"))], limit=1)
                if existing:
                    continue
                
                message_vals = {
                    "conversation_id": conversation.id,
                    "direction": direction,
                    "message_type": msg_type,
                    "content": content,
                    "whatsapp_message_id": key.get("id"),
                    "status": "read" if direction == "in" else "sent",
                }
                if media_info:
                    message_vals.update(media_info)
                
                new_message = Message.create(message_vals)
                
                if direction == "in":
                    self._send_bus_notification(conversation, new_message, push_name, phone)
                    self._trigger_chatbot(conversation, new_message)
            
            return {"status": "ok"}
            
        except Exception as e:
            _logger.error(f"Error handling message: {e}", exc_info=True)
            return {"status": "error", "message": str(e)}

    def _send_bus_notification(self, conversation, message, contact_name, phone):
        """Send bus notification for real-time updates"""
        try:
            payload = {
                "conversation_id": conversation.id,
                "contact_name": contact_name or conversation.computed_name,
                "phone": phone,
                "message": {
                    "id": message.id,
                    "direction": message.direction,
                    "message_type": message.message_type,
                    "content": message.content,
                    "status": message.status,
                    "create_date": str(message.create_date),
                }
            }
            request.env["bus.bus"]._sendone("bader_inbox", "bader_inbox_new_message", payload)
        except Exception as e:
            _logger.warning(f"Bus notification error: {e}")

    def _trigger_chatbot(self, conversation, message):
        """Check and execute chatbot rules"""
        try:
            Chatbot = request.env["bader.inbox.chatbot"].sudo()
            rules = Chatbot.search([("active", "=", True)], order="priority")
            for rule in rules:
                if rule.check_trigger(conversation, message):
                    rule.execute_action(conversation, message)
                    break
        except Exception as e:
            _logger.warning(f"Chatbot error: {e}")

    def _parse_message_content(self, message):
        """Parse message content from WhatsApp format"""
        msg_type = "text"
        content = ""
        media_info = {}
        
        if not message:
            return msg_type, content, media_info
        
        if "conversation" in message:
            content = message["conversation"]
        elif "extendedTextMessage" in message:
            content = message["extendedTextMessage"].get("text", "")
        elif "imageMessage" in message:
            msg_type = "image"
            img = message["imageMessage"]
            content = img.get("caption", "")
            media_info = {"media_mimetype": img.get("mimetype"), "media_url": img.get("url")}
        elif "audioMessage" in message:
            msg_type = "audio"
            audio = message["audioMessage"]
            media_info = {"media_mimetype": audio.get("mimetype"), "media_url": audio.get("url")}
        elif "videoMessage" in message:
            msg_type = "video"
            video = message["videoMessage"]
            content = video.get("caption", "")
            media_info = {"media_mimetype": video.get("mimetype"), "media_url": video.get("url")}
        elif "documentMessage" in message:
            msg_type = "document"
            doc = message["documentMessage"]
            content = doc.get("caption", "")
            media_info = {"media_mimetype": doc.get("mimetype"), "media_filename": doc.get("fileName"), "media_url": doc.get("url")}
        elif "locationMessage" in message:
            msg_type = "location"
            loc = message["locationMessage"]
            media_info = {"latitude": loc.get("degreesLatitude"), "longitude": loc.get("degreesLongitude"), "location_name": loc.get("name")}
            content = loc.get("address", "")
        
        return msg_type, content, media_info

    def _handle_message_update(self, channel, data):
        """Handle message status update"""
        try:
            updates = data.get("data", [])
            if not isinstance(updates, list):
                updates = [updates]
            
            Message = request.env["bader.inbox.message"].sudo()
            status_map = {0: "pending", 1: "sent", 2: "sent", 3: "delivered", 4: "read", 5: "read"}
            
            for update in updates:
                msg_id = update.get("key", {}).get("id")
                if not msg_id:
                    continue
                message = Message.search([("whatsapp_message_id", "=", msg_id)], limit=1)
                if message:
                    new_status = update.get("update", {}).get("status", 0)
                    message.write({"status": status_map.get(new_status, "sent")})
            
            return {"status": "ok"}
        except Exception as e:
            _logger.error(f"Message update error: {e}")
            return {"status": "error", "message": str(e)}

    def _handle_connection_update(self, channel, data):
        """Handle connection status update"""
        try:
            connection = data.get("data", {})
            state = connection.get("state", "") or connection.get("status", "")
            # Handle both Evolution API formats: 'open'/'close' and 'connected'/'disconnected'
            state_map = {
                "open": "connected", 
                "connected": "connected",
                "connecting": "connecting", 
                "close": "disconnected",
                "disconnected": "disconnected",
                "qr_ready": "qr_ready"
            }
            new_state = state_map.get(state, channel.state)
            
            update_vals = {"state": new_state}
            if new_state == "connected":
                instance_info = connection.get("instance", {})
                if instance_info.get("wuid"):
                    update_vals["phone"] = instance_info["wuid"].replace("@s.whatsapp.net", "")
                if instance_info.get("profileName"):
                    update_vals["phone_name"] = instance_info["profileName"]
            
            channel.sudo().write(update_vals)
            return {"status": "ok"}
        except Exception as e:
            _logger.error(f"Connection update error: {e}")
            return {"status": "error", "message": str(e)}

    def _handle_qrcode_update(self, channel, data):
        """Handle QR code update"""
        try:
            qr_data = data.get("data", {})
            qr_base64 = qr_data.get("qrcode", {}).get("base64", "")
            if qr_base64:
                if "," in qr_base64:
                    qr_base64 = qr_base64.split(",")[1]
                channel.sudo().write({"qrcode_base64": qr_base64, "state": "qr_ready"})
            return {"status": "ok"}
        except Exception as e:
            _logger.error(f"QR update error: {e}")
            return {"status": "error", "message": str(e)}
