# Copyright 2026 Bader Business
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import json
import logging
from datetime import datetime, timedelta

import requests

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


# ──────────────────────────────────────────
# TOOL DEFINITIONS (OpenAI Function Calling)
# ──────────────────────────────────────────
TOOLS = {
    "search_products": {
        "type": "function",
        "function": {
            "name": "search_products",
            "description": "Search products by name, reference or category. Returns name, price, stock, reference.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Product name or reference to search"},
                    "limit": {"type": "integer", "description": "Max results (default 5)"},
                },
                "required": ["query"],
            },
        },
    },
    "get_product_details": {
        "type": "function",
        "function": {
            "name": "get_product_details",
            "description": "Get full details of a specific product by ID including price, stock, description, category.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "integer", "description": "Product ID"},
                },
                "required": ["product_id"],
            },
        },
    },
    "check_stock": {
        "type": "function",
        "function": {
            "name": "check_stock",
            "description": "Check available stock quantity for a product.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "integer", "description": "Product ID"},
                },
                "required": ["product_id"],
            },
        },
    },
    "search_opportunities": {
        "type": "function",
        "function": {
            "name": "search_opportunities",
            "description": "Search CRM opportunities/leads by name, contact, phone or email.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search term"},
                },
                "required": ["query"],
            },
        },
    },
    "create_quote": {
        "type": "function",
        "function": {
            "name": "create_quote",
            "description": "Create a sales quotation/presupuesto for the customer. Requires at least one product.",
            "parameters": {
                "type": "object",
                "properties": {
                    "partner_name": {"type": "string", "description": "Customer name"},
                    "products": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "product_id": {"type": "integer"},
                                "quantity": {"type": "number", "default": 1},
                            },
                            "required": ["product_id"],
                        },
                        "description": "Products with IDs and quantities",
                    },
                    "note": {"type": "string", "description": "Internal note"},
                },
                "required": ["products"],
            },
        },
    },
    "schedule_meeting": {
        "type": "function",
        "function": {
            "name": "schedule_meeting",
            "description": "Schedule a meeting/reunion in the calendar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "Meeting subject"},
                    "date": {"type": "string", "description": "Date in YYYY-MM-DD HH:MM format"},
                    "duration": {"type": "number", "description": "Duration in hours (default 1)"},
                    "description": {"type": "string", "description": "Meeting details"},
                },
                "required": ["subject", "date"],
            },
        },
    },
    "search_contacts": {
        "type": "function",
        "function": {
            "name": "search_contacts",
            "description": "Search contacts/partners by name, phone or email.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Name, phone or email to search"},
                },
                "required": ["query"],
            },
        },
    },
    "transfer_to_human": {
        "type": "function",
        "function": {
            "name": "transfer_to_human",
            "description": "Transfer conversation to a human agent when the AI cannot help or the customer requests it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "Reason for transfer"},
                },
                "required": ["reason"],
            },
        },
    },
}


