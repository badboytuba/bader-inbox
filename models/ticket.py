# Copyright 2026 Bader Business
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo import api, fields, models, _


class BaderInboxTicket(models.Model):
    _name = "bader.inbox.ticket"
    _description = "Bader Inbox - Tickets (Melhorias & Bugs)"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "priority desc, create_date desc"

    name = fields.Char(string="Título", required=True, tracking=True)
    description = fields.Html(
        string="Descrição",
        sanitize=True,
        sanitize_attributes=False,
        sanitize_form=False,
        strip_style=False,
    )
    ticket_type = fields.Selection(
        [("bug", "🐛 Bug"),
         ("improvement", "✨ Melhoria"),
         ("question", "❓ Dúvida")],
        string="Tipo", required=True, default="improvement", tracking=True,
    )
    priority = fields.Selection(
        [("0", "Normal"),
         ("1", "Alta"),
         ("2", "Urgente"),
         ("3", "Crítica")],
        string="Prioridade", default="0", tracking=True,
    )
    state = fields.Selection(
        [("new", "Novo"),
         ("analyzing", "Em análise"),
         ("in_progress", "Em desenvolvimento"),
         ("waiting_info", "A aguardar info"),
         ("resolved", "Resolvido"),
         ("cancelled", "Cancelado")],
        string="Estado", default="new", tracking=True, required=True,
    )
    user_id = fields.Many2one(
        "res.users", string="Solicitante",
        default=lambda self: self.env.user, required=True, tracking=True,
        index=True,
    )
    assigned_to_id = fields.Many2one(
        "res.users", string="Atribuído a", tracking=True,
    )
    attachment_ids = fields.Many2many(
        "ir.attachment",
        "bader_inbox_ticket_attachment_rel",
        "ticket_id", "attachment_id",
        string="Anexos",
    )
    resolution_notes = fields.Html(string="Notas de resolução")
    commit_ref = fields.Char(string="Commit/Deploy de resolução")
    resolved_date = fields.Datetime(string="Data de resolução", readonly=True)
    kanban_color = fields.Integer(string="Cor", default=0)

    @api.model_create_multi
    def create(self, vals_list):
        tickets = super().create(vals_list)
        for ticket in tickets:
            ticket.message_subscribe(partner_ids=[ticket.user_id.partner_id.id])
        return tickets

    def write(self, vals):
        res = super().write(vals)
        if "state" in vals and vals["state"] == "resolved":
            for t in self:
                if not t.resolved_date:
                    t.resolved_date = fields.Datetime.now()
        return res

    def action_start(self):
        self.write({"state": "in_progress"})

    def action_request_info(self):
        for t in self:
            t.state = "waiting_info"
            if t.user_id and t.user_id.id != self.env.uid:
                t.activity_schedule(
                    "mail.mail_activity_data_todo",
                    user_id=t.user_id.id,
                    summary=_("Informação adicional pedida no ticket: %s") % t.name,
                    note=_("Precisamos de mais informação para continuar com este ticket."),
                )

    def action_resolve(self):
        for t in self:
            t.write({"state": "resolved"})
            if t.user_id and t.user_id.id != self.env.uid:
                t.activity_schedule(
                    "mail.mail_activity_data_todo",
                    user_id=t.user_id.id,
                    summary=_("Ticket resolvido: %s") % t.name,
                    note=t.resolution_notes or _("O teu ticket foi resolvido."),
                )

    def action_cancel(self):
        self.write({"state": "cancelled"})

    def action_reopen(self):
        self.write({"state": "analyzing", "resolved_date": False})
