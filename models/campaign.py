# Copyright 2026 Bader Business
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import json
import logging
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


# ─── Node type/subtype definitions ───────────────────────────────────────────

TRIGGER_SUBTYPES = [
    ("cart_abandoned", "Carrinho Abandonado"),
    ("order_confirmed", "Pedido Confirmado"),
    ("order_shipped", "Pedido Enviado"),
    ("invoice_overdue", "Fatura Vencida"),
    ("lead_created", "Novo Lead"),
    ("lead_stage_changed", "Lead Mudou Etapa"),
    ("opportunity_won", "Oportunidade Ganha"),
    ("opportunity_lost", "Oportunidade Perdida"),
    ("ticket_created", "Ticket Criado"),
    ("website_visit", "Visita Website"),
    ("product_viewed", "Produto Visualizado"),
    ("tag_added", "Tag Adicionada"),
    ("manual_segment", "Segmento Manual"),
    ("date_trigger", "Data/Aniversário"),
    ("webhook_received", "Webhook Externo"),
]

ACTION_SUBTYPES = [
    ("send_text", "Enviar Texto"),
    ("send_media", "Enviar Media"),
    ("send_product_card", "Enviar Card Produto"),
    ("send_catalog", "Enviar Catálogo"),
    ("send_template", "Enviar Template"),
    ("update_crm", "Atualizar CRM"),
    ("add_tag", "Adicionar Tag"),
    ("remove_tag", "Remover Tag"),
    ("assign_pipeline", "Mover Pipeline"),
    ("create_task", "Criar Atividade"),
    ("http_request", "HTTP Request"),
    ("ai_generate", "IA Gerar Mensagem"),
]

CONDITION_SUBTYPES = [
    ("has_tag", "Tem Tag?"),
    ("replied", "Respondeu?"),
    ("read_message", "Leu Mensagem?"),
    ("clicked_link", "Clicou Link?"),
    ("has_order", "Tem Pedido?"),
    ("field_value", "Campo = Valor?"),
    ("time_elapsed", "Tempo Passado?"),
    ("ab_split", "Split A/B"),
]

DELAY_SUBTYPES = [
    ("wait_fixed", "Esperar Tempo Fixo"),
    ("wait_until", "Esperar Até Hora"),
    ("wait_event", "Esperar Evento"),
]

ALL_SUBTYPES = TRIGGER_SUBTYPES + ACTION_SUBTYPES + CONDITION_SUBTYPES + DELAY_SUBTYPES


