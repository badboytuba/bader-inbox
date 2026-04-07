# Copyright 2026 Bader Business
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import logging
import json
import base64
import time
import requests as req_lib
from odoo import http, fields
from odoo.http import request

_logger = logging.getLogger(__name__)

# BUG 4 FIX: Module-level LID → phone cache for resolving LID contacts
# Key: LID (e.g. "258363908694225@lid"), Value: phone number
# Persisted to DB via ir.config_parameter to survive restarts
_lid_phone_cache = {}
_lid_cache_loaded = False


def _load_lid_cache():
    """Load LID→phone cache from DB on first use."""
    global _lid_phone_cache, _lid_cache_loaded
    if _lid_cache_loaded:
        return
    try:
        params = request.env["ir.config_parameter"].sudo()
        raw = params.get_param("bader_inbox.lid_phone_cache", "{}")
        _lid_phone_cache = json.loads(raw) if raw else {}
        _lid_cache_loaded = True
    except Exception:
        _lid_cache_loaded = True


def _save_lid_to_db(lid, phone):
    """Save a new LID→phone mapping to DB."""
    _lid_phone_cache[lid] = phone
    try:
        params = request.env["ir.config_parameter"].sudo()
        # Keep cache size reasonable (max 500 entries)
        if len(_lid_phone_cache) > 500:
            keys = list(_lid_phone_cache.keys())
            for k in keys[:100]:
                del _lid_phone_cache[k]
        params.set_param("bader_inbox.lid_phone_cache", json.dumps(_lid_phone_cache))
    except Exception as e:
        _logger.debug(f"Failed to persist LID cache: {e}")


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
            # H1 FIX: Access check — browse WITHOUT sudo to respect record rules
            # The user must have access to the message's conversation via channel
            message_check = request.env["bader.inbox.message"].search(
                [("id", "=", message_id)], limit=1
            )
            if not message_check:
                return request.not_found()
            # Now use sudo for the actual media operations
            message = message_check.sudo()
            
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
            
            # 2) Try downloadMedia API endpoint using stored key/content
            if not data and message.whatsapp_key_json and message.whatsapp_content_json:
                try:
                    msg_key = json.loads(message.whatsapp_key_json)
                    msg_content = json.loads(message.whatsapp_content_json)
                    channel = message.conversation_id.channel_id
                    instance_name = channel.evolution_instance_name if channel else None
                    if instance_name:
                        api = request.env["bader.inbox.evolution_api"].sudo()
                        result = api.download_media(instance_name, msg_key, msg_content)
                        if result and result.get("base64"):
                            data = base64.b64decode(result["base64"])
                            try:
                                update_vals = {"media_data": result["base64"]}
                                if result.get("mimetype") and not message.media_mimetype:
                                    update_vals["media_mimetype"] = result["mimetype"]
                                message.write(update_vals)
                            except Exception as ce:
                                _logger.warning(f"Failed to cache API media: {ce}")
                except Exception as e:
                    _logger.warning(f"downloadMedia API failed: {e}")
            
            # 3) Download from media_url (WA CDN) if not stored
            if not data and message.media_url:
                try:
                    resp = req_lib.get(message.media_url, timeout=15, stream=True)
                    if resp.status_code == 200:
                        data = resp.content
                        try:
                            message.write({"media_data": base64.b64encode(data).decode()})
                        except Exception as ce:
                            _logger.warning(f"Failed to cache media: {ce}")
                        ct = resp.headers.get("Content-Type")
                        if ct and not message.media_mimetype:
                            message.write({"media_mimetype": ct.split(";")[0]})
                except Exception as e:
                    _logger.warning(f"Media URL download failed: {e}")
            
            if not data:
                return request.not_found()
            
            # Detect Lottie/WAZ stickers (ZIP format) - extract Lottie JSON for frontend player
            if message.message_type == "sticker" and data[:2] == b"PK":
                try:
                    import zipfile
                    import io
                    with zipfile.ZipFile(io.BytesIO(data), "r") as z:
                        # Find the animation JSON file inside the WAZ
                        json_name = None
                        for name in z.namelist():
                            if name.endswith(".json") and "trust_token" not in name:
                                json_name = name
                                break
                        if json_name:
                            lottie_data = z.read(json_name)
                            return request.make_response(lottie_data, [
                                ("Content-Type", "application/json"),
                                ("Content-Length", str(len(lottie_data))),
                                ("Cache-Control", "public, max-age=86400"),
                                ("X-Sticker-Type", "lottie"),
                            ])
                except Exception as ze:
                    _logger.warning(f"Failed to extract Lottie from WAZ: {ze}")
            
            mimetype = message.media_mimetype or "application/octet-stream"
            headers = [
                ("Content-Type", mimetype),
                ("Content-Length", str(len(data))),
                ("Cache-Control", "public, max-age=86400"),
            ]
            # L1 FIX: Sanitize filename for Content-Disposition header
            if message.media_filename:
                safe_name = message.media_filename.replace('"', '').replace('\n', '').replace('\r', '')[:200]
                headers.append(("Content-Disposition", f'inline; filename="{safe_name}"'))
            
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
            # L3 FIX: Reduced logging — only log event type, not full body with PII
            _logger.debug(f"Webhook body for channel {channel_id}: {len(raw_body)} bytes")
            
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
            elif event in ("groups.upsert", "groups.update"):
                return self._handle_group_update(channel, data)
            elif event == "group-participants.update":
                return self._handle_group_participants_update(channel, data)
            elif event == "connection.update":
                return self._handle_connection_update(channel, data)
            elif event == "qrcode.updated":
                return self._handle_qrcode_update(channel, data)
            
            _logger.info(f"Ignoring event: {event}")
            return _json_ok({"status": "ignored"})
            
        except Exception as e:
            # M2 FIX: Don't leak internal error details to external callers
            _logger.error(f"Webhook error: {e}", exc_info=True)
            return _json_error("Internal error")
    def _handle_group_update(self, channel, data):
        """Handle groups.update / groups.upsert events.
        
        Updates group subject, description, picture, etc. when changed.
        """
        try:
            group_data = data.get("data", {})
            # Data can be a list or single dict
            groups_list = group_data if isinstance(group_data, list) else [group_data]
            
            Conv = request.env["bader.inbox.conversation"].sudo()
            
            for g in groups_list:
                if not isinstance(g, dict):
                    continue
                jid = g.get("id") or g.get("jid") or ""
                if not jid or "@g.us" not in jid:
                    continue
                
                conversation = Conv.search([
                    ("group_jid", "=", jid),
                    ("channel_id", "=", channel.id),
                ], limit=1)
                
                if not conversation:
                    continue
                
                vals = {}
                if g.get("subject"):
                    vals["group_subject"] = g["subject"]
                    vals["contact_name"] = g["subject"]
                if g.get("desc") or g.get("description"):
                    vals["group_description"] = g.get("desc") or g.get("description")
                if g.get("profilePictureUrl") or g.get("picture"):
                    vals["group_pic_url"] = g.get("profilePictureUrl") or g.get("picture")
                
                if vals:
                    conversation.write(vals)
                    _logger.info(f"Group updated: {jid} → {vals}")
            
            return _json_ok({"status": "group_updated"})
        except Exception as e:
            _logger.error(f"Group update handler error: {e}", exc_info=True)
            return _json_ok({"status": "error"})

    def _handle_group_participants_update(self, channel, data):
        """Handle group-participants.update events.
        
        Fired when members join/leave a group.
        Payload: {"groupJid": "...@g.us", "action": "add|remove|promote|demote",
                  "participants": ["phone@s.whatsapp.net", ...]}
        """
        try:
            event_data = data.get("data", {})
            if not isinstance(event_data, dict):
                return _json_ok({"status": "ignored"})
            
            group_jid = event_data.get("groupJid") or event_data.get("id") or ""
            if not group_jid or "@g.us" not in group_jid:
                return _json_ok({"status": "ignored"})
            
            action = event_data.get("action", "unknown")
            participants = event_data.get("participants", [])
            
            Conv = request.env["bader.inbox.conversation"].sudo()
            conversation = Conv.search([
                ("group_jid", "=", group_jid),
                ("channel_id", "=", channel.id),
            ], limit=1)
            
            if conversation:
                # Update member count
                member_count = conversation.member_count or 0
                if action == "add":
                    member_count += len(participants)
                elif action == "remove":
                    member_count = max(0, member_count - len(participants))
                conversation.write({"member_count": member_count})
                _logger.info(f"Group participants {action}: {group_jid} ({len(participants)} members, total: {member_count})")
            
            return _json_ok({"status": "participants_updated"})
        except Exception as e:
            _logger.error(f"Group participants handler error: {e}", exc_info=True)
            return _json_ok({"status": "error"})

    def _extract_phone_info(self, key):
        """Extract phone and remote_jid from message key.
        
        Evolution API often uses LID (Linked ID) as remoteJid but provides
        the real phone number in senderPn. We must check senderPn first.
        
        Returns: (phone, whatsapp_id, phone_source, group_info)
        group_info is None for individual chats, or a dict with
        {group_jid, participant_phone, participant_jid} for group messages.
        """
        remote_jid = key.get("remoteJid", "")
        sender_pn = key.get("senderPn", "")
        participant = key.get("participant", "")
        
        # Check if this is a group message
        if remote_jid and remote_jid.endswith("@g.us"):
            group_jid = remote_jid
            group_phone = remote_jid.split("@")[0]  # numeric part as phone
            # Extract participant (who sent the message in the group)
            # Priority: participantPn (real phone) > senderPn > participant (may be LID)
            participant_pn = key.get("participantPn", "")
            participant_phone = ""
            if participant_pn and "@s.whatsapp.net" in participant_pn:
                participant_phone = participant_pn.split("@")[0]
            elif sender_pn and "@s.whatsapp.net" in sender_pn:
                participant_phone = sender_pn.split("@")[0]
            elif participant and "@" in participant:
                # Fallback: strip @lid suffix (this gives LID number, not real phone)
                participant_phone = participant.split("@")[0]
            group_info = {
                "group_jid": group_jid,
                "participant_phone": participant_phone,
                "participant_jid": participant_pn or participant or sender_pn or "",
            }
            _logger.info(f"Group message: {group_jid}, participant: {participant_phone}")
            return group_phone, group_jid, group_jid, group_info
        
        # Skip newsletters, broadcasts, status
        skip_suffixes = ("@newsletter", "@broadcast", "@status")
        if remote_jid and any(remote_jid.endswith(s) for s in skip_suffixes):
            _logger.info(f"Skipping non-personal JID: {remote_jid}")
            return None, None, None, None
        
        # Individual chat: Priority senderPn > participant > remoteJid
        phone_source = ""
        if sender_pn and "@s.whatsapp.net" in sender_pn:
            phone_source = sender_pn
            # Persist LID → phone mapping to DB (survives restarts)
            if remote_jid and remote_jid.endswith("@lid"):
                phone = sender_pn.split("@")[0]
                _save_lid_to_db(remote_jid, phone)
                _logger.info(f"LID cache saved: {remote_jid} → {phone}")
        elif participant and "@s.whatsapp.net" in participant:
            phone_source = participant
        elif remote_jid and "@s.whatsapp.net" in remote_jid:
            phone_source = remote_jid
        else:
            # LID without senderPn — try resolving from cache, then API
            if remote_jid and remote_jid.endswith("@lid"):
                _load_lid_cache()
                cached_phone = _lid_phone_cache.get(remote_jid)
                if cached_phone:
                    _logger.info(f"LID resolved from cache: {remote_jid} → {cached_phone}")
                    phone_source = f"{cached_phone}@s.whatsapp.net"
                else:
                    # Fallback: try Evolution API findContacts to resolve the LID
                    resolved_phone = self._resolve_lid_via_api(remote_jid)
                    if resolved_phone:
                        _save_lid_to_db(remote_jid, resolved_phone)
                        _logger.info(f"LID resolved via API: {remote_jid} → {resolved_phone}")
                        phone_source = f"{resolved_phone}@s.whatsapp.net"
                    else:
                        # LID unresolvable — use LID number as temporary phone
                        lid_number = remote_jid.split("@")[0]
                        _logger.warning(f"LID unresolvable: {remote_jid} — using LID as temp phone: {lid_number}")
                        phone_source = remote_jid
            else:
                _logger.warning(f"Unknown JID format: {remote_jid}")
                return None, None, None, None
        
        # Strip any @ suffix to get just the number
        phone = phone_source.split("@")[0] if "@" in phone_source else phone_source
        whatsapp_id = phone_source
        
        return phone, whatsapp_id, phone_source, None

    def _resolve_lid_via_api(self, lid):
        """Try to resolve a LID to a phone number via Evolution API findContacts.
        
        Queries all connected channels' instances to find the contact.
        Returns the phone number string or None.
        """
        try:
            channels = request.env["bader.inbox.channel"].sudo().search([
                ("state", "=", "connected"),
                ("evolution_instance_name", "!=", False),
            ], limit=5)
            
            api = request.env["bader.inbox.evolution.api"].sudo()
            
            for channel in channels:
                try:
                    result = api._request(
                        "POST",
                        f"/chat/findContacts/{channel.evolution_instance_name}",
                        {"where": {"id": lid}}
                    )
                    contacts = result if isinstance(result, list) else [result] if isinstance(result, dict) else []
                    for contact in contacts:
                        if not isinstance(contact, dict):
                            continue
                        # The contact may have a 'id' with @s.whatsapp.net format
                        wid = contact.get("wid") or contact.get("id") or ""
                        if "@s.whatsapp.net" in str(wid):
                            phone = str(wid).split("@")[0]
                            if phone.isdigit() and len(phone) >= 8:
                                return phone
                        # Also check 'number' field
                        number = contact.get("number") or contact.get("phone") or ""
                        if str(number).isdigit() and len(str(number)) >= 8:
                            return str(number)
                except Exception as e:
                    _logger.debug(f"LID API resolve attempt failed for {channel.name}: {e}")
                    continue
        except Exception as e:
            _logger.debug(f"LID API resolution error: {e}")
        return None

    def _process_media_download(self, channel, message, key, message_content=None):
        """Download media content for a message.
        
        Strategy:
        1) Use the new /message/downloadMedia/{instance} endpoint (key + content)
        2) Fallback: download from media_url if available
        """
        try:
            instance_name = channel.evolution_instance_name
            
            # Strategy 1: Use downloadMedia API endpoint
            if instance_name and key and message_content:
                try:
                    api = request.env["bader.inbox.evolution_api"]
                    result = api.download_media(instance_name, key, message_content)
                    if result and result.get("base64"):
                        update_vals = {"media_data": result["base64"]}
                        if result.get("mimetype") and not message.media_mimetype:
                            update_vals["media_mimetype"] = result["mimetype"]
                        message.sudo().write(update_vals)
                        _logger.info(f"Media downloaded via API for message {message.id}")
                        return
                except Exception as api_err:
                    _logger.warning(f"downloadMedia API failed for message {message.id}: {api_err}")
            
            # Strategy 2: Fallback to direct URL download
            if message.media_url:
                try:
                    resp = req_lib.get(message.media_url, timeout=30, stream=True)
                    if resp.status_code == 200:
                        media_data = base64.b64encode(resp.content).decode()
                        update_vals = {"media_data": media_data}
                        ct = resp.headers.get("Content-Type")
                        if ct and not message.media_mimetype:
                            update_vals["media_mimetype"] = ct.split(";")[0]
                        message.sudo().write(update_vals)
                        _logger.info(f"Media downloaded from URL for message {message.id}")
                        return
                    else:
                        _logger.warning(f"Media URL download HTTP {resp.status_code} for message {message.id}")
                except Exception as url_err:
                    _logger.warning(f"Media URL download failed for message {message.id}: {url_err}")
            
            _logger.warning(f"No media download method available for message {message.id}")
        except Exception as me:
            _logger.error(f"Media download error: {me}")

    def _create_message(self, conversation, direction, msg_type, content, msg_id,
                        media_info, quote_info=None, sender_name=None, sender_phone=None):
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
        if sender_name:
            message_vals["sender_name"] = sender_name
        if sender_phone:
            message_vals["sender_phone"] = sender_phone
        if media_info:
            # Strip non-model fields before create
            safe_info = {k: v for k, v in media_info.items() if k not in ("is_animated",)}
            message_vals.update(safe_info)
        if quote_info:
            message_vals.update(quote_info)
        
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
            
            # DEBUG: Log the full message object keys to understand structure
            _logger.info(f"MSG_OBJ keys: {list(msg_obj.keys())}")
            
            key = msg_obj.get("key", {})
            push_name = msg_obj.get("pushName", "")
            
            # Extract message content from multiple possible locations
            # Evolution API (Baileys) may place content in different fields:
            # - "message" (dict with conversation/imageMessage/etc.)
            # - "content" (sometimes same structure as message)
            # - Direct text fields
            message_content = None
            for content_key in ("message", "content", "messageContent"):
                candidate = msg_obj.get(content_key)
                if candidate and isinstance(candidate, dict) and len(candidate) > 0:
                    message_content = candidate
                    _logger.info(f"Content found in '{content_key}': keys={list(candidate.keys())}")
                    break
                elif candidate and isinstance(candidate, str) and candidate.strip():
                    message_content = candidate
                    _logger.info(f"Content found as string in '{content_key}': {candidate[:80]}")
                    break
            
            if not message_content:
                # Fallback: check messageType field for simple messages
                msg_type_field = msg_obj.get("messageType", "")
                _logger.info(f"No content dict found. messageType={msg_type_field}, trying fallback extraction")
                
                # For conversation type, text might be directly accessible
                if msg_type_field == "conversation":
                    # The text might be in msg_obj itself
                    raw_text = msg_obj.get("body") or msg_obj.get("text") or ""
                    if raw_text:
                        message_content = raw_text
                    else:
                        # Build a synthetic content dict
                        message_content = {"conversation": ""}
                elif msg_type_field == "extendedTextMessage":
                    raw_text = msg_obj.get("body") or msg_obj.get("text") or ""
                    message_content = {"extendedTextMessage": {"text": raw_text}}
                else:
                    message_content = {}
                
                _logger.warning(f"Content fallback used. Full msg_obj keys: {list(msg_obj.keys())}, messageType: {msg_type_field}")
            
            # Inject senderPn from message level into key if not already present
            if not key.get("senderPn") and msg_obj.get("senderPn"):
                key["senderPn"] = msg_obj["senderPn"]
            phone, whatsapp_id, phone_source, group_info = self._extract_phone_info(key)

            if not phone:
                # Only warn if this wasn't an expected skip (newsletter, broadcast, status)
                remote_jid = key.get("remoteJid", "")
                skip_suffixes = ("@newsletter", "@broadcast", "@status")
                if not any(remote_jid.endswith(s) for s in skip_suffixes):
                    _logger.warning(f"No phone extracted from JID: {remote_jid}")
                return _json_error("No phone number")
            
            is_group = group_info is not None
            from_me = key.get("fromMe", False)
            Conversation = request.env["bader.inbox.conversation"].sudo()

            # For outgoing messages (broadcasts), pushName is the sender's own name,
            # not the recipient's. Try to resolve the real contact name via Evolution API.
            if not from_me or is_group:
                # Incoming message or group: use pushName directly
                resolved_contact_name = push_name if not is_group else (push_name or "")
            else:
                # Outgoing message: try to fetch the recipient's WhatsApp name
                resolved_contact_name = None
                try:
                    EvolutionAPI = request.env["bader.inbox.evolution.api"].sudo()
                    instance_name = channel.evolution_instance
                    if instance_name:
                        resolved_contact_name = EvolutionAPI.fetch_contact_name(instance_name, phone)
                        if resolved_contact_name:
                            _logger.info(f"Resolved outgoing contact name via API: {phone} → {resolved_contact_name}")
                except Exception as e:
                    _logger.debug(f"Could not fetch contact name for {phone}: {e}")

            # BUG 3 FIX: Retry on serialization failures (concurrent webhook updates)
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    conversation = Conversation.get_or_create(
                        channel_id=channel.id,
                        phone=phone,
                        whatsapp_id=whatsapp_id,
                        contact_name=resolved_contact_name,
                        is_group=is_group,
                        group_jid=group_info.get("group_jid") if group_info else None,
                    )
                    break
                except Exception as retry_err:
                    err_str = str(retry_err).lower()
                    if "serialize" in err_str or "concurrent" in err_str:
                        if attempt < max_retries - 1:
                            request.env.cr.rollback()
                            wait_time = 0.1 * (attempt + 1)
                            _logger.warning(f"Serialization retry {attempt + 1}/{max_retries} for phone {phone}, waiting {wait_time}s")
                            time.sleep(wait_time)
                            continue
                    raise
            
            # For groups without a subject: try extracting name from webhook payload
            if is_group and conversation.is_group and not conversation.group_subject:
                group_name = self._extract_group_name_from_payload(data, msg_obj)
                if group_name:
                    conversation.write({
                        'group_subject': group_name,
                        'contact_name': group_name,
                    })
                    _logger.info(f"Group name extracted from webhook: {group_name}")
                else:
                    # Webhook doesn't include group subject — try API sync
                    try:
                        conversation.auto_sync_group_info()
                    except Exception:
                        pass
            
            # Parse content
            msg_type, content, media_info = self._parse_message_content(message_content)
            _logger.info(f"Parsed: type={msg_type}, content_len={len(content) if content else 0}, media_keys={list(media_info.keys()) if media_info else []}")
            
            direction = "out" if from_me else "in"
            msg_id = key.get("id", "")

            # Handle EDITED messages
            # Method 1: Evolution API sets isEdited + editedMessageId (some versions)
            is_edited = msg_obj.get("isEdited", False)
            edited_msg_id = msg_obj.get("editedMessageId", "")
            if is_edited and edited_msg_id:
                return self._handle_edited_message(conversation, edited_msg_id, content, msg_type)

            # Method 2: Baileys sends edits as protocolMessage.editedMessage
            # The protocolMessage contains the new content AND the original message key
            if isinstance(message_content, dict) and "protocolMessage" in message_content:
                proto = message_content["protocolMessage"]
                if isinstance(proto, dict) and proto.get("editedMessage"):
                    # Extract original message key ID
                    proto_key = proto.get("key", {})
                    original_msg_id = proto_key.get("id", "")
                    if not original_msg_id:
                        # Fallback: some versions put key directly in protocolMessage
                        original_msg_id = proto.get("editedMessageId", "")
                    if original_msg_id:
                        # Parse the new content from the edited message
                        edited_content = proto["editedMessage"]
                        new_type, new_text, _ = self._parse_message_content(edited_content)
                        _logger.info(
                            f"Edit via protocolMessage detected: original_id={original_msg_id}, "
                            f"new_text='{(new_text or '')[:60]}'"
                        )
                        return self._handle_edited_message(
                            conversation, original_msg_id, new_text, new_type
                        )
                    _logger.warning("protocolMessage.editedMessage found but no original key ID")
                # Non-edit protocol messages (read receipts, etc.) — skip
                elif proto.get("type") not in (None, 0):
                    _logger.info(f"Skipping non-edit protocolMessage type={proto.get('type')}")
                    return _json_ok({"status": "protocol_ignored"})

            # Extract quote info from contextInfo
            quote_info = self._extract_quote_info(message_content)
            
            # Determine sender info for group messages
            sender_name_val = None
            sender_phone_val = None
            if is_group and direction == "in":
                sender_name_val = push_name or ""
                sender_phone_val = group_info.get("participant_phone", "") if group_info else ""

            # Create message
            new_message = self._create_message(
                conversation, direction, msg_type, content, msg_id, media_info, quote_info,
                sender_name=sender_name_val, sender_phone=sender_phone_val
            )
            if not new_message:
                return _json_ok({"status": "duplicate"})
            
            _logger.info(f"Message created: id={new_message.id}, type={msg_type}, content='{(content or '')[:50]}'")

            # Extract UTM / tracking data from message
            tracking = msg_obj.get("tracking")
            if tracking and isinstance(tracking, dict) and not conversation.tracking_code:
                tracking_vals = {}
                field_map = {
                    "source": "utm_source",
                    "medium": "utm_medium",
                    "campaign": "utm_campaign",
                    "channel": "channel_origin",
                    "referrer_url": "referrer_url",
                    "landing_page": "landing_page",
                    "tracking_code": "tracking_code",
                }
                for src_key, dst_field in field_map.items():
                    val = tracking.get(src_key)
                    if val:
                        tracking_vals[dst_field] = str(val)[:256]
                tracked_at = tracking.get("tracked_at")
                if tracked_at:
                    try:
                        from datetime import datetime
                        tracking_vals["tracked_at"] = datetime.fromisoformat(
                            tracked_at.replace("Z", "+00:00")
                        )
                    except (ValueError, TypeError):
                        tracking_vals["tracked_at"] = fields.Datetime.now()
                else:
                    tracking_vals["tracked_at"] = fields.Datetime.now()
                if tracking_vals:
                    conversation.sudo().write(tracking_vals)
                    _logger.info(f"Tracking saved for conv {conversation.id}: source={tracking_vals.get('utm_source')}, campaign={tracking_vals.get('utm_campaign')}")

            # Extract CTWA (Click-to-WhatsApp) ad context from Meta campaigns
            # Meta Ads send ad data inside contextInfo.externalAdReply within message content
            if not conversation.utm_source and isinstance(message_content, dict):
                ad_reply = self._extract_ctwa_ad_context(message_content)
                if ad_reply:
                    ctwa_vals = {"tracked_at": fields.Datetime.now()}
                    source_type = ad_reply.get("sourceType", "")
                    if source_type:
                        ctwa_vals["utm_source"] = source_type  # e.g. "ctwa"
                    else:
                        ctwa_vals["utm_source"] = "meta_ads"
                    ctwa_vals["utm_medium"] = "click_to_whatsapp"
                    # Use ad title as campaign name if available
                    ad_title = ad_reply.get("title", "")
                    ad_body = ad_reply.get("body", "")
                    if ad_title:
                        ctwa_vals["utm_campaign"] = str(ad_title)[:256]
                    ctwa_vals["channel_origin"] = "Meta Ads (CTWA)"
                    # Store source URL and CTWA click ID
                    source_url = ad_reply.get("sourceUrl", "")
                    if source_url:
                        ctwa_vals["referrer_url"] = str(source_url)[:256]
                    ctwa_clid = ad_reply.get("ctwaClid", "")
                    if ctwa_clid:
                        ctwa_vals["tracking_code"] = str(ctwa_clid)[:256]
                    # Store ad body in landing_page field for reference
                    if ad_body:
                        ctwa_vals["landing_page"] = str(ad_body)[:256]
                    conversation.sudo().write(ctwa_vals)
                    _logger.info(
                        f"CTWA ad context saved for conv {conversation.id}: "
                        f"source={ctwa_vals.get('utm_source')}, "
                        f"campaign={ctwa_vals.get('utm_campaign')}, "
                        f"clid={ctwa_clid[:30] if ctwa_clid else 'N/A'}"
                    )            
            # Store raw key/content for ALL messages (useful for debugging and deferred media download)
            try:
                update_vals = {}
                if key:
                    update_vals["whatsapp_key_json"] = json.dumps(key)
                if isinstance(message_content, dict):
                    update_vals["whatsapp_content_json"] = json.dumps(message_content)
                if update_vals:
                    new_message.sudo().write(update_vals)
            except Exception as wk_err:
                _logger.warning(f"Failed to store WA key/content: {wk_err}")
            
            # Download media if needed
            if msg_type in ("image", "audio", "video", "document", "sticker") and key:
                self._process_media_download(channel, new_message, key, message_content)

            # Extract link preview for text messages
            if msg_type == "text" and content:
                try:
                    self._extract_link_preview(new_message)
                except Exception:
                    pass

            # Send bus notification for ALL messages (incoming + outgoing from phone)
            self._send_bus_notification(conversation, new_message, push_name, phone)

            if direction == "in":
                # Auto-reopen resolved conversations when customer replies
                if conversation.state == "resolved":
                    conversation.sudo().write({"state": "open"})
                    _logger.info(f"Auto-reopened resolved conversation {conversation.id}")
                self._trigger_chatbot(conversation, new_message)
                # AI Agent auto-reply
                self._trigger_ai_agent(channel, conversation, content)
            
            return _json_ok()
            
        except Exception as e:
            _logger.error(f"Error handling message: {e}", exc_info=True)
            return _json_error("Internal error")

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
                    "sender_name": message.sender_name or "",
                    "sender_phone": message.sender_phone or "",
                }
            }
            request.env["bus.bus"]._sendone("bader_inbox", "bader_inbox_new_message", payload)
        except Exception as e:
            _logger.warning(f"Bus notification error: {e}")

    @staticmethod
    def _is_safe_url(url):
        """C2 FIX: Block SSRF — reject internal/private IPs and localhost."""
        import ipaddress
        from urllib.parse import urlparse
        try:
            parsed = urlparse(url)
            hostname = parsed.hostname or ""
            # Block obvious internal hostnames
            blocked_hosts = ("localhost", "127.0.0.1", "0.0.0.0", "[::1]", "metadata.google", "169.254.169.254")
            if hostname.lower() in blocked_hosts or hostname.endswith(".local"):
                return False
            # Resolve and check for private IP ranges
            import socket
            try:
                ip = socket.gethostbyname(hostname)
                addr = ipaddress.ip_address(ip)
                if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
                    return False
            except (socket.gaierror, ValueError):
                return False
            return True
        except Exception:
            return False

    def _extract_link_preview(self, message):
        """Extract OG tags from URLs in message content (with SSRF protection)"""
        import re as re_mod
        if not message.content or message.message_type != "text":
            return
        url_pattern = r'https?://[^\s<>\"\']+' 
        urls = re_mod.findall(url_pattern, message.content)
        if not urls:
            return
        url = urls[0]
        # C2 FIX: SSRF protection — block internal/private URLs
        if not self._is_safe_url(url):
            _logger.warning(f"Link preview blocked (SSRF protection): {url}")
            return
        try:
            # L2 FIX: Stream response with size limit to prevent memory exhaustion
            resp = req_lib.get(url, timeout=5, headers={
                "User-Agent": "Mozilla/5.0 (compatible; BaderBot/1.0)"
            }, allow_redirects=True, stream=True)
            if resp.status_code != 200:
                return
            # Read max 100KB to prevent memory exhaustion
            chunks = []
            size = 0
            for chunk in resp.iter_content(chunk_size=8192, decode_unicode=True):
                chunks.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8", errors="ignore"))
                size += len(chunk)
                if size > 100_000:
                    break
            html = "".join(chunks)[:50000]
            resp.close()

            def _og(prop):
                m = re_mod.search(
                    rf'<meta\s+(?:property|name)=["\']og:{prop}["\']\s+content=["\']([^"\']*)["\']',
                    html, re_mod.IGNORECASE
                )
                if not m:
                    m = re_mod.search(
                        rf'<meta\s+content=["\']([^"\']*)["\']\s+(?:property|name)=["\']og:{prop}["\']',
                        html, re_mod.IGNORECASE
                    )
                return m.group(1) if m else None

            title = _og("title")
            if not title:
                tm = re_mod.search(r'<title[^>]*>([^<]+)</title>', html, re_mod.IGNORECASE)
                title = tm.group(1).strip() if tm else url

            description = _og("description") or ""
            image = _og("image") or ""
            from urllib.parse import urlparse
            domain = urlparse(url).netloc

            preview = json.dumps({
                "url": url,
                "title": title[:200],
                "description": description[:300],
                "image": image,
                "domain": domain
            })
            message.sudo().write({"link_preview": preview})
        except Exception as e:
            _logger.debug(f"Link preview extraction failed: {e}")

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

    def _trigger_ai_agent(self, channel, conversation, message_text):
        """Check and execute AI Agent auto-reply"""
        try:
            if not message_text:
                _logger.info("AI Agent skipped: empty message_text")
                return

            AIAgent = request.env["bader.inbox.ai_assistant"].sudo()
            agent = AIAgent._get_agent_for_channel(channel.id)
            if not agent:
                _logger.info(f"AI Agent skipped: no agent configured for channel {channel.id}")
                return

            # Check if AI is active for this conversation
            if not conversation.ai_active:
                _logger.info(f"AI Agent skipped: ai_active=False for conv {conversation.id}")
                return

            # Check if conversation is assigned to human
            if agent.only_unassigned and conversation.assigned_user_id:
                _logger.info(f"AI Agent skipped: conv {conversation.id} assigned to user {conversation.assigned_user_id.id}")
                return

            # Check schedule (business hours)
            if not agent.is_within_schedule():
                _logger.info("AI Agent skipped: outside schedule")
                return

            # Process with AI
            _logger.info(f"AI Agent processing: conv={conversation.id}, text='{message_text[:80]}'")
            result = agent.process_message(conversation.id, message_text)
            _logger.info(f"AI Agent result: {result}")
            
            if result.get("skip"):
                _logger.info(f"AI Agent skipped: {result.get('reason', 'unknown')}")
                return
            if result.get("error"):
                _logger.warning(f"AI Agent error result: {result.get('error')}")
                # Still try to send fallback if available
                response = result.get("response", "")
                if not response:
                    return
            else:
                response = result.get("response", "")
            
            if not response:
                _logger.info("AI Agent: no response generated")
                return

            # Send response via Evolution API
            Message = request.env["bader.inbox.message"].sudo()
            Message.send_message(conversation.id, response, msg_type="text")
            _logger.info(f"AI Agent replied to conversation {conversation.id}: '{response[:80]}'")

        except Exception as e:
            _logger.error(f"AI Agent exception: {e}", exc_info=True)

    def _extract_quote_info(self, content):
        """Extract quoted/reply message info from contextInfo.
        
        WhatsApp reply messages include contextInfo with:
        - stanzaId: WA message ID of the quoted message
        - participant: who sent the quoted message
        - quotedMessage: {conversation: "text"} or {extendedTextMessage: ...} etc.
        
        This info can appear inside extendedTextMessage, imageMessage, etc.
        """
        if not content or not isinstance(content, dict):
            return None
        
        # contextInfo can be in any message type dict
        context_info = None
        for msg_key in ("extendedTextMessage", "imageMessage", "videoMessage",
                        "audioMessage", "documentMessage", "stickerMessage",
                        "locationMessage"):
            inner = content.get(msg_key)
            if isinstance(inner, dict) and "contextInfo" in inner:
                context_info = inner["contextInfo"]
                break
        
        if not context_info or not isinstance(context_info, dict):
            return None
        
        stanza_id = context_info.get("stanzaId", "")
        if not stanza_id:
            return None
        
        participant = context_info.get("participant", "")
        
        # Extract text from quoted message
        quoted_text = ""
        quoted_msg = context_info.get("quotedMessage", {})
        if isinstance(quoted_msg, dict):
            if "conversation" in quoted_msg:
                quoted_text = quoted_msg["conversation"]
            elif "extendedTextMessage" in quoted_msg:
                etm = quoted_msg["extendedTextMessage"]
                quoted_text = etm.get("text", "") if isinstance(etm, dict) else str(etm)
            elif "imageMessage" in quoted_msg:
                quoted_text = quoted_msg["imageMessage"].get("caption", "📷 Image")
            elif "videoMessage" in quoted_msg:
                quoted_text = quoted_msg["videoMessage"].get("caption", "🎥 Video")
            elif "audioMessage" in quoted_msg:
                quoted_text = "🎵 Audio"
            elif "documentMessage" in quoted_msg:
                doc = quoted_msg["documentMessage"]
                quoted_text = doc.get("fileName", "📄 Document")
            elif "stickerMessage" in quoted_msg:
                quoted_text = "🏷️ Sticker"
            else:
                quoted_text = "💬 Message"
        elif isinstance(quoted_msg, str):
            quoted_text = quoted_msg
        
        _logger.info(f"Quote extracted: stanza={stanza_id}, participant={participant}, text='{quoted_text[:60]}'")
        
        return {
            "quoted_message_id": stanza_id,
            "quoted_text": quoted_text[:500] if quoted_text else "",
            "quoted_participant": participant.split("@")[0] if "@" in participant else participant,
        }

    def _extract_ctwa_ad_context(self, content):
        """Extract Click-to-WhatsApp (CTWA) ad context from Meta campaigns.
        
        Meta Ads (Facebook/Instagram) attach ad metadata inside:
        contextInfo.externalAdReply within the message content.
        
        Typical structure:
        {
            "extendedTextMessage": {
                "text": "¡Hola! Quiero más información",
                "contextInfo": {
                    "externalAdReply": {
                        "title": "Ad Campaign Title",
                        "body": "Ad body text",
                        "sourceUrl": "https://fb.me/...",
                        "mediaUrl": "https://...",
                        "sourceType": "ctwa",
                        "ctwaClid": "campaign_click_id_123"
                    }
                }
            }
        }
        
        Also check top-level contextInfo for simple conversation messages.
        """
        if not content or not isinstance(content, dict):
            return None
        
        # Check contextInfo in all message type wrappers
        context_info = None
        for msg_key in ("extendedTextMessage", "conversation", "imageMessage",
                        "videoMessage", "audioMessage", "documentMessage",
                        "contactMessage", "locationMessage"):
            inner = content.get(msg_key)
            if isinstance(inner, dict) and "contextInfo" in inner:
                context_info = inner["contextInfo"]
                break
        
        # Also check top-level contextInfo (some Evolution API versions)
        if not context_info:
            context_info = content.get("contextInfo")
        
        if not context_info or not isinstance(context_info, dict):
            return None
        
        ad_reply = context_info.get("externalAdReply")
        if not ad_reply or not isinstance(ad_reply, dict):
            return None
        
        _logger.info(f"CTWA ad context found: {json.dumps(ad_reply, default=str)[:300]}")
        return ad_reply

    def _handle_edited_message(self, conversation, edited_msg_id, new_content, msg_type):
        """Handle an edited message by updating the original message content.
        
        When a user edits a message, Evolution API sends a messages.upsert
        with isEdited=true and editedMessageId pointing to the original.
        """
        Message = request.env["bader.inbox.message"].sudo()
        original = Message.search([
            ("whatsapp_message_id", "=", edited_msg_id),
            ("conversation_id", "=", conversation.id),
        ], limit=1)
        
        if not original:
            _logger.warning(f"Edited message: original {edited_msg_id} not found in conv {conversation.id}")
            return _json_ok({"status": "original_not_found"})
        
        update_vals = {"is_edited": True}
        if new_content:
            update_vals["content"] = new_content
        
        original.write(update_vals)
        _logger.info(f"Message {original.id} edited: WA ID={edited_msg_id}, new text='{(new_content or '')[:50]}'")
        
        # Notify frontend via bus
        try:
            request.env["bus.bus"]._sendone(
                "bader_inbox", "bader_inbox_message_edited",
                {
                    "message_id": original.id,
                    "conversation_id": conversation.id,
                    "content": new_content,
                    "is_edited": True,
                }
            )
        except Exception:
            pass
        
        return _json_ok({"status": "edited"})

    def _extract_group_name_from_payload(self, data, msg_obj):
        """Try to extract the group name/subject from the webhook payload.
        
        Baileys/Evolution API may include group metadata in various locations
        depending on the version. This method checks all known locations.
        Returns the group subject string or None.
        """
        # Check in the root data object
        for root in (data, data.get("data", {})):
            if not isinstance(root, dict):
                continue
            # Direct subject field
            if root.get("subject"):
                return root["subject"]
            # verifiedBizName sometimes holds group name
            if root.get("verifiedBizName"):
                return root["verifiedBizName"]
            # groupMetadata sub-object
            gm = root.get("groupMetadata") or root.get("groupInfo") or {}
            if isinstance(gm, dict) and gm.get("subject"):
                return gm["subject"]
        
        # Check inside msg_obj
        if isinstance(msg_obj, dict):
            if msg_obj.get("verifiedBizName"):
                return msg_obj["verifiedBizName"]
            # Some versions include source with metadata
            source = msg_obj.get("source", {})
            if isinstance(source, dict):
                if source.get("verifiedBizName"):
                    return source["verifiedBizName"]
        
        return None

    def _parse_message_content(self, content):
        """Parse message content from API format
        
        The "content" / "message" field contains the actual message data:
        - Text: {"conversation": "texto"} or {"extendedTextMessage": {"text": "..."}}
        - Image: {"imageMessage": {"caption": "...", "mimetype": "...", "url": "..."}}
        - Audio: {"audioMessage": {...}}
        - Document: {"documentMessage": {"mimetype": "...", "fileName": "...", "url": "..."}}
        - Document with caption: {"documentWithCaptionMessage": {"message": {"documentMessage": {...}}}}
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
        
        if not isinstance(content, dict):
            _logger.warning(f"Unexpected content type: {type(content)}")
            return msg_type, str(content), media_info
        
        # Log content keys for debugging
        _logger.info(f"_parse_message_content keys: {list(content.keys())}")
        
        # Handle editedMessage wrapper (protocolMessage with editedMessage inside)
        if "protocolMessage" in content:
            proto = content["protocolMessage"]
            if isinstance(proto, dict):
                edited_msg = proto.get("editedMessage", {})
                if edited_msg and isinstance(edited_msg, dict):
                    # Recurse into the edited message content
                    return self._parse_message_content(edited_msg)
            _logger.info("Skipping protocolMessage (not an edit)")
            return msg_type, "", media_info
        
        if "conversation" in content:
            text = content["conversation"]
        elif "extendedTextMessage" in content:
            etm = content["extendedTextMessage"]
            text = etm.get("text", "") if isinstance(etm, dict) else str(etm)
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
            media_info = {
                "media_mimetype": doc.get("mimetype"),
                "media_filename": doc.get("fileName") or doc.get("filename"),
                "media_url": doc.get("url"),
            }
        elif "documentWithCaptionMessage" in content:
            # Baileys wraps documents with captions in this extra layer
            msg_type = "document"
            wrapper = content["documentWithCaptionMessage"]
            inner_msg = wrapper.get("message", {}) if isinstance(wrapper, dict) else {}
            doc = inner_msg.get("documentMessage", {})
            text = doc.get("caption", "")
            media_info = {
                "media_mimetype": doc.get("mimetype"),
                "media_filename": doc.get("fileName") or doc.get("filename"),
                "media_url": doc.get("url"),
            }
        elif "stickerMessage" in content:
            msg_type = "sticker"
            stk = content["stickerMessage"]
            media_info = {
                "media_mimetype": stk.get("mimetype", "image/webp"),
                "media_url": stk.get("url"),
                "is_animated": stk.get("isAnimated", False),
            }
        elif "lottieStickerMessage" in content:
            # Lottie animated stickers — unwrap to inner stickerMessage
            wrapper = content["lottieStickerMessage"]
            if isinstance(wrapper, dict) and "message" in wrapper:
                return self._parse_message_content(wrapper["message"])
            if isinstance(wrapper, dict) and "stickerMessage" in wrapper:
                return self._parse_message_content(wrapper)
            # Fallback: treat as sticker with available fields
            msg_type = "sticker"
            media_info = {
                "media_mimetype": wrapper.get("mimetype", "image/webp"),
                "media_url": wrapper.get("url"),
                "is_animated": True,
            }
        elif "stickerSentByMeMessage" in content:
            # Unwrap stickers sent by the user (wrapped by Evolution API)
            wrapper = content["stickerSentByMeMessage"]
            if isinstance(wrapper, dict) and "message" in wrapper:
                return self._parse_message_content(wrapper["message"])
            # Direct sticker data in wrapper
            if isinstance(wrapper, dict) and "stickerMessage" in wrapper:
                return self._parse_message_content(wrapper)
        elif "locationMessage" in content:
            msg_type = "location"
            loc = content["locationMessage"]
            media_info = {"latitude": loc.get("degreesLatitude"), "longitude": loc.get("degreesLongitude"), "location_name": loc.get("name")}
            text = loc.get("address", "")
        elif "reactionMessage" in content:
            msg_type = "reaction"
            reaction = content["reactionMessage"]
            text = reaction.get("text", "") if isinstance(reaction, dict) else str(reaction)
        elif "ephemeralMessage" in content:
            # Ephemeral (disappearing) messages - extract inner message
            ephemeral = content["ephemeralMessage"]
            if isinstance(ephemeral, dict) and "message" in ephemeral:
                return self._parse_message_content(ephemeral["message"])
        elif "viewOnceMessageV2" in content:
            # View once messages - extract inner message
            view_once = content["viewOnceMessageV2"]
            if isinstance(view_once, dict) and "message" in view_once:
                return self._parse_message_content(view_once["message"])
        elif "templateMessage" in content:
            # WhatsApp template messages (sent by businesses)
            tmpl = content["templateMessage"]
            if isinstance(tmpl, dict):
                # Try hydratedTemplate (most common in Baileys/Evolution API)
                hydrated = (
                    tmpl.get("hydratedTemplate")
                    or tmpl.get("hydratedFourRowTemplate")
                    or {}
                )
                if isinstance(hydrated, dict):
                    title = hydrated.get("hydratedTitleText", "")
                    body = hydrated.get("hydratedContentText", "")
                    footer = hydrated.get("hydratedFooterText", "")
                    text = "\n".join(filter(None, [title, body, footer]))
                # Some versions wrap the actual message inside
                if not text and "message" in tmpl:
                    return self._parse_message_content(tmpl["message"])
                # highlyStructuredMessage format
                if not text and "hydratedHsm" in tmpl:
                    hsm = tmpl["hydratedHsm"]
                    if isinstance(hsm, dict):
                        hydrated = hsm.get("hydratedTemplate", {})
                        if isinstance(hydrated, dict):
                            title = hydrated.get("hydratedTitleText", "")
                            body = hydrated.get("hydratedContentText", "")
                            text = "\n".join(filter(None, [title, body]))
                # Fallback: try body/text fields directly
                if not text:
                    text = tmpl.get("body") or tmpl.get("text") or tmpl.get("caption") or ""
            if not text:
                text = "[Template message]"
                _logger.warning(f"Could not extract text from templateMessage: {list(tmpl.keys()) if isinstance(tmpl, dict) else type(tmpl)}")
        elif "contactMessage" in content:
            msg_type = "contact"
            contact = content["contactMessage"]
            display_name = contact.get("displayName", "")
            vcard = contact.get("vcard", "")
            # Extract phone from vCard (TEL field)
            phone = ""
            if vcard:
                import re
                tel_match = re.search(r'TEL[^:]*:([\+\d\s\-]+)', vcard)
                if tel_match:
                    phone = tel_match.group(1).strip()
            # Human-readable text for preview + full metadata in media_info
            parts = []
            if display_name:
                parts.append(f"📇 {display_name}")
            if phone:
                parts.append(f"📞 {phone}")
            text = "\n".join(parts) if parts else "📇 Contacto compartido"
            media_info = {"displayName": display_name, "phone": phone, "vcard": vcard}
        elif "contactsArrayMessage" in content:
            msg_type = "contact"
            arr = content["contactsArrayMessage"]
            contacts = arr.get("contacts", [])
            parsed = []
            names = []
            for c in contacts:
                display_name = c.get("displayName", "")
                vcard = c.get("vcard", "")
                phone = ""
                if vcard:
                    import re
                    tel_match = re.search(r'TEL[^:]*:([\+\d\s\-]+)', vcard)
                    if tel_match:
                        phone = tel_match.group(1).strip()
                parsed.append({"displayName": display_name, "phone": phone})
                if display_name:
                    names.append(display_name)
            text = f"📇 {len(parsed)} contactos: {', '.join(names)}" if names else f"📇 {len(parsed)} contactos compartidos"
            media_info = {"contacts": parsed, "count": len(parsed)}
        else:
            # Unknown content type — log it for debugging
            _logger.warning(f"Unknown message content format. Keys: {list(content.keys())}")
            # Try to extract any text-like field as fallback
            for fallback_key in ("text", "body", "caption"):
                if fallback_key in content:
                    text = content[fallback_key]
                    break
        
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

            # Push channel status change to UI via bus
            try:
                request.env["bus.bus"]._sendone(
                    "bader_inbox", "bader_inbox_channel_update",
                    {
                        "channel_id": channel.id,
                        "state": new_state,
                        "phone": update_vals.get("phone"),
                        "phone_name": update_vals.get("phone_name"),
                    }
                )
            except Exception:
                pass

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
