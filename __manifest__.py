# Copyright 2026 Bader Business
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

{
    "name": "Bader Inbox",
    "summary": "Omnichannel WhatsApp Inbox - Simple, Modern, Powerful",
    "description": """
        Modern WhatsApp inbox for Odoo inspired by Respond.io and Clientify.
        
        Features:
        - Ultra-simple setup: Name + QR Code = Connected
        - Modern 3-panel interface
        - Multi-channel support (multiple WhatsApp numbers)
        - Team collaboration (assign conversations)
        - Full CRM integration (contacts, opportunities, activities)
        - Quick reply templates
        - Chatbot automations
        - Real-time notifications
        - Uses Evolution API for QR-based connection
    """,
    "version": "16.0.2.0.0",
    "license": "AGPL-3",
    "author": "Bader Business",
    "website": "https://github.com/badboytuba/bader-inbox-pro",
    "category": "Productivity/Discuss",
    "depends": ["base", "contacts", "crm", "mail", "bus"],
    "external_dependencies": {"python": ["requests"]},
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "data/system_parameters.xml",
        "views/channel_views.xml",
        "views/conversation_views.xml",
        "views/template_views.xml",
        "views/chatbot_views.xml",
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
