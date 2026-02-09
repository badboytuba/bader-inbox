/** @odoo-module **/

import { Component, useState, onWillStart, useRef, onMounted } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

export class BaderInboxMain extends Component {
    static template = "bader_inbox.InboxMain";
    static props = {};

    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.action = useService("action");
        this.user = useService("user");

        // Try to get bus service if available
        try {
            this.bus = useService("bus_service");
        } catch (e) {
            this.bus = null;
        }

        this.state = useState({
            // Conversations
            conversations: [],
            selectedConversation: null,
            loadingConversations: true,
            filter: "all",
            searchQuery: "",

            // Messages
            messages: [],
            loadingMessages: false,
            composerText: "",
            sendingMessage: false,

            // UI
            showContactPanel: true,
            showTemplates: false,
            templates: [],
            internalNote: "",

            // Channels
            channels: [],
        });

        this.messagesRef = useRef("messagesContainer");
        this.avatarColors = ["green", "blue", "purple", "orange", "pink", "red"];

        onWillStart(async () => {
            await this.loadChannels();
            await this.loadConversations();
            await this.loadTemplates();
        });

        onMounted(() => {
            this.setupBusSubscription();
        });
    }

    // ==========================================
    // DATA LOADING
    // ==========================================

    async loadChannels() {
        try {
            this.state.channels = await this.orm.searchRead(
                "bader.inbox.channel",
                [["state", "=", "connected"]],
                ["id", "name", "phone"]
            );
        } catch (e) {
            console.error("Error loading channels:", e);
        }
    }

    async loadConversations() {
        this.state.loadingConversations = true;
        try {
            let domain = [];
            if (this.state.filter === "unread") {
                domain.push(["unread_count", ">", 0]);
            } else if (this.state.filter === "mine") {
                domain.push(["assigned_user_id", "=", this.user.userId]);
            }

            this.state.conversations = await this.orm.searchRead(
                "bader.inbox.conversation",
                domain,
                [
                    "id", "computed_name", "phone", "last_message", "last_message_date",
                    "unread_count", "state", "assigned_user_id", "partner_id", "channel_id", "tag_ids"
                ],
                { order: "last_message_date desc", limit: 100 }
            );
        } catch (e) {
            console.error("Error loading conversations:", e);
        }
        this.state.loadingConversations = false;
    }

    async loadMessages(conversationId) {
        if (!conversationId) return;
        this.state.loadingMessages = true;
        try {
            this.state.messages = await this.orm.searchRead(
                "bader.inbox.message",
                [["conversation_id", "=", conversationId]],
                ["id", "direction", "message_type", "content", "status", "create_date", "media_url"],
                { order: "create_date asc" }
            );

            // Mark as read
            await this.orm.call("bader.inbox.conversation", "mark_as_read", [conversationId]);

            // Scroll to bottom
            setTimeout(() => {
                const container = this.messagesRef.el;
                if (container) container.scrollTop = container.scrollHeight;
            }, 100);
        } catch (e) {
            console.error("Error loading messages:", e);
        }
        this.state.loadingMessages = false;
    }

    async loadTemplates() {
        try {
            this.state.templates = await this.orm.searchRead(
                "bader.inbox.template",
                [],
                ["id", "name", "shortcut", "content", "category"]
            );
        } catch (e) {
            console.error("Error loading templates:", e);
        }
    }

    // ==========================================
    // REAL-TIME UPDATES
    // ==========================================

    setupBusSubscription() {
        if (this.bus) {
            this.bus.addChannel("bader_inbox");
            this.bus.addEventListener("notification", this.onBusNotification.bind(this));
        }
    }

    onBusNotification(event) {
        const { type, payload } = event.detail || {};
        if (type === "bader_inbox_new_message") {
            this.onNewMessage(payload);
        }
    }

    onNewMessage(payload) {
        // Refresh conversations
        this.loadConversations();

        // If current conversation, refresh messages
        if (this.state.selectedConversation?.id === payload.conversation_id) {
            this.loadMessages(payload.conversation_id);
        }

        // Show notification
        this.notification.add(
            `📱 ${payload.contact_name}: ${payload.message?.content?.substring(0, 50) || "Nova mensagem"}`,
            { type: "info", sticky: false }
        );
    }

    // ==========================================
    // CONVERSATION ACTIONS
    // ==========================================

    async selectConversation(conv) {
        this.state.selectedConversation = conv;
        await this.loadMessages(conv.id);

        // Update unread count locally
        const idx = this.state.conversations.findIndex(c => c.id === conv.id);
        if (idx >= 0) {
            this.state.conversations[idx].unread_count = 0;
        }
    }

    setFilter(filter) {
        this.state.filter = filter;
        this.loadConversations();
    }

    get filteredConversations() {
        if (!this.state.searchQuery) return this.state.conversations;
        const query = this.state.searchQuery.toLowerCase();
        return this.state.conversations.filter(c =>
            (c.computed_name || "").toLowerCase().includes(query) ||
            (c.phone || "").includes(query) ||
            (c.last_message || "").toLowerCase().includes(query)
        );
    }

    onSearch(event) {
        this.state.searchQuery = event.target.value;
    }

    async assignConversation() {
        if (!this.state.selectedConversation) return;
        // Open user selection dialog
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "bader.inbox.conversation",
            res_id: this.state.selectedConversation.id,
            views: [[false, "form"]],
            target: "new",
        });
    }

    async resolveConversation() {
        if (!this.state.selectedConversation) return;
        try {
            await this.orm.write("bader.inbox.conversation", [this.state.selectedConversation.id], {
                state: "resolved"
            });
            this.notification.add(_t("Conversa resolvida!"), { type: "success" });
            this.loadConversations();
        } catch (e) {
            console.error(e);
        }
    }

    addTag() {
        // TODO: Implement tag dialog
        this.notification.add(_t("Em breve: Adicionar tags"), { type: "info" });
    }

    // ==========================================
    // MESSAGE ACTIONS
    // ==========================================

    async sendMessage() {
        if (!this.state.composerText.trim() || !this.state.selectedConversation) return;

        this.state.sendingMessage = true;
        try {
            await this.orm.call(
                "bader.inbox.message",
                "send_message",
                [this.state.selectedConversation.id, this.state.composerText, "text"]
            );
            this.state.composerText = "";
            await this.loadMessages(this.state.selectedConversation.id);
        } catch (e) {
            console.error(e);
            this.notification.add(_t("Erro ao enviar mensagem"), { type: "danger" });
        }
        this.state.sendingMessage = false;
    }

    onComposerKeydown(event) {
        if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            this.sendMessage();
        }
    }

    showTemplatesPopup() {
        this.state.showTemplates = !this.state.showTemplates;
    }

    // ==========================================
    // CONTACT PANEL ACTIONS
    // ==========================================

    toggleContactPanel() {
        this.state.showContactPanel = !this.state.showContactPanel;
    }

    async createOpportunity() {
        if (!this.state.selectedConversation) return;
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "crm.lead",
            views: [[false, "form"]],
            target: "new",
            context: {
                default_name: `WhatsApp - ${this.state.selectedConversation.computed_name || this.state.selectedConversation.phone}`,
                default_phone: this.state.selectedConversation.phone,
                default_partner_id: this.state.selectedConversation.partner_id?.[0],
            }
        });
    }

    async viewPartner() {
        if (!this.state.selectedConversation) return;
        const partnerId = this.state.selectedConversation.partner_id?.[0];

        if (partnerId) {
            this.action.doAction({
                type: "ir.actions.act_window",
                res_model: "res.partner",
                res_id: partnerId,
                views: [[false, "form"]],
                target: "new",
            });
        } else {
            // Create new partner
            this.action.doAction({
                type: "ir.actions.act_window",
                res_model: "res.partner",
                views: [[false, "form"]],
                target: "new",
                context: {
                    default_name: this.state.selectedConversation.computed_name,
                    default_phone: this.state.selectedConversation.phone,
                }
            });
        }
    }

    async scheduleActivity() {
        if (!this.state.selectedConversation) return;
        this.notification.add(_t("Em breve: Agendar atividade"), { type: "info" });
    }

    async viewHistory() {
        if (!this.state.selectedConversation) return;
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "bader.inbox.message",
            views: [[false, "list"], [false, "form"]],
            domain: [["conversation_id", "=", this.state.selectedConversation.id]],
            name: _t("Histórico de Mensagens"),
        });
    }

    // ==========================================
    // NAVIGATION
    // ==========================================

    openChannels() {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "bader.inbox.channel",
            views: [[false, "list"], [false, "form"]],
            name: _t("Canais WhatsApp"),
        });
    }

    openTemplates() {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "bader.inbox.template",
            views: [[false, "list"], [false, "form"]],
            name: _t("Templates de Resposta"),
        });
    }

    openSettings() {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "ir.config_parameter",
            views: [[false, "list"], [false, "form"]],
            domain: [["key", "like", "bader_inbox%"]],
            name: _t("Configurações Bader Inbox"),
        });
    }

    // ==========================================
    // UTILITIES
    // ==========================================

    formatTime(dateStr) {
        if (!dateStr) return "";
        const date = new Date(dateStr);
        const now = new Date();
        const diffDays = Math.floor((now - date) / (1000 * 60 * 60 * 24));

        if (diffDays === 0) {
            return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
        } else if (diffDays === 1) {
            return "Ontem";
        } else if (diffDays < 7) {
            return date.toLocaleDateString([], { weekday: "short" });
        } else {
            return date.toLocaleDateString([], { day: "2-digit", month: "2-digit" });
        }
    }

    getInitials(name) {
        if (!name) return "?";
        return name.split(" ").map(n => n[0]).join("").substring(0, 2).toUpperCase();
    }

    getAvatarColor(id) {
        return this.avatarColors[(id || 0) % this.avatarColors.length];
    }

    getUserInitials() {
        return this.getInitials(this.user.name || "U");
    }
}

registry.category("actions").add("bader_inbox_main", BaderInboxMain);
