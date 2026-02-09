# Copyright 2026 Bader Business
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import logging
from odoo import api, fields, models, _
from odoo.exceptions import UserError

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
            
            # Create instance
            result = api.create_instance(instance_name)
            if not result.get("success"):
                raise UserError(_("Failed to create instance: %s") % result.get("error"))
            
            # Set webhook
            base_url = self.env["ir.config_parameter"].sudo().get_param("web.base.url")
            webhook_url = f"{base_url}/bader-inbox/webhook/{self.id}"
            self.webhook_url = webhook_url
            
            api.set_webhook(instance_name, webhook_url)
            
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