class BaderInboxAIAssistant(models.Model):
    """AI Agent with Tool-Calling for WhatsApp + Odoo Integration"""

    _name = "bader.inbox.ai_assistant"
    _description = "AI Agent"

    name = fields.Char(string="Name", default="Bader AI Agent", required=True)
    active = fields.Boolean(default=True)

    # ── Provider ──
    provider = fields.Selection([
        ("openai", "OpenAI"),
        ("claude", "Claude"),
    ], default="openai", string="Provider", required=True)
    api_key = fields.Char(string="API Key")
    model_name = fields.Char(string="Model", default="gpt-4o-mini")
    temperature = fields.Float(string="Temperature", default=0.7)
    max_tokens = fields.Integer(string="Max Tokens", default=1000)

    # ── Auto-Reply ──
    auto_reply = fields.Boolean(string="Auto-Reply", default=False,
        help="Agent responds automatically to incoming WhatsApp messages")
    auto_reply_delay = fields.Integer(string="Delay (seconds)", default=2)
    only_unassigned = fields.Boolean(string="Only Unassigned Conversations", default=True,
        help="AI only responds when no human agent is assigned")

    # ── Access Permissions (Odoo Modules) ──
    access_products = fields.Boolean(string="📦 Products & Prices", default=True)
    access_stock = fields.Boolean(string="📊 Stock / Inventory", default=False)
    access_crm = fields.Boolean(string="💼 CRM / Opportunities", default=False)
    access_quotes = fields.Boolean(string="📋 Create Quotes", default=False)
    access_calendar = fields.Boolean(string="📅 Schedule Meetings", default=False)
    access_contacts = fields.Boolean(string="👤 Contacts", default=True)

    # ── Channels ──
    channel_ids = fields.Many2many("bader.inbox.channel", string="Active Channels",
        help="Channels where this agent is active. Empty = all channels.")

    # ── Prompts ──
    system_prompt = fields.Text(
        string="System Prompt",
        default=(
            "You are a professional sales assistant for Bader, a hardware and tools company. "
            "You help customers find products, check prices and availability, create quotes, "
            "and schedule meetings. Be friendly, professional, and respond in the same language "
            "as the customer. When you don't know something, use the available tools to search. "
            "If you cannot help, offer to transfer to a human agent."
        )
    )
    welcome_message = fields.Text(string="Welcome Message",
        default="👋 Hello! I'm the Bader AI Assistant. How can I help you today?")
    fallback_message = fields.Text(string="Fallback Message",
        default="I'll transfer you to a human agent who can better assist you.")
    max_tool_calls = fields.Integer(string="Max Tool Loops", default=5)

    # ── Stats ──
    total_conversations = fields.Integer(string="Conversations Handled", default=0, readonly=True)
    total_messages = fields.Integer(string="Messages Sent", default=0, readonly=True)

    # ──────────────────────────────────────
    # CONFIG HELPERS
    # ──────────────────────────────────────
    @api.model
    def _get_config(self):
        config = self.search([("active", "=", True)], limit=1)
        return config if config and config.api_key else None

    @api.model
    def _get_agent_for_channel(self, channel_id):
        """Get active agent for a specific channel"""
        agents = self.search([("active", "=", True), ("auto_reply", "=", True)])
        for agent in agents:
            if not agent.channel_ids or channel_id in agent.channel_ids.ids:
                return agent
        return None

    def _get_available_tools(self):
        """Build tool list based on access permissions"""
        self.ensure_one()
        tools = []
        tool_map = {
            "access_products": ["search_products", "get_product_details"],
            "access_stock": ["check_stock"],
            "access_crm": ["search_opportunities"],
            "access_quotes": ["create_quote"],
            "access_calendar": ["schedule_meeting"],
            "access_contacts": ["search_contacts"],
        }
        # Always available
        tools.append(TOOLS["transfer_to_human"])

        for field_name, tool_names in tool_map.items():
            if getattr(self, field_name):
                for name in tool_names:
                    if name in TOOLS:
                        tools.append(TOOLS[name])
        return tools

    # ──────────────────────────────────────
    # MAIN AGENT LOOP
    # ──────────────────────────────────────
    @api.model
    def process_message(self, conversation_id, message_text):
        """Main entry point: process incoming message with AI agent"""
        config = self._get_config()
        if not config:
            return {"error": "AI not configured", "response": ""}

        conversation = self.env["bader.inbox.conversation"].browse(conversation_id)
        if not conversation.exists():
            return {"error": "Conversation not found"}

        # Check if should respond
        if config.only_unassigned and conversation.assigned_user_id:
            return {"skip": True, "reason": "Conversation assigned to human"}

        # Build conversation history
        Message = self.env["bader.inbox.message"]
        messages = Message.search([
            ("conversation_id", "=", conversation_id),
            ("message_type", "=", "text"),
        ], order="create_date desc", limit=15)

        chat_history = []
        for msg in reversed(messages):
            role = "user" if msg.direction == "in" else "assistant"
            chat_history.append({"role": role, "content": msg.content or ""})

        # Get available tools
        tools = config._get_available_tools()

        # Build context info
        context_info = config._build_context(conversation)

        try:
            if config.provider == "openai":
                response = config._agent_loop_openai(chat_history, tools, context_info, conversation)
            elif config.provider == "claude":
                response = config._agent_loop_claude(chat_history, tools, context_info, conversation)
            else:
                return {"error": "Unknown provider"}

            # Update stats
            config.sudo().write({
                "total_messages": config.total_messages + 1,
            })

            return {"response": response}
        except Exception as e:
            _logger.error(f"AI Agent error: {e}", exc_info=True)
            return {"error": str(e), "response": config.fallback_message or ""}

    def _build_context(self, conversation):
        """Build context string with conversation metadata"""
        self.ensure_one()
        parts = [f"Customer phone: {conversation.phone}"]
        if conversation.contact_name:
            parts.append(f"Customer name: {conversation.contact_name}")
        if conversation.partner_id:
            parts.append(f"Odoo contact: {conversation.partner_id.name} (ID: {conversation.partner_id.id})")
        if conversation.lead_id:
            parts.append(f"CRM opportunity: {conversation.lead_id.name}")
        if conversation.tag_ids:
            parts.append(f"Tags: {', '.join(conversation.tag_ids.mapped('name'))}")
        parts.append(f"Current date/time: {fields.Datetime.now()}")
        return "\n".join(parts)

    # ──────────────────────────────────────
    # OPENAI AGENT LOOP
    # ──────────────────────────────────────
    def _agent_loop_openai(self, chat_history, tools, context_info, conversation):
        self.ensure_one()
        system_msg = f"{self.system_prompt}\n\n--- CONTEXT ---\n{context_info}"
        api_msgs = [{"role": "system", "content": system_msg}]
        api_msgs.extend(chat_history)

        max_loops = self.max_tool_calls or 5

        for _ in range(max_loops):
            payload = {
                "model": self.model_name or "gpt-4o-mini",
                "messages": api_msgs,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens or 1000,
            }
            if tools:
                payload["tools"] = tools
                payload["tool_choice"] = "auto"

            resp = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            choice = data["choices"][0]
            msg = choice["message"]

            # If no tool calls, return the text
            if not msg.get("tool_calls"):
                return msg.get("content", "").strip()

            # Process tool calls
            api_msgs.append(msg)
            for tc in msg["tool_calls"]:
                fn_name = tc["function"]["name"]
                fn_args = json.loads(tc["function"]["arguments"])
                _logger.info(f"AI Agent tool call: {fn_name}({fn_args})")

                result = self._execute_tool(fn_name, fn_args, conversation)
                api_msgs.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                })

        # Max loops reached
        return api_msgs[-1].get("content", self.fallback_message or "")

    # ──────────────────────────────────────
    # CLAUDE AGENT LOOP
    # ──────────────────────────────────────
    def _agent_loop_claude(self, chat_history, tools, context_info, conversation):
        self.ensure_one()
        system_msg = f"{self.system_prompt}\n\n--- CONTEXT ---\n{context_info}"

        # Convert tools to Claude format
        claude_tools = []
        for t in tools:
            fn = t["function"]
            claude_tools.append({
                "name": fn["name"],
                "description": fn["description"],
                "input_schema": fn["parameters"],
            })

        # Convert chat history for Claude
        claude_msgs = []
        for m in chat_history:
            role = m["role"] if m["role"] in ("user", "assistant") else "user"
            claude_msgs.append({"role": role, "content": m["content"]})

        max_loops = self.max_tool_calls or 5

        for _ in range(max_loops):
            payload = {
                "model": self.model_name or "claude-3-haiku-20240307",
                "system": system_msg,
                "messages": claude_msgs,
                "max_tokens": self.max_tokens or 1000,
                "temperature": self.temperature,
            }
            if claude_tools:
                payload["tools"] = claude_tools

            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            # Check if there are tool uses
            tool_uses = [b for b in data.get("content", []) if b["type"] == "tool_use"]
            text_blocks = [b for b in data.get("content", []) if b["type"] == "text"]

            if not tool_uses:
                return text_blocks[0]["text"].strip() if text_blocks else ""

            # Add assistant response and process tools
            claude_msgs.append({"role": "assistant", "content": data["content"]})
            tool_results = []
            for tu in tool_uses:
                fn_name = tu["name"]
                fn_args = tu["input"]
                _logger.info(f"AI Agent tool call: {fn_name}({fn_args})")

                result = self._execute_tool(fn_name, fn_args, conversation)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu["id"],
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                })

            claude_msgs.append({"role": "user", "content": tool_results})

        # Max loops reached
        return self.fallback_message or ""

    # ──────────────────────────────────────
    # TOOL EXECUTION DISPATCHER
    # ──────────────────────────────────────
    def _execute_tool(self, name, args, conversation):
        """Execute a tool and return the result"""
        try:
            method = getattr(self, f"_tool_{name}", None)
            if method:
                return method(args, conversation)
            return {"error": f"Unknown tool: {name}"}
        except Exception as e:
            _logger.error(f"Tool {name} error: {e}")
            return {"error": str(e)}

    # ──────────────────────────────────────
    # TOOL IMPLEMENTATIONS
    # ──────────────────────────────────────
    def _tool_search_products(self, args, conversation):
        query = args.get("query", "")
        limit = min(args.get("limit", 5), 10)
        Product = self.env["product.product"].sudo()
        domain = [
            "|", "|",
            ("name", "ilike", query),
            ("default_code", "ilike", query),
            ("barcode", "ilike", query),
        ]
        products = Product.search(domain, limit=limit)
        results = []
        for p in products:
            item = {
                "id": p.id,
                "name": p.name,
                "ref": p.default_code or "",
                "price": p.list_price,
                "currency": p.currency_id.name,
            }
            if self.access_stock:
                item["stock"] = p.qty_available
            results.append(item)
        return {"products": results, "count": len(results)}

    def _tool_get_product_details(self, args, conversation):
        pid = args.get("product_id")
        Product = self.env["product.product"].sudo()
        p = Product.browse(pid)
        if not p.exists():
            return {"error": "Product not found"}
        result = {
            "id": p.id,
            "name": p.name,
            "ref": p.default_code or "",
            "price": p.list_price,
            "currency": p.currency_id.name,
            "category": p.categ_id.name if p.categ_id else "",
            "description": (p.description_sale or "")[:500],
            "active": p.active,
        }
        if self.access_stock:
            result["stock_available"] = p.qty_available
            result["stock_virtual"] = p.virtual_available
        return result

    def _tool_check_stock(self, args, conversation):
        pid = args.get("product_id")
        Product = self.env["product.product"].sudo()
        p = Product.browse(pid)
        if not p.exists():
            return {"error": "Product not found"}
        return {
            "product": p.name,
            "available": p.qty_available,
            "virtual": p.virtual_available,
            "incoming": p.incoming_qty,
            "outgoing": p.outgoing_qty,
        }

    def _tool_search_opportunities(self, args, conversation):
        query = args.get("query", "")
        Lead = self.env["crm.lead"].sudo()
        domain = [
            "|", "|",
            ("name", "ilike", query),
            ("partner_name", "ilike", query),
            ("phone", "ilike", query),
        ]
        leads = Lead.search(domain, limit=5)
        return {
            "opportunities": [{
                "id": l.id,
                "name": l.name,
                "stage": l.stage_id.name if l.stage_id else "",
                "expected_revenue": l.expected_revenue,
                "contact": l.partner_name or "",
                "phone": l.phone or "",
            } for l in leads]
        }

    def _tool_create_quote(self, args, conversation):
        partner = conversation.partner_id
        if not partner:
            # Try to find/create partner
            partner_name = args.get("partner_name", conversation.contact_name or "")
            if partner_name:
                Partner = self.env["res.partner"].sudo()
                partner = Partner.search([
                    "|",
                    ("phone", "ilike", conversation.phone),
                    ("mobile", "ilike", conversation.phone),
                ], limit=1)
                if not partner:
                    partner = Partner.create({
                        "name": partner_name,
                        "phone": conversation.phone,
                    })
                conversation.sudo().write({"partner_id": partner.id})

        if not partner:
            return {"error": "No customer associated. Ask for customer name first."}

        products_data = args.get("products", [])
        if not products_data:
            return {"error": "No products specified"}

        try:
            SaleOrder = self.env["sale.order"].sudo()
            order = SaleOrder.create({
                "partner_id": partner.id,
                "note": args.get("note", "Created by AI Agent"),
            })

            Product = self.env["product.product"].sudo()
            for item in products_data:
                product = Product.browse(item["product_id"])
                if product.exists():
                    self.env["sale.order.line"].sudo().create({
                        "order_id": order.id,
                        "product_id": product.id,
                        "product_uom_qty": item.get("quantity", 1),
                    })

            return {
                "success": True,
                "quote_id": order.id,
                "quote_name": order.name,
                "total": order.amount_total,
                "currency": order.currency_id.name,
                "url": f"/web#id={order.id}&model=sale.order&view_type=form",
            }
        except Exception as e:
            return {"error": f"Failed to create quote: {e}"}

    def _tool_schedule_meeting(self, args, conversation):
        subject = args.get("subject", "Meeting")
        date_str = args.get("date", "")
        duration = args.get("duration", 1.0)
        description = args.get("description", "")

        try:
            start = datetime.strptime(date_str, "%Y-%m-%d %H:%M")
        except ValueError:
            try:
                start = datetime.strptime(date_str, "%Y-%m-%d")
                start = start.replace(hour=10, minute=0)
            except ValueError:
                return {"error": f"Invalid date format: {date_str}. Use YYYY-MM-DD HH:MM"}

        stop = start + timedelta(hours=duration)

        partner_ids = []
        if conversation.partner_id:
            partner_ids.append(conversation.partner_id.id)

        try:
            event = self.env["calendar.event"].sudo().create({
                "name": subject,
                "start": start,
                "stop": stop,
                "description": description,
                "partner_ids": [(6, 0, partner_ids)] if partner_ids else False,
            })
            return {
                "success": True,
                "event_id": event.id,
                "name": event.name,
                "start": str(event.start),
                "stop": str(event.stop),
            }
        except Exception as e:
            return {"error": f"Failed to schedule: {e}"}

    def _tool_search_contacts(self, args, conversation):
        query = args.get("query", "")
        Partner = self.env["res.partner"].sudo()
        domain = [
            "|", "|",
            ("name", "ilike", query),
            ("phone", "ilike", query),
            ("email", "ilike", query),
        ]
        partners = Partner.search(domain, limit=5)
        return {
            "contacts": [{
                "id": p.id,
                "name": p.name,
                "phone": p.phone or p.mobile or "",
                "email": p.email or "",
                "company": p.parent_id.name if p.parent_id else "",
            } for p in partners]
        }

    def _tool_transfer_to_human(self, args, conversation):
        reason = args.get("reason", "Customer requested human agent")
        _logger.info(f"AI Agent transferring conversation {conversation.id}: {reason}")
        # Mark conversation as needing human attention
        conversation.sudo().write({
            "ai_active": False,
        })
        return {
            "success": True,
            "message": "Conversation transferred to human agent",
            "reason": reason,
        }

    # ──────────────────────────────────────
    # PHASE 3 LEGACY METHODS (keep for compatibility)
    # ──────────────────────────────────────
    @api.model
    def suggest_reply(self, conversation_id):
        """Suggest 2-3 replies based on conversation context"""
        config = self._get_config()
        if not config:
            return {"error": "AI not configured", "suggestions": []}

        Message = self.env["bader.inbox.message"]
        messages = Message.search([
            ("conversation_id", "=", conversation_id),
            ("message_type", "=", "text"),
        ], order="create_date desc", limit=10)

        if not messages:
            return {"suggestions": []}

        chat_history = []
        for msg in reversed(messages):
            role = "user" if msg.direction == "in" else "assistant"
            chat_history.append({"role": role, "content": msg.content or ""})

        suggest_prompt = (
            "Suggest 3 short, helpful reply options in the same language as "
            "the customer's message. Return JSON array of strings, nothing else."
        )

        try:
            resp = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {config.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": config.model_name or "gpt-4o-mini",
                    "messages": [
                        {"role": "system", "content": suggest_prompt},
                        *chat_history,
                    ],
                    "temperature": config.temperature,
                    "max_tokens": 300,
                },
                timeout=15,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            return {"suggestions": self._parse_suggestions(content)}
        except Exception as e:
            _logger.error(f"AI suggestion error: {e}")
            return {"error": str(e), "suggestions": []}

    def _parse_suggestions(self, text):
        try:
            text = text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            result = json.loads(text)
            if isinstance(result, list):
                return [str(s)[:500] for s in result[:3]]
        except Exception:
            pass
        return [text[:500]] if text else []

    @api.model
    def detect_language(self, text):
        config = self._get_config()
        if not config or not text:
            return ""
        try:
            prompt = (
                f'What language is this text? Return ONLY the ISO 639-1 code '
                f'(e.g. "en", "es", "pt"). Text: "{text[:200]}"'
            )
            resp = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {config.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": config.model_name or "gpt-4o-mini",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 5,
                    "temperature": 0,
                },
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip().lower()[:5]
        except Exception as e:
            _logger.debug(f"Language detection error: {e}")
        return ""

    @api.model
    def translate_message(self, message_id, target_lang="es"):
        config = self._get_config()
        if not config:
            return {"error": "AI not configured"}

        message = self.env["bader.inbox.message"].browse(message_id)
        if not message.exists() or not message.content:
            return {"error": "Message not found"}

        if message.translated_content:
            return {"translated": message.translated_content, "language": message.detected_language}

        try:
            prompt = (
                f'Translate the following text to {target_lang}. '
                f'Return ONLY the translation.\n\nText: {message.content}'
            )
            resp = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {config.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": config.model_name or "gpt-4o-mini",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 500,
                    "temperature": 0.3,
                },
                timeout=15,
            )
            resp.raise_for_status()
            translated = resp.json()["choices"][0]["message"]["content"].strip()

            lang = self.detect_language(message.content)
            message.sudo().write({
                "translated_content": translated,
                "detected_language": lang,
            })
            return {"translated": translated, "language": lang}
        except Exception as e:
            _logger.error(f"Translation error: {e}")
            return {"error": str(e)}
