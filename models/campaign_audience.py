# Copyright 2026 Bader Business
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import logging
from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)


class BaderInboxCampaignAudience(models.Model):
    """Audience segment for campaigns — filter contacts by tags, fields, etc."""

    _name = "bader.inbox.campaign.audience"
    _description = "Campaign Audience"
    _order = "name"

    name = fields.Char(string="Nome", required=True)
    description = fields.Text(string="Descrição")

    # Filter by tags (res.partner.category)
    tag_ids = fields.Many2many(
        "res.partner.category", string="Tags",
        help="Filtrar contactos por tags do Odoo"
    )
    tag_mode = fields.Selection([
        ("any", "Qualquer tag (OR)"),
        ("all", "Todas as tags (AND)"),
    ], default="any", string="Modo de Tags")

    # Filter by partner fields
    country_ids = fields.Many2many(
        "res.country", string="Países"
    )
    has_phone = fields.Boolean(
        string="Só com telemóvel", default=True,
        help="Incluir apenas contactos com número de telemóvel"
    )
    has_email = fields.Boolean(string="Só com email", default=False)

    # Filter by activity
    has_order_last_days = fields.Integer(
        string="Com pedido nos últimos X dias", default=0,
        help="0 = ignorar filtro"
    )
    no_order_last_days = fields.Integer(
        string="Sem pedido nos últimos X dias", default=0,
        help="0 = ignorar filtro"
    )

    # Custom domain
    custom_domain = fields.Char(
        string="Domínio Personalizado",
        help="Domínio Odoo extra em formato JSON. Ex: [['is_company','=',True]]"
    )

    # Exclusion
    exclude_opted_out = fields.Boolean(
        string="Excluir opt-out", default=True
    )

    # Computed
    contact_count = fields.Integer(
        compute="_compute_contact_count", string="Contactos"
    )
    contact_ids = fields.Many2many(
        "res.partner", compute="_compute_contacts", string="Contactos",
        store=False
    )

    active = fields.Boolean(default=True)

    @api.depends("tag_ids", "tag_mode", "country_ids", "has_phone", "custom_domain")
    def _compute_contact_count(self):
        for rec in self:
            rec.contact_count = len(rec._get_contacts())

    def _compute_contacts(self):
        for rec in self:
            rec.contact_ids = rec._get_contacts()

    def _get_contacts(self):
        """Build and execute domain to get matching contacts"""
        self.ensure_one()
        domain = []

        # Phone filter
        if self.has_phone:
            domain.append("|")
            domain.append(("mobile", "!=", False))
            domain.append(("phone", "!=", False))

        # Email filter
        if self.has_email:
            domain.append(("email", "!=", False))

        # Tag filter
        if self.tag_ids:
            if self.tag_mode == "all":
                for tag in self.tag_ids:
                    domain.append(("category_id", "in", [tag.id]))
            else:
                domain.append(("category_id", "in", self.tag_ids.ids))

        # Country filter
        if self.country_ids:
            domain.append(("country_id", "in", self.country_ids.ids))

        # Order history
        if self.has_order_last_days > 0:
            from datetime import timedelta
            since = fields.Datetime.now() - timedelta(days=self.has_order_last_days)
            partners_with_orders = self.env["sale.order"].search([
                ("state", "in", ["sale", "done"]),
                ("date_order", ">=", since),
            ]).mapped("partner_id.id")
            if partners_with_orders:
                domain.append(("id", "in", partners_with_orders))
            else:
                domain.append(("id", "=", 0))

        if self.no_order_last_days > 0:
            from datetime import timedelta
            since = fields.Datetime.now() - timedelta(days=self.no_order_last_days)
            partners_with_orders = self.env["sale.order"].search([
                ("state", "in", ["sale", "done"]),
                ("date_order", ">=", since),
            ]).mapped("partner_id.id")
            domain.append(("id", "not in", partners_with_orders))

        # Custom domain
        if self.custom_domain:
            import json
            try:
                extra_domain = json.loads(self.custom_domain)
                domain.extend(extra_domain)
            except Exception:
                pass

        # Opt-out exclusion
        if self.exclude_opted_out:
            opted_out_phones = self.env["bader.inbox.campaign.optout"].search([]).mapped("phone")
            if opted_out_phones:
                domain.append("&")
                domain.append(("mobile", "not in", opted_out_phones))
                domain.append(("phone", "not in", opted_out_phones))

        partners = self.env["res.partner"].search(domain)
        return partners

    def action_preview_contacts(self):
        """Open list of matching contacts"""
        self.ensure_one()
        contacts = self._get_contacts()
        return {
            "type": "ir.actions.act_window",
            "name": f"Contactos: {self.name}",
            "res_model": "res.partner",
            "view_mode": "tree,form",
            "domain": [("id", "in", contacts.ids)],
        }


class BaderInboxCampaignABVariant(models.Model):
    """A/B testing variant"""

    _name = "bader.inbox.campaign.ab.variant"
    _description = "A/B Test Variant"
    _order = "name"

    name = fields.Char(string="Nome", required=True, default="A")
    campaign_id = fields.Many2one(
        "bader.inbox.campaign", string="Campanha",
        required=True, ondelete="cascade"
    )
    percentage = fields.Float(
        string="Percentagem (%)", default=50.0,
        help="Percentagem de audiência para esta variante"
    )
    message_content = fields.Text(string="Conteúdo da Mensagem")
    media_url = fields.Char(string="URL Media")

    # Stats
    stat_sent = fields.Integer(string="Enviadas", default=0)
    stat_read = fields.Integer(string="Lidas", default=0)
    stat_replied = fields.Integer(string="Respondidas", default=0)
    stat_converted = fields.Integer(string="Convertidas", default=0)

    is_winner = fields.Boolean(string="Vencedora", default=False)
    is_active = fields.Boolean(string="Ativa", default=True)

    @api.depends("stat_sent", "stat_replied")
    def _compute_reply_rate(self):
        for rec in self:
            rec.reply_rate = (
                (rec.stat_replied / rec.stat_sent * 100) if rec.stat_sent > 0 else 0
            )

    reply_rate = fields.Float(
        compute="_compute_reply_rate", string="Taxa de Resposta (%)"
    )
