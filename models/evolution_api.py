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
    - Events are lowercase: messages.upsert, connection.update, qrcode.updated
    - No generic /message/sendMedia — use /message/image, /message/audio, etc.
    - No /chat/getBase64FromMediaMessage — download media from URL in webhook payload
    - No messages.update event — status tracking not supported by this API
    """
    
    _name = "bader.inbox.evolution_api"
    _description = "WhatsApp API Helper (Baileys)"

    def _get_config(self):
        """Get API configuration"""
        params = self.env["ir.config_parameter"].sudo()
        # Evolution API URL (Production)
        url = params.get_param(
            "bader_inbox.evolution_url", 
            "https://whatsapp.odontowave.com"
        )
        # Normalize URL - remove /api suffix if present (endpoints already include /api/)
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
        # Include webhook in creation (lowercase event names for Baileys API)
        if webhook_url:
            data["webhook"] = {
                "url": webhook_url,
                "enabled": True,
                "events": ["messages.upsert", "connection.update", "qrcode.updated"]
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
        """Configure webhook for instance (lowercase event names for Baileys API)"""
        data = {
            "webhookUrl": webhook_url,
            "enabled": True,
            "events": ["messages.upsert", "connection.update", "qrcode.updated"]
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
        """Send media message via type-specific endpoints.
        
        This API uses separate endpoints per media type:
        - /message/image/{instance}
        - /message/audio/{instance}
        - /message/video/{instance}
        - /message/document/{instance}
        """
        endpoint_map = {
            "image": "/message/image",
            "audio": "/message/audio",
            "video": "/message/video",
            "document": "/message/document",
            "sticker": "/message/image",  # fallback stickers to image
        }
        endpoint = endpoint_map.get(media_type, "/message/document")
        
        data = {"number": phone}
        
        if media_data:
            # Send as base64
            if "," in media_data:
                media_data = media_data.split(",")[1]
            data["media"] = media_data
        elif media_url:
            # Send as URL — use type-specific URL key
            url_key = f"{media_type}Url" if media_type != "document" else "documentUrl"
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

    def instance_exists(self, instance_name):
        """Check if an instance exists on Evolution API.
        
        Uses the webhook/find endpoint as a lightweight probe since
        connectionState is not available in all API versions.
        Returns True if instance responds, False if 'not found' or error.
        """
        try:
            result = self._request("GET", f"/webhook/find/{instance_name}")
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
        result = self._request("GET", "/instance/list")
        return result.get("instances", []) if isinstance(result, dict) else result
