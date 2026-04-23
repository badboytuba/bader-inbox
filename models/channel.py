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
        ("reconnecting", "Reconnecting"),
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
        """Refresh QR code. If instance is dead (dropped from memory after
        exhausting QR retries), the /qrcode endpoint auto-revives it.
        If still no QR after revive, try a full reconnect."""
        self.ensure_one()
        rec = self.sudo()
        if not rec.evolution_instance_name:
            return self.action_connect()

        api = self.env["bader.inbox.evolution_api"]
        qr_result = api.get_qrcode(rec.evolution_instance_name)

        if qr_result.get("qrcode"):
            rec.qrcode_base64 = qr_result["qrcode"]
            rec.state = "qr_ready"
            return True

        # QR not available — the API may have started reviving the instance.
        # Wait briefly and retry once.
        import time
        time.sleep(4)
        qr_result = api.get_qrcode(rec.evolution_instance_name)
        if qr_result.get("qrcode"):
            rec.qrcode_base64 = qr_result["qrcode"]
            rec.state = "qr_ready"
            return True

        # Still nothing — force full reconnect
        _logger.info(
            f"QR refresh failed for {rec.evolution_instance_name}, "
            f"attempting full reconnect"
        )
        return self.action_reconnect()

    def action_check_status(self):
        """Check connection status. If the API reports the instance needs
        reviving (in DB but not in memory), trigger a reconnect."""
        self.ensure_one()
        rec = self.sudo()
        if not rec.evolution_instance_name:
            return

        api = self.env["bader.inbox.evolution_api"]
        status = api.get_instance_status(rec.evolution_instance_name)

        if status.get("connected"):
            rec.state = "connected"
            if status.get("phone"):
                rec.phone = status["phone"]
            if status.get("name"):
                rec.phone_name = status["name"]
        elif status.get("status") == "disconnected" and rec.state not in ("qr_ready", "connecting"):
            rec.state = "disconnected"

    # ── Health Alert Cron ──────────────────────────────────────────

    _HEALTH_ALERT_MARKER = "[BIHEALTH]"
    _HEALTH_ALERT_USER_LOGIN = "ralf@bader.es"
    _HEALTH_ZOMBIE_THRESHOLD_MIN = 60

    @api.model
    def cron_health_alert(self):
        """Detect channel problems and raise mail.activity on the main
        admin user. Runs every 10 min. Non-destructive: re-evaluates
        state each tick and replaces previous [BIHEALTH] activities.

        Rules:
          * channel.state != 'connected' → warning
          * channel.state == 'connected' BUT last_message_date older than
            _HEALTH_ZOMBIE_THRESHOLD_MIN minutes AND channel has history
            of traffic (>5 msgs past 24h) → zombie suspect warning
        """
        User = self.env['res.users'].sudo()
        admin = User.search([('login', '=', self._HEALTH_ALERT_USER_LOGIN)], limit=1)
        if not admin:
            _logger.info("cron_health_alert: admin %s not found; skipping",
                         self._HEALTH_ALERT_USER_LOGIN)
            return

        # Clear previous BIHEALTH activities on this admin before reposting
        # only what is still relevant — keeps the dashboard free of stale
        # warnings once a problem is fixed.
        Activity = self.env['mail.activity'].sudo()
        old = Activity.search([
            ('user_id', '=', admin.id),
            ('summary', 'ilike', self._HEALTH_ALERT_MARKER),
        ])
        old.unlink()

        warning_type = self.env.ref('mail.mail_activity_data_warning',
                                     raise_if_not_found=False)
        activity_type_id = warning_type.id if warning_type else False
        model_users_id = self.env.ref('base.model_res_users').id

        now = fields.Datetime.now()
        channels = self.search([('evolution_instance_name', '!=', False)])
        alerts = []
        for ch in channels:
            if ch.state != 'connected':
                alerts.append((
                    ch,
                    f"{self._HEALTH_ALERT_MARKER} Canal {ch.name} en estado '{ch.state}'",
                    f"<p>El canal <b>{ch.name}</b> (id {ch.id}) está en estado "
                    f"<b>{ch.state}</b> desde el último health check "
                    f"({ch.last_health_check}).</p>"
                    f"<p>Revisa el dashboard de Evolution API: "
                    f"https://whatsapp.bader.es</p>",
                ))
                continue
            # Connected but zombie? Look up the latest message by joining
            # through conversation (channel itself has no last_message_date).
            Message = self.env['bader.inbox.message'].sudo()
            last_msg = Message.search(
                [('conversation_id.channel_id', '=', ch.id)],
                order='create_date desc',
                limit=1,
            )
            last_date = last_msg.create_date if last_msg else None
            threshold = now - timedelta(minutes=self._HEALTH_ZOMBIE_THRESHOLD_MIN)
            if last_date and last_date > threshold:
                continue  # recent traffic, not zombie
            # Only flag if channel has normal volume (>5 msgs past 24h)
            day_ago = now - timedelta(hours=24)
            msg_count = Message.search_count([
                ('conversation_id.channel_id', '=', ch.id),
                ('create_date', '>', day_ago),
            ])
            if msg_count < 5:
                continue  # low-traffic channel, ignore silence
            last_ts = last_date or ch.last_health_check or 'nunca'
            alerts.append((
                ch,
                f"{self._HEALTH_ALERT_MARKER} Canal {ch.name} posible zombie",
                f"<p>El canal <b>{ch.name}</b> (id {ch.id}) está marcado como "
                f"<b>connected</b> pero no ha procesado mensajes en más de "
                f"{self._HEALTH_ZOMBIE_THRESHOLD_MIN} min "
                f"(último: {last_ts}, {msg_count} mensajes en 24h).</p>"
                f"<p>Posible socket zombie. Considera reiniciar el servicio "
                f"Evolution.</p>",
            ))

        for ch, summary, note in alerts:
            Activity.create({
                'res_model': 'res.users',
                'res_model_id': model_users_id,
                'res_id': admin.id,
                'activity_type_id': activity_type_id,
                'summary': summary,
                'note': note,
                'user_id': admin.id,
                'date_deadline': fields.Date.today(),
            })
            _logger.info("cron_health_alert: raised activity '%s'", summary)

        if not alerts:
            _logger.info("cron_health_alert: all 6 channels healthy")

    # ── Health Check & Auto-Reconnect ──────────────────────────────

    @api.model
    def cron_sync_channel_states(self):
        """Fast pull (1 min) from Evolution /api/health to keep channel.state
        in sync with the live Baileys status — bounded fallback for when the
        connection.update webhook is missed (Evolution unreachable, rapid
        reconnect bursts, etc.). Emits bus.bus notifications so the OWL
        frontend updates dots/badges in real time.
        """
        api = self.env["bader.inbox.evolution_api"]
        try:
            live_status = api.fetch_health_summary()
        except Exception as e:
            _logger.warning("cron_sync_channel_states: fetch failed: %s", e)
            return
        if not live_status:
            return
        state_map = {
            "connected": "connected",
            "reconnecting": "reconnecting",
            "connecting": "connecting",
            "qr_ready": "qr_ready",
            "disconnected": "disconnected",
        }
        channels = self.search([("evolution_instance_name", "!=", False)])
        for ch in channels:
            live = live_status.get(ch.evolution_instance_name)
            if not live:
                continue
            new_state = state_map.get(live)
            if not new_state or new_state == ch.state:
                continue
            old_state = ch.state
            ch.sudo().write({"state": new_state})
            _logger.info(
                "Channel %s (#%s) state synced %s -> %s (live: %s)",
                ch.name, ch.id, old_state, new_state, live,
            )
            try:
                self.env["bus.bus"]._sendone(
                    "bader_inbox", "bader_inbox_channel_update",
                    {"channel_id": ch.id, "state": new_state},
                )
            except Exception:
                pass

            # Immediate backfill of any pending messages sitting on the
            # Evolution queue — covers webhook deliveries that failed while
            # the channel was flapping. Runs only on the *transition* to
            # connected, not on every tick, to avoid redundant work.
            if new_state == "connected" and old_state != "connected":
                try:
                    self._cron_sync_pending_messages(channel_ids=[ch.id])
                except Exception as e:
                    _logger.warning(
                        "Immediate pending-sync failed for channel %s: %s",
                        ch.name, e,
                    )

    @api.model
    def cron_health_check(self):
        """Cron job: verify Evolution API instances and auto-reconnect.

        Called every 5 minutes. For each channel that should be connected,
        checks if the Evolution API instance still exists (it may have been
        lost after an API server restart). If lost, recreates the instance
        and reconfigures the webhook automatically.

        Also auto-recovers disconnected channels (max 3 attempts before giving up).
        """
        api = self.env["bader.inbox.evolution_api"]
        now = fields.Datetime.now()

        # 1) Channels that SHOULD be connected
        channels = self.search([
            ("state", "in", ["connected", "connecting", "qr_ready"]),
            ("evolution_instance_name", "!=", False),
        ])

        # Pre-fetch channel data to avoid ORM reads during iteration
        # (reduces serialization conflicts with concurrent webhook writes)
        channel_data = [
            (c.id, c.name, c.evolution_instance_name, c.webhook_url, c.reconnect_attempts)
            for c in channels
        ]

        for ch_id, ch_name, instance_name, webhook_url, reconnect_attempts in channel_data:
            try:
                if not instance_name or not webhook_url:
                    continue

                # Use ensure_instance to check and recreate if needed
                result = api.ensure_instance(instance_name, webhook_url)

                if result.get("error"):
                    _logger.warning(
                        f"Health check failed for channel {ch_name} "
                        f"(#{ch_id}): {result['error']}"
                    )
                    self._health_check_update_raw(ch_id, now, "error", reconnect_attempts + 1)
                elif result.get("created"):
                    _logger.info(
                        f"Auto-recreated instance for channel {ch_name} "
                        f"(#{ch_id}). User must scan QR again."
                    )
                    self._health_check_update_raw(
                        ch_id, now, "warning",
                        reconnect_attempts + 1,
                        state="connecting", clear_qr=True,
                    )
                else:
                    self._health_check_update_raw(ch_id, now, "ok", 0)

            except Exception as e:
                _logger.error(
                    f"Health check error for channel {ch_name}: {e}",
                    exc_info=True
                )
                try:
                    self._health_check_update_raw(ch_id, now, "error")
                except Exception:
                    pass

        # 2) Auto-recover disconnected channels (max 3 attempts)
        disconnected = self.search([
            ("state", "=", "disconnected"),
            ("evolution_instance_name", "!=", False),
            ("webhook_url", "!=", False),
            ("reconnect_attempts", "<", 3),
        ])
        disc_data = [
            (c.id, c.name, c.evolution_instance_name, c.webhook_url, c.reconnect_attempts)
            for c in disconnected
        ]
        for ch_id, ch_name, instance_name, webhook_url, reconnect_attempts in disc_data:
            try:
                _logger.info(
                    f"Auto-recovery: attempting reconnect for channel "
                    f"{ch_name} (#{ch_id}), attempt {reconnect_attempts + 1}"
                )
                result = api.ensure_instance(instance_name, webhook_url)
                if result.get("error"):
                    self._health_check_update_raw(
                        ch_id, now, "error",
                        reconnect_attempts + 1,
                    )
                else:
                    self._health_check_update_raw(
                        ch_id, now, "warning",
                        reconnect_attempts + 1,
                        state="connecting",
                    )
            except Exception as e:
                _logger.error(f"Auto-recovery error for channel {ch_name}: {e}")
                try:
                    self._health_check_update_raw(
                        ch_id, now, "error",
                        reconnect_attempts + 1,
                    )
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
    def _cron_sync_pending_messages(self, channel_ids=None):
        """Fetch and process messages that arrived while Odoo was offline or
        while the channel was disconnected.

        Scheduled mode (channel_ids=None): runs every 2 minutes against every
        connected channel.

        Targeted mode (channel_ids=[...]): called inline after a state
        transition to 'connected' so the backfill happens within seconds of
        reconnection instead of waiting up to the next scheduled tick.

        Calls GET /api/messages/pending/:instance, creates any message the
        Odoo DB doesn't yet have (dedup by whatsapp_message_id), and ACKs
        the queue so the Evolution server drops them.
        """
        if channel_ids:
            channels = self.browse(channel_ids).filtered(
                lambda c: c.state == "connected" and c.evolution_instance_name
            )
        else:
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

