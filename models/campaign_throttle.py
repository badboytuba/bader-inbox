# Copyright 2026 Bader Business
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import logging
from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)

# Opt-out keywords in multiple languages
OPTOUT_KEYWORDS = [
    "parar", "stop", "cancelar", "cancel", "sair", "unsubscribe",
    "no quiero", "no deseo", "basta", "remover", "remove",
    "desuscribir", "desregistrar", "eliminar", "salir",
]


class BaderInboxCampaignThrottle(models.Model):
    """Anti-ban throttle configuration profile.
    Defines all rate-limiting and humanization parameters."""

    _name = "bader.inbox.campaign.throttle"
    _description = "Campaign Anti-Ban Profile"
    _order = "name"

    name = fields.Char(string="Nome do Perfil", required=True)
    description = fields.Text(string="Descrição")

    # ── Batch & Rate Limits ──────────────────────────────────────────────
    batch_size = fields.Integer(
        string="Tamanho do Lote", default=20,
        help="Mensagens por lote antes de uma pausa maior"
    )
    batch_pause_min = fields.Integer(
        string="Pausa entre Lotes (mín seg)", default=120,
        help="Pausa mínima entre lotes em segundos"
    )
    batch_pause_max = fields.Integer(
        string="Pausa entre Lotes (máx seg)", default=300,
        help="Pausa máxima entre lotes em segundos"
    )
    msg_delay_min_sec = fields.Integer(
        string="Delay entre Msgs (mín seg)", default=3,
        help="Delay mínimo entre mensagens individuais"
    )
    msg_delay_max_sec = fields.Integer(
        string="Delay entre Msgs (máx seg)", default=8,
        help="Delay máximo entre mensagens individuais"
    )
    daily_limit = fields.Integer(
        string="Limite Diário/Número", default=200,
        help="Máximo de mensagens por dia por número WhatsApp"
    )
    hourly_limit = fields.Integer(
        string="Limite Horário/Número", default=30,
        help="Máximo de mensagens por hora por número"
    )

    # ── Schedule Windows ─────────────────────────────────────────────────
    schedule_start = fields.Float(
        string="Hora Início", default=9.0,
        help="Hora de início de envios (ex: 9.0 = 09:00)"
    )
    schedule_end = fields.Float(
        string="Hora Fim", default=20.0,
        help="Hora de fim de envios (ex: 20.0 = 20:00)"
    )
    schedule_days = fields.Char(
        string="Dias Ativos", default="1,2,3,4,5",
        help="Dias da semana (1=Seg, 7=Dom). Ex: 1,2,3,4,5 = Seg-Sex"
    )

    # ── Number Rotation ──────────────────────────────────────────────────
    rotation_enabled = fields.Boolean(
        string="Rotação de Números", default=False,
        help="Alternar envios entre múltiplos números WhatsApp"
    )

    # ── Warm-up ──────────────────────────────────────────────────────────
    warmup_enabled = fields.Boolean(
        string="Warm-up Gradual", default=False,
        help="Aumentar lentamente o volume de envio em números novos"
    )
    warmup_daily_start = fields.Integer(
        string="Warm-up: Msgs/dia Inicial", default=10
    )
    warmup_daily_increment = fields.Integer(
        string="Warm-up: Aumento Diário", default=10
    )
    warmup_days = fields.Integer(
        string="Warm-up: Duração (dias)", default=14
    )

    # ── Message Variation ────────────────────────────────────────────────
    variation_enabled = fields.Boolean(
        string="Variação de Mensagem", default=True,
        help="Adicionar variações subtis ao texto automaticamente"
    )

    # ── Typing Simulation ────────────────────────────────────────────────
    typing_simulation = fields.Boolean(
        string="Simular Digitação", default=True,
        help="Enviar status 'digitando...' antes da mensagem"
    )
    typing_delay_per_char_ms = fields.Integer(
        string="Delay por Carácter (ms)", default=50,
        help="Milissegundos por carácter para simular velocidade de digitação"
    )

    # ── Circuit Breaker ──────────────────────────────────────────────────
    circuit_breaker_enabled = fields.Boolean(
        string="Circuit Breaker", default=True,
        help="Pausar campanha automaticamente se taxa de erro ultrapassa limite"
    )
    circuit_breaker_threshold = fields.Float(
        string="Limite de Falha (%)", default=10.0,
        help="Pausar se % de falhas excede este valor nas últimas 50 msgs"
    )
    circuit_breaker_window = fields.Integer(
        string="Janela de Análise", default=50,
        help="Número de mensagens recentes a analisar"
    )

    # ── Contact Fatigue ──────────────────────────────────────────────────
    fatigue_max_msgs_week = fields.Integer(
        string="Máx Msgs/Semana/Contacto", default=3,
        help="Limite de mensagens de campanha por contacto por semana"
    )

    # ── Behavioral Pauses ────────────────────────────────────────────────
    behavioral_pauses = fields.Boolean(
        string="Pausas Comportamentais", default=True,
        help="Inserir pausas longas aleatórias (5-15min) para simular comportamento humano"
    )
    behavioral_pause_probability = fields.Float(
        string="Probabilidade de Pausa (%)", default=15.0,
        help="Chance de inserir uma pausa longa entre lotes"
    )

    active = fields.Boolean(default=True)


class BaderInboxCampaignOptout(models.Model):
    """Contact opt-out registry"""

    _name = "bader.inbox.campaign.optout"
    _description = "Campaign Opt-out"
    _order = "create_date desc"

    phone = fields.Char(string="Telefone", required=True, index=True)
    partner_id = fields.Many2one(
        "res.partner", string="Contacto", ondelete="set null"
    )
    reason = fields.Selection([
        ("keyword", "Palavra-chave"),
        ("manual", "Manual"),
        ("complaint", "Reclamação"),
    ], default="keyword")
    keyword_used = fields.Char(string="Palavra usada")
    campaign_id = fields.Many2one(
        "bader.inbox.campaign", string="Campanha de origem"
    )
    opted_out_at = fields.Datetime(
        string="Data do Opt-out", default=fields.Datetime.now
    )

    _sql_constraints = [
        ("unique_phone", "UNIQUE(phone)", "Este telefone já está na lista de opt-out"),
    ]

    @api.model
    def check_and_optout(self, phone, message_text, campaign_id=None):
        """Check if message contains opt-out keyword and register"""
        if not message_text:
            return False
        text_lower = message_text.strip().lower()

        # Check custom keywords from settings
        custom_keywords = self.env["ir.config_parameter"].sudo().get_param(
            "bader_inbox.optout_keywords", ""
        )
        all_keywords = OPTOUT_KEYWORDS.copy()
        if custom_keywords:
            all_keywords.extend([k.strip() for k in custom_keywords.split(",") if k.strip()])

        for keyword in all_keywords:
            if keyword in text_lower:
                # Check if already opted out
                existing = self.search([("phone", "=", phone)], limit=1)
                if not existing:
                    partner = self.env["res.partner"].search([
                        "|", ("phone", "ilike", phone[-9:]),
                        ("mobile", "ilike", phone[-9:])
                    ], limit=1)

                    self.create({
                        "phone": phone,
                        "partner_id": partner.id if partner else False,
                        "reason": "keyword",
                        "keyword_used": keyword,
                        "campaign_id": campaign_id,
                    })
                    _logger.info(f"Opt-out registered for {phone} (keyword: {keyword})")
                return True

        return False
