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
        return {
            "url": params.get_param("bader_inbox.evolution_url", "https://whatsapp.odontowave.com"),
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

    def create_instance(self, instance_name):
        """Create new WhatsApp instance"""
        data = {"instanceName": instance_name}
        result = self._request("POST", "/instance/create", data)
        return {"success": "instance" in result or "instanceName" in result, **result}

    def delete_instance(self, instance_name):
        """Delete WhatsApp instance"""
        return self._request("DELETE", f"/instance/delete/{instance_name}")


    def get_qrcode(self, instance_name):
        """Get QR code for instance"""
        result = self._request("GET", f"/instance/connect/{instance_name}")
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
        data = {"webhookUrl": webhook_url}
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

    def send_media(self, instance_name, phone, media_type, media_url, filename=None, caption=None):
        """Send media message via URL"""
        endpoint_map = {
            "image": "/message/image",
            "audio": "/message/audio",
            "video": "/message/video",
            "document": "/message/document",
        }
        endpoint = endpoint_map.get(media_type, "/message/document")
        
        # Use URL-based endpoints per Evolution API docs
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

    def list_instances(self):
        """List all instances"""
        result = self._request("GET", "/instance/list")
        return result.get("instances", []) if isinstance(result, dict) else result
