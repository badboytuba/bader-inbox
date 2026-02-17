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
        ondelete="set null", index=True
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
    
    # AI Agent
    ai_active = fields.Boolean(string="AI Active", default=True,
        help="Whether the AI agent auto-responds in this conversation")

    # AI Lead Qualification
    ai_lead_score = fields.Integer(string="AI Lead Score", default=0,
        help="Lead score 0-100 based on AI conversation analysis")
    ai_lead_temperature = fields.Selection([
        ("cold", "🧊 Cold"),
        ("warm", "🌤️ Warm"),
        ("hot", "🔥 Hot"),
    ], string="Lead Temperature", compute="_compute_lead_temperature", store=True)

    # AI Analytics
    ai_response_count = fields.Integer(string="AI Responses", default=0)
    ai_tools_used = fields.Text(string="AI Tools Used",
        help="JSON tracking of tool usage counts")
    ai_resolution = fields.Selection([
        ("pending", "Pending"),
        ("resolved", "Resolved by AI"),
        ("escalated", "Escalated to Human"),
    ], string="AI Resolution", default="pending")

    # AI Escalation
    ai_escalation_reason = fields.Text(string="Escalation Reason")

    # WhatsApp Profile Picture
    profile_pic_url = fields.Char(string="Profile Picture URL")
    profile_pic_date = fields.Datetime(string="Profile Pic Fetched At")

    # UTM / Contact Origin Tracking
    utm_source = fields.Char(string="Source",
        help="Traffic source: meta, google, website, organic")
    utm_medium = fields.Char(string="Medium",
        help="Traffic medium: instagram, facebook, cpc, widget")
    utm_campaign = fields.Char(string="Campaign",
        help="Campaign name: promo_verao_2026")
    channel_origin = fields.Char(string="Origin Channel",
        help="Origin channel: ads, widget, organic, web")
    referrer_url = fields.Char(string="Referrer URL")
    landing_page = fields.Char(string="Landing Page")
    tracking_code = fields.Char(string="Tracking Code", index=True)
    tracked_at = fields.Datetime(string="Tracked At")

    @api.depends("ai_lead_score")
    def _compute_lead_temperature(self):
        for rec in self:
            if rec.ai_lead_score >= 60:
                rec.ai_lead_temperature = "hot"
            elif rec.ai_lead_score >= 25:
                rec.ai_lead_temperature = "warm"
            else:
                rec.ai_lead_temperature = "cold"
    
    # Pipeline assignments
    pipeline_assignment_ids = fields.One2many(
        "bader.inbox.conversation.pipeline", "conversation_id",
        string="Pipeline Assignments"
    )
    pipeline_count = fields.Integer(
        compute="_compute_pipeline_count", string="Pipelines"
    )

    @api.depends("pipeline_assignment_ids")
    def _compute_pipeline_count(self):
        for rec in self:
            rec.pipeline_count = len(rec.pipeline_assignment_ids)
    
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
    def _clean_phone(self, phone):
        """Normalize phone number to digits only (no spaces, dashes, +, parentheses)"""
        if not phone:
            return False
        return phone.replace(" ", "").replace("-", "").replace("(", "").replace(")", "").replace("+", "")

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("phone"):
                vals["phone"] = self._clean_phone(vals["phone"])
        return super().create(vals_list)

    def write(self, vals):
        if vals.get("phone"):
            vals["phone"] = self._clean_phone(vals["phone"])
        return super().write(vals)

    @api.model
    def get_or_create(self, channel_id, phone, whatsapp_id=None, contact_name=None):
        """Get existing or create new conversation.
        
        Uses PostgreSQL advisory lock to prevent race conditions when
        multiple webhooks arrive simultaneously for the same phone+channel.
        
        Priority:
        1. Existing conversation for this channel + phone
        2. Orphaned conversation (same phone, no channel) — re-link it
        3. Create new conversation
        """
        phone = self._clean_phone(phone)
        if not phone:
            return self.browse()
        
        # Advisory lock: serialize concurrent requests for same channel+phone
        # Uses a hash of (channel_id, phone) as the lock key
        lock_key = hash((channel_id, phone)) % (2**31)
        self.env.cr.execute("SELECT pg_advisory_xact_lock(%s)", [lock_key])
        
        # 1. Try exact match: same channel + phone
        conversation = self.search(
            [("channel_id", "=", channel_id), ("phone", "=", phone)], limit=1
        )
        
        # 2. Re-link orphaned conversation (channel was deleted)
        if not conversation:
            conversation = self.search(
                [("channel_id", "=", False), ("phone", "=", phone)], limit=1
            )
            if conversation:
                conversation.write({"channel_id": channel_id})
                _logger.info(f"Re-linked orphaned conversation {conversation.id} (phone={phone}) to channel {channel_id}")
        
        if not conversation:
            # Use last 9 digits for flexible matching against formatted partner phones
            search_phone = phone[-9:] if len(phone) > 9 else phone
            partner = self.env["res.partner"].search([
                "|", ("phone", "ilike", search_phone), ("mobile", "ilike", search_phone)
            ], limit=1)
            
            try:
                conversation = self.create({
                    "channel_id": channel_id,
                    "phone": phone,
                    "whatsapp_id": whatsapp_id,
                    "contact_name": contact_name,
                    "partner_id": partner.id if partner else False,
                })
            except Exception as e:
                # Unique violation fallback: another concurrent request created it
                _logger.warning(f"Create failed (likely race condition), retrying search: {e}")
                self.env.cr.rollback()
                conversation = self.search(
                    [("channel_id", "=", channel_id), ("phone", "=", phone)], limit=1
                )
                if not conversation:
                    raise
        elif contact_name and not conversation.contact_name:
            conversation.contact_name = contact_name
        
        return conversation

    @api.model
    def search_contacts(self, query="", limit=20):
        """Search Odoo contacts (res.partner) for new conversation modal.
        
        Returns partners with phone/mobile, matching by name, phone, mobile or email.
        """
        Partner = self.env["res.partner"].sudo()
        domain = [
            "|", ("phone", "!=", False), ("mobile", "!=", False),
        ]
        if query and query.strip():
            q = query.strip()
            domain = [
                "&",
                "|", ("phone", "!=", False), ("mobile", "!=", False),
                "|", "|", "|",
                ("name", "ilike", q),
                ("phone", "ilike", q),
                ("mobile", "ilike", q),
                ("email", "ilike", q),
            ]
        
        partners = Partner.search(domain, limit=limit, order="name asc")
        
        results = []
        for p in partners:
            results.append({
                "id": p.id,
                "name": p.name or "",
                "phone": p.phone or "",
                "mobile": p.mobile or "",
                "email": p.email or "",
                "company_name": p.parent_id.name if p.parent_id else (p.company_name or ""),
                "has_image": bool(p.image_128),
                "display_phone": p.mobile or p.phone or "",
            })
        
        return results

    @api.model
    def open_or_create_by_phone(self, phone, partner_id=None, contact_name=None, model=None):
        """Find or create conversation by phone - used by PhoneWhatsAppWidget"""
        if not phone:
            return {"success": False, "error": "No phone number provided"}
        
        # Clean phone number
        clean_phone = self._clean_phone(phone)
        
        # Find existing conversation with this phone
        conversation = self.search([
            ("phone", "ilike", clean_phone)
        ], limit=1)
        
        if not conversation:
            # Get the default/first active channel
            channel = self.env["bader.inbox.channel"].search([
                ("state", "=", "connected")
            ], limit=1)
            
            if not channel:
                channel = self.env["bader.inbox.channel"].search([], limit=1)
            
            if not channel:
                return {"success": False, "error": "No WhatsApp channel available"}
            
            # Try to get partner data
            partner = None
            if partner_id:
                partner = self.env["res.partner"].browse(partner_id)
                if not partner.exists():
                    partner = None
            
            if not partner and phone:
                partner = self.env["res.partner"].search([
                    "|", ("phone", "ilike", clean_phone), ("mobile", "ilike", clean_phone)
                ], limit=1)
            
            # Create conversation
            conversation = self.create({
                "channel_id": channel.id,
                "phone": clean_phone,
                "contact_name": contact_name or (partner.name if partner else ""),
                "partner_id": partner.id if partner else False,
            })
        
        return {
            "success": True,
            "conversation_id": conversation.id,
            "channel_id": conversation.channel_id.id,
            "phone": conversation.phone,
            "contact_name": conversation.contact_name,
            "partner_id": conversation.partner_id.id if conversation.partner_id else False,
        }

    def action_mark_read(self):
        """Mark all messages as read"""
        self.ensure_one()
        self.unread_count = 0

    @api.model
    def mark_as_read(self, conversation_id):
        """Mark conversation as read - called from JS frontend"""
        conv = self.browse(conversation_id)
        if conv.exists():
            conv.unread_count = 0

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

    def auto_link_partner(self):
        """Auto-link partner to conversation by phone match.
        
        When multiple partner matches exist, prefer the one
        with existing CRM leads or sale orders.
        """
        self.ensure_one()
        conv = self.sudo()
        if conv.partner_id:
            return True  # Already linked or doesn't exist
        
        phone = conv.phone
        if not phone:
            return False
        
        # Normalize: strip +, spaces, dashes
        clean = ''.join(c for c in phone if c.isdigit())
        if len(clean) < 6:
            return False
        
        # Search for ALL partners with matching phone or mobile
        Partner = self.env["res.partner"].sudo()
        partners = Partner.search([
            "|", "|", "|",
            ("phone", "like", clean[-9:]),
            ("mobile", "like", clean[-9:]),
            ("phone", "like", phone),
            ("mobile", "like", phone),
        ], limit=10)
        
        if not partners:
            return False
        
        # If multiple matches, prefer the one with CRM/sales data
        best = partners[0]
        if len(partners) > 1:
            CrmLead = self.env["crm.lead"].sudo()
            SaleOrder = self.env["sale.order"].sudo()
            for p in partners:
                has_crm = CrmLead.search_count([("partner_id", "=", p.id)], limit=1)
                has_sales = SaleOrder.search_count([("partner_id", "=", p.id)], limit=1)
                if has_crm or has_sales:
                    best = p
                    break
        
        conv.partner_id = best.id
        if conv.computed_name == conv.phone or not conv.contact_name:
            conv.contact_name = best.name
        _logger.info(f"Auto-linked partner {best.name} (ID {best.id}) to conversation {conv.id}")
        return True

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

    @api.model
    def get_dashboard_stats(self):
        """Return dashboard KPIs"""
        cr = self.env.cr

        # Open vs resolved today
        cr.execute("""
            SELECT
                COUNT(*) FILTER (WHERE state IN ('open', 'pending')) AS open_count,
                COUNT(*) FILTER (WHERE state = 'done' AND write_date::date = CURRENT_DATE) AS resolved_today
            FROM bader_inbox_conversation
        """)
        row = cr.dictfetchone()

        # Messages today
        cr.execute("""
            SELECT
                COUNT(*) FILTER (WHERE direction = 'out') AS sent_today,
                COUNT(*) FILTER (WHERE direction = 'in') AS received_today
            FROM bader_inbox_message
            WHERE create_date::date = CURRENT_DATE
        """)
        msg_row = cr.dictfetchone()

        # Messages last 7 days (chart data)
        cr.execute("""
            SELECT
                d::date AS day,
                COUNT(*) FILTER (WHERE m.direction = 'in') AS received,
                COUNT(*) FILTER (WHERE m.direction = 'out') AS sent
            FROM generate_series(
                CURRENT_DATE - INTERVAL '6 days', CURRENT_DATE, '1 day'
            ) d
            LEFT JOIN bader_inbox_message m ON m.create_date::date = d::date
            GROUP BY d::date
            ORDER BY d::date
        """)
        activity = [
            {"day": str(r["day"]), "received": r["received"] or 0, "sent": r["sent"] or 0}
            for r in cr.dictfetchall()
        ]

        # Top agents
        cr.execute("""
            SELECT
                u.login AS agent,
                COUNT(m.id) AS msg_count
            FROM bader_inbox_message m
            JOIN res_users u ON u.id = m.author_id
            WHERE m.direction = 'out'
                AND m.create_date >= CURRENT_DATE - INTERVAL '7 days'
            GROUP BY u.login
            ORDER BY msg_count DESC
            LIMIT 5
        """)
        top_agents = [{"agent": r["agent"], "count": r["count"] if "count" in r else r["msg_count"]} for r in cr.dictfetchall()]

        return {
            "open_count": row["open_count"] or 0,
            "resolved_today": row["resolved_today"] or 0,
            "sent_today": msg_row["sent_today"] or 0,
            "received_today": msg_row["received_today"] or 0,
            "activity": activity,
            "top_agents": top_agents,
        }


    @api.model
    def search_catalog_products(self, query="", limit=12):
        """Search products for catalog send feature — returns image + prices + URL."""
        Product = self.env["product.product"].sudo()
        domain = [
            ("sale_ok", "=", True),
            ("active", "=", True),
            "|", "|",
            ("name", "ilike", query),
            ("default_code", "ilike", query),
            ("barcode", "ilike", query),
        ]
        products = Product.search(domain, limit=limit, order="name asc")
        results = []
        base_url = "https://qas.bader4business.com"
        for p in products:
            tmpl = p.product_tmpl_id
            # Build website slug
            slug = ""
            if hasattr(tmpl, "website_url") and tmpl.website_url:
                slug = tmpl.website_url
            else:
                # Fallback: build from template id
                slug = "/shop/product/%d" % tmpl.id

            # Get image (use image_256 for thumbnails)
            image = ""
            for img_field in ("image_256", "image_512", "image_1920"):
                val = getattr(p, img_field, None)
                if val:
                    image = val.decode("utf-8") if isinstance(val, bytes) else val
                    break

            # Prices
            pvp = p.list_price or 0.0
            offer = 0.0
            if hasattr(p, "offer_price") and p.offer_price:
                offer = p.offer_price
            elif hasattr(tmpl, "compare_list_price") and tmpl.compare_list_price and tmpl.compare_list_price > pvp:
                # Odoo standard: compare_list_price is the "was" price
                offer = pvp
                pvp = tmpl.compare_list_price

            results.append({
                "id": p.id,
                "name": p.name,
                "ref": p.default_code or "",
                "pvp": pvp,
                "offer": offer,
                "currency": p.currency_id.symbol or "€",
                "image": image,
                "url": base_url + slug,
                "stock": p.qty_available if hasattr(p, "qty_available") else 0,
                "category": p.categ_id.name if p.categ_id else "",
            })
        return results


class BaderInboxTag(models.Model):
    """Conversation tags"""
    
    _name = "bader.inbox.tag"
    _description = "Bader Inbox Tag"

    name = fields.Char(required=True)
    color = fields.Integer(default=0)