class BaderInboxCampaign(models.Model):
    """Marketing campaign orchestrator"""

    _name = "bader.inbox.campaign"
    _description = "Marketing Campaign"
    _order = "create_date desc"
    _inherit = ["mail.thread"]

    name = fields.Char(string="Name", required=True, tracking=True)
    description = fields.Text(string="Description")
    status = fields.Selection([
        ("draft", "Rascunho"),
        ("active", "Ativa"),
        ("paused", "Pausada"),
        ("completed", "Concluída"),
        ("cancelled", "Cancelada"),
    ], default="draft", tracking=True, index=True)

    campaign_type = fields.Selection([
        ("one_shot", "Envio Único"),
        ("triggered", "Automática (Trigger)"),
        ("recurring", "Recorrente"),
    ], default="one_shot", required=True, string="Tipo")

    # Flow
    flow_id = fields.Many2one(
        "bader.inbox.campaign.flow", string="Fluxo",
        ondelete="set null"
    )

    # Audience
    audience_ids = fields.Many2many(
        "bader.inbox.campaign.audience", string="Audiências"
    )

    # Anti-ban config
    throttle_id = fields.Many2one(
        "bader.inbox.campaign.throttle", string="Perfil Anti-Ban",
        required=True
    )

    # Channels for rotation
    channel_ids = fields.Many2many(
        "bader.inbox.channel", string="Canais WhatsApp",
        help="Canais disponíveis para rotação"
    )

    # Schedule
    start_date = fields.Datetime(string="Início")
    end_date = fields.Datetime(string="Fim")

    # A/B Testing
    ab_enabled = fields.Boolean(string="A/B Testing")
    ab_variant_ids = fields.One2many(
        "bader.inbox.campaign.ab.variant", "campaign_id", string="Variantes A/B"
    )
    ab_auto_winner = fields.Boolean(
        string="Auto-Winner", default=True,
        help="Pausar variante perdedora após X envios"
    )
    ab_auto_winner_count = fields.Integer(
        string="Msgs antes de escolher", default=200
    )

    # Stats (computed)
    total_audience = fields.Integer(compute="_compute_stats", string="Audiência Total")
    stat_sent = fields.Integer(string="Enviadas", default=0)
    stat_delivered = fields.Integer(string="Entregues", default=0)
    stat_read = fields.Integer(string="Lidas", default=0)
    stat_replied = fields.Integer(string="Respondidas", default=0)
    stat_failed = fields.Integer(string="Falhadas", default=0)
    stat_opted_out = fields.Integer(string="Opt-out", default=0)

    # Execution tracking
    execution_ids = fields.One2many(
        "bader.inbox.campaign.execution", "campaign_id", string="Execuções"
    )
    execution_count = fields.Integer(compute="_compute_stats")

    # Owner
    user_id = fields.Many2one(
        "res.users", string="Responsável",
        default=lambda s: s.env.user
    )

    @api.depends("execution_ids", "audience_ids")
    def _compute_stats(self):
        for rec in self:
            rec.execution_count = len(rec.execution_ids)
            rec.total_audience = sum(a.contact_count for a in rec.audience_ids)

    def action_activate(self):
        """Start or resume campaign"""
        for rec in self:
            if not rec.flow_id:
                raise UserError(_("Configure um fluxo antes de ativar a campanha."))
            if not rec.channel_ids:
                raise UserError(_("Selecione pelo menos um canal WhatsApp."))
            rec.status = "active"

    def action_pause(self):
        for rec in self:
            rec.status = "paused"

    def action_cancel(self):
        for rec in self:
            rec.status = "cancelled"

    def action_complete(self):
        for rec in self:
            rec.status = "completed"

    def action_open_flow_designer(self):
        """Open the visual flow designer"""
        self.ensure_one()
        if not self.flow_id:
            flow = self.env["bader.inbox.campaign.flow"].create({
                "name": f"Fluxo - {self.name}",
                "campaign_id": self.id,
            })
            self.flow_id = flow.id
        return {
            "type": "ir.actions.client",
            "tag": "bader_inbox_flow_designer",
            "params": {
                "campaign_id": self.id,
                "flow_id": self.flow_id.id,
            },
        }

    @api.model
    def _cron_circuit_breaker_check(self):
        """Check active campaigns for high failure rates and auto-pause"""
        active_campaigns = self.search([("status", "=", "active")])
        ExecLog = self.env["bader.inbox.campaign.exec.log"]

        for campaign in active_campaigns:
            throttle = campaign.throttle_id
            if not throttle or not throttle.circuit_breaker_enabled:
                continue

            window = throttle.circuit_breaker_window or 50
            threshold = throttle.circuit_breaker_threshold or 10.0

            # Get recent logs for this campaign
            recent_logs = ExecLog.search([
                ("execution_id.campaign_id", "=", campaign.id),
                ("action", "in", [
                    "send_text", "send_media",
                    "send_product_card", "send_template"
                ]),
            ], limit=window, order="executed_at desc")

            if len(recent_logs) < 10:
                continue  # Not enough data

            failed = len(recent_logs.filtered(lambda l: l.result == "failed"))
            failure_rate = (failed / len(recent_logs)) * 100

            if failure_rate >= threshold:
                campaign.status = "paused"
                campaign.message_post(
                    body=_(
                        "⚠️ <b>Circuit Breaker ativado!</b><br/>"
                        "Campanha pausada automaticamente.<br/>"
                        "Taxa de falha: <b>%.1f%%</b> (limite: %.1f%%)<br/>"
                        "Últimas %d mensagens analisadas."
                    ) % (failure_rate, threshold, len(recent_logs)),
                    message_type="notification",
                )
                _logger.warning(
                    f"Circuit breaker: paused campaign {campaign.id} "
                    f"({campaign.name}) — failure rate {failure_rate:.1f}%"
                )



