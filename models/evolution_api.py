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
            "url": params.get_param("bader_inbox.evolution_url", "http://localhost:8080"),
            "key": params.get_param("bader_inbox.evolution_key", ""),
        }

    def _request(self, method, endpoint, data=None):
        """Make API request"""
        config = self._get_config()
        url = f"{config['url'].rstrip('/')}/{endpoint.lstrip('/')}"
        headers = {
            "apikey": config["key"],
            "Content-Type": "application/json",
        }
        
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
        data = {
            "instanceName": instance_name,
            "qrcode": True,
            "integration": "WHATSAPP-BAILEYS",
        }
        result = self._request("POST", "/instance/create", data)
        return {"success": "instance" in result, **result}

    def delete_instance(self, instance_name):
        """Delete WhatsApp instance"""
        return self._request("DELETE", f"/instance/delete/{instance_name}")

    def get_qrcode(self, instance_name):
        """Get QR code for instance"""
        result = self._request("GET", f"/instance/qrcode/{instance_name}")
        qr = result.get("qrcode", {})
        if isinstance(qr, dict):
            return {"qrcode": qr.get("base64")}
        return {"qrcode": qr}

    def get_instance_status(self, instance_name):
        """Get instance connection status"""
        result = self._request("GET", f"/instance/connectionState/{instance_name}")
        state = result.get("instance", {})
        return {
            "connected": state.get("state") == "open",
            "phone": state.get("wuid", "").replace("@s.whatsapp.net", ""),
            "name": state.get("profileName"),
        }

    def set_webhook(self, instance_name, webhook_url):
        """Configure webhook for instance"""
        data = {
            "webhook": {
                "enabled": True,
                "url": webhook_url,
                "events": [
                    "MESSAGES_UPSERT",
                    "MESSAGES_UPDATE", 
                    "CONNECTION_UPDATE",
                    "QRCODE_UPDATED",
                ],
            }
        }
        return self._request("POST", f"/webhook/set/{instance_name}", data)

    def send_text(self, instance_name, phone, text):
        """Send text message"""
        data = {
            "number": phone,
            "text": text,
        }
        result = self._request("POST", f"/message/sendText/{instance_name}", data)
        return {
            "success": "key" in result,
            "message_id": result.get("key", {}).get("id"),
        }

    def send_media(self, instance_name, phone, media_type, media_data, filename=None, caption=None):
        """Send media message"""
        endpoint_map = {
            "image": "sendImage",
            "audio": "sendWhatsAppAudio",
            "video": "sendVideo",
            "document": "sendDocument",
        }
        endpoint = endpoint_map.get(media_type, "sendDocument")
        
        data = {
            "number": phone,
            "media": f"data:application/octet-stream;base64,{media_data}",
        }
        if caption:
            data["caption"] = caption
        if filename:
            data["fileName"] = filename
        
        result = self._request("POST", f"/message/{endpoint}/{instance_name}", data)
        return {
            "success": "key" in result,
            "message_id": result.get("key", {}).get("id"),
        }
