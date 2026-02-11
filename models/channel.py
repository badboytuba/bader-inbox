# Copyright 2026 Bader Business
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import logging
from datetime import timedelta
from odoo import api, fields, models, _
from odoo.exceptions import UserError
import uuid

_logger = logging.getLogger(__name__)


class BaderInboxChannel(models.Model):
    """WhatsApp Channel - represents a connected WhatsApp number"""
    
    _name = "bader.inbox.channel"
    _description = "Bader Inbox Channel"
    _order = "name"

    name = fields.Char(string="Channel Name", required=True)
    phone = fields.Char(string="Phone Number", readonly=True)
    phone_name = fields.Char(string="WhatsApp Name", readonly=True)
    
    state = fields.Selection([
        ("draft", "Draft"),
        ("connecting", "Connecting"),
        ("qr_ready", "QR Ready"),
        ("connected", "Connected"),
        ("disconnected", "Disconnected"),
        ("error", "Error"),
    ], default="draft", string="Status")
    
    # Evolution API
    evolution_instance_name = fields.Char(string="Instance Name", readonly=True)
    qrcode_base64 = fields.Text(string="QR Code", readonly=True)
    
    # Webhook
    webhook_url = fields.Char(string="Webhook URL", readonly=True)
    webhook_token = fields.Char(string="Webhook Token", readonly=True, copy=False)
    
    # Health monitoring
    last_health_check = fields.Datetime(string="Last Health Check", readonly=True)
    health_status = fields.Selection([
        ("ok", "Healthy"),
        ("warning", "Warning"),
        ("error", "Error"),
    ], string="Health", default="ok", readonly=True)
    reconnect_attempts = fields.Integer(string="Reconnect Attempts", default=0, readonly=True)
    
    # Relations
    conversation_ids = fields.One2many(
        "bader.inbox.conversation", "channel_id", string="Conversations"
    )
    conversation_count = fields.Integer(
        compute="_compute_conversation_count", string="Conversations"
    )
    
    # Company
    company_id = fields.Many2one(
        "res.company", string="Company",
        default=lambda self: self.env.company
    )
    
    @api.depends("conversation_ids")
    def _compute_conversation_count(self):
        for rec in self:
            rec.conversation_count = len(rec.conversation_ids)

    def action_connect(self):
        """Start connection process - create instance and get QR"""
        self.ensure_one()
        try:
            api = self.env["bader.inbox.evolution_api"]
            
            # Generate instance name
            instance_name = f"bader_{self.id}_{self.name.lower().replace(' ', '_')}"
            self.evolution_instance_name = instance_name
            
            # Generate webhook URL BEFORE creating instance
            base_url = self.env["ir.config_parameter"].sudo().get_param("web.base.url")
            
            # Generate or reuse token
            if not self.webhook_token:
                self.webhook_token = str(uuid.uuid4())
                
            webhook_url = f"{base_url}/bader-inbox/webhook/{self.id}/{self.webhook_token}"
            self.webhook_url = webhook_url
            
            # Create instance WITH webhook configured
            result = api.create_instance(instance_name, webhook_url=webhook_url)
            if not result.get("success"):
                raise UserError(_("Failed to create instance: %s") % result.get("error"))
            
            # Also try set_webhook as fallback for older API versions
            try:
                api.set_webhook(instance_name, webhook_url)
            except Exception:
                pass  # Ignore - webhook was already set during creation
            
            # Get QR code
            qr_result = api.get_qrcode(instance_name)
            if qr_result.get("qrcode"):
                self.qrcode_base64 = qr_result["qrcode"]
                self.state = "qr_ready"
            else:
                self.state = "connecting"
            
            return True
            
        except Exception as e:
            _logger.error(f"Connection error: {e}")
            self.state = "error"
            raise UserError(_("Connection failed: %s") % str(e))

    def action_disconnect(self):
        """Disconnect and delete instance"""
        self.ensure_one()
        try:
            api = self.env["bader.inbox.evolution_api"]
            api.delete_instance(self.evolution_instance_name)
            self.state = "disconnected"
            self.qrcode_base64 = False
        except Exception as e:
            _logger.warning(f"Disconnect error: {e}")
            self.state = "disconnected"

    def action_refresh_qr(self):
        """Refresh QR code"""
        self.ensure_one()
        if self.evolution_instance_name:
            api = self.env["bader.inbox.evolution_api"]
            qr_result = api.get_qrcode(self.evolution_instance_name)
            if qr_result.get("qrcode"):
                self.qrcode_base64 = qr_result["qrcode"]
                self.state = "qr_ready"

    def action_check_status(self):
        """Check connection status"""
        self.ensure_one()
        if self.evolution_instance_name:
            api = self.env["bader.inbox.evolution_api"]
            status = api.get_instance_status(self.evolution_instance_name)
            if status.get("connected"):
                self.state = "connected"
                if status.get("phone"):
                    self.phone = status["phone"]
                if status.get("name"):
                    self.phone_name = status["name"]
            else:
                self.state = "disconnected"

    # ── Health Check & Auto-Reconnect ──────────────────────────────

    @api.model
    def cron_health_check(self):
        """Cron job: verify Evolution API instances and auto-reconnect.
        
        Called every 5 minutes. For each channel that should be connected,
        checks if the Evolution API instance still exists (it may have been
        lost after an API server restart). If lost, recreates the instance
        and reconfigures the webhook automatically.
        """
        # Channels that SHOULD be connected
        channels = self.search([
            ("state", "in", ["connected", "connecting", "qr_ready"]),
            ("evolution_instance_name", "!=", False),
        ])
        
        if not channels:
            return
        
        api = self.env["bader.inbox.evolution_api"]
        now = fields.Datetime.now()
        
        for channel in channels:
            try:
                instance_name = channel.evolution_instance_name
                webhook_url = channel.webhook_url
                
                if not instance_name or not webhook_url:
                    continue
                
                # Use ensure_instance to check and recreate if needed
                result = api.ensure_instance(instance_name, webhook_url)
                
                if result.get("error"):
                    _logger.warning(
                        f"Health check failed for channel {channel.name} "
                        f"(#{channel.id}): {result['error']}"
                    )
                    channel.write({
                        "last_health_check": now,
                        "health_status": "error",
                        "reconnect_attempts": channel.reconnect_attempts + 1,
                    })
                    continue
                
                if result.get("created"):
                    # Instance was recreated — channel needs QR scan again
                    _logger.info(
                        f"Auto-recreated instance for channel {channel.name} "
                        f"(#{channel.id}). User must scan QR again."
                    )
                    channel.write({
                        "state": "connecting",
                        "last_health_check": now,
                        "health_status": "warning",
                        "reconnect_attempts": channel.reconnect_attempts + 1,
                        "qrcode_base64": False,
                    })
                else:
                    # Instance exists and webhook is configured — all good
                    channel.write({
                        "last_health_check": now,
                        "health_status": "ok",
                        "reconnect_attempts": 0,
                    })
                    
            except Exception as e:
                _logger.error(
                    f"Health check error for channel {channel.name}: {e}",
                    exc_info=True
                )
                channel.write({
                    "last_health_check": now,
                    "health_status": "error",
                })
        
        # Commit after all channels are processed
        self.env.cr.commit()

    def action_reconnect(self):
        """Manual reconnect: recreate instance and reconfigure webhook."""
        self.ensure_one()
        
        if not self.evolution_instance_name:
            # No instance name yet — use normal connect flow
            return self.action_connect()
        
        api = self.env["bader.inbox.evolution_api"]
        
        # Try to delete old instance (ignore errors)
        try:
            api.delete_instance(self.evolution_instance_name)
        except Exception:
            pass
        
        # Recreate
        try:
            result = api.create_instance(
                self.evolution_instance_name,
                webhook_url=self.webhook_url
            )
            if result.get("success") is False:
                raise UserError(
                    _("Failed to recreate instance: %s") % result.get("error")
                )
            
            # Set webhook
            try:
                api.set_webhook(self.evolution_instance_name, self.webhook_url)
            except Exception:
                pass
            
            # Get QR code
            qr_result = api.get_qrcode(self.evolution_instance_name)
            if qr_result.get("qrcode"):
                self.qrcode_base64 = qr_result["qrcode"]
                self.state = "qr_ready"
            else:
                self.state = "connecting"
            
            self.reconnect_attempts = 0
            self.health_status = "ok"
            
        except Exception as e:
            _logger.error(f"Manual reconnect error: {e}")
            self.state = "error"
            raise UserError(_("Reconnect failed: %s") % str(e))
