# Copyright 2026 Bader Business
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

{
    "name": "Bader Inbox",
    "summary": "WhatsApp Integration - Simple Setup via QR Code",
    "description": """
        Simple WhatsApp inbox for Odoo using Evolution API.
        
        Features:
        - Simple setup: Name + QR Code = Connected
        - Modern 3-panel interface
        - Send and receive WhatsApp messages
        - Full synchronization with your phone
        - CRM integration (contacts, opportunities)
    """,
    "version": "16.0.3.0.0",
    "license": "AGPL-3",
    "author": "Bader Business",
    "website": "https://github.com/badboytuba/bader-inbox",
    "category": "Productivity/Discuss",
    "depends": ["base", "contacts", "crm", "mail", "bus"],
    "external_dependencies": {"python": ["requests"]},
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "data/system_parameters.xml",
        "views/settings_views.xml",
        "views/channel_views.xml",
        "views/conversation_views.xml",
        "views/chatbot_views.xml",
        "views/template_views.xml",
        "data/chatbot_cron.xml",
        "views/phone_widget_views.xml",
        "views/menus.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "bader_inbox/static/src/components/**/*.js",
            "bader_inbox/static/src/components/**/*.xml",
            "bader_inbox/static/src/scss/**/*.scss",
        ],
    },
    "application": True,
    "installable": True,
}
