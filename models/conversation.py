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

    _sql_constraints = [
        ('unique_channel_phone',
         'UNIQUE(channel_id, phone)',
         'A conversation with this phone already exists on this channel.'),
    ]

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

    # Group support
    is_group = fields.Boolean(string="Is Group", default=False, index=True)
    group_jid = fields.Char(string="Group JID", index=True,
        help="Full WhatsApp group JID (e.g. 120363150484497988@g.us)")
    group_subject = fields.Char(string="Group Name",
        help="The subject/name of the WhatsApp group")
    group_description = fields.Text(string="Group Description")
    group_pic_url = fields.Char(string="Group Picture URL")
    member_count = fields.Integer(string="Members", compute="_compute_member_count", store=True)
    is_muted = fields.Boolean(string="Muted", default=False,
        help="Muted groups don't generate notifications")
    group_member_ids = fields.One2many(
        "bader.inbox.group.member", "conversation_id", string="Group Members")

    # Pin conversation
    is_pinned = fields.Boolean(string="Pinned", default=False)

    # Notes (for record rule channel permissions)
    note_ids = fields.One2many(
        "bader.inbox.note", "conversation_id", string="Internal Notes"
    )
    
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

    def write(self, vals):
        """Override to sync tag changes to res.partner when sync_to_contact is enabled."""
        # Phone normalization
        if vals.get("phone"):
            vals["phone"] = self._clean_phone(vals["phone"])

        # Capture old state before write
        old_tags = {}
        old_partners = {}
        if "tag_ids" in vals:
            for conv in self:
                old_tags[conv.id] = set(conv.tag_ids.ids)
        if "partner_id" in vals:
            for conv in self:
                old_partners[conv.id] = conv.partner_id.id or False

        result = super().write(vals)

        # ── Case 1: tag_ids changed → sync added/removed tags to partner ──
        if "tag_ids" in vals:
            self._sync_tag_changes(old_tags)

        # ── Case 2: partner_id just linked → sync ALL existing tags to new partner ──
        if "partner_id" in vals:
            self._sync_tags_on_partner_link(old_partners)

        return result

    def _sync_tag_changes(self, old_tags):
        """Sync added/removed tags to partner when tag_ids changes."""
        for conv in self:
            partner = conv.partner_id

            # Fallback: find partner by phone if not linked
            if not partner and conv.phone:
                phone = conv.phone.strip().lstrip("+")
                partner = self.env["res.partner"].sudo().search([
                    "|", "|",
                    ("phone", "like", phone[-9:]),
                    ("mobile", "like", phone[-9:]),
                    ("phone", "like", "+" + phone),
                ], limit=1)
                if partner:
                    _logger.info(
                        "Tag sync: found partner '%s' (id=%s) by phone for conv %s",
                        partner.name, partner.id, conv.id
                    )
                    conv.with_context(skip_tag_sync=True).partner_id = partner
                else:
                    continue

            if not partner:
                continue

            new_tag_ids = set(conv.tag_ids.ids)
            prev_tag_ids = old_tags.get(conv.id, set())

            added = new_tag_ids - prev_tag_ids
            removed = prev_tag_ids - new_tag_ids

            # Add partner categories for added tags
            if added:
                tags_to_sync = self.env["bader.inbox.tag"].browse(list(added)).filtered("sync_to_contact")
                for tag in tags_to_sync:
                    try:
                        category = tag._ensure_partner_category()
                        if category and category.exists() and category.id not in partner.category_id.ids:
                            partner.sudo().write({"category_id": [(4, category.id)]})
                            _logger.info("Synced tag '%s' → partner '%s' (id=%s)", tag.name, partner.name, partner.id)
                    except Exception as e:
                        _logger.warning("Tag sync failed for tag '%s': %s", tag.name, e)

            # Remove partner categories for removed tags
            if removed:
                tags_to_unsync = self.env["bader.inbox.tag"].browse(list(removed)).filtered(
                    lambda t: t.sync_to_contact and t.partner_category_id
                )
                for tag in tags_to_unsync:
                    try:
                        if tag.partner_category_id.exists() and tag.partner_category_id.id in partner.category_id.ids:
                            partner.sudo().write({"category_id": [(3, tag.partner_category_id.id)]})
                            _logger.info("Removed tag '%s' from partner '%s' (id=%s)", tag.name, partner.name, partner.id)
                    except Exception as e:
                        _logger.warning("Tag unsync failed for tag '%s': %s", tag.name, e)

    def _sync_tags_on_partner_link(self, old_partners):
        """When partner_id is first set, sync all existing tags to the new partner."""
        for conv in self:
            was_empty = not old_partners.get(conv.id)
            has_partner_now = bool(conv.partner_id)

            if was_empty and has_partner_now and conv.tag_ids:
                tags_to_sync = conv.tag_ids.filtered("sync_to_contact")
                for tag in tags_to_sync:
                    try:
                        category = tag._ensure_partner_category()
                        if category and category.exists() and category.id not in conv.partner_id.category_id.ids:
                            conv.partner_id.sudo().write({"category_id": [(4, category.id)]})
                            _logger.info(
                                "Partner linked: synced tag '%s' → partner '%s' (id=%s)",
                                tag.name, conv.partner_id.name, conv.partner_id.id
                            )
                    except Exception as e:
                        _logger.warning("Tag sync on partner link failed for tag '%s': %s", tag.name, e)


    
    # Notes (internal)
    note_ids = fields.One2many(
        "bader.inbox.note", "conversation_id", string="Internal Notes"
    )

    # Assigned agent
    assigned_user_id = fields.Many2one(
        "res.users", string="Assigned To",
        help="User assigned to handle this conversation",
        tracking=True,
    )

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
    
    @api.depends("group_member_ids")
    def _compute_member_count(self):
        for rec in self:
            rec.member_count = len(rec.group_member_ids)

    @api.depends("contact_name", "phone", "partner_id", "group_subject")
    def _compute_name(self):
        for rec in self:
            if rec.is_group and rec.group_subject:
                rec.computed_name = rec.group_subject
            elif rec.is_group and rec.contact_name:
                rec.computed_name = rec.contact_name
            elif rec.is_group:
                rec.computed_name = rec.group_jid or rec.phone or "Group"
            elif rec.partner_id:
                rec.computed_name = rec.partner_id.name
            elif rec.contact_name:
                rec.computed_name = rec.contact_name
            else:
                rec.computed_name = rec.phone or "Unknown"

    def action_sync_group_info(self):
        """Fetch group metadata and members from Evolution API."""
        self.ensure_one()
        if not self.is_group or not self.group_jid or not self.channel_id:
            return {"error": "Not a valid group conversation"}

        channel = self.channel_id
        instance = channel.evolution_instance_name
        if not instance:
            return {"error": "Channel has no Evolution instance"}

        api = self.env["bader.inbox.evolution_api"]

        # Fetch group info (subject, description, picture)
        info = api.fetch_group_info(instance, self.group_jid)
        if info:
            vals = {}
            if info.get("subject"):
                vals["group_subject"] = info["subject"]
                vals["contact_name"] = info["subject"]
            if info.get("desc") or info.get("description"):
                vals["group_description"] = info.get("desc") or info.get("description")
            if info.get("profilePictureUrl") or info.get("picture"):
                vals["group_pic_url"] = info.get("profilePictureUrl") or info.get("picture")
            if vals:
                self.write(vals)

        # Fetch and sync participants
        participants = api.fetch_group_participants(instance, self.group_jid)
        if participants:
            self._sync_group_members(participants)

        return {"success": True, "members": len(participants or [])}

    def auto_sync_group_info(self):
        """Lightweight auto-sync: fetch group name/description from API.
        
        Called automatically on first group message. Only fetches metadata,
        NOT participants (to keep webhook fast). Silent on errors.
        """
        self.ensure_one()
        if not self.is_group or not self.group_jid or not self.channel_id:
            return
        channel = self.channel_id
        instance = channel.evolution_instance_name
        if not instance:
            return
        try:
            api = self.env["bader.inbox.evolution_api"]
            info = api.fetch_group_info(instance, self.group_jid)
            if not info:
                return
            vals = {}
            subject = info.get("subject") or ""
            if subject:
                vals["group_subject"] = subject
                vals["contact_name"] = subject
            desc = info.get("desc") or info.get("description") or ""
            if desc:
                vals["group_description"] = desc
            pic = info.get("profilePictureUrl") or info.get("picture") or ""
            if pic:
                vals["group_pic_url"] = pic
            # Count members from size field
            size = info.get("size") or info.get("participants", [])
            if isinstance(size, int) and size > 0:
                vals["member_count"] = size
            elif isinstance(size, list):
                vals["member_count"] = len(size)
            if vals:
                self.write(vals)
                _logger.info(f"Auto-synced group {self.group_jid}: {vals.get('group_subject', '?')}")
        except Exception as e:
            _logger.debug(f"Auto group sync error for {self.group_jid}: {e}")

    def _sync_group_members(self, participants):
        """Sync group members from API response."""
        Member = self.env["bader.inbox.group.member"]
        existing = {m.phone: m for m in self.group_member_ids}
        seen = set()

        for p in participants:
            if not isinstance(p, dict):
                continue
            # Extract phone from participant id (e.g. '5491123456789@s.whatsapp.net')
            pid = p.get("id", "") or p.get("wid", "")
            phone = str(pid).split("@")[0] if "@" in str(pid) else str(pid)
            if not phone or not phone.replace("+", "").isdigit():
                continue

            is_admin = p.get("admin") == "admin" or p.get("isAdmin", False)
            is_superadmin = p.get("admin") == "superadmin" or p.get("isSuperAdmin", False)
            name = p.get("name") or p.get("pushName") or p.get("notify") or ""

            seen.add(phone)
            if phone in existing:
                member = existing[phone]
                update = {}
                if name and name != member.name:
                    update["name"] = name
                if is_admin != member.is_admin:
                    update["is_admin"] = is_admin
                if is_superadmin != member.is_superadmin:
                    update["is_superadmin"] = is_superadmin
                if update:
                    member.write(update)
            else:
                Member.create({
                    "conversation_id": self.id,
                    "phone": phone,
                    "name": name,
                    "is_admin": is_admin,
                    "is_superadmin": is_superadmin,
                })

        # Remove members no longer in the group
        for phone, member in existing.items():
            if phone not in seen:
                member.unlink()

    def action_toggle_mute(self):
        """Toggle mute status for a group conversation."""
        self.ensure_one()
        self.is_muted = not self.is_muted
        return {"muted": self.is_muted}

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


    @api.model
    def _find_partner_by_phone(self, phone):
        """Search res.partner by phone number (last 9 digits for flexible matching)."""
        if not phone:
            return self.env["res.partner"].browse()
        search_phone = phone[-9:] if len(phone) > 9 else phone
        return self.env["res.partner"].search([
            "|", ("phone", "ilike", search_phone), ("mobile", "ilike", search_phone)
        ], limit=1)

    @api.model
    def get_or_create(self, channel_id, phone, whatsapp_id=None, contact_name=None,
                      is_group=False, group_jid=None):
        """Get existing or create new conversation.
        
        Uses PostgreSQL advisory lock + unique constraint to prevent
        duplicate conversations when multiple webhooks/campaigns arrive
        simultaneously for the same phone+channel.
        
        For groups: phone stores the numeric part of the group JID,
        group_jid stores the full JID (e.g. 120363150484497988@g.us).
        """
        phone = self._clean_phone(phone)
        if not phone:
            return self.browse()
        
        # Advisory lock: serialize concurrent requests for same channel+phone
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
            partner = self._find_partner_by_phone(phone) if not is_group else None
            try:
                with self.env.cr.savepoint():
                    vals = {
                        "channel_id": channel_id,
                        "phone": phone,
                        "whatsapp_id": whatsapp_id,
                        # For groups: DON'T use sender pushName as contact_name
                        "contact_name": None if is_group else contact_name,
                        "partner_id": partner.id if partner else False,
                        "is_group": is_group,
                    }
                    if group_jid:
                        vals["group_jid"] = group_jid
                    conversation = self.create(vals)
                    # Auto-sync group info from Evolution API
                    if is_group and group_jid:
                        try:
                            self.env.cr.commit()  # Commit creation first
                            conversation.auto_sync_group_info()
                        except Exception as sync_err:
                            _logger.warning(f"Auto group sync failed for {group_jid}: {sync_err}")
            except Exception as e:
                _logger.warning(f"Create failed (unique constraint), fetching existing: {e}")
                conversation = self.search(
                    [("channel_id", "=", channel_id), ("phone", "=", phone)], limit=1
                )
                if not conversation:
                    raise
        else:
            # Existing conversation: auto-link partner if not already linked
            if not is_group and not conversation.partner_id:
                partner = self._find_partner_by_phone(phone)
                if partner:
                    conversation.partner_id = partner
                    _logger.info(f"Auto-linked partner {partner.id} ({partner.name}) to conversation {conversation.id}")
            # For groups: never overwrite contact_name with sender pushName
            # Only update contact_name for individual chats
            if not is_group and contact_name and not conversation.contact_name and not conversation.partner_id:
                conversation.contact_name = contact_name
            if group_jid and not conversation.group_jid:
                conversation.group_jid = group_jid
            if is_group and not conversation.is_group:
                conversation.is_group = True
            # Auto-sync group info if we don't have the subject yet
            if is_group and not conversation.group_subject and conversation.group_jid:
                try:
                    conversation.auto_sync_group_info()
                except Exception as sync_err:
                    _logger.debug(f"Auto group sync skipped: {sync_err}")
        
        return conversation

    @api.model
    def auto_link_partners(self):
        """Bulk fix: link res.partner to all existing conversations without partner_id.
        Call this once to fix historical data. Can be run from Odoo shell or scheduled action.
        """
        unlinked = self.search([("partner_id", "=", False)])
        linked_count = 0
        for conv in unlinked:
            partner = self._find_partner_by_phone(conv.phone)
            if partner:
                conv.partner_id = partner
                linked_count += 1
        _logger.info(f"auto_link_partners: linked {linked_count}/{len(unlinked)} conversations")
        return {"linked": linked_count, "total": len(unlinked)}

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
        """Return dashboard KPIs - scoped by user's channels.
        Managers see all data + extra KPIs. Users see only their channels.
        """
        cr = self.env.cr
        user = self.env.user

        # Check if user is manager
        is_manager = user.has_group('bader_inbox.group_bader_inbox_manager')

        # Build channel filter for Users
        channel_ids = None
        if not is_manager:
            # Get channels where user is in allowed_user_ids
            cr.execute("""
                SELECT channel_id FROM bader_inbox_channel_user_rel
                WHERE user_id = %s
            """, (user.id,))
            channel_ids = [r[0] for r in cr.fetchall()]
            if not channel_ids:
                # User has no channels assigned → return empty dashboard
                return {
                    "open_count": 0, "resolved_today": 0,
                    "sent_today": 0, "received_today": 0,
                    "activity": [], "top_agents": [],
                    "is_manager": False,
                }

        # M1 FIX: Use parameterized queries instead of f-string interpolation
        channel_ids_tuple = tuple(channel_ids) if channel_ids else None

        # ─── Open vs resolved today ───
        if channel_ids_tuple:
            cr.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE state IN ('open', 'pending')) AS open_count,
                    COUNT(*) FILTER (WHERE state = 'done' AND write_date::date = CURRENT_DATE) AS resolved_today
                FROM bader_inbox_conversation c
                WHERE c.channel_id = ANY(%s)
            """, (list(channel_ids_tuple),))
        else:
            cr.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE state IN ('open', 'pending')) AS open_count,
                    COUNT(*) FILTER (WHERE state = 'done' AND write_date::date = CURRENT_DATE) AS resolved_today
                FROM bader_inbox_conversation c
            """)
        row = cr.dictfetchone()

        # ─── Messages today ───
        if channel_ids_tuple:
            cr.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE m.direction = 'out') AS sent_today,
                    COUNT(*) FILTER (WHERE m.direction = 'in') AS received_today
                FROM bader_inbox_message m
                JOIN bader_inbox_conversation conv ON conv.id = m.conversation_id
                WHERE m.create_date::date = CURRENT_DATE
                    AND conv.channel_id = ANY(%s)
            """, (list(channel_ids_tuple),))
        else:
            cr.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE m.direction = 'out') AS sent_today,
                    COUNT(*) FILTER (WHERE m.direction = 'in') AS received_today
                FROM bader_inbox_message m
                JOIN bader_inbox_conversation conv ON conv.id = m.conversation_id
                WHERE m.create_date::date = CURRENT_DATE
            """)
        msg_row = cr.dictfetchone()

        # ─── Messages last 7 days (chart data) ───
        if channel_ids_tuple:
            cr.execute("""
                SELECT
                    d::date AS day,
                    COUNT(*) FILTER (WHERE m.direction = 'in') AS received,
                    COUNT(*) FILTER (WHERE m.direction = 'out') AS sent
                FROM generate_series(
                    CURRENT_DATE - INTERVAL '6 days', CURRENT_DATE, '1 day'
                ) d
                LEFT JOIN bader_inbox_message m ON m.create_date::date = d::date
                LEFT JOIN bader_inbox_conversation conv ON conv.id = m.conversation_id
                WHERE (conv.channel_id = ANY(%s) OR m.id IS NULL)
                GROUP BY d::date
                ORDER BY d::date
            """, (list(channel_ids_tuple),))
        else:
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

        # ─── Top agents ───
        if channel_ids_tuple:
            cr.execute("""
                SELECT
                    u.login AS agent,
                    COUNT(m.id) AS msg_count
                FROM bader_inbox_message m
                JOIN res_users u ON u.id = m.author_id
                JOIN bader_inbox_conversation conv ON conv.id = m.conversation_id
                WHERE m.direction = 'out'
                    AND m.create_date >= CURRENT_DATE - INTERVAL '7 days'
                    AND conv.channel_id = ANY(%s)
                GROUP BY u.login
                ORDER BY msg_count DESC
                LIMIT 5
            """, (list(channel_ids_tuple),))
        else:
            cr.execute("""
                SELECT
                    u.login AS agent,
                    COUNT(m.id) AS msg_count
                FROM bader_inbox_message m
                JOIN res_users u ON u.id = m.author_id
                JOIN bader_inbox_conversation conv ON conv.id = m.conversation_id
                WHERE m.direction = 'out'
                    AND m.create_date >= CURRENT_DATE - INTERVAL '7 days'
                GROUP BY u.login
                ORDER BY msg_count DESC
                LIMIT 5
            """)
        top_agents = [{"agent": r["agent"], "count": r["msg_count"]} for r in cr.dictfetchall()]

        result = {
            "open_count": row["open_count"] or 0,
            "resolved_today": row["resolved_today"] or 0,
            "sent_today": msg_row["sent_today"] or 0,
            "received_today": msg_row["received_today"] or 0,
            "activity": activity,
            "top_agents": top_agents,
            "is_manager": is_manager,
        }

        # ─── Manager-only extra KPIs ───
        if is_manager:
            # Per-channel breakdown
            cr.execute("""
                SELECT
                    ch.name AS channel_name,
                    COUNT(*) FILTER (WHERE c.state IN ('open', 'pending')) AS open_count,
                    COUNT(*) FILTER (WHERE c.state = 'done' AND c.write_date::date = CURRENT_DATE) AS resolved_today
                FROM bader_inbox_conversation c
                JOIN bader_inbox_channel ch ON ch.id = c.channel_id
                GROUP BY ch.name
                ORDER BY ch.name
            """)
            result["channel_breakdown"] = [
                {"channel": r["channel_name"], "open": r["open_count"] or 0, "resolved": r["resolved_today"] or 0}
                for r in cr.dictfetchall()
            ]

            # Per-agent performance (last 7 days)
            cr.execute("""
                SELECT
                    u.name AS agent_name,
                    COUNT(DISTINCT m.conversation_id) AS conversations_handled,
                    COUNT(m.id) AS messages_sent
                FROM bader_inbox_message m
                JOIN res_users u ON u.id = m.author_id
                WHERE m.direction = 'out'
                    AND m.create_date >= CURRENT_DATE - INTERVAL '7 days'
                GROUP BY u.name
                ORDER BY messages_sent DESC
            """)
            result["agent_performance"] = [
                {"agent": r["agent_name"], "conversations": r["conversations_handled"] or 0, "messages": r["messages_sent"] or 0}
                for r in cr.dictfetchall()
            ]

        return result


    @api.model
    def search_users(self, query="", limit=15):
        """Search active Odoo users for @mention dropdown.

        Returns users matching by name or login, with avatar info.
        """
        User = self.env["res.users"].sudo()
        domain = [("active", "=", True), ("share", "=", False)]
        if query and query.strip():
            q = query.strip()
            domain += [
                "|",
                ("name", "ilike", q),
                ("login", "ilike", q),
            ]
        users = User.search(domain, limit=limit, order="name asc")
        return [
            {
                "id": u.id,
                "name": u.name,
                "login": u.login,
                "has_avatar": bool(u.image_128),
            }
            for u in users
        ]

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

    @api.model
    def get_shared_conversations(self, fields_list, limit=50):
        """Get conversations shared with current user via @mentions,
        from channels the user is NOT directly assigned to."""
        user = self.env.user
        notes = self.env["bader.inbox.note"].search([
            ("mentioned_user_ids", "in", [user.id])
        ])
        conv_ids = list(set(notes.mapped("conversation_id").ids))
        if not conv_ids:
            return []

        # Filter: only conversations from channels the user is NOT assigned to
        convs = self.browse(conv_ids).filtered(
            lambda c: c.channel_id.allowed_user_ids
            and user not in c.channel_id.allowed_user_ids
        )
        if not convs:
            return []

        return convs.sorted("last_message_date", reverse=True)[:limit].read(fields_list)


