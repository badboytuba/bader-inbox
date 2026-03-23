# Copyright 2026 Bader Business
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import logging
import requests

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class BaderInboxTranslationSettings(models.TransientModel):
    """Translation configuration — separate from AI Agent"""

    _name = "bader.inbox.translation.settings"
    _description = "Bader Inbox Translation Settings"

    openai_api_key = fields.Char(
        string="OpenAI API Key",
        default=lambda self: self.env["ir.config_parameter"].sudo().get_param(
            "bader_inbox.translation_api_key", ""
        ),
    )

    openai_model = fields.Selection(
        [
            ("gpt-4o-mini", "GPT-4o Mini (rápido, económico)"),
            ("gpt-4o", "GPT-4o (más preciso)"),
            ("gpt-3.5-turbo", "GPT-3.5 Turbo (legado)"),
        ],
        string="Modelo OpenAI",
        default=lambda self: self.env["ir.config_parameter"].sudo().get_param(
            "bader_inbox.translation_model", "gpt-4o-mini"
        ),
    )

    target_language = fields.Selection(
        [
            ("es", "Español"),
            ("pt", "Português"),
            ("en", "English"),
            ("de", "Deutsch"),
            ("fr", "Français"),
            ("it", "Italiano"),
            ("ar", "العربية"),
            ("zh", "中文"),
        ],
        string="Idioma Destino",
        default=lambda self: self.env["ir.config_parameter"].sudo().get_param(
            "bader_inbox.translation_target_lang", "es"
        ),
    )

    auto_translate_incoming = fields.Boolean(
        string="Auto-traducir mensajes recibidos",
        default=lambda self: self.env["ir.config_parameter"].sudo().get_param(
            "bader_inbox.translation_auto_incoming", "False"
        ) == "True",
    )

    auto_translate_outgoing = fields.Boolean(
        string="Traducir compositor antes de enviar",
        default=lambda self: self.env["ir.config_parameter"].sudo().get_param(
            "bader_inbox.translation_auto_outgoing", "False"
        ) == "True",
    )

    def action_save(self):
        self.ensure_one()
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("bader_inbox.translation_api_key", self.openai_api_key or "")
        ICP.set_param("bader_inbox.translation_model", self.openai_model or "gpt-4o-mini")
        ICP.set_param("bader_inbox.translation_target_lang", self.target_language or "es")
        ICP.set_param("bader_inbox.translation_auto_incoming", str(self.auto_translate_incoming))
        ICP.set_param("bader_inbox.translation_auto_outgoing", str(self.auto_translate_outgoing))

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "✅ Traducción Configurada",
                "message": "Configuración de traducción guardada con éxito!",
                "type": "success",
                "sticky": False,
            }
        }

    def action_test(self):
        """Test translation API connection"""
        self.ensure_one()
        api_key = self.openai_api_key
        model = self.openai_model or "gpt-4o-mini"

        if not api_key:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "❌ Error",
                    "message": "Ingrese una API Key primero",
                    "type": "danger",
                    "sticky": False,
                }
            }

        try:
            resp = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "Translate to English: Hola mundo"}],
                    "max_tokens": 20,
                    "temperature": 0,
                },
                timeout=10,
            )
            resp.raise_for_status()
            result = resp.json()["choices"][0]["message"]["content"].strip()
            msg = f"✅ Conexión OK! Resultado: '{result}' (modelo: {model})"
            msg_type = "success"
        except requests.exceptions.HTTPError as e:
            msg = f"❌ Error HTTP: {e.response.status_code} — {e.response.text[:200]}"
            msg_type = "danger"
        except Exception as e:
            msg = f"❌ Error: {str(e)[:200]}"
            msg_type = "danger"

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Test de Traducción",
                "message": msg,
                "type": msg_type,
                "sticky": True,
            }
        }

    @api.model
    def _get_api_key_and_model(self):
        """Get translation API key and model, falling back to AI Agent config"""
        ICP = self.env["ir.config_parameter"].sudo()
        api_key = ICP.get_param("bader_inbox.translation_api_key", "")
        model = ICP.get_param("bader_inbox.translation_model", "gpt-4o-mini")
        if not api_key:
            # Fallback to AI Agent config
            agent = self.env["bader.inbox.ai_assistant"].sudo().search([], limit=1)
            if agent and agent.api_key:
                api_key = agent.api_key
                model = agent.model_name or model
        return api_key, model

    @api.model
    def get_translation_config(self):
        """Return translation config for frontend"""
        ICP = self.env["ir.config_parameter"].sudo()
        api_key, _ = self._get_api_key_and_model()
        return {
            "enabled": bool(api_key),
            "target_language": ICP.get_param("bader_inbox.translation_target_lang", "es"),
            "auto_incoming": ICP.get_param("bader_inbox.translation_auto_incoming", "False") == "True",
            "auto_outgoing": ICP.get_param("bader_inbox.translation_auto_outgoing", "False") == "True",
            "model": ICP.get_param("bader_inbox.translation_model", "gpt-4o-mini"),
        }

    @api.model
    def translate_text(self, text, target_lang=None):
        """Translate text using translation API config (fallback to AI Agent)"""
        ICP = self.env["ir.config_parameter"].sudo()
        api_key, model = self._get_api_key_and_model()
        if not target_lang:
            target_lang = ICP.get_param("bader_inbox.translation_target_lang", "es")

        if not api_key:
            return {"error": "No API key configured. Go to Configuration > 🌐 Traducción or AI Assistente"}
        if not text or not text.strip():
            return {"error": "Empty text"}

        lang_names = {
            "es": "Spanish", "pt": "Portuguese", "en": "English",
            "de": "German", "fr": "French", "it": "Italian",
            "ar": "Arabic", "zh": "Chinese",
        }
        lang_name = lang_names.get(target_lang, target_lang)

        try:
            resp = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                f"You are a translator. Translate the user's text to {lang_name}. "
                                "Return ONLY the translation, nothing else. "
                                "Preserve the tone and style of the original message."
                            ),
                        },
                        {"role": "user", "content": text},
                    ],
                    "max_tokens": 1000,
                    "temperature": 0.3,
                },
                timeout=15,
            )
            resp.raise_for_status()
            translated = resp.json()["choices"][0]["message"]["content"].strip()
            return {"translated": translated, "target_lang": target_lang}
        except Exception as e:
            _logger.error(f"Translation error: {e}")
            return {"error": str(e)[:200]}