class BaderInboxCampaignFlow(models.Model):
    """Visual flow — the graph of nodes and edges"""

    _name = "bader.inbox.campaign.flow"
    _description = "Campaign Flow"
    _order = "name"

    name = fields.Char(string="Name", required=True)
    campaign_id = fields.Many2one(
        "bader.inbox.campaign", string="Campanha", ondelete="cascade"
    )
    node_ids = fields.One2many(
        "bader.inbox.campaign.node", "flow_id", string="Nós"
    )
    edge_ids = fields.One2many(
        "bader.inbox.campaign.edge", "flow_id", string="Conexões"
    )
    canvas_data = fields.Text(
        string="Canvas Data (JSON)",
        help="Zoom, scroll position, etc."
    )
    is_template = fields.Boolean(
        string="Template Reutilizável", default=False
    )
    active = fields.Boolean(default=True)

    def get_flow_data(self):
        """Return full flow structure as JSON for the frontend designer"""
        self.ensure_one()
        nodes = []
        for node in self.node_ids:
            nodes.append({
                "id": node.id,
                "type": node.node_type,
                "subtype": node.node_subtype,
                "label": node.label or node.node_subtype,
                "config": json.loads(node.config or "{}"),
                "pos_x": node.pos_x,
                "pos_y": node.pos_y,
                "color": node.color,
            })
        edges = []
        for edge in self.edge_ids:
            edges.append({
                "id": edge.id,
                "source": edge.source_node_id.id,
                "target": edge.target_node_id.id,
                "label": edge.label or "",
                "condition_output": edge.condition_output,
            })
        return {
            "id": self.id,
            "name": self.name,
            "nodes": nodes,
            "edges": edges,
            "canvas": json.loads(self.canvas_data or "{}"),
        }

    def save_flow_data(self, data):
        """Save flow from frontend designer"""
        self.ensure_one()
        Node = self.env["bader.inbox.campaign.node"]
        Edge = self.env["bader.inbox.campaign.edge"]

        # Canvas position
        if data.get("canvas"):
            self.canvas_data = json.dumps(data["canvas"])

        # Sync nodes
        existing_node_ids = set(self.node_ids.ids)
        incoming_node_ids = set()

        for node_data in data.get("nodes", []):
            node_id = node_data.get("id")
            vals = {
                "flow_id": self.id,
                "node_type": node_data["type"],
                "node_subtype": node_data["subtype"],
                "label": node_data.get("label", ""),
                "config": json.dumps(node_data.get("config", {})),
                "pos_x": node_data.get("pos_x", 0),
                "pos_y": node_data.get("pos_y", 0),
                "color": node_data.get("color", ""),
            }

            if node_id and node_id in existing_node_ids:
                Node.browse(node_id).write(vals)
                incoming_node_ids.add(node_id)
            else:
                new_node = Node.create(vals)
                incoming_node_ids.add(new_node.id)

        # Delete removed nodes
        to_delete = existing_node_ids - incoming_node_ids
        if to_delete:
            Node.browse(list(to_delete)).unlink()

        # Sync edges (simpler: delete all and recreate)
        self.edge_ids.unlink()
        for edge_data in data.get("edges", []):
            Edge.create({
                "flow_id": self.id,
                "source_node_id": edge_data["source"],
                "target_node_id": edge_data["target"],
                "label": edge_data.get("label", ""),
                "condition_output": edge_data.get("condition_output", "default"),
            })

        return True

    def validate_flow(self):
        """Check flow is valid before activating campaign"""
        self.ensure_one()
        errors = []
        triggers = self.node_ids.filtered(lambda n: n.node_type == "trigger")
        if not triggers:
            errors.append(_("O fluxo precisa de pelo menos um nó Trigger."))
        actions = self.node_ids.filtered(lambda n: n.node_type == "action")
        if not actions:
            errors.append(_("O fluxo precisa de pelo menos um nó Action."))

        # Check for orphan nodes (no connections)
        connected_ids = set()
        for edge in self.edge_ids:
            connected_ids.add(edge.source_node_id.id)
            connected_ids.add(edge.target_node_id.id)

        for node in self.node_ids:
            if node.id not in connected_ids and len(self.node_ids) > 1:
                errors.append(_("Nó '%s' não está conectado a nenhum outro nó.") % node.label)

        if errors:
            raise ValidationError("\n".join(errors))
        return True


class BaderInboxCampaignNode(models.Model):
    """A node in the flow — trigger, action, condition, or delay"""

    _name = "bader.inbox.campaign.node"
    _description = "Campaign Flow Node"
    _order = "id"

    flow_id = fields.Many2one(
        "bader.inbox.campaign.flow", string="Fluxo",
        required=True, ondelete="cascade", index=True
    )
    node_type = fields.Selection([
        ("trigger", "Trigger"),
        ("action", "Action"),
        ("condition", "Condition"),
        ("delay", "Delay"),
    ], required=True, string="Tipo")

    node_subtype = fields.Selection(
        ALL_SUBTYPES, string="Subtipo", required=True
    )
    label = fields.Char(string="Label")
    config = fields.Text(
        string="Config (JSON)", default="{}",
        help="Node-specific configuration in JSON format"
    )
    pos_x = fields.Integer(string="X Position", default=0)
    pos_y = fields.Integer(string="Y Position", default=0)
    color = fields.Char(string="Color", default="")

    # Edges
    outgoing_edges = fields.One2many(
        "bader.inbox.campaign.edge", "source_node_id", string="Outgoing"
    )
    incoming_edges = fields.One2many(
        "bader.inbox.campaign.edge", "target_node_id", string="Incoming"
    )

    def get_config(self):
        """Parse JSON config"""
        self.ensure_one()
        return json.loads(self.config or "{}")

    def get_next_nodes(self, condition_output="default"):
        """Get next nodes based on edge condition"""
        self.ensure_one()
        edges = self.outgoing_edges.filtered(
            lambda e: e.condition_output == condition_output or e.condition_output == "default"
        )
        return edges.mapped("target_node_id")


class BaderInboxCampaignEdge(models.Model):
    """Connection between two nodes"""

    _name = "bader.inbox.campaign.edge"
    _description = "Campaign Flow Edge"

    flow_id = fields.Many2one(
        "bader.inbox.campaign.flow", string="Fluxo",
        required=True, ondelete="cascade", index=True
    )
    source_node_id = fields.Many2one(
        "bader.inbox.campaign.node", string="From",
        required=True, ondelete="cascade"
    )
    target_node_id = fields.Many2one(
        "bader.inbox.campaign.node", string="To",
        required=True, ondelete="cascade"
    )
    label = fields.Char(string="Label")
    condition_output = fields.Selection([
        ("default", "Default"),
        ("true", "Sim/True"),
        ("false", "Não/False"),
    ], default="default", string="Condition Output")
