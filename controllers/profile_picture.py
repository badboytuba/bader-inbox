# Copyright 2026 Bader Business
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import base64
import logging
from datetime import timedelta
from odoo import http, fields
from odoo.http import request
import requests as req_lib

_logger = logging.getLogger(__name__)

# Retry interval for contacts where API returned no picture
# (WhatsApp privacy — but user may change settings)
_NO_PIC_RETRY_HOURS = 72  # retry every 3 days
_CACHE_HOURS = 168  # re-fetch weekly for contacts with picture


class BaderInboxProfilePictureController(http.Controller):

    @http.route(
        "/bader-inbox/profile-picture/<int:conversation_id>",
        type="json", auth="user", methods=["POST"],
    )
    def get_profile_picture(self, conversation_id, force=False):
        """Fetch WhatsApp profile picture for a conversation.

        Downloads the image and stores it as an ir.attachment.
        Returns a local Odoo URL that never expires.
        """
        conv = request.env["bader.inbox.conversation"].search(
            [("id", "=", conversation_id)], limit=1
        )
        if not conv:
            return {"error": "Conversation not found"}

        # Return cached local URL if fresh
        if not force and conv.profile_pic_url and conv.profile_pic_date:
            cache_hours = _CACHE_HOURS if conv.profile_pic_url else _NO_PIC_RETRY_HOURS
            if conv.profile_pic_date > fields.Datetime.now() - timedelta(hours=cache_hours):
                return {"url": conv.profile_pic_url}

        url = self._fetch_and_store_profile_pic(conv)
        return {"url": url}

    @http.route(
        "/bader-inbox/profile-pictures",
        type="json", auth="user", methods=["POST"],
    )
    def get_profile_pictures_batch(self, conversation_ids):
        """Fetch profile pictures for multiple conversations at once."""
        result = {}
        convs = request.env["bader.inbox.conversation"].search(
            [("id", "in", conversation_ids)]
        )
        now = fields.Datetime.now()
        cache_cutoff = now - timedelta(hours=_CACHE_HOURS)
        no_pic_cutoff = now - timedelta(hours=_NO_PIC_RETRY_HOURS)

        to_fetch = []
        for conv in convs:
            if conv.profile_pic_url and conv.profile_pic_date and conv.profile_pic_date > cache_cutoff:
                # Has picture and cache is fresh
                result[conv.id] = conv.profile_pic_url
            elif not conv.profile_pic_url and conv.profile_pic_date and conv.profile_pic_date > no_pic_cutoff:
                # Already checked, no picture available (privacy) — don't retry too soon
                result[conv.id] = None
            else:
                to_fetch.append(conv)

        # Fetch missing ones from API (limit to avoid timeout)
        for conv in to_fetch[:20]:
            url = self._fetch_and_store_profile_pic(conv)
            result[conv.id] = url

        return result

    @http.route(
        "/bader-inbox/avatar/<int:conversation_id>",
        type="http", auth="user", methods=["GET"], csrf=False,
    )
    def serve_avatar(self, conversation_id, **kwargs):
        """Serve locally stored profile picture for a conversation."""
        conv = request.env["bader.inbox.conversation"].search(
            [("id", "=", conversation_id)], limit=1
        )
        if not conv:
            return request.not_found()

        # Find the stored attachment
        attachment = request.env["ir.attachment"].sudo().search([
            ("res_model", "=", "bader.inbox.conversation"),
            ("res_id", "=", conv.id),
            ("res_field", "=", "profile_pic_data"),
        ], limit=1)

        if not attachment:
            return request.not_found()

        # Read raw binary data using Odoo's standard property
        try:
            data = base64.b64decode(attachment.datas) if attachment.datas else None
        except Exception:
            data = None

        if not data or len(data) < 100:
            return request.not_found()

        headers = [
            ("Content-Type", attachment.mimetype or "image/jpeg"),
            ("Content-Length", str(len(data))),
            ("Cache-Control", "public, max-age=86400"),
        ]
        return request.make_response(data, headers)

    def _fetch_and_store_profile_pic(self, conv):
        """Fetch profile picture from Evolution API, download it, and store locally."""
        if not conv.channel_id or not conv.channel_id.evolution_instance_name:
            conv.sudo().write({
                "profile_pic_url": False,
                "profile_pic_date": fields.Datetime.now(),
            })
            return None

        api = request.env["bader.inbox.evolution_api"]
        wa_url = api.get_profile_picture(
            conv.channel_id.evolution_instance_name,
            conv.phone,
        )

        if not wa_url:
            # No picture available (privacy settings or no WhatsApp account)
            conv.sudo().write({
                "profile_pic_url": False,
                "profile_pic_date": fields.Datetime.now(),
            })
            return None

        # Download the actual image from pps.whatsapp.net
        try:
            resp = req_lib.get(wa_url, timeout=10, stream=True)
            if resp.status_code == 200 and resp.content:
                image_data = base64.b64encode(resp.content).decode()
                local_url = f"/bader-inbox/avatar/{conv.id}"

                # Store as ir.attachment
                existing = request.env["ir.attachment"].sudo().search([
                    ("res_model", "=", "bader.inbox.conversation"),
                    ("res_id", "=", conv.id),
                    ("res_field", "=", "profile_pic_data"),
                ], limit=1)

                att_vals = {
                    "name": f"avatar_{conv.id}.jpg",
                    "type": "binary",
                    "datas": image_data,
                    "res_model": "bader.inbox.conversation",
                    "res_id": conv.id,
                    "res_field": "profile_pic_data",
                    "mimetype": "image/jpeg",
                    "public": True,
                }
                if existing:
                    existing.sudo().write(att_vals)
                else:
                    request.env["ir.attachment"].sudo().create(att_vals)

                conv.sudo().write({
                    "profile_pic_url": local_url,
                    "profile_pic_date": fields.Datetime.now(),
                })
                return local_url
            else:
                _logger.debug(f"Profile pic download failed: HTTP {resp.status_code} for {conv.phone}")
        except Exception as e:
            _logger.debug(f"Profile pic download error for {conv.phone}: {e}")

        # Fallback: store the WA URL directly (may expire)
        conv.sudo().write({
            "profile_pic_url": wa_url,
            "profile_pic_date": fields.Datetime.now(),
        })
        return wa_url
