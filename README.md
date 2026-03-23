# Bader Inbox — WhatsApp Integration for Odoo 16

> Simple WhatsApp inbox for Odoo using Evolution API.
> Setup: Name + QR Code = Connected ✅

## Quick Install (for AI agents)

```bash
# 1. Clone to Odoo addons directory
cd /opt/odoo/src/bader  # or any directory in addons_path
sudo git clone https://github.com/badboytuba/bader-inbox.git bader_inbox

# 2. Fix ownership
sudo chown -R odoo:odoo bader_inbox/

# 3. Ensure the parent directory is in Odoo's addons_path
# Edit /opt/odoo/conf/odoo-server.conf:
#   addons_path = ...,/opt/odoo/src/bader

# 4. Install Python dependency
sudo /opt/odoo/.venv/bin/pip install requests

# 5. Restart Odoo
sudo systemctl restart odoo

# 6. Install the module
sudo -u odoo /opt/odoo/.venv/bin/python3 /opt/odoo/src/odoo/odoo-bin \
  -d YOUR_DATABASE \
  --config=/opt/odoo/conf/odoo-server.conf \
  -i bader_inbox \
  --stop-after-init

# 7. Restart Odoo again
sudo systemctl restart odoo
```

## Features

- Real-time WhatsApp messaging (text, images, audio, video, docs)
- Group conversations with sender avatars
- CRM integration (contacts, leads)
- AI Assistant for auto-replies
- Chatbot flows
- Mass campaigns with templates
- Multi-channel (multiple WhatsApp numbers)
- Media handling
- Tags & sales pipeline

## Requirements

| Requirement | Version |
|------------|---------|
| Odoo | 16.0 (Community or Enterprise) |
| Python | 3.8+ |
| Evolution API | Baileys-based |
| Python packages | `requests` |

### Odoo Dependencies
`base`, `contacts`, `crm`, `mail`, `bus`, `sale_management`, `calendar`

## Configuration

After installation:
1. Go to **Bader Inbox** → **Configuração**
2. Set **Evolution API URL** and **API Key**
3. **Create Channel** → scan QR code → ✅ Connected

## Updating

```bash
cd /opt/odoo/src/bader/bader_inbox
sudo git pull origin main
sudo -u odoo /opt/odoo/.venv/bin/python3 /opt/odoo/src/odoo/odoo-bin \
  -d YOUR_DATABASE --config=/opt/odoo/conf/odoo-server.conf \
  -u bader_inbox --stop-after-init
sudo systemctl restart odoo
```

## License
AGPL-3.0
