# Copyright 2026 Bader Business
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import logging
from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class BaderInboxPipeline(models.Model):
    """Pipeline/Flow - represents a business workflow (Clínica, Laboratório, etc.)"""
    
    _name = "bader.inbox.pipeline"
    _description = "Bader Inbox Pipeline"
    _order = "sequence, name"

    name = fields.Char(string="Name", required=True)
    icon = fields.Char(string="Icon", default="📊", help="Emoji icon for this pipeline")
    color = fields.Integer(string="Color")
    description = fields.Text(string="Description")
    sequence = fields.Integer(string="Sequence", default=10)
    active = fields.Boolean(string="Active", default=True)
    
    # Stages
    stage_ids = fields.One2many(
        "bader.inbox.pipeline.stage", "pipeline_id", string="Stages",
        copy=True
    )
    stage_count = fields.Integer(
        compute="_compute_counts", string="Stages"
    )
    
    # Stats
    assignment_ids = fields.One2many(
        "bader.inbox.conversation.pipeline", "pipeline_id", string="Assignments"
    )
    assignment_count = fields.Integer(
        compute="_compute_counts", string="Conversations"
    )

    @api.depends("stage_ids", "assignment_ids")
    def _compute_counts(self):
        for rec in self:
            rec.stage_count = len(rec.stage_ids)
            rec.assignment_count = len(rec.assignment_ids)

    @api.model_create_multi
    def create(self, vals_list):
        pipelines = super().create(vals_list)
        # Auto-create default stages for new pipelines that don't have any
        for pipeline in pipelines:
            if not pipeline.stage_ids:
                self.env["bader.inbox.pipeline.stage"].create([
                    {"name": "Novo", "pipeline_id": pipeline.id, "sequence": 1},
                    {"name": "Em Progresso", "pipeline_id": pipeline.id, "sequence": 2},
                    {"name": "Concluído", "pipeline_id": pipeline.id, "sequence": 3, "fold": True},
                ])
        return pipelines


class BaderInboxPipelineStage(models.Model):
    """Stage within a Pipeline"""
    
    _name = "bader.inbox.pipeline.stage"
    _description = "Pipeline Stage"
    _order = "sequence, id"

    name = fields.Char(string="Name", required=True)
    pipeline_id = fields.Many2one(
        "bader.inbox.pipeline", string="Pipeline",
        required=True, ondelete="cascade"
    )
    sequence = fields.Integer(string="Sequence", default=10)
    fold = fields.Boolean(string="Fold in Kanban", default=False)
    description = fields.Text(string="Description")
    
    # Stats
    assignment_count = fields.Integer(
        compute="_compute_assignment_count", string="Conversations"
    )

    def _compute_assignment_count(self):
        for rec in self:
            rec.assignment_count = self.env["bader.inbox.conversation.pipeline"].search_count([
                ("stage_id", "=", rec.id)
            ])


class BaderInboxConversationPipeline(models.Model):
    """Junction: Conversation assigned to a Pipeline at a specific Stage.
    This is the model that powers the Kanban view."""
    
    _name = "bader.inbox.conversation.pipeline"
    _description = "Conversation Pipeline Assignment"
    _order = "pipeline_id, stage_sequence, id"
    _rec_name = "display_name_computed"

    # Core relations
    conversation_id = fields.Many2one(
        "bader.inbox.conversation", string="Conversation",
        required=True, ondelete="cascade", index=True
    )
    pipeline_id = fields.Many2one(
        "bader.inbox.pipeline", string="Pipeline",
        required=True, ondelete="cascade", index=True
    )
    stage_id = fields.Many2one(
        "bader.inbox.pipeline.stage", string="Stage",
        required=True, ondelete="restrict",
        domain="[('pipeline_id', '=', pipeline_id)]",
        group_expand="_group_expand_stage_id"
    )
    
    # Stage sequence for ordering
    stage_sequence = fields.Integer(
        related="stage_id.sequence", store=True, string="Stage Order"
    )
    
    # Assignment metadata
    assigned_user_id = fields.Many2one("res.users", string="Assigned To")
    priority = fields.Selection([
        ("0", "Normal"),
        ("1", "Low"),
        ("2", "High"),
        ("3", "Urgent"),
    ], default="0", string="Priority")
    notes = fields.Text(string="Notes")
    entered_date = fields.Datetime(
        string="Entered Date", default=fields.Datetime.now, readonly=True
    )
    kanban_state = fields.Selection([
        ("normal", "In Progress"),
        ("done", "Ready"),
        ("blocked", "Blocked"),
    ], default="normal", string="Kanban State")
    color = fields.Integer(string="Color")
    
    # Related fields from conversation (for display in kanban cards)
    contact_name = fields.Char(
        related="conversation_id.computed_name", string="Contact", store=True
    )
    phone = fields.Char(
        related="conversation_id.phone", string="Phone", store=True
    )
    partner_id = fields.Many2one(
        related="conversation_id.partner_id", string="Contact Record", store=True
    )
    last_message = fields.Text(
        related="conversation_id.last_message", string="Last Message"
    )
    unread_count = fields.Integer(
        related="conversation_id.unread_count", string="Unread"
    )
    channel_id = fields.Many2one(
        related="conversation_id.channel_id", string="Channel", store=True
    )
    
    # Computed display name
    display_name_computed = fields.Char(
        compute="_compute_display_name_custom", store=True, string="Name"
    )

    @api.depends("contact_name", "pipeline_id.name")
    def _compute_display_name_custom(self):
        for rec in self:
            rec.display_name_computed = rec.contact_name or "Unknown"

    @api.model
    def _group_expand_stage_id(self, stages, domain, order):
        """Show all stages of the current pipeline in kanban, even if empty."""
        # Extract pipeline_id from the domain
        pipeline_id = None
        for leaf in domain:
            if isinstance(leaf, (list, tuple)) and leaf[0] == 'pipeline_id' and leaf[1] == '=':
                pipeline_id = leaf[2]
                break
        
        if pipeline_id:
            return self.env["bader.inbox.pipeline.stage"].search([
                ("pipeline_id", "=", pipeline_id)
            ], order=order)
        return stages

    _sql_constraints = [
        (
            "unique_conversation_pipeline",
            "UNIQUE(conversation_id, pipeline_id)",
            "A conversation can only be assigned once to each pipeline!"
        ),
    ]

    @api.onchange("pipeline_id")
    def _onchange_pipeline_id(self):
        """Set default first stage when pipeline changes"""
        if self.pipeline_id:
            first_stage = self.env["bader.inbox.pipeline.stage"].search([
                ("pipeline_id", "=", self.pipeline_id.id)
            ], limit=1, order="sequence")
            if first_stage:
                self.stage_id = first_stage
