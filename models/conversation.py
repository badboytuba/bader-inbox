# Copyright 2026 Bader Business
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import logging
from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)


class BaderInboxConversation(models.Model):
    """Conversation with a contact"""
    
    _name = "bader.inbox.conversation"
    _description = "Bader Inbox Conversation"
    _order = "last_message_date desc"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _rec_name = "computed_name"

    computed_name = fields.Char(compute="_compute_name", store=True, string="Name")
    
    # Contact info
    phone = fields.Char(string="Phone", required=True, index=True)
    whatsapp_id = fields.Char(string="WhatsApp ID", index=True)
    contact_name = fields.Char(string="Contact Name")
    
    # Channel
    channel_id = fields.Many2one(
        "bader.inbox.channel", string="Channel",
        required=True, ondelete="cascade"
    )
    
    # Messages
    inbox_message_ids = fields.One2many(
        "bader.inbox.message", "conversation_id", string="Messages"
    )
    last_message = fields.Text(string="Last Message", readonly=True)
    last_message_date = fields.Datetime(string="Last Activity", readonly=True)
    unread_count = fields.Integer(string="Unread", default=0)
    
    # State
    state = fields.Selection([
        ("open", "Open"),
        ("pending", "Pending"),
        ("resolved", "Resolved"),
    ], default="open", string="Status", tracking=True)
    
    # Assignment
    assigned_user_id = fields.Many2one(
        "res.users", string="Assigned To", tracking=True
    )
    team_id = fields.Many2one("crm.team", string="Team")
    
    # CRM Integration
    partner_id = fields.Many2one("res.partner", string="Contact")
    lead_id = fields.Many2one("crm.lead", string="Opportunity")
    
    # Tags
    tag_ids = fields.Many2many("bader.inbox.tag", string="Tags")
    
    @api.depends("contact_name", "phone", "partner_id")
    def _compute_name(self):
        for rec in self:
            if rec.partner_id:
                rec.computed_name = rec.partner_id.name
            elif rec.contact_name:
                rec.computed_name = rec.contact_name
            else:
                rec.computed_name = rec.phone or "Unknown"

    @api.model
    def get_or_create(self, channel_id, phone, whatsapp_id=None, contact_name=None):
        """Get existing or create new conversation"""
        domain = [("channel_id", "=", channel_id), ("phone", "=", phone)]
        conversation = self.search(domain, limit=1)
        
        if not conversation:
            # Try to find partner
            partner = self.env["res.partner"].search([
                "|", ("phone", "ilike", phone), ("mobile", "ilike", phone)
            ], limit=1)
            
            conversation = self.create({
                "channel_id": channel_id,
                "phone": phone,
                "whatsapp_id": whatsapp_id,
                "contact_name": contact_name,
                "partner_id": partner.id if partner else False,
            })
        elif contact_name and not conversation.contact_name:
            conversation.contact_name = contact_name
        
        return conversation

    def action_mark_read(self):
        """Mark all messages as read"""
        self.ensure_one()
        self.unread_count = 0

    def action_assign_to_me(self):
        """Assign to current user"""
        self.ensure_one()
        self.assigned_user_id = self.env.user

    def action_close(self):
        """Close/resolve conversation"""
        self.ensure_one()
        self.state = "resolved"

    def action_reopen(self):
        """Reopen conversation"""
        self.ensure_one()
        self.state = "open"

    def action_create_opportunity(self):
        """Create CRM opportunity"""
        self.ensure_one()
        if not self.partner_id:
            # Create partner first
            self.partner_id = self.env["res.partner"].create({
                "name": self.contact_name or self.phone,
                "phone": self.phone,
            })
        
        lead = self.env["crm.lead"].create({
            "name": f"WhatsApp - {self.display_name}",
            "partner_id": self.partner_id.id,
            "phone": self.phone,
            "type": "opportunity",
        })
        self.lead_id = lead
        
        return {
            "type": "ir.actions.act_window",
            "res_model": "crm.lead",
            "res_id": lead.id,
            "view_mode": "form",
        }

    def action_create_activity(self):
        """Create follow-up activity"""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": "mail.activity",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_res_model_id": self.env["ir.model"]._get_id(self._name),
                "default_res_id": self.id,
            }
        }


class BaderInboxTag(models.Model):
    """Conversation tags"""
    
    _name = "bader.inbox.tag"
    _description = "Bader Inbox Tag"

    name = fields.Char(required=True)
    color = fields.Integer(default=0)
