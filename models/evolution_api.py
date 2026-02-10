# Copyright 2026 Bader Business
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import logging
import requests
from odoo import api, models

_logger = logging.getLogger(__name__)


class BaderInboxEvolutionAPI(models.AbstractModel):
    """Helper class for Evolution API communication"""
    
    _name = "bader.inbox.evolution_api"
    _description = "Evolution API Helper"

    def _get_config(self):
        """Get API configuration"""
        params = self.env["ir.config_parameter"].sudo()
        # Evolution API URL (Production)
        url = params.get_param(
            "bader_inbox.evolution_url", 
            "https://whatsapp.odontowave.com"
        )
        # Normalize URL - remove /api suffix if present (endpoints already include /api/)
        import re
        url = re.sub(r'/api/?$', '', url.rstrip('/'))
        return {
            "url": url,
            "key": params.get_param("bader_inbox.evolution_key", ""),
        }

    def _request(self, method, endpoint, data=None):
        """Make API request"""
        config = self._get_config()
        # Normalize URL and ensure /api prefix
        base_url = config['url'].rstrip('/')
        if not endpoint.startswith('/api'):
            endpoint = '/api' + endpoint
        url = f"{base_url}{endpoint}"
        
        headers = {
            "apikey": config["key"],
            "Content-Type": "application/json",
        }
        
        _logger.info(f"Evolution API: {method} {url}")
        
        try:
            response = requests.request(
                method, url, json=data, headers=headers, timeout=30
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            _logger.error(f"API request failed: {e}")
            return {"success": False, "error": str(e)}

    def create_instance(self, instance_name, webhook_url=None):
        """Create new WhatsApp instance with webhook configured"""
        data = {"instanceName": instance_name}
        # Include webhook in creation for Evolution API versions that support it
        if webhook_url:
            data["webhook"] = {
                "url": webhook_url,
                "enabled": True,
                "events": ["MESSAGES_UPSERT", "MESSAGES_UPDATE", "CONNECTION_UPDATE", "QRCODE_UPDATED"]
            }
        result = self._request("POST", "/instance/create", data)
        return {"success": "instance" in result or "instanceName" in result, **result}

    def delete_instance(self, instance_name):
        """Delete WhatsApp instance"""
        return self._request("DELETE", f"/instance/delete/{instance_name}")


    def get_qrcode(self, instance_name):
        """Get QR code for instance"""
        result = self._request("GET", f"/instance/qrcode/{instance_name}")
        # Handle different response formats
        qr = result.get("qrcode") or result.get("base64")
        if isinstance(qr, dict):
            qr = qr.get("base64") or qr.get("qrcode")
        # Strip data-uri prefix if present (Odoo image widget needs pure base64)
        if qr and qr.startswith("data:"):
            qr = qr.split(",", 1)[1] if "," in qr else qr
        return {"qrcode": qr, "status": result.get("status", "unknown")}


    def get_instance_status(self, instance_name):
        """Get instance connection status"""
        result = self._request("GET", f"/instance/status/{instance_name}")
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
        """Configure webhook for instance"""
        data = {
            "webhookUrl": webhook_url,
            "enabled": True,
            "events": ["MESSAGES_UPSERT", "MESSAGES_UPDATE", "CONNECTION_UPDATE", "QRCODE_UPDATED"]
        }
        return self._request("POST", f"/webhook/set/{instance_name}", data)

    def send_text(self, instance_name, phone, text):
        """Send text message"""
        data = {
            "number": phone,
            "text": text,
        }
        result = self._request("POST", f"/message/text/{instance_name}", data)
        return {
            "success": "key" in result or result.get("status") == "sent",
            "message_id": result.get("key", {}).get("id") if isinstance(result.get("key"), dict) else result.get("messageId"),
        }

    def send_media(self, instance_name, phone, media_type, media_url=None, media_data=None, filename=None, caption=None):
        """Send media message via URL or Base64"""
        
        # Priority 1: Send Base64 if available (New generic endpoint /message/sendMedia)
        if media_data:
            # Evolution API v2 generic media endpoint
            endpoint = "/message/sendMedia"
            
            # Ensure base64 prefix is stripped
            if "," in media_data:
                media_data = media_data.split(",")[1]
            
            data = {
                "number": phone,
                "mediatype": media_type,
                "media": media_data,
                "fileName": filename or "file",
                "caption": caption or "",
            }
            # mimetype is optional if API can detect, but better to pass if we knew it
            # bader_inbox Message model stores media_mimetype
            
            result = self._request("POST", f"{endpoint}/{instance_name}", data)
            return {
                "success": "key" in result or result.get("status") == "sent",
                "message_id": result.get("key", {}).get("id") if isinstance(result.get("key"), dict) else result.get("messageId"),
            }

        # Priority 2: Send URL (specific endpoints)
        endpoint_map = {
            "image": "/message/image",
            "audio": "/message/audio",
            "video": "/message/video",
            "document": "/message/document",
        }
        endpoint = endpoint_map.get(media_type, "/message/document")
        
        url_key = f"{media_type}Url" if media_type != "document" else "documentUrl"
        data = {
            "number": phone,
            url_key: media_url,
        }
        if caption:
            data["caption"] = caption
        if filename:
            data["fileName"] = filename
        
        result = self._request("POST", f"{endpoint}/{instance_name}", data)
        return {
            "success": "key" in result or result.get("status") == "sent",
            "message_id": result.get("key", {}).get("id") if isinstance(result.get("key"), dict) else result.get("messageId"),
        }

    def get_base64_from_media(self, instance_name, message_key):
        """Download media content as base64 from Evolution API
        
        Args:
            instance_name: Evolution API instance name
            message_key: The message key dict with remoteJid, id, fromMe
        
        Returns:
            dict with base64 data and mimetype, or None on failure
        """
        try:
            data = {"key": message_key}
            result = self._request(
                "POST", 
                f"/chat/getBase64FromMediaMessage/{instance_name}",
                data
            )
            if result and isinstance(result, dict):
                base64_data = result.get("base64") or result.get("data")
                mimetype = result.get("mimetype") or result.get("mediaType", "")
                if base64_data:
                    # Strip data URI prefix if present
                    if "," in base64_data and base64_data.startswith("data:"):
                        base64_data = base64_data.split(",", 1)[1]
                    return {"base64": base64_data, "mimetype": mimetype}
            _logger.warning(f"No base64 data returned for media: {result}")
            return None
        except Exception as e:
            _logger.error(f"Error downloading media: {e}")
            return None

    def list_instances(self):
        """List all instances"""
        result = self._request("GET", "/instance/list")
        return result.get("instances", []) if isinstance(result, dict) else result
