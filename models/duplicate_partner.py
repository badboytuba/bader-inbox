# Copyright 2026 Bader Business
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import logging
import re

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class BaderInboxDuplicateGroup(models.Model):
    """Group of res.partner records that share the same phone number."""

    _name = "bader.inbox.duplicate.group"
    _description = "Duplicate Contact Group"
    _order = "partner_count desc, id desc"

    phone_normalized = fields.Char(string="Número (normalizado)", readonly=True, index=True)
    phone_display = fields.Char(string="Teléfono", readonly=True)
    partner_ids = fields.Many2many("res.partner", string="Contactos Duplicados", readonly=True)
    partner_count = fields.Integer(string="Qty", compute="_compute_partner_count", store=True)
    partner_names = fields.Char(string="Nomes", compute="_compute_partner_count", store=True)
    state = fields.Selection([
        ("pending", "Pendiente"),
        ("merged", "Fusionado"),
        ("ignored", "Ignorado"),
    ], default="pending", string="Estado")

    @api.depends("partner_ids")
    def _compute_partner_count(self):
        for rec in self:
            rec.partner_count = len(rec.partner_ids)
            rec.partner_names = " | ".join(
                str(n) for n in rec.partner_ids.mapped("name")[:5] if n
            )

    @api.model
    def action_scan_duplicates(self):
        """Scan res_partner for duplicate phone numbers and create groups."""
        self.search([("state", "=", "pending")]).unlink()

        self.env.cr.execute("""
            WITH cleaned AS (
                SELECT
                    id,
                    regexp_replace(COALESCE(phone, ''), '[^0-9]', '', 'g') AS clean_phone,
                    regexp_replace(COALESCE(mobile, ''), '[^0-9]', '', 'g') AS clean_mobile
                FROM res_partner
                WHERE active = true
                  AND (phone IS NOT NULL AND phone != '' OR mobile IS NOT NULL AND mobile != '')
            ),
            all_phones AS (
                SELECT id, clean_phone AS num FROM cleaned WHERE clean_phone != '' AND LENGTH(clean_phone) >= 6
                UNION ALL
                SELECT id, clean_mobile AS num FROM cleaned WHERE clean_mobile != '' AND LENGTH(clean_mobile) >= 6
            ),
            -- Use last 10 digits for grouping to handle country code variations
            normalized AS (
                SELECT id, num,
                    CASE WHEN LENGTH(num) > 10 THEN RIGHT(num, 10) ELSE num END AS suffix
                FROM all_phones
            ),
            dupes AS (
                SELECT suffix, array_agg(DISTINCT id ORDER BY id) AS partner_ids
                FROM normalized
                GROUP BY suffix
                HAVING COUNT(DISTINCT id) > 1
            )
            SELECT suffix, partner_ids FROM dupes
            ORDER BY array_length(partner_ids, 1) DESC
        """)

        rows = self.env.cr.fetchall()
        created = 0
        ignored_phones = set(
            self.search([("state", "=", "ignored")]).mapped("phone_normalized")
        )

        for suffix, partner_ids in rows:
            if suffix in ignored_phones:
                continue
            partners = self.env["res.partner"].browse(partner_ids).exists()
            if len(partners) < 2:
                continue
            # Use full phone from first partner for display
            display = partners[0].phone or partners[0].mobile or suffix
            self.create({
                "phone_normalized": suffix,
                "phone_display": display,
                "partner_ids": [(6, 0, partners.ids)],
            })
            created += 1

        _logger.info("Duplicate scan complete: %d groups found", created)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Scan Completo"),
                "message": _("%d grupos de duplicados encontrados.") % created,
                "type": "success",
                "sticky": False,
            },
        }

    def action_merge(self):
        """Merge duplicate partners: keep the best one, transfer data from others."""
        self.ensure_one()
        if self.state != "pending":
            raise UserError(_("Este grupo já foi processado."))

        partners = self.partner_ids.sorted(key=lambda p: p.id)
        if len(partners) < 2:
            self.state = "merged"
            return True

        # Pick the "best" partner to keep:
        # Priority: has CRM leads > has sales > has inbox conversations > has email > oldest (lowest ID)
        best = partners[0]
        best_score = 0
        CrmLead = self.env["crm.lead"].sudo()
        SaleOrder = self.env["sale.order"].sudo()
        Conversation = self.env["bader.inbox.conversation"].sudo()

        for p in partners:
            score = 0
            if CrmLead.search_count([("partner_id", "=", p.id)], limit=1):
                score += 100
            if SaleOrder.search_count([("partner_id", "=", p.id)], limit=1):
                score += 50
            if Conversation.search_count([("partner_id", "=", p.id)], limit=1):
                score += 25
            if p.email:
                score += 10
            if p.phone and p.mobile:
                score += 5
            if score > best_score:
                best_score = score
                best = p

        others = partners - best
        _logger.info(
            "Merging partners: keeping %s (ID %s), merging %s",
            best.name or "Unnamed", best.id,
            ", ".join(f"{p.name or 'Unnamed'} (ID {p.id})" for p in others),
        )

        # Transfer relationships from others to best
        for other in others:
            # Transfer inbox conversations
            Conversation.search([("partner_id", "=", other.id)]).write({"partner_id": best.id})

            # Transfer CRM leads
            CrmLead.search([("partner_id", "=", other.id)]).write({"partner_id": best.id})

            # Transfer sale orders
            SaleOrder.search([("partner_id", "=", other.id)]).write({"partner_id": best.id})

            # Fill missing fields on best from other
            if not best.email and other.email:
                best.email = other.email
            if not best.phone and other.phone:
                best.phone = other.phone
            if not best.mobile and other.mobile:
                best.mobile = other.mobile
            if not best.street and other.street:
                best.street = other.street
            if not best.city and other.city:
                best.city = other.city
            if not best.vat and other.vat:
                best.vat = other.vat

            # Archive the duplicate (don't delete — safer)
            other.active = False

        self.state = "merged"
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Fusión Completa"),
                "message": _("Se mantuvo '%s' y se archivaron %d duplicados.") % (best.name or "Sin nombre", len(others)),
                "type": "success",
                "sticky": False,
            },
        }

    def action_merge_all(self):
        """Merge all pending duplicate groups."""
        pending = self.search([("state", "=", "pending")])
        merged = 0
        errors = 0
        for group in pending:
            try:
                group.action_merge()
                merged += 1
            except Exception as e:
                _logger.error("Failed to merge group %s: %s", group.id, e)
                errors += 1
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Fusión Masiva"),
                "message": _("%d grupos fusionados, %d errores.") % (merged, errors),
                "type": "success" if not errors else "warning",
                "sticky": False,
            },
        }

    def action_ignore(self):
        """Mark this group as ignored (not duplicates, e.g. shared phone)."""
        self.write({"state": "ignored"})

    def action_open_partners(self):
        """Open the partner list for this group."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Contactos Duplicados"),
            "res_model": "res.partner",
            "view_mode": "tree,form",
            "domain": [("id", "in", self.partner_ids.ids)],
            "target": "current",
        }
