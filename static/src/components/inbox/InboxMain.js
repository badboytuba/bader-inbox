/** @odoo-module **/

import { Component, useState, onWillStart, useRef } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

export class BaderInboxMain extends Component {
    static template = "bader_inbox.InboxMain";
    static props = {};

    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.bus = useService("bus_service");
        this.user = useService("user");

        this.state = useState({
            conversations: [],
            selectedConversation: null,
            loadingConversations: true,
            messages: [],
            loadingMessages: false,
            channels: [],
            filter: "all",
            searchQuery: "",
            showContactPanel: true,
            composerText: "",
            sendingMessage: false,
            templates: [],
            showTemplates: false,
        });

        this.messagesRef = useRef("messagesContainer");

        onWillStart(async () => {
            await this.loadChannels();
            await this.loadConversations();
        });
    }

    async loadChannels() {
        try {
            this.state.channels = await this.orm.searchRead(
                "bader.inbox.channel", [["state", "=", "connected"]], ["id", "name", "phone"]
            );
        } catch (e) { console.error(e); }
    }

    async loadConversations() {
        this.state.loadingConversations = true;
        try {
            let domain = [];
            if (this.state.filter === "unread") domain.push(["unread_count", ">", 0]);
            else if (this.state.filter === "mine") domain.push(["assigned_user_id", "=", this.user.userId]);

            this.state.conversations = await this.orm.searchRead(
                "bader.inbox.conversation", domain,
                ["id", "display_name", "phone", "last_message", "last_message_date", "unread_count", "state", "assigned_user_id", "partner_id", "channel_id"],
                { order: "last_message_date desc", limit: 100 }
            );
        } catch (e) { console.error(e); }
        this.state.loadingConversations = false;
    }

    async loadMessages(conversationId) {
        if (!conversationId) return;
        this.state.loadingMessages = true;
        try {
            this.state.messages = await this.orm.searchRead(
                "bader.inbox.message", [["conversation_id", "=", conversationId]],
                ["id", "direction", "message_type", "content", "status", "create_date"],
                { order: "create_date asc" }
            );
            setTimeout(() => {
                const c = this.messagesRef.el;
                if (c) c.scrollTop = c.scrollHeight;
            }, 100);
        } catch (e) { console.error(e); }
        this.state.loadingMessages = false;
    }

    async selectConversation(conv) {
        this.state.selectedConversation = conv;
        await this.loadMessages(conv.id);
    }

    async sendMessage() {
        if (!this.state.composerText.trim() || !this.state.selectedConversation) return;
        this.state.sendingMessage = true;
        try {
            await this.orm.call("bader.inbox.message", "send_message", [this.state.selectedConversation.id, this.state.composerText, "text"]);
            this.state.composerText = "";
            await this.loadMessages(this.state.selectedConversation.id);
        } catch (e) { console.error(e); }
        this.state.sendingMessage = false;
    }

    setFilter(filter) { this.state.filter = filter; this.loadConversations(); }

    formatTime(dateStr) {
        if (!dateStr) return "";
        const d = new Date(dateStr);
        return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }

    getInitials(name) {
        if (!name) return "?";
        return name.split(" ").map(n => n[0]).join("").substring(0, 2).toUpperCase();
    }
}

registry.category("actions").add("bader_inbox_main", BaderInboxMain);
