# Copyright 2026 Bader Business
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import logging
import json
import base64
import requests as req_lib
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
        "/bader-inbox/media/<int:message_id>",
        type="http", auth="user", methods=["GET"], csrf=False,
    )
    def serve_media(self, message_id, **kwargs):
        """Serve stored media content for a message.
        If not stored yet, downloads on-demand from media_url or Evolution API."""
        try:
            message = request.env["bader.inbox.message"].sudo().browse(message_id)
            if not message.exists():
                return request.not_found()
            
            media_types = ("image", "audio", "video", "document", "sticker")
            if message.message_type not in media_types:
                return request.not_found()
            
            # 1) Try stored media_data (ir.attachment)
            data = None
            if message.media_data:
                try:
                    data = base64.b64decode(message.media_data)
                except Exception:
                    data = None
            
            # 2) Download from media_url (WA CDN) if not stored
            if not data and message.media_url:
                try:
                    resp = req_lib.get(message.media_url, timeout=15, stream=True)
                    if resp.status_code == 200:
                        data = resp.content
                        # Cache it in media_data for future requests
                        try:
                            message.write({"media_data": base64.b64encode(data).decode()})
                            _logger.info(f"Media cached from URL for message {message_id}")
                        except Exception as ce:
                            _logger.warning(f"Failed to cache media: {ce}")
                        # Update mimetype from response if available
                        ct = resp.headers.get("Content-Type")
                        if ct and not message.media_mimetype:
                            message.write({"media_mimetype": ct.split(";")[0]})
                except Exception as e:
                    _logger.warning(f"Media URL download failed for {message_id}: {e}")
            
            if not data:
                _logger.warning(f"No media data available for message {message_id}")
                return request.not_found()
            
            mimetype = message.media_mimetype or "application/octet-stream"
            headers = [
                ("Content-Type", mimetype),
                ("Content-Length", str(len(data))),
                ("Cache-Control", "public, max-age=86400"),
            ]
            if message.media_filename:
                headers.append(("Content-Disposition", f'inline; filename="{message.media_filename}"'))
            
            return request.make_response(data, headers)
        except Exception as e:
            _logger.error(f"Error serving media: {e}")
            return request.not_found()

    @http.route(
        "/bader-inbox/webhook/<int:channel_id>/<string:webhook_token>",
        type="http", auth="none", methods=["POST"], csrf=False,
    )
    def webhook_handler(self, channel_id, webhook_token, **kwargs):
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
            
            # Removed debug file writing for security
            # _logger.debug(f"Webhook data: {json.dumps(data, default=str)}")
            
            channel = request.env["bader.inbox.channel"].sudo().browse(channel_id)
            if not channel.exists():
                _logger.warning(f"Channel {channel_id} not found")
                return _json_error("Channel not found")
                
            # Verify Webhook Token
            if channel.webhook_token != webhook_token:
                _logger.warning(f"Invalid webhook token for channel {channel_id}")
                return request.make_response("Forbidden", status=403)
            
            event = data.get("event", "")
            _logger.info(f"Processing event: {event} for channel {channel_id}")
            
            if event == "messages.upsert":
                return self._handle_message(channel, data)
            elif event == "messages.update":
                return self._handle_message_update(channel, data)
            elif event == "connection.update":
                return self._handle_connection_update(channel, data)
            elif event == "qrcode.updated":
                return self._handle_qrcode_update(channel, data)
            
            _logger.info(f"Ignoring event: {event}")
            return _json_ok({"status": "ignored"})
            
        except Exception as e:
            _logger.error(f"Webhook error: {e}", exc_info=True)
            return _json_error(str(e))

    def _extract_phone_info(self, key):
        """Extract phone and remote_jid from message key.
        
        Evolution API often uses LID (Linked ID) as remoteJid but provides
        the real phone number in senderPn. We must check senderPn first.
        """
        remote_jid = key.get("remoteJid", "")
        sender_pn = key.get("senderPn", "")
        participant = key.get("participant", "")
        
        # Priority: senderPn > participant > remoteJid
        phone_source = ""
        if sender_pn and "@s.whatsapp.net" in sender_pn:
            phone_source = sender_pn
        elif participant and "@s.whatsapp.net" in participant:
            phone_source = participant
        elif remote_jid and "@s.whatsapp.net" in remote_jid:
            phone_source = remote_jid
        else:
            # No valid @s.whatsapp.net source found
            # Skip groups, newsletters, broadcasts, status
            skip_suffixes = ("@g.us", "@newsletter", "@broadcast", "@status")
            if remote_jid and any(remote_jid.endswith(s) for s in skip_suffixes):
                _logger.info(f"Skipping non-personal JID: {remote_jid}")
                return None, None, None
            # LID without senderPn — can't resolve phone
            if remote_jid and remote_jid.endswith("@lid"):
                _logger.warning(f"LID without senderPn, cannot resolve: {remote_jid}")
                return None, None, None
            _logger.warning(f"Unknown JID format: {remote_jid}")
            return None, None, None
        
        # Strip any @ suffix to get just the number
        phone = phone_source.split("@")[0] if "@" in phone_source else phone_source
        whatsapp_id = phone_source
        
        return phone, whatsapp_id, phone_source

    def _process_media_download(self, channel, message, key):
        """Download media content for a message from its media_url.
        
        NOTE: This API has no getBase64FromMediaMessage endpoint.
        Media must be downloaded from the URL provided in the webhook payload.
        """
        try:
            if not message.media_url:
                _logger.warning(f"No media_url for message {message.id}, cannot download")
                return
            
            resp = req_lib.get(message.media_url, timeout=30, stream=True)
            if resp.status_code == 200:
                media_data = base64.b64encode(resp.content).decode()
                update_vals = {"media_data": media_data}
                # Update mimetype from response headers if not set
                ct = resp.headers.get("Content-Type")
                if ct and not message.media_mimetype:
                    update_vals["media_mimetype"] = ct.split(";")[0]
                message.sudo().write(update_vals)
                _logger.info(f"Media downloaded from URL for message {message.id}")
            else:
                _logger.warning(f"Media download HTTP {resp.status_code} for message {message.id}")
        except Exception as me:
            _logger.error(f"Media download error: {me}")

    def _create_message(self, conversation, direction, msg_type, content, msg_id, media_info):
        """Create a new message record"""
        Message = request.env["bader.inbox.message"].sudo()
        
        # Check duplicate
        if msg_id:
            existing = Message.search([("whatsapp_message_id", "=", msg_id)], limit=1)
            if existing:
                _logger.info(f"Duplicate message {msg_id}, skipping")
                return None
        
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
        
        return Message.create(message_vals)

    def _handle_message(self, channel, data):
        """Handle incoming/outgoing message"""
        try:
            # Evolution API wraps message in data.data or sends flat
            # Format: {event, instance, data: {key, message, pushName, ...}}
            msg_obj = data.get("data") or data.get("message")
            if isinstance(msg_obj, list):
                # Sometimes Evolution sends a list of messages
                msg_obj = msg_obj[0] if msg_obj else {}
            if not msg_obj or not isinstance(msg_obj, dict):
                _logger.warning(f"No message object in payload. Keys: {list(data.keys())}")
                return _json_error("No message data")
            
            key = msg_obj.get("key", {})
            message_content = msg_obj.get("content") or msg_obj.get("message", {})
            push_name = msg_obj.get("pushName", "")
            
            phone, whatsapp_id, phone_source = self._extract_phone_info(key)
            
            if not phone:
                _logger.warning("No phone extracted")
                return _json_error("No phone number")
            
            Conversation = request.env["bader.inbox.conversation"].sudo()
            conversation = Conversation.get_or_create(
                channel_id=channel.id,
                phone=phone,
                whatsapp_id=whatsapp_id,
                contact_name=push_name
            )
            
            # Parse content
            msg_type, content, media_info = self._parse_message_content(message_content)
            
            from_me = key.get("fromMe", False)
            direction = "out" if from_me else "in"
            msg_id = key.get("id", "")
            
            # Create message
            new_message = self._create_message(conversation, direction, msg_type, content, msg_id, media_info)
            if not new_message:
                return _json_ok({"status": "duplicate"})
            
            _logger.info(f"Message created: id={new_message.id}, type={msg_type}")
            
            # Download media if needed
            if msg_type in ("image", "audio", "video", "document", "sticker") and key:
                self._process_media_download(channel, new_message, key)
            
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
        elif "stickerMessage" in content:
            msg_type = "sticker"
            stk = content["stickerMessage"]
            media_info = {"media_mimetype": stk.get("mimetype"), "media_url": stk.get("url")}
        elif "locationMessage" in content:
            msg_type = "location"
            loc = content["locationMessage"]
            media_info = {"latitude": loc.get("degreesLatitude"), "longitude": loc.get("degreesLongitude"), "location_name": loc.get("name")}
            text = loc.get("address", "")
        
        return msg_type, text, media_info

    def _handle_message_update(self, channel, data):
        """Handle message status update (delivery/read receipts).
        
        Baileys API payload format:
        {
            "event": "messages.update",
            "instance": "bader_17_bader",
            "messageUpdate": {
                "key": {"remoteJid": "...", "fromMe": true, "id": "BAE5..."},
                "status": 3,
                "statusText": "delivered"
            }
        }
        Status codes: 2=SENT (✓), 3=DELIVERED (✓✓), 4=READ (✓✓ blue)
        """
        try:
            # Baileys API uses "messageUpdate" field
            update_obj = data.get("messageUpdate") or data.get("data")
            if not update_obj:
                _logger.warning("No messageUpdate in status payload")
                return _json_ok()
            
            # Handle both single object and array formats
            updates = update_obj if isinstance(update_obj, list) else [update_obj]
            
            Message = request.env["bader.inbox.message"].sudo()
            
            status_map = {
                2: "sent",
                3: "delivered",
                4: "read",
            }
            
            for update_item in updates:
                if not isinstance(update_item, dict):
                    continue
                
                key = update_item.get("key", {})
                msg_id = key.get("id")
                if not msg_id:
                    continue
                
                # Status is directly on messageUpdate, not nested in "update"
                raw_status = update_item.get("status")
                if raw_status is None:
                    continue
                
                new_status = status_map.get(int(raw_status), "sent")
                
                message = Message.search([
                    ("whatsapp_message_id", "=", msg_id)
                ], limit=1)
                
                if not message:
                    _logger.debug(f"Message update: WA ID {msg_id} not found, skipping")
                    continue
                
                # Only upgrade status (don't downgrade read→delivered)
                status_priority = {"pending": 0, "sent": 1, "delivered": 2, "read": 3, "failed": -1}
                current_priority = status_priority.get(message.status, 0)
                new_priority = status_priority.get(new_status, 0)
                
                if new_priority > current_priority:
                    message.write({
                        "status": new_status,
                        "status_timestamp": fields.Datetime.now(),
                    })
                    _logger.info(f"Message {message.id} status: {message.status} → {new_status}")
                    
                    # Send bus notification for real-time UI update
                    try:
                        request.env["bus.bus"]._sendone(
                            "bader_inbox", "bader_inbox_status_update",
                            {
                                "message_id": message.id,
                                "conversation_id": message.conversation_id.id,
                                "status": new_status,
                            }
                        )
                    except Exception:
                        pass
            
            return _json_ok()
        except Exception as e:
            _logger.error(f"Message update error: {e}", exc_info=True)
            return _json_error(str(e))

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
