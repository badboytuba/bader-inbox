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
    assigned_by_uid = fields.Many2one(
        "res.users", string="Assigned By",
        help="User who performed the assignment"
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
            # Count members from size field (don't use `or` — size=0 is falsy)
            size = info.get("size")
            participants = info.get("participants", [])
            if isinstance(size, int) and size >= 0:
                vals["member_count"] = size
            elif isinstance(participants, list) and participants:
                vals["member_count"] = len(participants)
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
        now = fields.Datetime.now()
        for vals in vals_list:
            if vals.get("phone"):
                vals["phone"] = self._clean_phone(vals["phone"])
            # Ensure last_message_date is never NULL so sorting works correctly
            if not vals.get("last_message_date"):
                vals["last_message_date"] = now
        return super().create(vals_list)


    @api.model
    def _find_partner_by_phone(self, phone):
        """Search res.partner by phone number, ignoring formatting.

        Partners store phones as '+54 9 261 541-2778' while WhatsApp uses
        '5492615412778'.  We strip non-digits on BOTH sides so they match.
        Tries last-10, last-9, last-8 digit suffixes to cover country-code
        variations.

        When multiple partners match, picks the best one based on CRM/sales data.
        """
        if not phone:
            return self.env["res.partner"].browse()
        Partner = self.env["res.partner"]
        clean = self._clean_phone(phone)
        if not clean or len(clean) < 6:
            return Partner.browse()

        # Use raw SQL: compare cleaned digits from both sides
        for suffix_len in (0, 10, 9, 8):
            suffix = clean if suffix_len == 0 else clean[-suffix_len:]
            if not suffix:
                continue
            self.env.cr.execute("""
                SELECT id FROM res_partner
                WHERE active = true
                  AND (regexp_replace(COALESCE(phone, ''), '[^0-9]', '', 'g') LIKE %s
                   OR regexp_replace(COALESCE(mobile, ''), '[^0-9]', '', 'g') LIKE %s)
                LIMIT 10
            """, [f'%{suffix}', f'%{suffix}'])
            rows = self.env.cr.fetchall()
            if not rows:
                continue
            if len(rows) == 1:
                return Partner.browse(rows[0][0])
            # Multiple matches: pick the best partner
            partners = Partner.browse([r[0] for r in rows])
            if len(partners) > 1:
                _logger.warning(
                    "Multiple partners match phone %s: %s",
                    phone, ", ".join(f"{p.name} (ID {p.id})" for p in partners),
                )
            return self._pick_best_partner(partners)

        return Partner.browse()

    @api.model
    def _pick_best_partner(self, partners):
        """From multiple partner matches, pick the one with most business data."""
        if not partners:
            return self.env["res.partner"].browse()
        if len(partners) == 1:
            return partners[0]

        best = partners[0]
        best_score = -1
        CrmLead = self.env["crm.lead"].sudo()
        SaleOrder = self.env["sale.order"].sudo()

        for p in partners:
            score = 0
            if CrmLead.search_count([("partner_id", "=", p.id)], limit=1):
                score += 100
            if SaleOrder.search_count([("partner_id", "=", p.id)], limit=1):
                score += 50
            if p.email:
                score += 10
            if p.phone and p.mobile:
                score += 5
            # Prefer older records (lower ID = created first)
            score += max(0, 10 - (p.id - partners[0].id) // 100)
            if score > best_score:
                best_score = score
                best = p

        return best

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
        # Use hashlib (deterministic across processes) instead of hash() which
        # returns different values per Python process (PYTHONHASHSEED).
        import hashlib
        lock_str = f"{channel_id}:{phone}"
        lock_key = int(hashlib.md5(lock_str.encode()).hexdigest()[:8], 16) % (2**31)
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
                if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                    _logger.info(f"Concurrent create for {phone} on channel {channel_id}, fetching existing")
                    # Savepoint was rolled back, search is safe
                    conversation = self.search(
                        [("channel_id", "=", channel_id), ("phone", "=", phone)], limit=1
                    )
                    if not conversation:
                        # Last resort: raw SQL to bypass ORM cache
                        self.env.cr.execute(
                            "SELECT id FROM bader_inbox_conversation WHERE channel_id = %s AND phone = %s LIMIT 1",
                            [channel_id, phone]
                        )
                        row = self.env.cr.fetchone()
                        if row:
                            conversation = self.browse(row[0])
                    if not conversation:
                        raise
                else:
                    raise
        else:
            # Existing conversation: auto-link partner if not already linked
            if not is_group and not conversation.partner_id:
                partner = self._find_partner_by_phone(phone)
                if partner:
                    conversation.write({
                        "partner_id": partner.id,
                        "contact_name": partner.name,
                    })
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
    def get_duplicate_partners(self, phone):
        """Find all partners matching a phone number (for duplicate detection in UI).

        Returns list of dicts with full partner info, or empty list if 0-1 matches.
        """
        if not phone:
            return []
        clean = self._clean_phone(phone)
        if not clean or len(clean) < 6:
            return []

        # Try suffix matching with decreasing specificity
        for suffix_len in (0, 10, 9, 8):
            suffix = clean if suffix_len == 0 else clean[-suffix_len:]
            if not suffix:
                continue
            self.env.cr.execute("""
                SELECT p.id, COALESCE(p.name, '') AS name, p.phone, p.mobile, p.email,
                       p.street, p.city, p.vat, p.comment,
                       p.create_date,
                       COALESCE(comp.name, '') AS company_name,
                       COALESCE(country.name::varchar, '') AS country_name,
                       COALESCE(state.name::varchar, '') AS state_name,
                       (SELECT COUNT(*) FROM crm_lead WHERE partner_id = p.id) AS lead_count,
                       (SELECT COUNT(*) FROM sale_order WHERE partner_id = p.id) AS sale_count,
                       (SELECT COUNT(*) FROM bader_inbox_conversation WHERE partner_id = p.id) AS conv_count,
                       (SELECT SUM(amount_total) FROM sale_order WHERE partner_id = p.id AND state IN ('sale', 'done')) AS total_revenue
                FROM res_partner p
                LEFT JOIN res_company comp ON comp.id = p.company_id
                LEFT JOIN res_country country ON country.id = p.country_id
                LEFT JOIN res_country_state state ON state.id = p.state_id
                WHERE p.active = true
                  AND (regexp_replace(COALESCE(p.phone, ''), '[^0-9]', '', 'g') LIKE %s
                   OR regexp_replace(COALESCE(p.mobile, ''), '[^0-9]', '', 'g') LIKE %s)
                ORDER BY p.id
                LIMIT 20
            """, [f'%{suffix}', f'%{suffix}'])
            rows = self.env.cr.dictfetchall()
            if len(rows) > 1:
                # Format dates and revenue for JSON serialization
                for row in rows:
                    if row.get("create_date"):
                        row["create_date"] = str(row["create_date"])[:10]
                    row["total_revenue"] = float(row.get("total_revenue") or 0)
                return rows
            if rows:
                return []  # Only 1 match = no duplicates
        return []

    @api.model
    def merge_duplicate_partners(self, keep_id, archive_ids):
        """Merge duplicate partners: keep one, archive others, transfer data."""
        Partner = self.env["res.partner"].sudo()
        keep = Partner.browse(keep_id)
        if not keep.exists():
            return {"error": "Partner to keep not found"}

        others = Partner.browse(archive_ids).exists()
        if not others:
            return {"error": "No partners to archive"}

        CrmLead = self.env["crm.lead"].sudo()
        SaleOrder = self.env["sale.order"].sudo()
        Conversation = self.env["bader.inbox.conversation"].sudo()

        merged_count = 0
        for other in others:
            Conversation.search([("partner_id", "=", other.id)]).write({"partner_id": keep.id})
            CrmLead.search([("partner_id", "=", other.id)]).write({"partner_id": keep.id})
            SaleOrder.search([("partner_id", "=", other.id)]).write({"partner_id": keep.id})

            # Fill missing fields from archived partner
            if not keep.email and other.email:
                keep.email = other.email
            if not keep.phone and other.phone:
                keep.phone = other.phone
            if not keep.mobile and other.mobile:
                keep.mobile = other.mobile
            if not keep.street and other.street:
                keep.street = other.street
            if not keep.city and other.city:
                keep.city = other.city
            if not keep.country_id and other.country_id:
                keep.country_id = other.country_id
            if not keep.state_id and other.state_id:
                keep.state_id = other.state_id
            if not keep.vat and other.vat:
                keep.vat = other.vat
            if not keep.comment and other.comment:
                keep.comment = other.comment

            other.active = False
            merged_count += 1
            _logger.info("Merged partner %s (ID %s) into %s (ID %s)", other.name, other.id, keep.name, keep.id)

        return {"success": True, "kept": keep.name, "merged": merged_count}

    @api.model
    def auto_link_partners(self):
        """Bulk fix: link res.partner to all existing conversations without partner_id.
        Call this once to fix historical data. Can be run from Odoo shell or scheduled action.
        """
        unlinked = self.search([("partner_id", "=", False), ("is_group", "=", False)])
        linked_count = 0
        for conv in unlinked:
            partner = self._find_partner_by_phone(conv.phone)
            if partner:
                conv.write({
                    "partner_id": partner.id,
                    "contact_name": partner.name,
                })
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

        clean_phone = self._clean_phone(phone)

        # Resolve the partner upfront (explicit id takes priority, then search)
        partner = None
        if partner_id:
            partner = self.env["res.partner"].browse(partner_id)
            if not partner.exists():
                partner = None
        if not partner:
            partner = self._find_partner_by_phone(clean_phone)

        # Find existing conversation (try exact, then suffix match)
        conversation = self.search([("phone", "=", clean_phone)], limit=1)
        if not conversation:
            # Suffix fallback: last 10 digits ilike
            suffix = clean_phone[-10:] if len(clean_phone) > 10 else clean_phone
            conversation = self.search([("phone", "ilike", suffix)], limit=1)

        if conversation:
            # Existing conversation: ensure partner is linked
            if partner and not conversation.partner_id:
                conversation.write({
                    "partner_id": partner.id,
                    "contact_name": partner.name,
                })
                _logger.info(
                    "Auto-linked partner %s (%s) to existing conversation %s",
                    partner.id, partner.name, conversation.id,
                )
        else:
            # Need to create — get a channel first
            channel = self.env["bader.inbox.channel"].search(
                [("state", "=", "connected")], limit=1
            ) or self.env["bader.inbox.channel"].search([], limit=1)
            if not channel:
                return {"success": False, "error": "No WhatsApp channel available"}

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
            "contact_name": conversation.computed_name,
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
        self.assigned_by_uid = self.env.user

    @api.model
    def action_assign_user(self, conversation_id, user_id):
        """Assign a specific user to a conversation and notify them."""
        conv = self.browse(conversation_id)
        if not conv.exists():
            return False
        old_user = conv.assigned_user_id
        conv.write({
            'assigned_user_id': user_id,
            'assigned_by_uid': self.env.uid,
        })
        self.env['bader.inbox.assignment.log'].sudo().create({
            'conversation_id': conversation_id,
            'action': 'assign',
            'user_id': user_id,
            'performed_by_id': self.env.uid,
        })
        new_user = self.env['res.users'].browse(user_id)
        if new_user.exists() and new_user.id != self.env.uid:
            assigner = self.env.user.name
            contact = conv.contact_name or conv.phone or _('Conversa')
            conv.message_post(
                body=_("%(assigner)s atribuiu-te esta conversa com %(contact)s.") % {
                    'assigner': assigner, 'contact': contact,
                },
                message_type="notification",
                subtype_xmlid="mail.mt_comment",
                partner_ids=[new_user.partner_id.id],
            )
            conv.activity_schedule(
                'mail.mail_activity_data_todo',
                user_id=new_user.id,
                summary=_("Conversa atribuída: %s") % contact,
                note=_("%(assigner)s atribuiu-te a conversa com %(contact)s.") % {
                    'assigner': assigner, 'contact': contact,
                },
            )
        return True

    @api.model
    def action_unassign(self, conversation_id):
        """Remove assignment. Only admin or the assigner can unassign."""
        conv = self.browse(conversation_id)
        if not conv.exists() or not conv.assigned_user_id:
            return False
        is_manager = self.env.user.has_group('bader_inbox.group_bader_inbox_manager')
        is_assigner = conv.assigned_by_uid.id == self.env.uid
        if not is_manager and not is_assigner:
            return {'error': 'No tiene permiso para desasignar esta conversación.'}
        old_user_id = conv.assigned_user_id.id
        conv.write({
            'assigned_user_id': False,
            'assigned_by_uid': False,
        })
        self.env['bader.inbox.assignment.log'].sudo().create({
            'conversation_id': conversation_id,
            'action': 'unassign',
            'user_id': old_user_id,
            'performed_by_id': self.env.uid,
        })
        return True

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

    # ── LID Merge Cron ─────────────────────────────────────────────

    @api.model
    def cron_lid_merge(self):
        """Resolve LID-based conversations and merge duplicates.

        Finds conversations whose phone looks like a LID number (very long
        numeric string, typically >12 digits, not a valid phone).
        For each, queries the Evolution API LID cache to try to resolve
        the real phone number. If a conversation with the real phone already
        exists on the same channel, merges messages into it.
        """
        import json

        # LID numbers are typically 15+ digit numeric strings
        # Real phone numbers are 7-15 digits
        self.env.cr.execute("""
            SELECT id, phone, channel_id, whatsapp_id
            FROM bader_inbox_conversation
            WHERE is_group = FALSE
              AND LENGTH(phone) > 15
              AND phone ~ '^[0-9]+$'
        """)
        lid_convs = self.env.cr.dictfetchall()

        if not lid_convs:
            return

        _logger.info(f"LID merge cron: found {len(lid_convs)} LID-based conversations to check")

        # Try to resolve via Odoo's LID cache (ir.config_parameter)
        params = self.env["ir.config_parameter"].sudo()
        raw_cache = params.get_param("bader_inbox.lid_phone_cache", "{}")
        try:
            lid_cache = json.loads(raw_cache) if raw_cache else {}
        except Exception:
            lid_cache = {}

        merged = 0
        resolved = 0

        for row in lid_convs:
            lid_phone = row["phone"]
            channel_id = row["channel_id"]
            conv_id = row["id"]

            # Try cache lookup: LID format is "123456@lid"
            real_phone = lid_cache.get(f"{lid_phone}@lid")

            if not real_phone:
                continue

            resolved += 1

            # Check if a conversation with the real phone already exists
            real_conv = self.search([
                ("channel_id", "=", channel_id),
                ("phone", "=", real_phone),
            ], limit=1)

            if real_conv:
                # Merge: move all messages from LID conv to real conv
                self.env.cr.execute("""
                    UPDATE bader_inbox_message
                    SET conversation_id = %s
                    WHERE conversation_id = %s
                """, [real_conv.id, conv_id])

                # Delete the LID conversation
                self.env.cr.execute("""
                    DELETE FROM bader_inbox_conversation WHERE id = %s
                """, [conv_id])

                merged += 1
                _logger.info(
                    f"LID merge: merged conversation {conv_id} "
                    f"(LID {lid_phone}) into {real_conv.id} (phone {real_phone})"
                )
            else:
                # No duplicate — just update the phone to the real number
                self.env.cr.execute("""
                    UPDATE bader_inbox_conversation
                    SET phone = %s, whatsapp_id = %s, write_date = NOW() AT TIME ZONE 'UTC'
                    WHERE id = %s
                """, [real_phone, f"{real_phone}@s.whatsapp.net", conv_id])
                _logger.info(
                    f"LID resolve: updated conversation {conv_id} "
                    f"phone {lid_phone} → {real_phone}"
                )

        if resolved > 0:
            self.env.cr.commit()
            _logger.info(f"LID merge cron done: {resolved} resolved, {merged} merged")


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


class BaderInboxAssignmentLog(models.Model):
    """Log of conversation assignment and unassignment actions."""

    _name = "bader.inbox.assignment.log"
    _description = "Assignment Log"
    _order = "create_date desc"

    conversation_id = fields.Many2one(
        "bader.inbox.conversation", string="Conversation",
        required=True, ondelete="cascade", index=True
    )
    action = fields.Selection([
        ("assign", "Assigned"),
        ("unassign", "Unassigned"),
    ], string="Action", required=True)
    user_id = fields.Many2one(
        "res.users", string="User",
        help="The user who was assigned/unassigned"
    )
    performed_by_id = fields.Many2one(
        "res.users", string="Performed By",
        help="The user who executed the action"
    )
