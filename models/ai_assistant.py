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
            "description": "Search products by name, SKU/reference, or category. Returns multiple matches with name, price, stock, reference, category and short description. ALWAYS use this tool when customer mentions any product — even partial names like 'Hilux'. If multiple results, present ALL options to the customer and ask which one they want.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Product name, SKU, or keyword to search"},
                    "category": {"type": "string", "description": "Optional: filter by category name"},
                    "limit": {"type": "integer", "description": "Max results (default 10)"},
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
            "description": "Create a NEW sales quotation/presupuesto for the customer. IMPORTANT: If the customer asks to UPDATE an existing quotation (add products, remove products, change quantities), use the 'update_quote' tool instead. Only use create_quote when the customer wants a brand new quotation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "partner_name": {"type": "string", "description": "Customer name"},
                    "products": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "product_id": {"type": "integer", "description": "Product ID from search_products results"},
                                "product_ref": {"type": "string", "description": "Product SKU/reference code (alternative to product_id)"},
                                "quantity": {"type": "number", "default": 1},
                            },
                        },
                        "description": "Products with IDs or references and quantities.",
                    },
                    "send_email": {"type": "boolean", "description": "Set true ONLY if the customer explicitly asked to receive it by email", "default": False},
                    "email": {"type": "string", "description": "Customer email (required if send_email is true)"},
                    "note": {"type": "string", "description": "Internal note"},
                },
                "required": ["products"],
            },
        },
    },
    "update_quote": {
        "type": "function",
        "function": {
            "name": "update_quote",
            "description": "Update an EXISTING sales quotation/presupuesto. Use this to add products, remove products, or change quantities on an existing SO. Use search_quotes first to find the quote_id. Can also send the updated quote by email or PDF.",
            "parameters": {
                "type": "object",
                "properties": {
                    "quote_id": {"type": "integer", "description": "The sale.order ID to update (from search_quotes results)"},
                    "add_products": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "product_id": {"type": "integer", "description": "Product ID from search_products"},
                                "product_ref": {"type": "string", "description": "Product SKU/reference code"},
                                "quantity": {"type": "number", "default": 1},
                            },
                        },
                        "description": "Products to ADD to the quotation",
                    },
                    "remove_line_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Order line IDs to REMOVE from the quotation",
                    },
                    "update_lines": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "line_id": {"type": "integer", "description": "Order line ID to update"},
                                "product_id": {"type": "integer", "description": "Match by product ID instead of line_id"},
                                "quantity": {"type": "number", "description": "New quantity"},
                            },
                        },
                        "description": "Lines to update with new quantities",
                    },
                    "send_email": {"type": "boolean", "description": "Send updated quote by email", "default": False},
                    "email": {"type": "string", "description": "Customer email"},
                },
                "required": ["quote_id"],
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
    "send_quote_pdf": {
        "type": "function",
        "function": {
            "name": "send_quote_pdf",
            "description": "Send the PDF of a previously created quotation/presupuesto directly in the WhatsApp conversation. Use this when the customer asks to receive the PDF.",
            "parameters": {
                "type": "object",
                "properties": {
                    "quote_id": {"type": "integer", "description": "The quote_id returned by create_quote"},
                },
                "required": ["quote_id"],
            },
        },
    },
    "search_quotes": {
        "type": "function",
        "function": {
            "name": "search_quotes",
            "description": "Search sales orders, quotations (presupuestos) and confirmed orders (pedidos) by customer name, phone, email or SO number. Returns all quotation/order details including products, amounts and status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Customer name, phone, email or SO number (e.g. SO79407)"},
                    "state": {"type": "string", "description": "Filter by state: 'draft' for quotations, 'sale' for confirmed orders, 'all' for everything (default: all)"},
                },
                "required": ["query"],
            },
        },
    },
    "track_delivery": {
        "type": "function",
        "function": {
            "name": "track_delivery",
            "description": "Track delivery/shipping status for the customer. Shows all pending and recent deliveries with status, tracking number, and estimated dates. Only shows deliveries for the current customer.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_name": {"type": "string", "description": "Optional: filter by specific SO number (e.g. SO79403)"},
                },
                "required": [],
            },
        },
    },
    "check_invoices": {
        "type": "function",
        "function": {
            "name": "check_invoices",
            "description": "Check invoice and payment status for the customer. Shows open/paid/overdue invoices, amounts due, and account balance. Only shows invoices for the current customer.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "Filter: 'open' for unpaid, 'paid' for paid, 'overdue' for past due, 'all' (default: all)"},
                    "invoice_name": {"type": "string", "description": "Optional: filter by specific invoice number"},
                },
                "required": [],
            },
        },
    },
    "confirm_quote": {
        "type": "function",
        "function": {
            "name": "confirm_quote",
            "description": "Confirm a draft quotation (presupuesto) and convert it to a confirmed sales order (pedido de venta). IMPORTANT: Only use when the customer EXPLICITLY confirms they want to proceed with the order. Always confirm the quote details with the customer before calling this.",
            "parameters": {
                "type": "object",
                "properties": {
                    "quote_id": {"type": "integer", "description": "The sale.order ID to confirm (from search_quotes results)"},
                    "send_email": {"type": "boolean", "description": "Send confirmation email to customer", "default": False},
                },
                "required": ["quote_id"],
            },
        },
    },
    "get_pricelist": {
        "type": "function",
        "function": {
            "name": "get_pricelist",
            "description": "Get personalized pricing for the customer. Shows the customer's specific pricelist, discounts, and final prices for requested products. Use this instead of generic product prices when the customer asks about THEIR price.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Product IDs to get pricing for",
                    },
                    "product_refs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Product reference codes (SKUs) to get pricing for",
                    },
                    "quantity": {"type": "number", "description": "Quantity for price calculation (default: 1)", "default": 1},
                },
                "required": [],
            },
        },
    },
    "list_categories": {
        "type": "function",
        "function": {
            "name": "list_categories",
            "description": "List all product categories available in the catalog. Use this to discover what types of products are available when the customer asks about a general type of product, or when a search by name returns no results. Returns category names with product counts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Optional filter to search categories by name (e.g. 'dental', 'herramientas')"},
                },
                "required": [],
            },
        },
    },
    "browse_category": {
        "type": "function",
        "function": {
            "name": "browse_category",
            "description": "Browse all products in a specific category. Use this after list_categories to show the customer what products are available in a category. Returns product names, prices, stock status and IDs. You can then use get_product_details for more info on specific products.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category_id": {"type": "integer", "description": "Category ID from list_categories results"},
                    "category_name": {"type": "string", "description": "Category name (alternative to category_id)"},
                    "limit": {"type": "integer", "description": "Max products to return (default 20)"},
                },
                "required": [],
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
    access_invoices = fields.Boolean(string="💰 Invoices & Payments", default=False)
    access_deliveries = fields.Boolean(string="📦 Deliveries & Tracking", default=False)

    # ── Channels ──
    channel_ids = fields.Many2many("bader.inbox.channel", string="Active Channels",
        help="Channels where this agent is active. Empty = all channels.")

    # ── Prompts ──
    system_prompt = fields.Text(
        string="System Prompt",
        default=(
            "You are a professional sales assistant for Bader Dental, a dental equipment and supplies company. "
            "You help customers find products, check prices and availability, create quotations, and schedule meetings.\n\n"
            "RULES:\n"
            "1. LANGUAGE: Always respond in the SAME language the customer uses.\n"
            "2. PRODUCTS: ALWAYS use search_products when a customer mentions ANY product name, even partial. "
            "If multiple results are found, LIST ALL options with name, reference, price and stock, then ASK the customer which one they want.\n"
            "3. NEVER INVENT: Never guess prices, stock or product info. Always use tools to get real data.\n"
            "4. SMART DATA: Check the CONTEXT section below. If the customer already has an email, phone, or address in our system, "
            "DO NOT ask for it again. Instead, CONFIRM: 'Shall I send to your email X?' and let them change it if needed.\n"
            "5. QUOTES: When creating a quotation, include ALL discussed products. After creation, inform the quote number and total.\n"
            "6. PROACTIVE: Suggest related products when relevant. Inform about stock availability proactively.\n"
            "7. HUMANIZED: Be warm, professional, use the customer's name when available. Give complete answers, not just 'yes/no'.\n"
            "8. TRANSFER: If you cannot help, offer to transfer to a human agent with a clear reason."
        )
    )
    welcome_message = fields.Text(string="Welcome Message",
        default="👋 Hello! I'm the Bader AI Assistant. How can I help you today?")
    fallback_message = fields.Text(string="Fallback Message",
        default="I'll transfer you to a human agent who can better assist you.")
    max_tool_calls = fields.Integer(string="Max Tool Loops", default=5)

    # ── Escalation Rules ──
    escalation_keywords = fields.Text(string="Escalation Keywords",
        default="reclamación,queja,complaint,hablar con persona,falar com pessoa,urgente,urgent,cancelar,cancel",
        help="Comma-separated keywords that trigger automatic escalation to human agent")
    escalation_max_unanswered = fields.Integer(string="Max Unanswered Messages", default=5,
        help="Auto-suggest transfer after this many messages without resolution")
    escalation_on_tool_failure = fields.Boolean(string="Escalate on Tool Failures", default=True,
        help="Auto-escalate after 2+ consecutive tool errors")

    # ── Stats ──
    total_conversations = fields.Integer(string="Conversations Handled", default=0, readonly=True)
    total_messages = fields.Integer(string="Messages Sent", default=0, readonly=True)

    # ── Schedule / Horário ──
    schedule_mode = fields.Selection([
        ("always", "Sempre ativo (24/7)"),
        ("outside_hours", "Fora do horário comercial"),
        ("during_hours", "Durante o horário comercial"),
    ], string="Modo de Horário", default="always",
        help="Define quando o agente responde automaticamente")
    schedule_start = fields.Float(string="Início (hora)", default=9.0,
        help="Hora de início do horário comercial (ex: 9.0 = 09:00)")
    schedule_end = fields.Float(string="Fim (hora)", default=18.0,
        help="Hora de fim do horário comercial (ex: 18.0 = 18:00)")
    schedule_timezone = fields.Selection([
        ("Europe/Madrid", "🇪🇸 Espanha (Madrid)"),
        ("Europe/Lisbon", "🇵🇹 Portugal (Lisboa)"),
        ("America/Argentina/Buenos_Aires", "🇦🇷 Argentina (Buenos Aires)"),
        ("America/Sao_Paulo", "🇧🇷 Brasil (São Paulo)"),
        ("UTC", "UTC"),
    ], string="Timezone", default="Europe/Madrid")
    schedule_weekend = fields.Boolean(string="Ativo nos fins de semana",
        default=True, help="Se ativado, o agente responde nos sábados e domingos")

    def is_within_schedule(self):
        """Check if AI agent should respond based on current time and schedule config"""
        if self.schedule_mode == "always":
            return True

        try:
            import pytz
            tz = pytz.timezone(self.schedule_timezone or "UTC")
        except Exception:
            return True

        now = datetime.now(tz)
        current_hour = now.hour + now.minute / 60.0
        is_weekend = now.weekday() >= 5  # Saturday=5, Sunday=6

        # Weekend check
        if is_weekend and self.schedule_weekend and self.schedule_mode == "outside_hours":
            return True
        if is_weekend and not self.schedule_weekend:
            return False

        is_business_hours = self.schedule_start <= current_hour < self.schedule_end

        if self.schedule_mode == "outside_hours":
            return not is_business_hours
        elif self.schedule_mode == "during_hours":
            return is_business_hours
        return True

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
            "access_products": ["search_products", "get_product_details", "list_categories", "browse_category"],
            "access_stock": ["check_stock"],
            "access_crm": ["search_opportunities"],
            "access_quotes": ["create_quote", "update_quote", "send_quote_pdf", "search_quotes", "confirm_quote", "get_pricelist"],
            "access_calendar": ["schedule_meeting"],
            "access_contacts": ["search_contacts"],
            "access_invoices": ["check_invoices"],
            "access_deliveries": ["track_delivery"],
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

            # --- Post-processing: Analytics, Memory, Lead Score ---
            try:
                config._post_process_response(conversation, message_text, response)
            except Exception as e:
                _logger.warning(f"Post-processing error: {e}")

            return {"response": response}
        except Exception as e:
            _logger.error(f"AI Agent error: {e}", exc_info=True)
            return {"error": str(e), "response": config.fallback_message or ""}

    def _build_context(self, conversation):
        """Build rich context string with conversation and partner metadata"""
        self.ensure_one()
        parts = [f"Customer phone: {conversation.phone}"]
        if conversation.contact_name:
            parts.append(f"Customer name: {conversation.contact_name}")

        partner = conversation.partner_id
        if partner:
            parts.append(f"Odoo contact: {partner.name} (ID: {partner.id})")
            if partner.email:
                parts.append(f"Customer email: {partner.email}")
            if partner.mobile and partner.mobile != conversation.phone:
                parts.append(f"Customer mobile: {partner.mobile}")
            if partner.street:
                addr_parts = [partner.street]
                if partner.city:
                    addr_parts.append(partner.city)
                if partner.state_id:
                    addr_parts.append(partner.state_id.name)
                if partner.country_id:
                    addr_parts.append(partner.country_id.name)
                parts.append(f"Customer address: {', '.join(addr_parts)}")
            if partner.lang:
                parts.append(f"Customer language: {partner.lang}")
            if partner.comment:
                parts.append(f"Internal notes: {partner.comment[:200]}")

            # Order history (confirmed + draft quotations)
            try:
                all_orders = self.env["sale.order"].sudo().search([
                    ("partner_id", "=", partner.id),
                ], order="date_order desc", limit=10)
                if all_orders:
                    confirmed = all_orders.filtered(lambda o: o.state in ('sale', 'done'))
                    drafts = all_orders.filtered(lambda o: o.state == 'draft')
                    if confirmed:
                        parts.append(f"Confirmed orders: {len(confirmed)}")
                        for o in confirmed[:3]:
                            parts.append(f"  - {o.name}: {o.amount_total} {o.currency_id.name} ({o.date_order.strftime('%Y-%m-%d') if o.date_order else ''})")
                    if drafts:
                        parts.append(f"Open quotations (presupuestos): {len(drafts)}")
                        for o in drafts[:5]:
                            parts.append(f"  - {o.name}: {o.amount_total} {o.currency_id.name} ({o.date_order.strftime('%Y-%m-%d') if o.date_order else ''})")
            except Exception:
                pass

        if conversation.lead_id:
            parts.append(f"CRM opportunity: {conversation.lead_id.name}")
        if conversation.tag_ids:
            parts.append(f"Tags: {', '.join(conversation.tag_ids.mapped('name'))}")

        # AI Memory (customer preferences from past sessions)
        if partner and partner.ai_memory:
            try:
                memory = json.loads(partner.ai_memory)
                if memory:
                    parts.append("--- CUSTOMER MEMORY (from past sessions) ---")
                    if memory.get("preferred_lang"):
                        parts.append(f"Customer prefers language: {memory['preferred_lang']}")
                    if memory.get("interests"):
                        parts.append(f"Customer interests: {', '.join(memory['interests'])}")
                    if memory.get("communication"):
                        parts.append(f"Communication preference: {memory['communication']}")
                    if memory.get("notes"):
                        parts.append(f"AI notes: {memory['notes']}")
            except (json.JSONDecodeError, TypeError):
                pass

        # Multi-language detection
        if partner and partner.lang:
            lang_map = {
                "es_ES": "Spanish", "es_AR": "Spanish (Argentina)",
                "pt_PT": "Portuguese", "pt_BR": "Portuguese (Brazil)",
                "en_US": "English", "fr_FR": "French", "de_DE": "German",
                "it_IT": "Italian", "ca_ES": "Catalan",
            }
            lang_name = lang_map.get(partner.lang, partner.lang)
            parts.append(f"IMPORTANT: This customer's language is {lang_name}. Always respond in {lang_name}.")

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

                # Track tool usage for analytics
                self._track_tool_usage(conversation, fn_name)

                # Check escalation before executing
                escalation = self._check_escalation(conversation, fn_name, fn_args)
                if escalation:
                    return escalation

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
    # POST-PROCESSING (Memory, Scoring, Analytics)
    # ──────────────────────────────────────
    def _post_process_response(self, conversation, user_message, ai_response):
        """Run after each AI response: update analytics, memory, and lead score"""
        self.ensure_one()

        # 1. Update response counter
        conversation.sudo().write({
            "ai_response_count": (conversation.ai_response_count or 0) + 1,
        })

        # 2. Qualify lead based on tools used in this session
        self._qualify_lead(conversation)

        # 3. Update customer memory
        self._update_memory(conversation, user_message, ai_response)

        # 4. Check if resolved (no more messages needed)
        if conversation.ai_resolution == "pending" and conversation.ai_response_count >= 2:
            tools_used = json.loads(conversation.ai_tools_used or "{}")
            if any(t in tools_used for t in ["create_quote", "confirm_quote", "send_quote_pdf"]):
                conversation.sudo().write({"ai_resolution": "resolved"})

    def _track_tool_usage(self, conversation, tool_name):
        """Track tool usage for analytics"""
        try:
            tools = json.loads(conversation.ai_tools_used or "{}")
        except (json.JSONDecodeError, TypeError):
            tools = {}
        tools[tool_name] = tools.get(tool_name, 0) + 1
        conversation.sudo().write({
            "ai_tools_used": json.dumps(tools),
        })

    def _check_escalation(self, conversation, tool_name, tool_args):
        """Check if conversation should be auto-escalated to human"""
        self.ensure_one()

        # 1. Check if transfer_to_human was called
        if tool_name == "transfer_to_human":
            reason = tool_args.get("reason", "AI decided to transfer")
            conversation.sudo().write({
                "ai_resolution": "escalated",
                "ai_escalation_reason": reason,
            })
            return None  # Let the tool execute normally

        # 2. Keyword-based escalation (check user message from context)
        if self.escalation_keywords:
            keywords = [k.strip().lower() for k in self.escalation_keywords.split(",") if k.strip()]
            last_msg = self.env["bader.inbox.message"].sudo().search([
                ("conversation_id", "=", conversation.id),
                ("direction", "=", "in"),
            ], order="create_date desc", limit=1)
            if last_msg and last_msg.content:
                msg_lower = last_msg.content.lower()
                for kw in keywords:
                    if kw in msg_lower:
                        _logger.info(f"Escalation triggered by keyword '{kw}' in conversation {conversation.id}")
                        conversation.sudo().write({
                            "ai_escalation_reason": f"Keyword detected: {kw}",
                        })
                        # Don't force escalation, let AI decide but note it
                        break

        # 3. Too many AI responses without resolution
        max_unanswered = self.escalation_max_unanswered or 5
        if conversation.ai_response_count >= max_unanswered and conversation.ai_resolution == "pending":
            _logger.info(f"Escalation suggested: {conversation.ai_response_count} responses without resolution")
            # Don't force, just log

        return None  # No forced escalation

    def _qualify_lead(self, conversation):
        """Score the lead based on AI conversation tools used"""
        try:
            tools = json.loads(conversation.ai_tools_used or "{}")
        except (json.JSONDecodeError, TypeError):
            tools = {}

        score = conversation.ai_lead_score or 0
        score_changes = {
            "create_quote": 30,
            "confirm_quote": 40,
            "update_quote": 20,
            "get_pricelist": 20,
            "search_products": 10,
            "get_product_details": 10,
            "check_stock": 15,
            "track_delivery": 5,
            "check_invoices": 5,
            "search_quotes": 10,
            "send_quote_pdf": 15,
        }

        new_score = 0
        for tool_name, points in score_changes.items():
            if tool_name in tools:
                new_score += points

        # Cap at 100
        final_score = min(max(score, new_score), 100)
        if final_score != score:
            conversation.sudo().write({"ai_lead_score": final_score})

    def _update_memory(self, conversation, user_message, ai_response):
        """Update customer memory with insights from the conversation"""
        partner = conversation.partner_id
        if not partner:
            return

        try:
            memory = json.loads(partner.ai_memory or "{}")
        except (json.JSONDecodeError, TypeError):
            memory = {}

        # Track tools to understand interests
        try:
            tools = json.loads(conversation.ai_tools_used or "{}")
        except (json.JSONDecodeError, TypeError):
            tools = {}

        # Update interests based on tool usage
        interests = set(memory.get("interests", []))
        if "search_products" in tools or "get_product_details" in tools:
            interests.add("product_research")
        if "create_quote" in tools or "confirm_quote" in tools:
            interests.add("purchasing")
        if "check_invoices" in tools:
            interests.add("accounting")
        if "track_delivery" in tools:
            interests.add("delivery_tracking")

        if interests:
            memory["interests"] = list(interests)

        # Detect communication preference
        if "send_quote_pdf" in tools:
            memory["communication"] = "prefers WhatsApp PDF"

        # Update last interaction
        memory["last_interaction"] = fields.Datetime.now().isoformat() if fields.Datetime.now() else ""

        # Save memory
        try:
            partner.sudo().write({"ai_memory": json.dumps(memory, ensure_ascii=False)})
        except Exception as e:
            _logger.warning(f"Failed to update AI memory: {e}")

    @api.model
    def get_ai_analytics(self):
        """Return AI agent performance metrics for dashboard"""
        cr = self.env.cr

        # Total conversations handled by AI
        cr.execute("""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE ai_resolution = 'resolved') AS resolved,
                COUNT(*) FILTER (WHERE ai_resolution = 'escalated') AS escalated,
                COUNT(*) FILTER (WHERE ai_resolution = 'pending') AS pending,
                AVG(ai_response_count) FILTER (WHERE ai_response_count > 0) AS avg_responses,
                COUNT(*) FILTER (WHERE ai_lead_temperature = 'hot') AS hot_leads,
                COUNT(*) FILTER (WHERE ai_lead_temperature = 'warm') AS warm_leads,
                COUNT(*) FILTER (WHERE ai_lead_temperature = 'cold') AS cold_leads
            FROM bader_inbox_conversation
            WHERE ai_response_count > 0
        """)
        stats = cr.dictfetchone()

        # Tool usage aggregation
        cr.execute("""
            SELECT ai_tools_used
            FROM bader_inbox_conversation
            WHERE ai_tools_used IS NOT NULL AND ai_tools_used != '{}'
        """)
        tool_totals = {}
        for row in cr.dictfetchall():
            try:
                tools = json.loads(row["ai_tools_used"])
                for name, count in tools.items():
                    tool_totals[name] = tool_totals.get(name, 0) + count
            except (json.JSONDecodeError, TypeError):
                pass

        # Sort by usage
        top_tools = sorted(tool_totals.items(), key=lambda x: x[1], reverse=True)[:10]

        # Resolution rate
        total = stats["total"] or 1
        resolution_rate = round((stats["resolved"] or 0) / total * 100, 1)

        return {
            "total_conversations": stats["total"] or 0,
            "resolved_by_ai": stats["resolved"] or 0,
            "escalated": stats["escalated"] or 0,
            "pending": stats["pending"] or 0,
            "resolution_rate": resolution_rate,
            "avg_responses_per_conversation": round(stats["avg_responses"] or 0, 1),
            "hot_leads": stats["hot_leads"] or 0,
            "warm_leads": stats["warm_leads"] or 0,
            "cold_leads": stats["cold_leads"] or 0,
            "top_tools": [{"tool": t[0], "count": t[1]} for t in top_tools],
        }

    # ──────────────────────────────────────
    # TOOL EXECUTION DISPATCHER
    # ──────────────────────────────────────
    def _execute_tool(self, name, args, conversation):
        """Execute a tool and return the result (isolated with savepoint)"""
        method = getattr(self, f"_tool_{name}", None)
        if not method:
            return {"error": f"Unknown tool: {name}"}
        try:
            # Use savepoint to isolate SQL errors from aborting the webhook transaction
            with self.env.cr.savepoint():
                result = method(args, conversation)
                return result
        except Exception as e:
            _logger.error(f"Tool {name} error: {e}")
            return {"error": f"Tool failed: {e}"}

    # ──────────────────────────────────────
    # TOOL IMPLEMENTATIONS
    # ──────────────────────────────────────
    def _tool_list_categories(self, args, conversation):
        """List product categories with product counts."""
        query = args.get("query", "")
        Category = self.env["product.category"].sudo()
        domain = []
        if query:
            domain = [("name", "ilike", query)]
        categories = Category.search(domain, order="name asc", limit=50)
        results = []
        Product = self.env["product.product"].sudo()
        for cat in categories:
            count = Product.search_count([
                ("categ_id", "child_of", cat.id),
                ("sale_ok", "=", True),
                ("active", "=", True),
            ])
            if count > 0:
                results.append({
                    "id": cat.id,
                    "name": cat.complete_name or cat.name,
                    "product_count": count,
                })
        return {
            "categories": results,
            "count": len(results),
            "hint": "Present the relevant categories to the customer and ask which one interests them. Then use browse_category to show products."
        }

    def _tool_browse_category(self, args, conversation):
        """List all products in a specific category."""
        cat_id = args.get("category_id")
        cat_name = args.get("category_name", "")
        limit = min(args.get("limit", 20), 30)
        Product = self.env["product.product"].sudo()

        if not cat_id and cat_name:
            cat = self.env["product.category"].sudo().search(
                [("name", "ilike", cat_name)], limit=1
            )
            if cat:
                cat_id = cat.id

        if not cat_id:
            return {"error": "Category not found. Use list_categories first to find available categories."}

        products = Product.search([
            ("categ_id", "child_of", cat_id),
            ("sale_ok", "=", True),
            ("active", "=", True),
        ], limit=limit, order="name asc")

        cat_obj = self.env["product.category"].sudo().browse(cat_id)
        results = []
        for p in products:
            item = {
                "id": p.id,
                "name": p.name,
                "ref": p.default_code or "",
                "price": p.list_price,
                "currency": p.currency_id.name,
                "description": (p.description_sale or p.description or "")[:150],
            }
            if self.access_stock:
                item["stock"] = p.qty_available
                item["stock_status"] = "Available" if p.qty_available > 0 else "Out of stock"
            results.append(item)

        return {
            "category": cat_obj.complete_name or cat_obj.name,
            "products": results,
            "count": len(results),
            "hint": f"Found {len(results)} products in '{cat_obj.name}'. Present ALL to the customer with prices and ask which interests them. Suggest the most relevant ones based on their query. Use get_product_details for more info."
        }

    def _tool_search_products(self, args, conversation):
        query = args.get("query", "")
        category = args.get("category", "")
        limit = min(args.get("limit", 10), 20)
        Product = self.env["product.product"].sudo()
        domain = [
            ("sale_ok", "=", True),
            "|", "|",
            ("name", "ilike", query),
            ("default_code", "ilike", query),
            ("barcode", "ilike", query),
        ]
        if category:
            domain.append(("categ_id.name", "ilike", category))
        products = Product.search(domain, limit=limit)
        results = []
        for p in products:
            desc = p.description_sale or p.description or ""
            item = {
                "id": p.id,
                "name": p.name,
                "ref": p.default_code or "",
                "price": p.list_price,
                "currency": p.currency_id.name,
                "category": p.categ_id.name if p.categ_id else "",
                "description": desc[:200] if desc else "",
            }
            if self.access_stock:
                item["stock"] = p.qty_available
                item["stock_status"] = "Available" if p.qty_available > 0 else "Out of stock"
            results.append(item)
        return {
            "products": results,
            "count": len(results),
            "hint": f"Found {len(results)} products matching '{query}'." + (
                " Present ALL options to the customer and ask which one they want." if len(results) > 1 else ""
            ),
        }

    def _tool_get_product_details(self, args, conversation):
        pid = args.get("product_id")
        Product = self.env["product.product"].sudo()
        p = Product.browse(pid)
        if not p.exists():
            # Try as product.template
            tmpl = self.env["product.template"].sudo().browse(pid)
            if tmpl.exists():
                p = tmpl.product_variant_ids[:1]
            if not p or not p.exists():
                return {"error": "Product not found"}

        result = {
            "id": p.id,
            "name": p.name,
            "ref": p.default_code or "",
            "price": p.list_price,
            "currency": p.currency_id.name,
            "category": p.categ_id.name if p.categ_id else "",
            "description": (p.description_sale or p.description or "")[:800],
            "active": p.active,
        }

        # Stock info
        if self.access_stock:
            result["stock_available"] = p.qty_available
            result["stock_virtual"] = p.virtual_available
            result["stock_status"] = "Available" if p.qty_available > 0 else "Out of stock"

        # Weight & dimensions
        if p.weight:
            result["weight_kg"] = p.weight

        # Variants (if product has variants)
        try:
            tmpl = p.product_tmpl_id
            if tmpl and len(tmpl.product_variant_ids) > 1:
                variants = []
                for v in tmpl.product_variant_ids[:10]:
                    var_info = {
                        "id": v.id,
                        "name": v.display_name,
                        "ref": v.default_code or "",
                        "price": v.list_price,
                    }
                    if self.access_stock:
                        var_info["stock"] = v.qty_available
                    # Get attribute values (e.g. Color: Red, Size: M)
                    attrs = []
                    for val in v.product_template_attribute_value_ids:
                        attrs.append(f"{val.attribute_id.name}: {val.name}")
                    if attrs:
                        var_info["attributes"] = ", ".join(attrs)
                    variants.append(var_info)
                result["variants"] = variants
                result["variants_count"] = len(tmpl.product_variant_ids)
        except Exception:
            pass

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
        result = {
            "opportunities": [{
                "id": l.id,
                "name": l.name,
                "stage": l.stage_id.name if l.stage_id else "",
                "expected_revenue": l.expected_revenue,
                "contact": l.partner_name or "",
                "phone": l.phone or "",
            } for l in leads]
        }
        # Also include linked quotations for found opportunities
        for lead in leads:
            if lead.order_ids:
                for order in lead.order_ids[:3]:
                    result.setdefault("linked_quotations", []).append({
                        "opportunity_id": lead.id,
                        "order_name": order.name,
                        "order_id": order.id,
                        "amount_total": order.amount_total,
                        "currency": order.currency_id.name,
                        "state": order.state,
                        "date": order.date_order.strftime('%Y-%m-%d') if order.date_order else "",
                    })
        return result

    def _tool_search_quotes(self, args, conversation):
        """Search sales orders/quotations by customer, phone, or SO number"""
        query = args.get("query", "")
        state_filter = args.get("state", "all")
        SO = self.env["sale.order"].sudo()

        # Build domain
        search_domain = [
            "|", "|", "|",
            ("name", "ilike", query),
            ("partner_id.name", "ilike", query),
            ("partner_id.phone", "ilike", query),
            ("partner_id.mobile", "ilike", query),
        ]
        if state_filter == "draft":
            search_domain.append(("state", "=", "draft"))
        elif state_filter == "sale":
            search_domain.append(("state", "in", ["sale", "done"]))
        # else: all states

        orders = SO.search(search_domain, order="date_order desc", limit=10)

        result = []
        for o in orders:
            order_data = {
                "id": o.id,
                "name": o.name,
                "state": o.state,
                "state_label": dict(o._fields['state'].selection).get(o.state, o.state),
                "date": o.date_order.strftime('%Y-%m-%d') if o.date_order else "",
                "customer": o.partner_id.name if o.partner_id else "",
                "amount_untaxed": o.amount_untaxed,
                "amount_total": o.amount_total,
                "currency": o.currency_id.name,
                "salesperson": o.user_id.name if o.user_id else "",
            }
            # Include order lines (products)
            lines = []
            for line in o.order_line[:10]:
                lines.append({
                    "product": line.product_id.name if line.product_id else line.name,
                    "ref": line.product_id.default_code if line.product_id else "",
                    "qty": line.product_uom_qty,
                    "unit_price": line.price_unit,
                    "subtotal": line.price_subtotal,
                })
            order_data["lines"] = lines
            result.append(order_data)

        return {"quotations_orders": result, "total_found": len(result)}

    def _tool_create_quote(self, args, conversation):
        partner = conversation.partner_id
        if not partner:
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

        _logger.info(f"Creating quote for partner {partner.name} ({partner.id}), products: {products_data}")

        # Use ti@bader.es user for proper attribution
        ti_user = self.env["res.users"].sudo().search([("login", "=", "ti@bader.es")], limit=1)
        if not ti_user:
            ti_user = self.env.user
        _logger.info(f"Using user: {ti_user.name} ({ti_user.login})")

        # Switch to ti_user environment with sudo for full access
        SaleOrder = self.env["sale.order"].with_user(ti_user).sudo()

        order_vals = {
            "partner_id": partner.id,
            "note": args.get("note", "Created by AI Agent via WhatsApp"),
        }
        order = SaleOrder.create(order_vals)
        _logger.info(f"Sale order created: {order.name} (id={order.id})")

        OrderLine = self.env["sale.order.line"].with_user(ti_user).sudo()
        Product = self.env["product.product"].sudo()
        lines_created = 0
        for item in products_data:
            pid = item.get("product_id")
            pref = item.get("product_ref", "")
            qty = item.get("quantity", 1)
            product = Product.browse(0)  # empty recordset

            # Strategy 1: Search by ID in product.product
            if pid:
                product = Product.browse(pid)
                if not product.exists():
                    product = Product.browse(0)
                    _logger.info(f"  ID {pid} not found in product.product, trying template...")
                    # Strategy 2: Try as product.template ID
                    tmpl = self.env["product.template"].sudo().browse(pid)
                    if tmpl.exists() and tmpl.product_variant_ids:
                        product = tmpl.product_variant_ids[:1]
                        _logger.info(f"  Resolved template {pid} -> variant {product.id}")

            # Strategy 3: Search by reference/SKU (most reliable)
            if not product and pref:
                product = Product.search([("default_code", "=ilike", pref)], limit=1)
                if product:
                    _logger.info(f"  Found by ref '{pref}' -> product {product.id} ({product.name})")

            # Strategy 4: If still no product and we have a pid, search by default_code matching
            if not product and pid:
                # Last resort: maybe the ID is wrong, search broadly
                _logger.warning(f"  Product {pid} (ref={pref}) not found by ID or ref")
                continue

            if not product:
                _logger.warning(f"  Product not found: id={pid}, ref={pref}")
                continue

            line_vals = {
                "order_id": order.id,
                "product_id": product.id,
                "product_uom_qty": qty,
            }
            try:
                OrderLine.create(line_vals)
                lines_created += 1
                _logger.info(f"  Line added: {product.display_name} x{qty} (product.product id={product.id})")
            except Exception as e:
                _logger.error(f"  Failed to add line for product {product.id}: {e}")

        if lines_created == 0:
            _logger.warning("No lines were added to the quote!")

        # Refresh computed fields
        order.invalidate_recordset()

        # Send quotation by email ONLY if customer explicitly asked
        email_sent = False
        send_email = args.get("send_email", False)
        partner_email = args.get("email") or partner.email
        if send_email and partner_email:
            try:
                if args.get("email") and args["email"] != partner.email:
                    partner.sudo().write({"email": args["email"]})
                template = self.env.ref("sale.email_template_edi_sale", raise_if_not_found=False)
                if template:
                    template.sudo().send_mail(order.id, force_send=False)
                    email_sent = True
                    _logger.info(f"  Email queued for {partner_email}")
            except Exception as e:
                _logger.error(f"  Failed to send email: {e}")

        result = {
            "success": True,
            "quote_id": order.id,
            "quote_name": order.name,
            "total": order.amount_total,
            "currency": order.currency_id.name,
            "lines_count": lines_created,
            "email_sent": email_sent,
            "hint": "Ask the customer how they want to receive it: by email, by PDF here in WhatsApp, or both. Use send_quote_pdf to send the PDF in the chat.",
        }
        _logger.info(f"Quote result: {result}")
        return result

    def _tool_update_quote(self, args, conversation):
        """Update an existing sale.order: add/remove/update lines"""
        quote_id = args.get("quote_id")
        if not quote_id:
            return {"error": "quote_id is required. Use search_quotes to find it."}

        ti_user = self.env["res.users"].sudo().search([("login", "=", "ti@bader.es")], limit=1)
        if not ti_user:
            ti_user = self.env.user

        SaleOrder = self.env["sale.order"].with_user(ti_user).sudo()
        order = SaleOrder.browse(quote_id)
        if not order.exists():
            return {"error": f"Quotation ID {quote_id} not found"}

        # Only allow updates on draft quotations
        if order.state != 'draft':
            return {"error": f"Cannot update {order.name}: state is '{order.state}', only draft quotations can be modified"}

        _logger.info(f"Updating quote {order.name} (id={order.id})")
        changes = []

        OrderLine = self.env["sale.order.line"].with_user(ti_user).sudo()
        Product = self.env["product.product"].sudo()

        # 1. REMOVE lines
        remove_ids = args.get("remove_line_ids", [])
        if remove_ids:
            lines_to_remove = OrderLine.browse(remove_ids).filtered(lambda l: l.order_id.id == order.id)
            for line in lines_to_remove:
                changes.append(f"Removed: {line.product_id.display_name}")
                _logger.info(f"  Removing line {line.id}: {line.product_id.display_name}")
            lines_to_remove.unlink()

        # 2. UPDATE line quantities
        update_lines = args.get("update_lines", [])
        for upd in update_lines:
            line = None
            if upd.get("line_id"):
                line = OrderLine.browse(upd["line_id"])
                if not line.exists() or line.order_id.id != order.id:
                    line = None
            if not line and upd.get("product_id"):
                # Find by product_id in order lines
                line = order.order_line.filtered(lambda l: l.product_id.id == upd["product_id"])[:1]
            if line and upd.get("quantity"):
                old_qty = line.product_uom_qty
                line.write({"product_uom_qty": upd["quantity"]})
                changes.append(f"Updated qty: {line.product_id.display_name} {old_qty} -> {upd['quantity']}")
                _logger.info(f"  Updated line {line.id}: qty {old_qty} -> {upd['quantity']}")
            elif not line:
                _logger.warning(f"  Line not found for update: {upd}")
                changes.append(f"Line not found: {upd}")

        # 3. ADD new products
        add_products = args.get("add_products", [])
        for item in add_products:
            pid = item.get("product_id")
            pref = item.get("product_ref", "")
            qty = item.get("quantity", 1)
            product = Product.browse(0)

            if pid:
                product = Product.browse(pid)
                if not product.exists():
                    product = Product.browse(0)
                    tmpl = self.env["product.template"].sudo().browse(pid)
                    if tmpl.exists() and tmpl.product_variant_ids:
                        product = tmpl.product_variant_ids[:1]

            if not product and pref:
                product = Product.search([("default_code", "=ilike", pref)], limit=1)

            if not product:
                _logger.warning(f"  Product not found: id={pid}, ref={pref}")
                changes.append(f"Product not found: id={pid}, ref={pref}")
                continue

            # Check if product already exists in order, update qty instead
            existing_line = order.order_line.filtered(lambda l: l.product_id.id == product.id)[:1]
            if existing_line:
                old_qty = existing_line.product_uom_qty
                existing_line.write({"product_uom_qty": old_qty + qty})
                changes.append(f"Added qty to existing: {product.display_name} {old_qty} -> {old_qty + qty}")
                _logger.info(f"  Updated existing line: {product.display_name} qty {old_qty} -> {old_qty + qty}")
            else:
                OrderLine.create({
                    "order_id": order.id,
                    "product_id": product.id,
                    "product_uom_qty": qty,
                })
                changes.append(f"Added: {product.display_name} x{qty}")
                _logger.info(f"  Added line: {product.display_name} x{qty}")

        # Refresh computed fields
        order.invalidate_recordset()

        # Send email if requested
        email_sent = False
        send_email = args.get("send_email", False)
        partner = order.partner_id
        partner_email = args.get("email") or (partner.email if partner else "")
        if send_email and partner_email:
            try:
                if args.get("email") and args["email"] != partner.email:
                    partner.sudo().write({"email": args["email"]})
                template = self.env.ref("sale.email_template_edi_sale", raise_if_not_found=False)
                if template:
                    template.sudo().send_mail(order.id, force_send=False)
                    email_sent = True
            except Exception as e:
                _logger.error(f"  Failed to send email: {e}")

        # Build current lines summary
        current_lines = []
        for line in order.order_line:
            current_lines.append({
                "line_id": line.id,
                "product": line.product_id.display_name,
                "ref": line.product_id.default_code or "",
                "qty": line.product_uom_qty,
                "unit_price": line.price_unit,
                "subtotal": line.price_subtotal,
            })

        result = {
            "success": True,
            "quote_id": order.id,
            "quote_name": order.name,
            "total": order.amount_total,
            "currency": order.currency_id.name,
            "changes": changes,
            "current_lines": current_lines,
            "email_sent": email_sent,
            "hint": "Use send_quote_pdf to send the updated PDF in WhatsApp.",
        }
        _logger.info(f"Update quote result: {result}")
        return result

    def _tool_send_quote_pdf(self, args, conversation):
        """Send PDF of a quotation via WhatsApp"""
        quote_id = args.get("quote_id")
        if not quote_id:
            return {"error": "quote_id is required"}

        order = self.env["sale.order"].sudo().browse(quote_id)
        if not order.exists():
            return {"error": f"Quote {quote_id} not found"}

        try:
            report = self.env.ref("sale.action_report_saleorder", raise_if_not_found=False)
            if not report:
                return {"error": "Report template not found"}

            pdf_content, _ = report.sudo()._render_qweb_pdf(report.report_name, [order.id])
            if not pdf_content:
                return {"error": "Failed to generate PDF"}

            import base64
            pdf_b64 = base64.b64encode(pdf_content).decode("utf-8")
            filename = f"{order.name}.pdf"

            Message = self.env["bader.inbox.message"].sudo()
            Message.send_message(
                conversation.id,
                f"📄 {order.name}",
                msg_type="document",
                media_data=pdf_b64,
                media_filename=filename,
            )
            _logger.info(f"PDF sent via WhatsApp: {filename}")
            return {
                "success": True,
                "message": f"PDF {filename} sent in the conversation",
            }
        except Exception as e:
            _logger.error(f"Failed to send PDF: {e}")
            return {"error": f"Failed to generate/send PDF: {e}"}

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

    def _tool_track_delivery(self, args, conversation):
        """Track deliveries for the customer's partner"""
        partner = conversation.partner_id
        if not partner:
            return {"error": "No customer associated with this conversation. Cannot track deliveries."}

        Picking = self.env["stock.picking"].sudo()
        domain = [
            ("partner_id", "=", partner.id),
            ("picking_type_code", "=", "outgoing"),
        ]

        # Optional filter by SO number
        order_name = args.get("order_name", "")
        if order_name:
            sale_order = self.env["sale.order"].sudo().search([("name", "=ilike", order_name)], limit=1)
            if sale_order:
                domain.append(("origin", "ilike", sale_order.name))
            else:
                return {"error": f"Order '{order_name}' not found"}

        pickings = Picking.search(domain, order="scheduled_date desc", limit=10)
        if not pickings:
            return {"deliveries": [], "message": "No deliveries found for this customer"}

        deliveries = []
        for p in pickings:
            delivery = {
                "name": p.name,
                "origin": p.origin or "",
                "state": p.state,
                "state_label": dict(p._fields['state'].selection).get(p.state, p.state),
                "scheduled_date": p.scheduled_date.strftime('%Y-%m-%d %H:%M') if p.scheduled_date else "",
                "date_done": p.date_done.strftime('%Y-%m-%d %H:%M') if p.date_done else "",
                "carrier": p.carrier_id.name if p.carrier_id else "",
                "tracking_ref": p.carrier_tracking_ref or "",
            }
            # Products in this delivery
            products = []
            for move in p.move_ids[:5]:
                products.append({
                    "product": move.product_id.display_name,
                    "qty_ordered": move.product_uom_qty,
                    "qty_delivered": move.quantity_done,
                })
            delivery["products"] = products
            deliveries.append(delivery)

        return {"deliveries": deliveries, "total_found": len(deliveries)}

    def _tool_check_invoices(self, args, conversation):
        """Check invoices and payment status for the customer's partner"""
        partner = conversation.partner_id
        if not partner:
            return {"error": "No customer associated with this conversation. Cannot check invoices."}

        Invoice = self.env["account.move"].sudo()
        status_filter = args.get("status", "all")
        invoice_name = args.get("invoice_name", "")

        domain = [
            ("partner_id", "=", partner.id),
            ("move_type", "in", ["out_invoice", "out_refund"]),
        ]

        if invoice_name:
            domain.append(("name", "ilike", invoice_name))

        if status_filter == "open":
            domain.append(("payment_state", "in", ["not_paid", "partial"]))
            domain.append(("state", "=", "posted"))
        elif status_filter == "paid":
            domain.append(("payment_state", "=", "paid"))
        elif status_filter == "overdue":
            domain.append(("payment_state", "in", ["not_paid", "partial"]))
            domain.append(("state", "=", "posted"))
            domain.append(("invoice_date_due", "<", fields.Date.today()))

        invoices = Invoice.search(domain, order="invoice_date desc", limit=15)

        invoice_list = []
        total_open = 0.0
        total_overdue = 0.0
        for inv in invoices:
            inv_data = {
                "name": inv.name,
                "type": "invoice" if inv.move_type == "out_invoice" else "credit_note",
                "date": inv.invoice_date.strftime('%Y-%m-%d') if inv.invoice_date else "",
                "due_date": inv.invoice_date_due.strftime('%Y-%m-%d') if inv.invoice_date_due else "",
                "amount_total": inv.amount_total,
                "amount_residual": inv.amount_residual,
                "currency": inv.currency_id.name,
                "payment_state": inv.payment_state,
                "state": inv.state,
            }
            if inv.payment_state in ("not_paid", "partial") and inv.state == "posted":
                total_open += inv.amount_residual
                if inv.invoice_date_due and inv.invoice_date_due < fields.Date.today():
                    inv_data["overdue"] = True
                    total_overdue += inv.amount_residual
            invoice_list.append(inv_data)

        return {
            "invoices": invoice_list,
            "total_found": len(invoice_list),
            "total_open_amount": total_open,
            "total_overdue_amount": total_overdue,
            "currency": partner.currency_id.name if partner.currency_id else "EUR",
            "customer": partner.name,
        }

    def _tool_confirm_quote(self, args, conversation):
        """Confirm a draft quotation to a sales order"""
        quote_id = args.get("quote_id")
        if not quote_id:
            return {"error": "quote_id is required. Use search_quotes to find it."}

        ti_user = self.env["res.users"].sudo().search([("login", "=", "ti@bader.es")], limit=1)
        if not ti_user:
            ti_user = self.env.user

        SaleOrder = self.env["sale.order"].with_user(ti_user).sudo()
        order = SaleOrder.browse(quote_id)
        if not order.exists():
            return {"error": f"Quotation ID {quote_id} not found"}

        # Verify this belongs to the conversation's partner
        partner = conversation.partner_id
        if partner and order.partner_id.id != partner.id:
            return {"error": f"This quotation does not belong to the current customer"}

        if order.state != 'draft':
            return {"error": f"Cannot confirm {order.name}: state is '{order.state}', only draft quotations can be confirmed"}

        if not order.order_line:
            return {"error": f"Cannot confirm {order.name}: no products in the quotation"}

        _logger.info(f"Confirming quote {order.name} (id={order.id})")

        try:
            order.action_confirm()
            _logger.info(f"Quote {order.name} confirmed successfully")
        except Exception as e:
            _logger.error(f"Failed to confirm {order.name}: {e}")
            return {"error": f"Failed to confirm: {e}"}

        # Send email if requested
        email_sent = False
        if args.get("send_email", False):
            try:
                template = self.env.ref("sale.email_template_edi_sale", raise_if_not_found=False)
                if template:
                    template.sudo().send_mail(order.id, force_send=False)
                    email_sent = True
            except Exception as e:
                _logger.error(f"Failed to send confirmation email: {e}")

        # Build summary of confirmed lines
        lines_summary = []
        for line in order.order_line:
            lines_summary.append({
                "product": line.product_id.display_name,
                "qty": line.product_uom_qty,
                "subtotal": line.price_subtotal,
            })

        return {
            "success": True,
            "quote_id": order.id,
            "quote_name": order.name,
            "state": order.state,
            "amount_total": order.amount_total,
            "currency": order.currency_id.name,
            "lines": lines_summary,
            "email_sent": email_sent,
            "message": f"Quotation {order.name} has been confirmed as a sales order!",
        }

    def _tool_get_pricelist(self, args, conversation):
        """Get personalized pricing for the customer"""
        partner = conversation.partner_id
        if not partner:
            return {"error": "No customer associated. Cannot get personalized pricing."}

        Product = self.env["product.product"].sudo()
        products_to_check = Product.browse()

        # Collect products by ID
        for pid in args.get("product_ids", []):
            p = Product.browse(pid)
            if p.exists():
                products_to_check |= p

        # Collect products by reference
        for ref in args.get("product_refs", []):
            p = Product.search([("default_code", "=ilike", ref)], limit=1)
            if p:
                products_to_check |= p

        # If no products specified, get products from recent quotations
        if not products_to_check:
            recent_orders = self.env["sale.order"].sudo().search([
                ("partner_id", "=", partner.id),
                ("state", "=", "draft"),
            ], order="date_order desc", limit=1)
            if recent_orders:
                for line in recent_orders.order_line[:5]:
                    if line.product_id:
                        products_to_check |= line.product_id

        if not products_to_check:
            return {"error": "No products specified or found. Use product_ids or product_refs, or search_products first."}

        quantity = args.get("quantity", 1)
        pricelist = partner.property_product_pricelist

        prices = []
        for product in products_to_check:
            price_data = {
                "product_id": product.id,
                "product_name": product.display_name,
                "ref": product.default_code or "",
                "list_price": product.list_price,
            }
            if pricelist:
                try:
                    pricelist_price = pricelist._get_product_price(product, quantity, partner)
                    price_data["your_price"] = pricelist_price
                    price_data["pricelist_name"] = pricelist.name
                    if product.list_price > 0:
                        discount_pct = round((1 - pricelist_price / product.list_price) * 100, 1)
                        price_data["discount_percent"] = discount_pct
                except Exception:
                    price_data["your_price"] = product.list_price
                    price_data["pricelist_name"] = "Standard"
            else:
                price_data["your_price"] = product.list_price
                price_data["pricelist_name"] = "Standard (no specific pricelist)"

            price_data["currency"] = product.currency_id.name if product.currency_id else "EUR"
            prices.append(price_data)

        return {
            "prices": prices,
            "customer": partner.name,
            "pricelist": pricelist.name if pricelist else "Standard",
            "quantity": quantity,
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
