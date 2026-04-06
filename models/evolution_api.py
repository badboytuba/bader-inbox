# Copyright 2026 Bader Business
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import logging
import re
import requests
from odoo import api, models

_logger = logging.getLogger(__name__)


class BaderInboxEvolutionAPI(models.AbstractModel):
    """Helper class for WhatsApp API communication (Custom Baileys-based API)
    
    NOTE: This is NOT the official Evolution API v1/v2.
    It's a custom Baileys-based API with different endpoints.
    - Events are lowercase: messages.upsert, messages.update, connection.update, qrcode.updated
    - Media: /message/image, /message/audio, etc. (accepts base64 via 'media' field or URL)
    - Download media: /message/downloadMedia/{instance} (POST with key + content from webhook)
    - Instance status: returns 404 if not found, 503 if disconnected
    """
    
    _name = "bader.inbox.evolution_api"
    _description = "WhatsApp API Helper (Baileys)"

    def _get_config(self):
        """Get API configuration"""
        params = self.env["ir.config_parameter"].sudo()
        # Evolution API URL (Production)
        url = params.get_param(
            "bader_inbox.evolution_url",
            "https://whatsapp.bader.es"
        )
        # Normalize URL - remove /api suffix if present (all endpoints include /api/ prefix)
        url = re.sub(r'/api/?$', '', url.rstrip('/'))
        return {
            "url": url,
            "key": params.get_param("bader_inbox.evolution_key", ""),
        }

    def _request(self, method, endpoint, data=None, params=None, timeout=30):
        """Make API request"""
        config = self._get_config()
        base_url = config['url'].rstrip('/')
        # Ensure endpoint starts with /
        if not endpoint.startswith('/'):
            endpoint = '/' + endpoint
        url = f"{base_url}{endpoint}"

        headers = {
            "apikey": config["key"],
            "Content-Type": "application/json",
        }

        _logger.info(f"Evolution API: {method} {url}")

        try:
            response = requests.request(
                method, url, json=data, headers=headers, timeout=timeout,
                params=params,
            )
            response.raise_for_status()
            # Handle empty responses (some endpoints return 200 with no body)
            if not response.content or not response.text.strip():
                return {}
            try:
                return response.json()
            except ValueError:
                _logger.warning(f"Non-JSON response from {url}: {response.text[:200]}")
                return {}
        except requests.exceptions.RequestException as e:
            _logger.error(f"API request failed: {e}")
            return {"success": False, "error": str(e)}

    def create_instance(self, instance_name, webhook_url=None):
        """Create new WhatsApp instance with webhook configured"""
        data = {"instanceName": instance_name}
        # Include webhook in creation (lowercase event names for Baileys API)
        if webhook_url:
            data["webhook"] = {
                "url": webhook_url,
                "enabled": True,
                "events": ["messages.upsert", "messages.update", "connection.update", "qrcode.updated"]
            }
        result = self._request("POST", "/api/instance/create", data, timeout=90)
        return {"success": "instance" in result or "instanceName" in result, **result}

    def delete_instance(self, instance_name):
        """Delete WhatsApp instance"""
        return self._request("DELETE", f"/api/instance/delete/{instance_name}", timeout=60)


    def get_qrcode(self, instance_name):
        """Get QR code for instance"""
        result = self._request("GET", f"/api/instance/qrcode/{instance_name}")
        # Handle different response formats
        qr = result.get("qrcode") or result.get("base64")
        if isinstance(qr, dict):
            qr = qr.get("base64") or qr.get("qrcode")
        # Strip data-uri prefix if present (Odoo image widget needs pure base64)
        if qr and qr.startswith("data:"):
            qr = qr.split(",", 1)[1] if "," in qr else qr
        return {"qrcode": qr, "status": result.get("status", "unknown")}


    def get_instance_status(self, instance_name):
        """Get instance connection status.
        
        API returns:
        - 200 with status info if connected
        - 404 if instance doesn't exist
        - 503 if instance exists but is disconnected
        """
        result = self._request("GET", f"/api/instance/status/{instance_name}")
        # Handle error responses (404, 503)
        if result.get("success") is False:
            error_str = str(result.get("error", "")).lower()
            if "404" in error_str:
                return {"connected": False, "status": "not_found", "phone": "", "name": ""}
            elif "503" in error_str:
                return {"connected": False, "status": "disconnected", "phone": "", "name": ""}
            return {"connected": False, "status": "error", "phone": "", "name": ""}
        # Extract connection info
        connected = result.get("status") == "connected" or result.get("state") == "open"
        instance = result.get("instance", {})
        return {
            "connected": connected,
            "status": result.get("status", "unknown"),
            "phone": instance.get("wuid", "").replace("@s.whatsapp.net", "") if instance else "",
            "name": instance.get("profileName", ""),
        }

    def set_webhook(self, instance_name, webhook_url):
        """Configure webhook for instance (includes group events)"""
        data = {
            "webhookUrl": webhook_url,
            "enabled": True,
            "events": [
                "messages.upsert", "messages.update",
                "connection.update", "qrcode.updated",
                "groups.upsert", "groups.update",
                "group-participants.update",
            ]
        }
        return self._request("POST", f"/api/webhook/set/{instance_name}", data)

    def send_text(self, instance_name, phone, text):
        """Send text message"""
        data = {
            "number": phone,
            "text": text,
        }
        result = self._request("POST", f"/api/message/text/{instance_name}", data)
        return {
            "success": "key" in result or result.get("status") == "sent",
            "message_id": result.get("key", {}).get("id") if isinstance(result.get("key"), dict) else result.get("messageId"),
        }

    def send_media(self, instance_name, phone, media_type, media_url=None, media_data=None, filename=None, caption=None):
        """Send media message via type-specific endpoints.
        
        This API uses separate endpoints per media type:
        - /message/image/{instance}
        - /message/audio/{instance}
        - /message/video/{instance}
        - /message/document/{instance}
        
        The API expects: {"number": phone, "{type}Url": url_or_data_uri}
        For base64, use data URI format: "data:{mimetype};base64,{data}"
        """
        endpoint_map = {
            "image": "/api/message/image",
            "audio": "/api/message/audio",
            "video": "/api/message/video",
            "document": "/api/message/document",
            "sticker": "/api/message/image",  # fallback stickers to image
        }
        # Map media type to the URL key the API expects
        url_key_map = {
            "image": "imageUrl",
            "audio": "audioUrl",
            "video": "videoUrl",
            "document": "documentUrl",
            "sticker": "imageUrl",
        }
        # Map media type to default mimetype for data URI
        mimetype_map = {
            "image": "image/png",
            "audio": "audio/ogg",
            "video": "video/mp4",
            "document": "application/octet-stream",
            "sticker": "image/webp",
        }
        
        endpoint = endpoint_map.get(media_type, "/api/message/document")
        url_key = url_key_map.get(media_type, "documentUrl")
        
        data = {"number": phone}
        
        if media_data:
            # Send as base64 data URI — API requires {type}Url key
            # Strip existing data URI prefix if present
            if media_data.startswith("data:") and "," in media_data:
                # Already has data URI prefix, use as-is
                data[url_key] = media_data
            else:
                # Add data URI prefix
                mimetype = mimetype_map.get(media_type, "application/octet-stream")
                # Try to detect mimetype from filename
                if filename:
                    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
                    ext_mime = {
                        "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                        "gif": "image/gif", "webp": "image/webp",
                        "mp3": "audio/mpeg", "ogg": "audio/ogg", "wav": "audio/wav",
                        "mp4": "video/mp4", "avi": "video/avi",
                        "pdf": "application/pdf", "doc": "application/msword",
                        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    }
                    mimetype = ext_mime.get(ext, mimetype)
                data[url_key] = f"data:{mimetype};base64,{media_data}"
        elif media_url:
            data[url_key] = media_url
        
        if caption:
            data["caption"] = caption
        if filename:
            data["fileName"] = filename
        
        result = self._request("POST", f"{endpoint}/{instance_name}", data)
        return {
            "success": "key" in result or result.get("status") == "sent",
            "message_id": result.get("key", {}).get("id") if isinstance(result.get("key"), dict) else result.get("messageId"),
        }

    def download_media(self, instance_name, message_key, message_content):
        """Download media from a received message using the API endpoint.
        
        POST /api/message/downloadMedia/{instance}
        Body: {"key": {...}, "content": {...}}
        Returns base64 encoded media data.
        """
        # Unwrap Evolution API wrapper types to get the inner message content
        content = self._unwrap_message_content(message_content) if isinstance(message_content, dict) else message_content
        
        data = {
            "messageKey": message_key,
            "messageContent": content,
        }
        try:
            result = self._request("POST", f"/api/message/downloadMedia/{instance_name}", data)
            if isinstance(result, dict):
                base64_data = result.get("base64") or result.get("data") or result.get("media")
                mimetype = result.get("mimetype") or result.get("mediaType", "")
                if base64_data:
                    # Strip data URI prefix if present
                    if base64_data.startswith("data:") and "," in base64_data:
                        base64_data = base64_data.split(",", 1)[1]
                    return {"base64": base64_data, "mimetype": mimetype}
            _logger.warning(f"No media data returned: {result}")
            return None
        except Exception as e:
            _logger.error(f"Error downloading media: {e}")
            return None

    @staticmethod
    def _unwrap_message_content(content):
        """Unwrap Evolution API wrapper types to get the inner message.
        
        Handles: lottieStickerMessage, stickerSentByMeMessage,
        documentWithCaptionMessage, ephemeralMessage, messageContextInfo.
        """
        if not isinstance(content, dict):
            return content
        
        # Remove messageContextInfo wrapper (metadata, not needed for download)
        # The actual message type key is the OTHER key besides messageContextInfo
        keys = [k for k in content.keys() if k != "messageContextInfo"]
        
        # If we have a known wrapper type, unwrap it
        wrapper_types = {
            "lottieStickerMessage", "stickerSentByMeMessage",
            "documentWithCaptionMessage", "ephemeralMessage",
        }
        for key in keys:
            if key in wrapper_types:
                inner = content[key]
                if isinstance(inner, dict) and "message" in inner:
                    return inner["message"]
                return inner
        
        # If only messageContextInfo remains alongside a normal message type, return without it
        if "messageContextInfo" in content and keys:
            # Return content without messageContextInfo
            return {k: v for k, v in content.items() if k != "messageContextInfo"}
        
        return content

    def instance_exists(self, instance_name):
        """Check if an instance exists on Evolution API.
        
        Uses the webhook/find endpoint as a lightweight probe since
        connectionState is not available in all API versions.
        Returns True if instance responds, False if 'not found' or error.
        """
        try:
            result = self._request("GET", f"/api/webhook/find/{instance_name}")
            if isinstance(result, dict) and result.get("error"):
                error_msg = str(result.get("error", "")) + str(result.get("message", ""))
                if "not found" in error_msg.lower() or "does not exist" in error_msg.lower():
                    return False
            return True
        except Exception:
            return False

    def ensure_instance(self, instance_name, webhook_url):
        """Ensure instance exists and webhook is configured.
        
        If instance doesn't exist, creates it and sets webhook.
        Returns dict with 'exists', 'created', 'webhook_set' flags.
        """
        result = {"exists": False, "created": False, "webhook_set": False, "error": None}
        
        try:
            # Check if instance already exists
            if self.instance_exists(instance_name):
                result["exists"] = True
                # Re-set webhook (it may have been lost after API restart)
                try:
                    self.set_webhook(instance_name, webhook_url)
                    result["webhook_set"] = True
                except Exception as e:
                    _logger.warning(f"Failed to re-set webhook for {instance_name}: {e}")
                return result
            
            # Instance doesn't exist — recreate it
            _logger.info(f"Instance {instance_name} not found, recreating...")
            create_result = self.create_instance(instance_name, webhook_url=webhook_url)
            if create_result.get("success") is False:
                result["error"] = create_result.get("error", "Unknown create error")
                return result
            
            result["created"] = True
            result["exists"] = True
            
            # Also set webhook explicitly as fallback
            try:
                self.set_webhook(instance_name, webhook_url)
                result["webhook_set"] = True
            except Exception:
                pass  # Webhook was already set during creation
            
            return result
            
        except Exception as e:
            result["error"] = str(e)
            _logger.error(f"ensure_instance error for {instance_name}: {e}")
            return result

    def list_instances(self):
        """List all instances"""
        result = self._request("GET", "/api/instance/list")
        return result.get("instances", []) if isinstance(result, dict) else result

    def get_profile_picture(self, instance_name, phone):
        """Get WhatsApp profile picture URL for a phone number.

        GET /api/instance/profilePicture/{instanceName}?number={phone}
        Returns: {"exists": true, "profilePictureUrl": "https://..."} or {"exists": false}
        """
        clean_phone = re.sub(r'[^\d]', '', str(phone))
        try:
            config = self._get_config()
            base_url = config['url'].rstrip('/')
            url = f"{base_url}/api/instance/profilePicture/{instance_name}?number={clean_phone}"
            headers = {"apikey": config["key"]}

            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            data = response.json()

            if data.get("exists") and data.get("profilePictureUrl"):
                return data["profilePictureUrl"]
            return None
        except Exception as e:
            _logger.debug(f"Profile picture fetch failed for {phone}: {e}")
            return None

    def fetch_contact_name(self, instance_name, phone):
        """Fetch WhatsApp profile name for a phone number.

        Uses POST /chat/findContacts/{instance} with the contact's JID.
        Returns the pushName if available, None otherwise.
        """
        clean_phone = re.sub(r'[^\d]', '', str(phone))
        if not clean_phone:
            return None
        try:
            jid = f"{clean_phone}@s.whatsapp.net"
            result = self._request(
                "POST",
                f"/api/chat/findContacts/{instance_name}",
                {"where": {"id": jid}}
            )
            # Response can be a list or dict with contact info
            contacts = result if isinstance(result, list) else [result] if isinstance(result, dict) else []
            for contact in contacts:
                if not isinstance(contact, dict):
                    continue
                name = contact.get("pushName") or contact.get("name") or contact.get("notify") or ""
                if name and name.strip():
                    return name.strip()
            return None
        except Exception as e:
            _logger.debug(f"Contact name fetch failed for {phone}: {e}")
            return None

    def fetch_pending_messages(self, instance_name):
        """Fetch messages that were queued while Bader Inbox was offline.
        
        GET /api/messages/pending/:instanceName
        Returns list of pending message objects.
        """
        try:
            result = self._request("GET", f"/api/messages/pending/{instance_name}")
            if isinstance(result, list):
                return result
            if isinstance(result, dict) and result.get("messages"):
                return result["messages"]
            if isinstance(result, dict) and result.get("error"):
                _logger.warning(f"Pending messages error: {result['error']}")
                return []
            return result if isinstance(result, list) else []
        except Exception as e:
            _logger.error(f"Failed to fetch pending messages: {e}")
            return []

    def acknowledge_messages(self, message_ids):
        """Acknowledge receipt of pending messages so they're marked as delivered.
        
        POST /api/messages/ack
        Body: {"ids": [1, 2, 3]}
        """
        if not message_ids:
            return True
        try:
            result = self._request("POST", "/api/messages/ack", {"ids": message_ids})
            _logger.info(f"Acknowledged {len(message_ids)} pending messages")
            return True
        except Exception as e:
            _logger.error(f"Failed to acknowledge messages: {e}")
            return False

    # ── Group API Methods ──────────────────────────────────────────────

    def fetch_group_info(self, instance_name, group_jid):
        """Fetch information about a specific group (subject, description, picture).
        
        Tries the new /api/instance/groupMetadata endpoint first,
        then falls back to /group/fetchAllGroups.
        Returns dict with group info or None.
        """
        try:
            # Try new dedicated endpoint first
            result = self._request(
                "GET",
                f"/api/instance/groupMetadata/{instance_name}",
                params={"groupJid": group_jid}
            )
            if isinstance(result, dict) and (result.get("subject") or result.get("id")):
                return result
            # If result is a dict with error, fall through to legacy
        except Exception:
            pass
        
        # Fallback: fetch all groups and filter
        try:
            result = self._request(
                "GET",
                f"/api/group/fetchAllGroups/{instance_name}",
                params={"getParticipants": "false"}
            )
            groups = result if isinstance(result, list) else []
            for g in groups:
                if not isinstance(g, dict):
                    continue
                gid = g.get("id") or g.get("jid") or ""
                if gid == group_jid or str(gid).startswith(group_jid.split("@")[0]):
                    return g
        except Exception as e:
            _logger.debug(f"Group info fetch failed for {group_jid}: {e}")
        return None

    def fetch_group_participants(self, instance_name, group_jid):
        """Fetch the member list of a specific group.
        
        Uses GET /group/participants/{instance}?groupJid={jid}
        Returns a list of participant dicts or empty list.
        """
        try:
            result = self._request(
                "GET",
                f"/api/group/participants/{instance_name}",
                params={"groupJid": group_jid}
            )
            if isinstance(result, list):
                return result
            if isinstance(result, dict):
                return result.get("participants") or result.get("members") or []
            return []
        except Exception as e:
            _logger.debug(f"Group participants fetch failed for {group_jid}: {e}")
            return []

    def fetch_all_groups(self, instance_name):
        """Fetch all groups for batch sync.
        
        Tries new /api/instance/groups endpoint first,
        then falls back to /group/fetchAllGroups.
        """
        try:
            # Try new endpoint
            result = self._request(
                "GET",
                f"/api/instance/groups/{instance_name}"
            )
            if isinstance(result, list) and len(result) > 0:
                return result
        except Exception:
            pass
        
        # Fallback to legacy
        try:
            result = self._request(
                "GET",
                f"/api/group/fetchAllGroups/{instance_name}",
                params={"getParticipants": "true"}
            )
            return result if isinstance(result, list) else []
        except Exception as e:
            _logger.debug(f"Fetch all groups failed for {instance_name}: {e}")
            return []
