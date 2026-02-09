/** @odoo-module **/

import { Component, useState, onWillStart, useRef, onMounted, onWillUnmount } from "@odoo/owl";
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

            // Channels
            channels: [],

            // Channel Creation Modal
            showChannelModal: false,
            channelStep: 1,
            newChannelName: "",
            creatingChannel: false,
            qrCodeData: null,
            currentChannelId: null,
            connectedPhone: "",
        });

        this.messagesRef = useRef("messagesContainer");
        this.avatarColors = ["green", "blue", "purple", "orange", "pink", "red"];
        this.qrPollInterval = null;

        onWillStart(async () => {
            await this.loadChannels();
            await this.loadConversations();

            // Check if we need to open a specific conversation (from PhoneWhatsAppWidget)
            const params = this.props.action?.params || {};
            if (params.conversation_id) {
                await this.openConversationById(params.conversation_id);
            } else if (params.phone) {
                await this.openConversationByPhone(params.phone);
            }
        });

        onWillUnmount(() => {
            this.stopQRPolling();
        });
    }

    // ==========================================
    // DATA LOADING
    // ==========================================

    async loadChannels() {
        try {
            this.state.channels = await this.orm.searchRead(
                "bader.inbox.channel",
                [],
                ["id", "name", "phone", "state", "evolution_instance_name"]
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
                    "unread_count", "state", "assigned_user_id", "partner_id", "channel_id"
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
                ["id", "direction", "message_type", "content", "status", "create_date"],
                { order: "create_date asc" }
            );

            await this.orm.call("bader.inbox.conversation", "mark_as_read", [conversationId]);

            setTimeout(() => {
                const container = this.messagesRef.el;
                if (container) container.scrollTop = container.scrollHeight;
            }, 100);
        } catch (e) {
            console.error("Error loading messages:", e);
        }
        this.state.loadingMessages = false;
    }

    async openConversationById(conversationId) {
        // Open a specific conversation by ID
        const conv = this.state.conversations.find(c => c.id === conversationId);
        if (conv) {
            this.selectConversation(conv);
        } else {
            // Conversation might not be in current list, load it
            try {
                const conversations = await this.orm.searchRead(
                    "bader.inbox.conversation",
                    [["id", "=", conversationId]],
                    ["id", "computed_name", "phone", "last_message", "last_message_date",
                        "unread_count", "state", "assigned_user_id", "partner_id", "channel_id"]
                );
                if (conversations.length) {
                    this.state.conversations.unshift(conversations[0]);
                    this.selectConversation(conversations[0]);
                }
            } catch (e) {
                console.error("Error loading conversation:", e);
            }
        }
    }

    async openConversationByPhone(phone) {
        // Open or create conversation by phone number
        try {
            const result = await this.orm.call(
                "bader.inbox.conversation",
                "open_or_create_by_phone",
                [phone]
            );
            if (result && result.conversation_id) {
                await this.loadConversations();
                await this.openConversationById(result.conversation_id);
            }
        } catch (e) {
            console.error("Error opening conversation by phone:", e);
            this.notification.add(_t("Erro ao abrir conversa: ") + e.message, { type: "danger" });
        }
    }

    // ==========================================
    // CHANNEL CREATION (PLUG AND PLAY)
    // ==========================================

    openAddChannelModal() {
        this.state.showChannelModal = true;
        this.state.channelStep = 1;
        this.state.newChannelName = "";
        this.state.qrCodeData = null;
        this.state.currentChannelId = null;
    }

    closeChannelModal() {
        this.stopQRPolling();
        this.state.showChannelModal = false;
        this.loadChannels();
        this.loadConversations();
    }

    async createChannel() {
        if (!this.state.newChannelName.trim()) return;

        this.state.creatingChannel = true;
        try {
            // Create channel record
            const channelId = await this.orm.create("bader.inbox.channel", [{
                name: this.state.newChannelName.trim()
            }]);

            this.state.currentChannelId = channelId;

            // Connect to Evolution API (this creates instance and gets QR)
            await this.orm.call("bader.inbox.channel", "action_connect", [channelId]);

            // Move to step 2
            this.state.channelStep = 2;

            // Get QR code
            await this.fetchQRCode();

            // Start polling for connection
            this.startQRPolling();

        } catch (e) {
            console.error("Error creating channel:", e);
            this.notification.add(_t("Erro ao criar canal: " + e.message), { type: "danger" });
        }
        this.state.creatingChannel = false;
    }

    async fetchQRCode() {
        if (!this.state.currentChannelId) return;

        try {
            // Get updated channel with QR code
            const channels = await this.orm.searchRead(
                "bader.inbox.channel",
                [["id", "=", this.state.currentChannelId]],
                ["qrcode_base64", "state", "phone", "phone_name"]
            );

            if (channels.length) {
                const channel = channels[0];

                if (channel.state === "connected") {
                    // Connected - close modal and go to inbox!
                    this.stopQRPolling();
                    this.notification.add(_t("WhatsApp conectado com sucesso!"), { type: "success" });
                    this.state.showChannelModal = false;
                    this.loadChannels();
                    this.loadConversations();
                } else if (channel.qrcode_base64) {
                    this.state.qrCodeData = channel.qrcode_base64;
                }
            }
        } catch (e) {
            console.error("Error fetching QR:", e);
        }
    }

    async refreshQRCode() {
        if (!this.state.currentChannelId) return;

        try {
            await this.orm.call("bader.inbox.channel", "action_refresh_qr", [this.state.currentChannelId]);
            await this.fetchQRCode();
        } catch (e) {
            console.error("Error refreshing QR:", e);
        }
    }

    startQRPolling() {
        this.stopQRPolling();
        this.qrPollInterval = setInterval(() => {
            this.fetchQRCode();
        }, 3000); // Poll every 3 seconds
    }

    stopQRPolling() {
        if (this.qrPollInterval) {
            clearInterval(this.qrPollInterval);
            this.qrPollInterval = null;
        }
    }

    // ==========================================
    // CONVERSATION ACTIONS
    // ==========================================

    async selectConversation(conv) {
        this.state.selectedConversation = conv;
        await this.loadMessages(conv.id);

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
            (c.phone || "").includes(query)
        );
    }

    async assignConversation() {
        if (!this.state.selectedConversation) return;
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
