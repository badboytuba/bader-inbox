# Copyright 2026 Bader Business
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import logging
from datetime import timedelta
from odoo import http, fields
from odoo.http import request

_logger = logging.getLogger(__name__)


class BaderInboxProfilePictureController(http.Controller):

    @http.route(
        "/bader-inbox/profile-picture/<int:conversation_id>",
        type="json", auth="user", methods=["POST"],
    )
    def get_profile_picture(self, conversation_id, force=False):
        """Fetch WhatsApp profile picture for a conversation.

        Caches the URL in the conversation record for 24h to avoid
        excessive API calls. Pass force=True to refresh.
        """
        conv = request.env["bader.inbox.conversation"].browse(conversation_id)
        if not conv.exists():
            return {"error": "Conversation not found"}

        # Return cached URL if fresh (< 24h old)
        if (
            not force
            and conv.profile_pic_url
            and conv.profile_pic_date
            and conv.profile_pic_date > fields.Datetime.now() - timedelta(hours=24)
        ):
            return {"url": conv.profile_pic_url}

        # Need channel with evolution instance
        if not conv.channel_id or not conv.channel_id.evolution_instance_name:
            return {"url": None}

        api = request.env["bader.inbox.evolution_api"]
        url = api.get_profile_picture(
            conv.channel_id.evolution_instance_name,
            conv.phone,
        )

        # Cache result
        conv.sudo().write({
            "profile_pic_url": url or False,
            "profile_pic_date": fields.Datetime.now(),
        })

        return {"url": url}

    @http.route(
        "/bader-inbox/profile-pictures",
        type="json", auth="user", methods=["POST"],
    )
    def get_profile_pictures_batch(self, conversation_ids):
        """Fetch profile pictures for multiple conversations at once.

        Returns {conversation_id: url_or_null, ...}
        Only fetches from API for conversations without a cached URL.
        """
        result = {}
        convs = request.env["bader.inbox.conversation"].browse(conversation_ids)
        now = fields.Datetime.now()
        cutoff = now - timedelta(hours=24)

        to_fetch = []
        for conv in convs.filtered(lambda c: c.exists()):
            # Use cache if fresh
            if conv.profile_pic_url and conv.profile_pic_date and conv.profile_pic_date > cutoff:
                result[conv.id] = conv.profile_pic_url
            elif conv.profile_pic_date and conv.profile_pic_date > cutoff:
                # We already checked and there's no picture
                result[conv.id] = None
            else:
                to_fetch.append(conv)

        # Fetch missing ones from API (limit to avoid timeout)
        api = request.env["bader.inbox.evolution_api"]
        for conv in to_fetch[:20]:
            if not conv.channel_id or not conv.channel_id.evolution_instance_name:
                result[conv.id] = None
                continue

            url = api.get_profile_picture(
                conv.channel_id.evolution_instance_name,
                conv.phone,
            )
            result[conv.id] = url

            conv.sudo().write({
                "profile_pic_url": url or False,
                "profile_pic_date": now,
            })

        return result
