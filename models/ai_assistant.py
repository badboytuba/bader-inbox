# Copyright 2026 Bader Business
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import json
import logging

import requests

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class BaderInboxAIAssistant(models.Model):
    """AI Assistant Configuration & Methods"""

    _name = "bader.inbox.ai_assistant"
    _description = "AI Assistant"

    name = fields.Char(string="Name", default="Default", required=True)
    active = fields.Boolean(default=True)

    provider = fields.Selection([
        ("openai", "OpenAI"),
        ("claude", "Claude"),
    ], default="openai", string="Provider", required=True)

    api_key = fields.Char(string="API Key")
    model_name = fields.Char(string="Model", default="gpt-4o-mini")
    temperature = fields.Float(string="Temperature", default=0.7)
    max_tokens = fields.Integer(string="Max Tokens", default=300)
    system_prompt = fields.Text(
        string="System Prompt",
        default=(
            "You are a professional customer support assistant for Bader, "
            "a hardware and tools company. Suggest 3 short, helpful reply options "
            "in the same language as the customer's message. "
            "Return JSON array of strings, nothing else."
        )
    )

    @api.model
    def _get_config(self):
        """Get active AI config"""
        config = self.search([("active", "=", True)], limit=1)
        return config if config and config.api_key else None

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

        try:
            if config.provider == "openai":
                return config._call_openai(chat_history)
            elif config.provider == "claude":
                return config._call_claude(chat_history)
        except Exception as e:
            _logger.error(f"AI suggestion error: {e}")
            return {"error": str(e), "suggestions": []}

    def _call_openai(self, chat_history):
        self.ensure_one()
        api_messages = [{"role": "system", "content": self.system_prompt}]
        api_messages.extend(chat_history)

        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            },
            json={
                "model": self.model_name or "gpt-4o-mini",
                "messages": api_messages,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens or 300,
            },
            timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return {"suggestions": self._parse_suggestions(content)}

    def _call_claude(self, chat_history):
        self.ensure_one()
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json"
            },
            json={
                "model": self.model_name or "claude-3-haiku-20240307",
                "system": self.system_prompt,
                "messages": [
                    {"role": m["role"] if m["role"] != "assistant" else "assistant",
                     "content": m["content"]}
                    for m in chat_history
                ],
                "max_tokens": self.max_tokens or 300,
                "temperature": self.temperature,
            },
            timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["content"][0]["text"]
        return {"suggestions": self._parse_suggestions(content)}

    def _parse_suggestions(self, text):
        """Parse JSON array from LLM response"""
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
        """Detect language of text using LLM"""
        config = self._get_config()
        if not config or not text:
            return ""
        try:
            prompt = (
                f'What language is this text? Return ONLY the ISO 639-1 code '
                f'(e.g. "en", "es", "pt", "fr", "de", "ar"). Text: "{text[:200]}"'
            )
            if config.provider == "openai":
                resp = requests.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {config.api_key}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": config.model_name or "gpt-4o-mini",
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 5,
                        "temperature": 0,
                    },
                    timeout=10
                )
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"].strip().lower()[:5]
            elif config.provider == "claude":
                resp = requests.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": config.api_key,
                        "anthropic-version": "2023-06-01",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": config.model_name or "claude-3-haiku-20240307",
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 5,
                    },
                    timeout=10
                )
                resp.raise_for_status()
                return resp.json()["content"][0]["text"].strip().lower()[:5]
        except Exception as e:
            _logger.debug(f"Language detection error: {e}")
        return ""

    @api.model
    def translate_message(self, message_id, target_lang="es"):
        """Translate a message to target language"""
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
                f'Return ONLY the translation, no quotes or explanation.\n\n'
                f'Text: {message.content}'
            )
            if config.provider == "openai":
                resp = requests.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {config.api_key}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": config.model_name or "gpt-4o-mini",
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 500,
                        "temperature": 0.3,
                    },
                    timeout=15
                )
                resp.raise_for_status()
                translated = resp.json()["choices"][0]["message"]["content"].strip()
            elif config.provider == "claude":
                resp = requests.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": config.api_key,
                        "anthropic-version": "2023-06-01",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": config.model_name or "claude-3-haiku-20240307",
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 500,
                    },
                    timeout=15
                )
                resp.raise_for_status()
                translated = resp.json()["content"][0]["text"].strip()
            else:
                return {"error": "Unknown provider"}

            lang = self.detect_language(message.content)
            message.sudo().write({
                "translated_content": translated,
                "detected_language": lang,
            })
            return {"translated": translated, "language": lang}
        except Exception as e:
            _logger.error(f"Translation error: {e}")
            return {"error": str(e)}
