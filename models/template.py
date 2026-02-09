# Copyright 2026 Bader Business
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo import api, fields, models, _


class BaderInboxTemplate(models.Model):
    """Quick Reply Templates"""
    
    _name = "bader.inbox.template"
    _description = "Quick Reply Template"
    _order = "sequence, name"

    name = fields.Char(string="Name", required=True)
    shortcut = fields.Char(string="Shortcut", help="e.g., /hello")
    content = fields.Text(string="Content", required=True)
    category = fields.Selection([
        ("greeting", "Greeting"),
        ("info", "Information"),
        ("faq", "FAQ"),
        ("closing", "Closing"),
        ("other", "Other"),
    ], default="other", string="Category")
    channel_ids = fields.Many2many("bader.inbox.channel", string="Channels")
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    use_count = fields.Integer(string="Times Used", default=0, readonly=True)


class BaderInboxChatbot(models.Model):
    """Chatbot / Automation Rules"""
    
    _name = "bader.inbox.chatbot"
    _description = "Chatbot Rule"
    _order = "priority, name"

    name = fields.Char(string="Rule Name", required=True)
    active = fields.Boolean(default=True)
    priority = fields.Integer(default=10)
    
    trigger_type = fields.Selection([
        ("keyword", "Contains Keyword"),
        ("first_message", "First Message"),
        ("no_reply", "No Reply After X Minutes"),
        ("all", "All Messages"),
    ], default="keyword", string="Trigger", required=True)
    
    trigger_keywords = fields.Char(string="Keywords", help="Comma-separated")
    trigger_delay_minutes = fields.Integer(default=5)
    channel_ids = fields.Many2many("bader.inbox.channel", string="Channels")
    only_outside_hours = fields.Boolean(string="Only Outside Business Hours")
    business_hours_start = fields.Float(default=9.0)
    business_hours_end = fields.Float(default=18.0)
    
    action_type = fields.Selection([
        ("reply", "Send Reply"),
        ("assign", "Assign to User"),
        ("tag", "Add Tag"),
        ("notify", "Notify User"),
    ], default="reply", string="Action", required=True)
    
    reply_content = fields.Text(string="Reply Message")
    reply_template_id = fields.Many2one("bader.inbox.template", string="Reply Template")
    assign_user_id = fields.Many2one("res.users", string="Assign To")
    add_tag_ids = fields.Many2many("bader.inbox.tag", string="Add Tags")
    notify_user_ids = fields.Many2many("res.users", string="Notify Users")
    
    trigger_count = fields.Integer(default=0, readonly=True)
    last_triggered = fields.Datetime(readonly=True)

    def check_trigger(self, conversation, message):
        """Check if rule should trigger"""
        self.ensure_one()
        if self.channel_ids and conversation.channel_id not in self.channel_ids:
            return False
        
        if self.trigger_type == "keyword":
            if not message or not message.content:
                return False
            content_lower = message.content.lower()
            keywords = [k.strip().lower() for k in (self.trigger_keywords or "").split(",")]
            return any(kw in content_lower for kw in keywords if kw)
        elif self.trigger_type == "first_message":
            return len(conversation.inbox_message_ids) == 1
        elif self.trigger_type == "all":
            return True
        return False

    def execute_action(self, conversation, message=None):
        """Execute rule action"""
        self.ensure_one()
        self.sudo().write({
            "trigger_count": self.trigger_count + 1,
            "last_triggered": fields.Datetime.now(),
        })
        
        if self.action_type == "reply":
            content = self.reply_template_id.content if self.reply_template_id else self.reply_content
            if content:
                content = content.replace("{name}", conversation.contact_name or conversation.phone or "")
                self.env["bader.inbox.message"].send_message(conversation.id, content, "text")
        elif self.action_type == "assign" and self.assign_user_id:
            conversation.assigned_user_id = self.assign_user_id
        elif self.action_type == "tag" and self.add_tag_ids:
            conversation.tag_ids = [(4, tag.id) for tag in self.add_tag_ids]