class BaderInboxTag(models.Model):
    """Conversation tags — optionally synced to res.partner.category"""
    
    _name = "bader.inbox.tag"
    _description = "Bader Inbox Tag"

    name = fields.Char(required=True)
    color = fields.Integer(default=0)

    # Sync to Odoo contact tags
    sync_to_contact = fields.Boolean(
        string="Sync to Contact",
        default=True,
        help="When enabled, applying this tag to a conversation will also "
             "add the corresponding tag to the Odoo contact (res.partner)."
    )
    partner_category_id = fields.Many2one(
        "res.partner.category",
        string="Odoo Contact Tag",
        help="Linked res.partner.category. Auto-created if not set.",
        ondelete="set null",
    )

    def _ensure_partner_category(self):
        """Find or create the matching res.partner.category.
        
        Duplicate prevention:
        1. If already linked → return it
        2. Search by exact name (case-insensitive) → link and return
        3. Not found → create, link, and return
        """
        self.ensure_one()
        if self.partner_category_id:
            # Validate cached category still exists (may have been deleted)
            if self.partner_category_id.exists():
                return self.partner_category_id
            # Category was deleted — clear stale reference
            _logger.warning("Partner category %s was deleted, clearing reference from tag '%s'", self.partner_category_id.id, self.name)
            self.partner_category_id = False

        Category = self.env["res.partner.category"].sudo()

        # Case-insensitive search
        existing = Category.search([
            ("name", "=ilike", self.name)
        ], limit=1)

        if existing:
            self.partner_category_id = existing.id
            return existing

        # Create new category
        new_cat = Category.create({"name": self.name})
        self.partner_category_id = new_cat.id
        _logger.info(
            "Created res.partner.category '%s' (id=%s) for inbox tag '%s' (id=%s)",
            self.name, new_cat.id, self.name, self.id
        )
        return new_cat
