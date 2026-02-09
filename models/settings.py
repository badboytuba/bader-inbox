# Copyright 2026 Bader Business
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo import api, fields, models


class BaderInboxSettings(models.TransientModel):
    """Settings wizard for Evolution API configuration"""
    
    _name = "bader.inbox.settings"
    _description = "Bader Inbox API Settings"
    
    evolution_url = fields.Char(
        string="URL da API Evolution",
        default=lambda self: self.env["ir.config_parameter"].sudo().get_param(
            "bader_inbox.evolution_url", "https://whatsapp.odontowave.com"
        ),
        required=True
    )
    
    evolution_key = fields.Char(
        string="Chave API (apikey)",
        default=lambda self: self.env["ir.config_parameter"].sudo().get_param(
            "bader_inbox.evolution_key", ""
        ),
        required=True
    )
    
    def action_save(self):
        """Save API settings to system parameters"""
        self.ensure_one()
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("bader_inbox.evolution_url", self.evolution_url)
        ICP.set_param("bader_inbox.evolution_key", self.evolution_key)
        
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "✅ Configuração Salva",
                "message": "API Evolution configurada com sucesso!",
                "type": "success",
                "sticky": False,
            }
        }
    
    def action_test_connection(self):
        """Test API connection"""
        self.ensure_one()
        # Temporarily save to test
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("bader_inbox.evolution_url", self.evolution_url)
        ICP.set_param("bader_inbox.evolution_key", self.evolution_key)
        
        try:
            api = self.env["bader.inbox.evolution_api"]
            result = api.list_instances()
            
            if isinstance(result, list):
                msg = f"✅ Conectado! {len(result)} instâncias encontradas."
                msg_type = "success"
            elif result.get("error"):
                msg = f"❌ Erro: {result.get('message', result.get('error'))}"
                msg_type = "danger"
            else:
                msg = "✅ Conexão funcionando!"
                msg_type = "success"
                
        except Exception as e:
            msg = f"❌ Erro de conexão: {str(e)}"
            msg_type = "danger"
        
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Teste de Conexão",
                "message": msg,
                "type": msg_type,
                "sticky": True,
            }
        }
