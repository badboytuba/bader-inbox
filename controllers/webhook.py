# Copyright 2026 Bader Business
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import logging
import json
from odoo import http, fields
from odoo.http import request

_logger = logging.getLogger(__name__)


def _json_ok(data=None):
    """Return a JSON HTTP response (for type='http' routes)"""
    return request.make_json_response(data or {"status": "ok"})


def _json_error(msg="error"):
    """Return a JSON error HTTP response"""
    return request.make_json_response({"status": "error", "message": msg})


class BaderInboxWebhook(http.Controller):
    """Webhook controller for Evolution API events
    
    IMPORTANT: Uses type='http' (NOT type='json') because Evolution API
    sends plain JSON, not Odoo JSON-RPC format.
    
    Payload format (messages.upsert):
    {
        "event": "messages.upsert",
        "instance": "bader_9_ventas",
        "message": {
            "key": {"remoteJid": "...", "fromMe": false, "id": "..."},
            "messageType": "conversation",
            "content": {"conversation": "texto"},
            "pushName": "Nome",
            "timestamp": 1705233600
        }
    }
    
    Events: messages.upsert, connection.update, qrcode.updated
    Note: Webhooks are in-memory. Must reconfigure after API server restart.
    """

    @http.route(
        "/bader-inbox/webhook/<int:channel_id>",
        type="http", auth="none", methods=["POST"], csrf=False,
    )
    def webhook_handler(self, channel_id, **kwargs):
        """Handle incoming webhook from Evolution API."""
        try:
            # Parse raw JSON body (Evolution API sends plain JSON, not JSON-RPC)
            raw_body = request.httprequest.get_data(as_text=True)
            _logger.info(f"Webhook raw body for channel {channel_id} (first 500 chars): {raw_body[:500]}")
            
            try:
                data = json.loads(raw_body) if raw_body else {}
            except json.JSONDecodeError as je:
                _logger.error(f"Invalid JSON in webhook body: {je}")
                return _json_error("Invalid JSON")
            
            _logger.info(f"Webhook for channel {channel_id}: event={data.get('event', 'unknown')}")
            
            # DEBUG: Save full payload to file for analysis
            try:
                with open(f"/tmp/webhook_debug_{channel_id}.json", "w") as f:
                    json.dump(data, f, indent=2, default=str)
            except:
                pass
            
            channel = request.env["bader.inbox.channel"].sudo().browse(channel_id)
            if not channel.exists():
                _logger.warning(f"Channel {channel_id} not found")
                return _json_error("Channel not found")
            
            event = data.get("event", "")
            _logger.info(f"Processing event: {event} for channel {channel_id}")
            
            if event == "messages.upsert":
                return self._handle_message(channel, data)
            elif event == "connection.update":
                return self._handle_connection_update(channel, data)
            elif event == "qrcode.updated":
                return self._handle_qrcode_update(channel, data)
            
            _logger.info(f"Ignoring event: {event}")
            return _json_ok({"status": "ignored"})
            
        except Exception as e:
            _logger.error(f"Webhook error: {e}", exc_info=True)
            return _json_error(str(e))

    def _handle_message(self, channel, data):
        """Handle incoming/outgoing message
        
        Payload: key, content, pushName are INSIDE data["message"]
        {
            "event": "messages.upsert",
            "message": {
                "key": {"remoteJid": "...", "fromMe": false, "id": "..."},
                "messageType": "conversation",
                "content": {"conversation": "texto"},
                "pushName": "Nome do Contato"
            }
        }
        """
        try:
            msg_obj = data.get("message", {})
            if not msg_obj:
                _logger.warning("No message object in payload")
                return _json_error("No message data")
            
            # Extract fields from INSIDE the message object
            key = msg_obj.get("key", {})
            message_content = msg_obj.get("content", {})
            push_name = msg_obj.get("pushName", "")
            message_type_raw = msg_obj.get("messageType", "conversation")
            
            _logger.info(f"Message: key={key}, pushName={push_name}, type={message_type_raw}")
            
            from_me = key.get("fromMe", False)
            direction = "out" if from_me else "in"
            
            remote_jid = key.get("remoteJid", "")
            phone = remote_jid.replace("@s.whatsapp.net", "").replace("@g.us", "")
            
            if not phone or "@" in phone:
                _logger.warning(f"Invalid phone: {remote_jid}")
                return _json_error("Invalid phone")
            
            _logger.info(f"Processing message from {phone} (direction={direction})")
            
            Conversation = request.env["bader.inbox.conversation"].sudo()
            conversation = Conversation.get_or_create(
                channel_id=channel.id,
                phone=phone,
                whatsapp_id=remote_jid,
                contact_name=push_name
            )
            
            # Parse message content from the "content" field
            msg_type, content, media_info = self._parse_message_content(message_content)
            
            # Check for duplicate
            msg_id = key.get("id", "")
            Message = request.env["bader.inbox.message"].sudo()
            if msg_id:
                existing = Message.search([("whatsapp_message_id", "=", msg_id)], limit=1)
                if existing:
                    _logger.info(f"Duplicate message {msg_id}, skipping")
                    return _json_ok({"status": "duplicate"})
            
            message_vals = {
                "conversation_id": conversation.id,
                "direction": direction,
                "message_type": msg_type,
                "content": content,
                "whatsapp_message_id": msg_id,
                "status": "read" if direction == "in" else "sent",
            }
            if media_info:
                message_vals.update(media_info)
            
            new_message = Message.create(message_vals)
            _logger.info(f"Message created: id={new_message.id}, conv={conversation.id}")
            
            if direction == "in":
                self._send_bus_notification(conversation, new_message, push_name, phone)
                self._trigger_chatbot(conversation, new_message)
            
            return _json_ok()
            
        except Exception as e:
            _logger.error(f"Error handling message: {e}", exc_info=True)
            return _json_error(str(e))

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

    def _parse_message_content(self, content):
        """Parse message content from API format
        
        The "content" field contains the actual message data:
        - Text: {"conversation": "texto"} or {"extendedTextMessage": {"text": "..."}}
        - Image: {"imageMessage": {"caption": "...", "mimetype": "...", "url": "..."}}
        - Audio: {"audioMessage": {...}}
        - etc.
        """
        msg_type = "text"
        text = ""
        media_info = {}
        
        if not content:
            return msg_type, text, media_info
        
        # Handle case where content is a simple string
        if isinstance(content, str):
            return msg_type, content, media_info
        
        if "conversation" in content:
            text = content["conversation"]
        elif "extendedTextMessage" in content:
            text = content["extendedTextMessage"].get("text", "")
        elif "imageMessage" in content:
            msg_type = "image"
            img = content["imageMessage"]
            text = img.get("caption", "")
            media_info = {"media_mimetype": img.get("mimetype"), "media_url": img.get("url")}
        elif "audioMessage" in content:
            msg_type = "audio"
            audio = content["audioMessage"]
            media_info = {"media_mimetype": audio.get("mimetype"), "media_url": audio.get("url")}
        elif "videoMessage" in content:
            msg_type = "video"
            video = content["videoMessage"]
            text = video.get("caption", "")
            media_info = {"media_mimetype": video.get("mimetype"), "media_url": video.get("url")}
        elif "documentMessage" in content:
            msg_type = "document"
            doc = content["documentMessage"]
            text = doc.get("caption", "")
            media_info = {"media_mimetype": doc.get("mimetype"), "media_filename": doc.get("fileName"), "media_url": doc.get("url")}
        elif "locationMessage" in content:
            msg_type = "location"
            loc = content["locationMessage"]
            media_info = {"latitude": loc.get("degreesLatitude"), "longitude": loc.get("degreesLongitude"), "location_name": loc.get("name")}
            text = loc.get("address", "")
        
        return msg_type, text, media_info

    def _handle_connection_update(self, channel, data):
        """Handle connection status update
        
        Payload fields are at root level (not in 'data'):
        {"event": "connection.update", "status": "connected", ...}
        """
        try:
            # Status can be at root or in a nested object
            state = data.get("status", "") or data.get("state", "")
            if not state:
                # Try nested data object
                connection = data.get("data", data.get("message", {}))
                state = connection.get("state", "") or connection.get("status", "")
            
            _logger.info(f"Connection update: state={state}")
            
            state_map = {
                "open": "connected", 
                "connected": "connected",
                "connecting": "connecting", 
                "close": "disconnected",
                "disconnected": "disconnected",
            }
            new_state = state_map.get(state, channel.state)
            
            update_vals = {"state": new_state}
            if new_state == "connected":
                # Try to get phone info from various payload locations
                instance_info = data.get("instance", data.get("data", {}).get("instance", {}))
                if isinstance(instance_info, dict):
                    if instance_info.get("wuid"):
                        update_vals["phone"] = instance_info["wuid"].replace("@s.whatsapp.net", "")
                    if instance_info.get("profileName"):
                        update_vals["phone_name"] = instance_info["profileName"]
            
            channel.sudo().write(update_vals)
            _logger.info(f"Channel {channel.id} state updated to {new_state}")
            return _json_ok()
        except Exception as e:
            _logger.error(f"Connection update error: {e}", exc_info=True)
            return _json_error(str(e))

    def _handle_qrcode_update(self, channel, data):
        """Handle QR code update
        
        Payload may have qrcode at root or in nested object
        """
        try:
            # Try different payload structures
            qr_base64 = ""
            
            # Root level
            if "qrcode" in data:
                qr_data = data["qrcode"]
                if isinstance(qr_data, dict):
                    qr_base64 = qr_data.get("base64", "")
                elif isinstance(qr_data, str):
                    qr_base64 = qr_data
            
            # Nested in data
            if not qr_base64 and "data" in data:
                nested = data["data"]
                if isinstance(nested, dict):
                    qr_obj = nested.get("qrcode", {})
                    if isinstance(qr_obj, dict):
                        qr_base64 = qr_obj.get("base64", "")
                    elif isinstance(qr_obj, str):
                        qr_base64 = qr_obj
            
            if qr_base64:
                if "," in qr_base64:
                    qr_base64 = qr_base64.split(",")[1]
                channel.sudo().write({"qrcode_base64": qr_base64, "state": "qr_ready"})
                _logger.info(f"QR code updated for channel {channel.id}")
            
            return _json_ok()
        except Exception as e:
            _logger.error(f"QR update error: {e}", exc_info=True)
            return _json_error(str(e))
