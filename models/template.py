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

    @api.model
    def _process_no_reply_rules(self):
        """Process 'no_reply' rules via cron"""
        rules = self.search([
            ("active", "=", True), 
            ("trigger_type", "=", "no_reply"),
            ("trigger_delay_minutes", ">", 0)
        ], order="priority")
        
        if not rules:
            return
            
        Conversation = self.env["bader.inbox.conversation"]
        now = fields.Datetime.now()
        
        for rule in rules:
            # Calculate threshold time
            threshold = fields.Datetime.subtract(now, minutes=rule.trigger_delay_minutes)
            
            # Find candidate conversations:
            # - Open/Pending state
            # - Last message older than threshold
            # - Last message was INCOMING (user waiting for reply)
            # OR - Unread count > 0 (implies incoming)
            # Optimization: Filter by date first
            
            domain = [
                ("state", "in", ["open", "pending"]),
                ("last_message_date", "<", threshold),
                ("unread_count", ">", 0) # Quick check for incoming pending
            ]
            if rule.channel_ids:
                domain.append(("channel_id", "in", rule.channel_ids.ids))
                
            conversations = Conversation.search(domain)
            
            for conv in conversations:
                # Double check last message direction explicitly to avoid logic errors
                last_msg = self.env["bader.inbox.message"].search([
                    ("conversation_id", "=", conv.id)
                ], order="create_date desc", limit=1)
                
                if last_msg and last_msg.direction == "in":
                    # Check if already triggered recently? Not needed if action updates last_message_date
                    rule.execute_action(conv, last_msg)

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
                # Sends message -> updates last_message_date -> breaks loop
                self.env["bader.inbox.message"].send_message(conversation.id, content, "text")
                
        elif self.action_type == "assign" and self.assign_user_id:
            conversation.assigned_user_id = self.assign_user_id
            # Log action to prevent loop
            conversation.message_post(body=f"Chatbot: Auto-assigned to {self.assign_user_id.name}")
            # Update last_message_date to prevent re-triggering immediately
            conversation.last_message_date = fields.Datetime.now()
            
        elif self.action_type == "tag" and self.add_tag_ids:
            conversation.tag_ids = [(4, tag.id) for tag in self.add_tag_ids]
            # Log action
            tag_names = ", ".join(self.add_tag_ids.mapped("name"))
            conversation.message_post(body=f"Chatbot: Added tags {tag_names}")
            conversation.last_message_date = fields.Datetime.now()
            
        elif self.action_type == "notify" and self.notify_user_ids:
            # Send internal notification
            subject = f"New WhatsApp from {conversation.computed_name}"
            body = f"Customer waiting for reply > {self.trigger_delay_minutes} min"
            for user in self.notify_user_ids:
                conversation.message_notify(
                    partner_ids=[user.partner_id.id],
                    body=body,
                    subject=subject,
                    record_name=conversation.computed_name,
                )
            conversation.message_post(body=f"Chatbot: Notified {len(self.notify_user_ids)} users")
            conversation.last_message_date = fields.Datetime.now()
