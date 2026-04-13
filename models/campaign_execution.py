# Copyright 2026 Bader Business
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import json
import logging
import random
from datetime import timedelta
from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)


class BaderInboxCampaignExecution(models.Model):
    """Individual execution of a campaign for a specific contact.
    Tracks the contact's progress through the flow."""

    _name = "bader.inbox.campaign.execution"
    _description = "Campaign Execution"
    _order = "create_date desc"

    campaign_id = fields.Many2one(
        "bader.inbox.campaign", string="Campanha",
        required=True, ondelete="cascade", index=True
    )
    partner_id = fields.Many2one(
        "res.partner", string="Contacto",
        required=True, ondelete="cascade", index=True
    )
    conversation_id = fields.Many2one(
        "bader.inbox.conversation", string="Conversa",
        ondelete="set null"
    )
    current_node_id = fields.Many2one(
        "bader.inbox.campaign.node", string="Nó Atual",
        ondelete="set null"
    )
    status = fields.Selection([
        ("queued", "Na Fila"),
        ("running", "Em Execução"),
        ("waiting", "Aguardando"),
        ("completed", "Concluída"),
        ("paused", "Pausada"),
        ("failed", "Erro"),
        ("opted_out", "Opt-out"),
    ], default="queued", index=True)

    started_at = fields.Datetime(string="Início")
    completed_at = fields.Datetime(string="Fim")
    next_action_at = fields.Datetime(string="Próxima Ação", index=True)

    # Context variables for this execution
    variables = fields.Text(
        string="Variáveis (JSON)", default="{}",
        help="Product, cart, order data available to message templates"
    )

    # A/B variant assigned
    ab_variant = fields.Char(string="Variante A/B")

    # Channel used (for rotation tracking)
    channel_id = fields.Many2one(
        "bader.inbox.channel", string="Canal Usado"
    )

    # Logs
    log_ids = fields.One2many(
        "bader.inbox.campaign.exec.log", "execution_id", string="Logs"
    )

    phone = fields.Char(related="partner_id.mobile", string="Telefone")

    def get_variables(self):
        self.ensure_one()
        return json.loads(self.variables or "{}")

    def set_variables(self, data):
        self.ensure_one()
        self.variables = json.dumps(data, default=str)

    def advance_to_node(self, node):
        """Move execution to the next node and schedule it"""
        self.ensure_one()
        self.current_node_id = node.id

        config = node.get_config()

        if node.node_type == "delay":
            delay = self._calculate_delay(node, config)
            self.next_action_at = fields.Datetime.now() + delay
            self.status = "waiting"
        else:
            # Execute immediately (with throttle delay applied by the cron)
            self.next_action_at = fields.Datetime.now()
            self.status = "running"

    def _calculate_delay(self, node, config):
        """Calculate delay based on node config"""
        subtype = node.node_subtype

        if subtype == "wait_fixed":
            amount = config.get("amount", 1)
            unit = config.get("unit", "hours")
            if unit == "minutes":
                return timedelta(minutes=amount)
            elif unit == "hours":
                return timedelta(hours=amount)
            elif unit == "days":
                return timedelta(days=amount)
        elif subtype == "wait_until":
            # Wait until specific time of day
            hour = config.get("hour", 9)
            minute = config.get("minute", 0)
            now = fields.Datetime.now()
            target = now.replace(hour=hour, minute=minute, second=0)
            if target <= now:
                target += timedelta(days=1)
            return target - now

        return timedelta(hours=1)

    @api.model
    def _cron_process_executions(self):
        """Main cron: process campaign executions that are due.
        Called every minute by Odoo cron."""
        now = fields.Datetime.now()

        # Find executions due for processing
        executions = self.search([
            ("status", "in", ["queued", "running", "waiting"]),
            ("next_action_at", "<=", now),
            ("campaign_id.status", "=", "active"),
        ], limit=50, order="next_action_at asc")

        if not executions:
            return

        _logger.info(f"Campaign engine: processing {len(executions)} executions")

        for execution in executions:
            try:
                self._process_single_execution(execution)
            except Exception as e:
                _logger.error(f"Execution {execution.id} failed: {e}")
                execution.status = "failed"
                self.env["bader.inbox.campaign.exec.log"].create({
                    "execution_id": execution.id,
                    "node_id": execution.current_node_id.id,
                    "action": "error",
                    "result": "failed",
                    "details": str(e)[:500],
                })

        self.env.cr.commit()

    def _process_single_execution(self, execution):
        """Process one execution — execute current node and advance"""
        node = execution.current_node_id
        campaign = execution.campaign_id

        if not node:
            # New execution — find the trigger node
            trigger = campaign.flow_id.node_ids.filtered(
                lambda n: n.node_type == "trigger"
            )
            if trigger:
                node = trigger[0]
                execution.current_node_id = node.id
                execution.started_at = fields.Datetime.now()
            else:
                execution.status = "failed"
                return

        # Check throttle before sending
        throttle = campaign.throttle_id
        if node.node_type == "action" and node.node_subtype.startswith("send_"):
            if not self._throttle_check(execution, throttle):
                # Reschedule with random delay
                delay = random.randint(
                    throttle.msg_delay_min_sec,
                    throttle.msg_delay_max_sec
                )
                execution.next_action_at = fields.Datetime.now() + timedelta(seconds=delay)
                return

        # Execute the node
        result = self._execute_node(execution, node)

        # Log
        self.env["bader.inbox.campaign.exec.log"].create({
            "execution_id": execution.id,
            "node_id": node.id,
            "action": node.node_subtype,
            "result": "success" if result.get("success") else "failed",
            "details": result.get("details", ""),
        })

        # Find next node(s)
        condition_output = result.get("output", "default")
        next_nodes = node.get_next_nodes(condition_output)

        if next_nodes:
            execution.advance_to_node(next_nodes[0])
        else:
            execution.status = "completed"
            execution.completed_at = fields.Datetime.now()
            campaign.stat_sent += 1

    def _execute_node(self, execution, node):
        """Execute a specific node type"""
        config = node.get_config()
        variables = execution.get_variables()

        if node.node_type == "trigger":
            return {"success": True, "output": "default"}

        elif node.node_type == "action":
            return self._execute_action(execution, node, config, variables)

        elif node.node_type == "condition":
            return self._execute_condition(execution, node, config, variables)

        elif node.node_type == "delay":
            return {"success": True, "output": "default"}

        return {"success": False, "details": f"Unknown node type: {node.node_type}"}

    def _execute_action(self, execution, node, config, variables):
        """Execute action nodes (send messages, update CRM, etc.)"""
        partner = execution.partner_id
        campaign = execution.campaign_id
        subtype = node.node_subtype

        if subtype in ("send_text", "send_media", "send_product_card", "send_template"):
            return self._send_message(execution, node, config, variables)

        elif subtype == "add_tag":
            tag_id = config.get("tag_id")
            if tag_id and partner:
                partner.write({"category_id": [(4, tag_id)]})
            return {"success": True}

        elif subtype == "remove_tag":
            tag_id = config.get("tag_id")
            if tag_id and partner:
                partner.write({"category_id": [(3, tag_id)]})
            return {"success": True}

        elif subtype == "update_crm":
            return self._update_crm(partner, config)

        elif subtype == "assign_pipeline":
            return self._assign_pipeline(execution, config)

        elif subtype == "ai_generate":
            return self._ai_generate_and_send(execution, config, variables)

        return {"success": True}

    def _send_message(self, execution, node, config, variables):
        """Send a WhatsApp message via Evolution API"""
        partner = execution.partner_id
        phone = partner.mobile or partner.phone
        if not phone:
            return {"success": False, "details": "No phone number"}

        campaign = execution.campaign_id
        throttle = campaign.throttle_id

        # Pick channel (with rotation)
        channel = self._pick_channel(campaign, throttle)
        if not channel:
            return {"success": False, "details": "No available channel"}

        execution.channel_id = channel.id

        # Build message content with variable replacement
        content = config.get("message", "")
        content = self._replace_variables(content, execution, variables)

        # Message variation
        if throttle.variation_enabled:
            content = self._vary_message(content)

        # Typing simulation
        if throttle.typing_simulation:
            api_helper = self.env["bader.inbox.evolution_api"]
            # We could simulate typing here if the API supports it

        # Send via Evolution API
        try:
            Message = self.env["bader.inbox.message"]

            # Find or create conversation
            Conversation = self.env["bader.inbox.conversation"]
            conv = Conversation.get_or_create(
                channel.id, phone, contact_name=partner.name
            )
            execution.conversation_id = conv.id

            subtype = node.node_subtype
            if subtype == "send_text":
                Message.send_message(conv.id, content, "text")
            elif subtype == "send_media":
                media_url = config.get("media_url", "")
                media_type = config.get("media_type", "image")
                Message.send_message(conv.id, content, media_type)
            elif subtype == "send_product_card":
                product_msg = self._build_product_card(config, variables)
                Message.send_message(conv.id, product_msg, "text")

            return {"success": True, "details": f"Sent to {phone}"}
        except Exception as e:
            return {"success": False, "details": str(e)[:300]}

    def _execute_condition(self, execution, node, config, variables):
        """Evaluate a condition node — returns 'true' or 'false' output"""
        partner = execution.partner_id
        subtype = node.node_subtype
        result = False

        if subtype == "has_tag":
            tag_id = config.get("tag_id")
            result = tag_id in partner.category_id.ids

        elif subtype == "replied":
            conv = execution.conversation_id
            if conv:
                recent = self.env["bader.inbox.message"].search([
                    ("conversation_id", "=", conv.id),
                    ("direction", "=", "in"),
                    ("create_date", ">", execution.started_at),
                ], limit=1)
                result = bool(recent)

        elif subtype == "has_order":
            days = config.get("days", 30)
            since = fields.Datetime.now() - timedelta(days=days)
            orders = self.env["sale.order"].search([
                ("partner_id", "=", partner.id),
                ("state", "in", ["sale", "done"]),
                ("date_order", ">=", since),
            ], limit=1)
            result = bool(orders)

        elif subtype == "field_value":
            field_name = config.get("field")
            operator = config.get("operator", "=")
            value = config.get("value")
            if field_name and hasattr(partner, field_name):
                field_val = getattr(partner, field_name)
                if operator == "=":
                    result = str(field_val) == str(value)
                elif operator == "!=":
                    result = str(field_val) != str(value)
                elif operator == "contains":
                    result = str(value).lower() in str(field_val).lower()

        elif subtype == "ab_split":
            split = config.get("split", 50)
            result = random.randint(1, 100) <= split
            execution.ab_variant = "A" if result else "B"

        return {"success": True, "output": "true" if result else "false"}

    def _throttle_check(self, execution, throttle):
        """Check if we're allowed to send now based on throttle config"""
        now = fields.Datetime.now()

        # Schedule window check
        current_hour = now.hour + now.minute / 60.0
        if current_hour < throttle.schedule_start or current_hour > throttle.schedule_end:
            return False

        # Day of week check
        day = str(now.isoweekday())
        if throttle.schedule_days and day not in throttle.schedule_days:
            return False

        # Contact fatigue check
        if throttle.fatigue_max_msgs_week > 0:
            week_ago = now - timedelta(days=7)
            sent_this_week = self.env["bader.inbox.campaign.exec.log"].search_count([
                ("execution_id.partner_id", "=", execution.partner_id.id),
                ("result", "=", "success"),
                ("action", "in", ["send_text", "send_media", "send_product_card", "send_template"]),
                ("executed_at", ">=", week_ago),
            ])
            if sent_this_week >= throttle.fatigue_max_msgs_week:
                return False

        # Opt-out check
        opted_out = self.env["bader.inbox.campaign.optout"].search([
            ("phone", "=", execution.partner_id.mobile or execution.partner_id.phone),
        ], limit=1)
        if opted_out:
            execution.status = "opted_out"
            return False

        return True

    def _pick_channel(self, campaign, throttle):
        """Pick a channel for sending, with rotation support"""
        channels = campaign.channel_ids
        if not channels:
            return False

        if not throttle.rotation_enabled or len(channels) == 1:
            return channels[0]

        # Round-robin: pick the channel with least sends today
        now = fields.Datetime.now()
        today_start = now.replace(hour=0, minute=0, second=0)

        best_channel = channels[0]
        best_count = float("inf")

        for ch in channels:
            count = self.search_count([
                ("channel_id", "=", ch.id),
                ("started_at", ">=", today_start),
                ("status", "not in", ["queued"]),
            ])
            # Check daily limit
            if throttle.daily_limit and count >= throttle.daily_limit:
                continue
            if count < best_count:
                best_count = count
                best_channel = ch

        return best_channel

    def _replace_variables(self, text, execution, variables):
        """Replace {{variable}} placeholders in message text"""
        partner = execution.partner_id

        replacements = {
            "name": partner.name or "",
            "first_name": (partner.name or "").split()[0] if partner.name else "",
            "phone": partner.mobile or partner.phone or "",
            "email": partner.email or "",
            "company": partner.parent_id.name if partner.parent_id else "",
        }
        replacements.update(variables)

        for key, value in replacements.items():
            text = text.replace("{{" + key + "}}", str(value))

        return text

    def _vary_message(self, text):
        """Apply subtle variations to message to avoid spam detection"""
        variations = [
            # Greetings
            ("Olá", random.choice(["Olá", "Oi", "Opa", "Hey"])),
            ("Hola", random.choice(["Hola", "Hey", "Buenas"])),
            # Punctuation
            ("!", random.choice(["!", "! ", "!!", " !"])),
            # Add/remove emoji at end
        ]
        for old, new in variations:
            if old in text and random.random() > 0.5:
                text = text.replace(old, new, 1)

        # Random invisible char (zero-width space) at random position
        if random.random() > 0.7:
            pos = random.randint(len(text) // 4, len(text) * 3 // 4)
            text = text[:pos] + "\u200b" + text[pos:]

        return text

    def _build_product_card(self, config, variables):
        """Build a text product card message"""
        product_id = config.get("product_id") or variables.get("product_id")
        if not product_id:
            return config.get("message", "")

        product = self.env["product.product"].browse(int(product_id))
        if not product.exists():
            return config.get("message", "")

        lines = []
        lines.append(f"🏷️ *{product.name}*")
        if product.description_sale:
            lines.append(product.description_sale[:200])
        lines.append(f"💰 Precio: *${product.list_price:.2f}*")
        if config.get("include_url"):
            base_url = self.env["ir.config_parameter"].sudo().get_param("web.base.url")
            lines.append(f"🔗 {base_url}/shop/product/{product.id}")
        return "\n".join(lines)

    def _update_crm(self, partner, config):
        """Update CRM lead/opportunity"""
        stage_id = config.get("stage_id")
        if stage_id:
            leads = self.env["crm.lead"].search([
                ("partner_id", "=", partner.id),
                ("type", "=", "opportunity"),
            ], limit=1)
            if leads:
                leads.write({"stage_id": stage_id})
        return {"success": True}

    def _assign_pipeline(self, execution, config):
        """Assign conversation to a pipeline stage"""
        pipeline_id = config.get("pipeline_id")
        stage_id = config.get("stage_id")
        conv = execution.conversation_id
        if conv and pipeline_id:
            self.env["bader.inbox.conversation.pipeline"].create({
                "conversation_id": conv.id,
                "pipeline_id": pipeline_id,
                "stage_id": stage_id,
            })
        return {"success": True}

    def _ai_generate_and_send(self, execution, config, variables):
        """Use AI to generate a personalized message and send it"""
        prompt = config.get("prompt", "")
        prompt = self._replace_variables(prompt, execution, variables)

        # Use existing AI assistant
        try:
            ai = self.env["bader.inbox.ai.assistant"]
            result = ai.generate_text(prompt)
            if result:
                conv = execution.conversation_id
                if conv:
                    self.env["bader.inbox.message"].send_message(
                        conv.id, result, "text"
                    )
                return {"success": True, "details": f"AI generated: {result[:100]}"}
        except Exception as e:
            return {"success": False, "details": f"AI error: {e}"}

        return {"success": False, "details": "AI generation failed"}


class BaderInboxCampaignExecLog(models.Model):
    """Log entry for each action executed"""

    _name = "bader.inbox.campaign.exec.log"
    _description = "Campaign Execution Log"
    _order = "executed_at desc"

    execution_id = fields.Many2one(
        "bader.inbox.campaign.execution", string="Execução",
        required=True, ondelete="cascade", index=True
    )
    node_id = fields.Many2one(
        "bader.inbox.campaign.node", string="Nó",
        ondelete="set null"
    )
    action = fields.Char(string="Ação")
    result = fields.Selection([
        ("success", "Sucesso"),
        ("failed", "Erro"),
        ("skipped", "Pulado"),
    ], string="Resultado")
    details = fields.Text(string="Detalhes")
    executed_at = fields.Datetime(
        string="Executado em", default=fields.Datetime.now
    )
