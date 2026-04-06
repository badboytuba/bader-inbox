# Copyright 2026 Bader Business
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import logging
from datetime import timedelta
from odoo import api, fields, models, _
from odoo.exceptions import UserError
import uuid

_logger = logging.getLogger(__name__)


class BaderInboxChannel(models.Model):
    """WhatsApp Channel - represents a connected WhatsApp number"""
    
    _name = "bader.inbox.channel"
    _description = "Bader Inbox Channel"
    _order = "name"

    name = fields.Char(string="Channel Name", required=True)
    phone = fields.Char(string="Phone Number", readonly=True)
    phone_name = fields.Char(string="WhatsApp Name", readonly=True)
    
    state = fields.Selection([
        ("draft", "Draft"),
        ("connecting", "Connecting"),
        ("qr_ready", "QR Ready"),
        ("connected", "Connected"),
        ("disconnected", "Disconnected"),
        ("error", "Error"),
    ], default="draft", string="Status")
    
    # Evolution API
    evolution_instance_name = fields.Char(string="Instance Name", readonly=True)
    qrcode_base64 = fields.Text(string="QR Code", readonly=True)
    
    # Webhook
    webhook_url = fields.Char(string="Webhook URL", readonly=True)
    webhook_token = fields.Char(string="Webhook Token", readonly=True, copy=False)
    
    # Health monitoring
    last_health_check = fields.Datetime(string="Last Health Check", readonly=True)
    health_status = fields.Selection([
        ("ok", "Healthy"),
        ("warning", "Warning"),
        ("error", "Error"),
    ], string="Health", default="ok", readonly=True)
    reconnect_attempts = fields.Integer(string="Reconnect Attempts", default=0, readonly=True)
    
    # Relations
    conversation_ids = fields.One2many(
        "bader.inbox.conversation", "channel_id", string="Conversations"
    )
    conversation_count = fields.Integer(
        compute="_compute_conversation_count", string="Conversations"
    )
    
    # Company
    company_id = fields.Many2one(
        "res.company", string="Company",
        default=lambda self: self.env.company
    )

    # Channel access control
    allowed_user_ids = fields.Many2many(
        "res.users", "bader_inbox_channel_user_rel",
        "channel_id", "user_id",
        string="Allowed Users",
        help="Users who can see conversations from this channel. Leave empty for everyone."
    )
    
    @api.depends("conversation_ids")
    def _compute_conversation_count(self):
        for rec in self:
            rec.conversation_count = len(rec.conversation_ids)

    @api.model_create_multi
    def create(self, vals_list):
        """Auto-add the creating user to allowed_user_ids so the record rule
        doesn't block the read-back after creation."""
        uid = self.env.uid
        for vals in vals_list:
            allowed = vals.get("allowed_user_ids", [])
            # Add current user via (4, uid) command if not already present
            has_user = any(
                (isinstance(cmd, (list, tuple)) and len(cmd) >= 2 and cmd[0] == 4 and cmd[1] == uid)
                for cmd in allowed
            )
            if not has_user:
                allowed.append((4, uid))
                vals["allowed_user_ids"] = allowed
        return super().create(vals_list)

    def action_connect(self):
        """Start connection process - create instance and get QR"""
        self.ensure_one()
        rec = self.sudo()  # bypass record rules for channel writes
        try:
            api = self.env["bader.inbox.evolution_api"]

            # Generate instance name
            instance_name = f"bader_{self.id}_{self.name.lower().replace(' ', '_')}"
            rec.evolution_instance_name = instance_name

            # Generate webhook URL BEFORE creating instance
            base_url = self.env["ir.config_parameter"].sudo().get_param("web.base.url")

            # Generate or reuse token
            if not rec.webhook_token:
                rec.webhook_token = str(uuid.uuid4())

            webhook_url = f"{base_url}/bader-inbox/webhook/{self.id}/{rec.webhook_token}"
            rec.webhook_url = webhook_url

            # Create instance WITH webhook configured
            # On timeout, the instance is likely created — QR will arrive via webhook
            result = api.create_instance(instance_name, webhook_url=webhook_url)
            timed_out = "timed out" in str(result.get("error", "")).lower()

            if not result.get("success") and not timed_out:
                raise UserError(_("Failed to create instance: %s") % result.get("error"))

            # Set state to connecting — webhook will upgrade to qr_ready/connected
            rec.state = "connecting"

            if not timed_out:
                # Also try set_webhook as fallback for older API versions
                try:
                    api.set_webhook(instance_name, webhook_url)
                except Exception:
                    pass  # Ignore - webhook was already set during creation

                # Get QR code (non-blocking: if it fails, webhook delivers it)
                try:
                    qr_result = api.get_qrcode(instance_name)
                    if qr_result.get("qrcode"):
                        rec.qrcode_base64 = qr_result["qrcode"]
                        rec.state = "qr_ready"
                except Exception:
                    _logger.info(f"QR fetch timed out for {instance_name}, waiting for webhook delivery")

            # Auto-associate the connecting user to this channel
            if self.env.user not in rec.allowed_user_ids:
                rec.allowed_user_ids = [(4, self.env.user.id)]

            return True

        except UserError:
            raise
        except Exception as e:
            _logger.error(f"Connection error: {e}")
            rec.state = "error"
            raise UserError(_("Connection failed: %s") % str(e))

    def action_disconnect(self):
        """Disconnect and delete instance"""
        self.ensure_one()
        rec = self.sudo()
        try:
            api = self.env["bader.inbox.evolution_api"]
            api.delete_instance(rec.evolution_instance_name)
            rec.state = "disconnected"
            rec.qrcode_base64 = False
        except Exception as e:
            _logger.warning(f"Disconnect error: {e}")
            rec.state = "disconnected"

    def action_refresh_qr(self):
        """Refresh QR code"""
        self.ensure_one()
        rec = self.sudo()
        if rec.evolution_instance_name:
            api = self.env["bader.inbox.evolution_api"]
            qr_result = api.get_qrcode(rec.evolution_instance_name)
            if qr_result.get("qrcode"):
                rec.qrcode_base64 = qr_result["qrcode"]
                rec.state = "qr_ready"

    def action_check_status(self):
        """Check connection status"""
        self.ensure_one()
        rec = self.sudo()
        if rec.evolution_instance_name:
            api = self.env["bader.inbox.evolution_api"]
            status = api.get_instance_status(rec.evolution_instance_name)
            if status.get("connected"):
                rec.state = "connected"
                if status.get("phone"):
                    rec.phone = status["phone"]
                if status.get("name"):
                    rec.phone_name = status["name"]
            else:
                rec.state = "disconnected"

    # ── Health Check & Auto-Reconnect ──────────────────────────────

    @api.model
    def cron_health_check(self):
        """Cron job: verify Evolution API instances and auto-reconnect.
        
        Called every 5 minutes. For each channel that should be connected,
        checks if the Evolution API instance still exists (it may have been
        lost after an API server restart). If lost, recreates the instance
        and reconfigures the webhook automatically.
        """
        # Channels that SHOULD be connected
        channels = self.search([
            ("state", "in", ["connected", "connecting", "qr_ready"]),
            ("evolution_instance_name", "!=", False),
        ])
        
        if not channels:
            return
        
        api = self.env["bader.inbox.evolution_api"]
        now = fields.Datetime.now()
        
        for channel in channels:
            try:
                instance_name = channel.evolution_instance_name
                webhook_url = channel.webhook_url
                
                if not instance_name or not webhook_url:
                    continue
                
                # Use ensure_instance to check and recreate if needed
                result = api.ensure_instance(instance_name, webhook_url)
                
                if result.get("error"):
                    _logger.warning(
                        f"Health check failed for channel {channel.name} "
                        f"(#{channel.id}): {result['error']}"
                    )
                    self._health_check_update_raw(channel.id, now, "error", channel.reconnect_attempts + 1)
                elif result.get("created"):
                    _logger.info(
                        f"Auto-recreated instance for channel {channel.name} "
                        f"(#{channel.id}). User must scan QR again."
                    )
                    self._health_check_update_raw(
                        channel.id, now, "warning",
                        channel.reconnect_attempts + 1,
                        state="connecting", clear_qr=True,
                    )
                else:
                    self._health_check_update_raw(channel.id, now, "ok", 0)
                    
            except Exception as e:
                _logger.error(
                    f"Health check error for channel {channel.name}: {e}",
                    exc_info=True
                )
                try:
                    self._health_check_update_raw(channel.id, now, "error")
                except Exception:
                    pass

    def _health_check_update_raw(self, channel_id, now, health_status, reconnect_attempts=None, state=None, clear_qr=False):
        """Update health check fields using raw SQL to avoid ORM serialization conflicts.
        
        Webhooks concurrently update the same channel table via ORM, causing
        serialization failures when the health check cron also uses ORM writes.
        Raw SQL targets only the specific row and avoids ORM flush() side effects.
        """
        try:
            sets = [
                "last_health_check = %s",
                "health_status = %s",
                "write_date = %s",
            ]
            params = [now, health_status, now]
            
            if reconnect_attempts is not None:
                sets.append("reconnect_attempts = %s")
                params.append(reconnect_attempts)
            if state:
                sets.append("state = %s")
                params.append(state)
            if clear_qr:
                sets.append("qrcode_base64 = NULL")
            
            params.append(channel_id)
            query = f"UPDATE bader_inbox_channel SET {', '.join(sets)} WHERE id = %s"
            self.env.cr.execute(query, params)
            self.env.cr.commit()
        except Exception as e:
            self.env.cr.rollback()
            _logger.debug(f"Health check raw update failed for channel {channel_id}: {e}")


    def action_reconnect(self):
        """Manual reconnect: recreate instance and reconfigure webhook."""
        self.ensure_one()
        rec = self.sudo()

        if not rec.evolution_instance_name:
            # No instance name yet — use normal connect flow
            return self.action_connect()

        api = self.env["bader.inbox.evolution_api"]

        # Try to delete old instance (ignore errors)
        try:
            api.delete_instance(rec.evolution_instance_name)
        except Exception:
            pass

        # Recreate
        try:
            result = api.create_instance(
                rec.evolution_instance_name,
                webhook_url=rec.webhook_url
            )
            timed_out = "timed out" in str(result.get("error", "")).lower()

            if result.get("success") is False and not timed_out:
                raise UserError(
                    _("Failed to recreate instance: %s") % result.get("error")
                )

            rec.state = "connecting"

            if not timed_out:
                # Set webhook
                try:
                    api.set_webhook(rec.evolution_instance_name, rec.webhook_url)
                except Exception:
                    pass

                # Get QR code (non-blocking)
                try:
                    qr_result = api.get_qrcode(rec.evolution_instance_name)
                    if qr_result.get("qrcode"):
                        rec.qrcode_base64 = qr_result["qrcode"]
                        rec.state = "qr_ready"
                except Exception:
                    _logger.info(f"QR fetch timed out for {rec.evolution_instance_name}, waiting for webhook")

            rec.reconnect_attempts = 0
            rec.health_status = "ok"

        except UserError:
            raise
        except Exception as e:
            _logger.error(f"Manual reconnect error: {e}")
            rec.state = "error"
            raise UserError(_("Reconnect failed: %s") % str(e))

    # ── Pending Message Sync ────────────────────────────────────────

    @api.model
    def _cron_sync_pending_messages(self):
        """Cron job: fetch and process messages that arrived while Odoo was offline.
        
        Calls the Evolution API /messages/pending/:instance endpoint,
        processes each message (reusing webhook parsing logic),
        then acknowledges receipt so they are not fetched again.
        
        Deduplication is handled by whatsapp_message_id check in _create_message.
        """
        channels = self.search([
            ("state", "=", "connected"),
            ("evolution_instance_name", "!=", False),
        ])
        
        if not channels:
            return

        api = self.env["bader.inbox.evolution_api"]
        Message = self.env["bader.inbox.message"].sudo()
        Conversation = self.env["bader.inbox.conversation"].sudo()

        for channel in channels:
            try:
                instance_name = channel.evolution_instance_name
                pending = api.fetch_pending_messages(instance_name)
                
                if not pending:
                    continue
                
                _logger.info(
                    f"Sync: {len(pending)} pending messages for "
                    f"channel {channel.name} ({instance_name})"
                )

                ack_ids = []

                for msg_data in pending:
                    try:
                        queue_id = msg_data.get("id")
                        
                        # Extract the actual message payload
                        # Replit stores the webhook data, so structure matches
                        data = msg_data.get("data") or msg_data
                        msg_obj = data.get("data") or data.get("message") or data
                        
                        if isinstance(msg_obj, list):
                            msg_obj = msg_obj[0] if msg_obj else {}
                        if not isinstance(msg_obj, dict):
                            if queue_id:
                                ack_ids.append(queue_id)
                            continue

                        key = msg_obj.get("key", {})
                        push_name = msg_obj.get("pushName", "")
                        
                        # Extract message content
                        message_content = None
                        for content_key in ("message", "content", "messageContent"):
                            candidate = msg_obj.get(content_key)
                            if candidate and isinstance(candidate, dict) and len(candidate) > 0:
                                message_content = candidate
                                break
                            elif candidate and isinstance(candidate, str) and candidate.strip():
                                message_content = candidate
                                break
                        
                        if not message_content:
                            msg_type_field = msg_obj.get("messageType", "")
                            if msg_type_field == "conversation":
                                raw_text = msg_obj.get("body") or msg_obj.get("text") or ""
                                message_content = raw_text if raw_text else {"conversation": ""}
                            elif msg_type_field == "extendedTextMessage":
                                raw_text = msg_obj.get("body") or msg_obj.get("text") or ""
                                message_content = {"extendedTextMessage": {"text": raw_text}}
                            else:
                                message_content = {}
                        
                        # Extract phone info
                        phone, whatsapp_id, _ = self._extract_phone_from_key(key)
                        if not phone:
                            if queue_id:
                                ack_ids.append(queue_id)
                            continue
                        
                        # Get or create conversation
                        conversation = Conversation.get_or_create(
                            channel_id=channel.id,
                            phone=phone,
                            whatsapp_id=whatsapp_id,
                            contact_name=push_name,
                        )
                        
                        # Parse content using webhook controller's logic
                        from odoo.addons.bader_inbox.controllers.webhook import BaderInboxWebhook
                        controller = BaderInboxWebhook()
                        msg_type, content, media_info = controller._parse_message_content(message_content)
                        
                        from_me = key.get("fromMe", False)
                        direction = "out" if from_me else "in"
                        msg_id = key.get("id", "")
                        
                        # Dedup check
                        if msg_id and Message.search([("whatsapp_message_id", "=", msg_id)], limit=1):
                            _logger.info(f"Sync: duplicate {msg_id}, skipping")
                            if queue_id:
                                ack_ids.append(queue_id)
                            continue

                        # Strip non-model fields
                        safe_media = {}
                        if media_info:
                            safe_media = {k: v for k, v in media_info.items() if k not in ("is_animated",)}
                        
                        # Create message
                        vals = {
                            "conversation_id": conversation.id,
                            "direction": direction,
                            "message_type": msg_type,
                            "content": content,
                            "whatsapp_message_id": msg_id,
                            "status": "read" if direction == "in" else "sent",
                        }
                        vals.update(safe_media)
                        
                        # Store original key/content for on-demand media download
                        import json
                        try:
                            vals["whatsapp_key_json"] = json.dumps(key)
                            if isinstance(message_content, dict):
                                vals["whatsapp_content_json"] = json.dumps(message_content)
                        except Exception:
                            pass
                        
                        Message.create(vals)
                        _logger.info(f"Sync: created msg {msg_id} ({msg_type})")
                        
                        if queue_id:
                            ack_ids.append(queue_id)

                    except Exception as e:
                        _logger.error(f"Sync: error processing pending msg: {e}")
                        # Still ack to prevent infinite retry
                        queue_id = msg_data.get("id")
                        if queue_id:
                            ack_ids.append(queue_id)

                # Acknowledge all processed messages
                if ack_ids:
                    api.acknowledge_messages(ack_ids)
                    _logger.info(f"Sync: acknowledged {len(ack_ids)} messages for {instance_name}")

            except Exception as e:
                _logger.error(
                    f"Sync: error for channel {channel.name}: {e}",
                    exc_info=True,
                )

    @staticmethod
    def _extract_phone_from_key(key):
        """Extract phone from message key (same logic as webhook controller)."""
        remote_jid = key.get("remoteJid", "")
        sender_pn = key.get("senderPn", "")
        participant = key.get("participant", "")
        
        phone_source = ""
        if sender_pn and "@s.whatsapp.net" in sender_pn:
            phone_source = sender_pn
        elif participant and "@s.whatsapp.net" in participant:
            phone_source = participant
        elif remote_jid and "@s.whatsapp.net" in remote_jid:
            phone_source = remote_jid
        else:
            skip = ("@g.us", "@newsletter", "@broadcast", "@status", "@lid")
            if remote_jid and any(remote_jid.endswith(s) for s in skip):
                return None, None, None
            return None, None, None
        
        phone = phone_source.split("@")[0] if "@" in phone_source else phone_source
        return phone, phone_source, phone_source

