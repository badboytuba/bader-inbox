/** @odoo-module **/

import { Component, useState, onWillStart, useRef, onMounted, onWillUnmount } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { deserializeDateTime } from "@web/core/l10n/dates";
const { DateTime } = luxon;

export class BaderInboxMain extends Component {
    static template = "bader_inbox.InboxMain";
    static props = { "*": true };

    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.action = useService("action");
        this.user = useService("user");
        this.busService = useService("bus_service");

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

            // Quick Replies
            quickReplies: [],
            showQuickReplies: false,
            quickReplyFilter: "",
            quickReplyIndex: 0,

            // Emoji Picker
            showEmojiPicker: false,
            emojiCategory: "people",
            emojiSearch: "",

            // Message Search
            showMessageSearch: false,
            messageSearchQuery: "",
            messageSearchResults: [],
            messageSearchIndex: -1,

            // Drag & Drop
            isDragOver: false,

            // UI
            showContactPanel: true,
            viewMode: "list", // "list" or "kanban"

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

            // Pipeline / Kanban
            pipelines: [],
            selectedPipelineId: null,
            kanbanStages: [],
            kanbanCards: {},  // { stageId: [cards] }
            loadingKanban: false,
            convPipelines: [],  // pipeline assignments for selected conversation
            loadingPipelines: false,

            // Add to Pipeline Modal
            showPipelineModal: false,
            pipelineModalPipelineId: null,

            // New Conversation Modal
            showNewConversationModal: false,
            contactSearchQuery: "",
            contactSearchResults: [],
            searchingContacts: false,
            selectedChannelId: null,
        });

        this.messagesRef = useRef("messagesContainer");
        this.fileInputRef = useRef("fileInput");
        this.composerRef = useRef("composerTextarea");
        this.avatarColors = ["green", "blue", "purple", "orange", "pink", "red"];
        this.qrPollInterval = null;
        this.conversationPollInterval = null;
        this._contactSearchTimeout = null;
        this.messagePollInterval = null;
        this._notificationsEnabled = false;
        this._boundBusHandler = this._onBusNotification.bind(this);

        onWillStart(async () => {
            this.busService.addChannel("bader_inbox");
            await this.loadChannels();
            await this.loadConversations();
            await this.loadPipelines();
            await this.loadQuickReplies();
            this._requestNotificationPermission();

            // Check if we need to open a specific conversation (from PhoneWhatsAppWidget)
            const params = this.props.action?.params || {};
            if (params.conversation_id) {
                await this.openConversationById(params.conversation_id);
            } else if (params.phone) {
                await this.openConversationByPhone(params.phone);
            }
        });

        onMounted(() => {
            this.busService.addEventListener("notification", this._boundBusHandler);

            // Reduced polling intervals (now mostly for fallback/sync)
            this.conversationPollInterval = setInterval(() => {
                this._refreshConversations();
            }, 60000); // 60s fallback

            this.messagePollInterval = setInterval(() => {
                // Keep checking messages occasionally just in case
                if (this.state.selectedConversation) {
                    this._refreshMessages(this.state.selectedConversation.id);
                }
            }, 30000); // 30s fallback
        });

        onWillUnmount(() => {
            this.stopQRPolling();
            this.busService.removeEventListener("notification", this._boundBusHandler);
            if (this.conversationPollInterval) clearInterval(this.conversationPollInterval);
            if (this.messagePollInterval) clearInterval(this.messagePollInterval);
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

    async _refreshConversations() {
        // Silent refresh - NO loading indicator, NO flicker
        try {
            let domain = [];
            if (this.state.filter === "unread") {
                domain.push(["unread_count", ">", 0]);
            } else if (this.state.filter === "mine") {
                domain.push(["assigned_user_id", "=", this.user.userId]);
            }

            const freshConvs = await this.orm.searchRead(
                "bader.inbox.conversation",
                domain,
                [
                    "id", "computed_name", "phone", "last_message", "last_message_date",
                    "unread_count", "state", "assigned_user_id", "partner_id", "channel_id"
                ],
                { order: "last_message_date desc", limit: 100 }
            );

            // Smart merge: only update if data actually changed
            const currentIds = this.state.conversations.map(c => c.id).join(',');
            const freshIds = freshConvs.map(c => c.id).join(',');
            const currentSnippets = this.state.conversations.map(c => c.last_message + c.unread_count).join('|');
            const freshSnippets = freshConvs.map(c => c.last_message + c.unread_count).join('|');

            if (currentIds !== freshIds || currentSnippets !== freshSnippets) {
                // Preserve selected conversation reference
                const selectedId = this.state.selectedConversation?.id;
                this.state.conversations = freshConvs;
                if (selectedId) {
                    const selected = freshConvs.find(c => c.id === selectedId);
                    if (selected) {
                        // Force unread_count=0 since user is viewing this conversation
                        selected.unread_count = 0;
                        this.state.selectedConversation = selected;
                    }
                }
            }
        } catch (e) {
            // Silent fail for polling
        }
    }

    async loadMessages(conversationId) {
        if (!conversationId) return;
        this.state.loadingMessages = true;
        try {
            this.state.messages = await this.orm.searchRead(
                "bader.inbox.message",
                [["conversation_id", "=", conversationId]],
                ["id", "direction", "message_type", "content", "status", "create_date", "media_url", "media_mimetype", "media_filename"],
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

    async _refreshMessages(conversationId) {
        // Silent refresh - no loading indicator, no scroll jump
        if (!conversationId) return;
        try {
            const newMessages = await this.orm.searchRead(
                "bader.inbox.message",
                [["conversation_id", "=", conversationId]],
                ["id", "direction", "message_type", "content", "status", "create_date", "media_url", "media_mimetype", "media_filename"],
                { order: "create_date asc" }
            );
            // Only update if there are new messages
            if (newMessages.length !== this.state.messages.length) {
                this.state.messages = newMessages;
                // Scroll to bottom for new messages
                setTimeout(() => {
                    const container = this.messagesRef.el;
                    if (container) container.scrollTop = container.scrollHeight;
                }, 100);
            }
        } catch (e) {
            // Silent fail for polling
        }
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
            this.notification.add(_.str.sprintf(_t("Erro ao abrir conversa: %s"), e.message), { type: "danger" });
        }
    }

    // ==========================================
    // NEW CONVERSATION
    // ==========================================

    openNewConversationModal() {
        this.state.showNewConversationModal = true;
        this.state.contactSearchQuery = "";
        this.state.contactSearchResults = [];
        this.state.searchingContacts = false;
        // Default to first connected channel
        const connected = this.state.channels.find(c => c.state === 'connected');
        this.state.selectedChannelId = connected ? connected.id : (this.state.channels[0]?.id || null);
    }

    closeNewConversationModal() {
        this.state.showNewConversationModal = false;
        this.state.contactSearchQuery = "";
        this.state.contactSearchResults = [];
        if (this._contactSearchTimeout) {
            clearTimeout(this._contactSearchTimeout);
            this._contactSearchTimeout = null;
        }
    }

    onContactSearchInput(ev) {
        this.state.contactSearchQuery = ev.target.value;
        // Debounce 300ms
        if (this._contactSearchTimeout) {
            clearTimeout(this._contactSearchTimeout);
        }
        this._contactSearchTimeout = setTimeout(() => {
            this.searchContacts();
        }, 300);
    }

    async searchContacts() {
        const query = this.state.contactSearchQuery.trim();
        if (!query) {
            this.state.contactSearchResults = [];
            return;
        }
        this.state.searchingContacts = true;
        try {
            const results = await this.orm.call(
                "bader.inbox.conversation",
                "search_contacts",
                [query, 20]
            );
            this.state.contactSearchResults = results;
        } catch (err) {
            console.error("Contact search error:", err);
            this.state.contactSearchResults = [];
        }
        this.state.searchingContacts = false;
    }

    async startConversation(contact) {
        const phone = contact.display_phone;
        if (!phone) {
            this.notification.add(_("Este contacto não tem telefone"), { type: "warning" });
            return;
        }
        try {
            const result = await this.orm.call(
                "bader.inbox.conversation",
                "open_or_create_by_phone",
                [phone, contact.id, contact.name]
            );
            if (result.success) {
                this.closeNewConversationModal();
                await this.loadConversations();
                await this.openConversationById(result.conversation_id);
                this.notification.add(
                    _(`Conversa aberta com ${contact.name}`),
                    { type: "success" }
                );
            } else {
                this.notification.add(
                    result.error || _("Erro ao criar conversa"),
                    { type: "danger" }
                );
            }
        } catch (err) {
            console.error("Start conversation error:", err);
            this.notification.add(_("Erro ao iniciar conversa"), { type: "danger" });
        }
    }

    getContactInitials(name) {
        if (!name) return "?";
        const parts = name.trim().split(/\s+/);
        if (parts.length >= 2) {
            return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
        }
        return name.substring(0, 2).toUpperCase();
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
            this.notification.add(_t("Erro ao criar canal") + ": " + e.message, { type: "danger" });
        }
        this.state.creatingChannel = false;
    }

    async fetchQRCode() {
        if (!this.state.currentChannelId) return;

        try {
            // Actively check connection status via Evolution API
            // (don't rely solely on webhook)
            try {
                await this.orm.call(
                    "bader.inbox.channel",
                    "action_check_status",
                    [this.state.currentChannelId]
                );
            } catch (e) {
                // Ignore — channel may not have instance yet
            }

            // Get updated channel with QR code and state
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
                    this.state.connectedPhone = channel.phone || "";
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
        await this.loadConvPipelines(conv.id);

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
        if (!this.state.selectedConversation) return;

        const content = this.state.composerText.trim();
        // Allow empty content if sending media
        if (!content && !this.state.attachment) return;

        this.state.sendingMessage = true;
        try {
            if (this.state.attachment) {
                // Send with media
                await this.orm.call(
                    "bader.inbox.message",
                    "send_message",
                    [],
                    {
                        conversation_id: this.state.selectedConversation.id,
                        content: content,
                        msg_type: this.state.attachment.type,
                        media_data: this.state.attachment.data,
                        media_filename: this.state.attachment.name
                    }
                );
                this.state.attachment = null;
            } else {
                // Send text only
                await this.orm.call(
                    "bader.inbox.message",
                    "send_message",
                    [this.state.selectedConversation.id, content, "text"]
                );
            }

            this.state.composerText = "";
            await this.loadMessages(this.state.selectedConversation.id);
        } catch (e) {
            console.error(e);
            this.notification.add(_t("Erro ao enviar mensagem"), { type: "danger" });
        }
        this.state.sendingMessage = false;
    }

    triggerFileInput() {
        if (this.fileInputRef.el) {
            this.fileInputRef.el.click();
        }
    }

    async onFileSelected(ev) {
        const file = ev.target.files[0];
        if (!file) return;

        const reader = new FileReader();
        reader.onload = () => {
            const base64Data = reader.result.split(',')[1];
            this.state.attachment = {
                name: file.name,
                data: base64Data,
                type: this._getMediaType(file.type),
                preview: reader.result
            };
            // Clear input so same file can be selected again
            ev.target.value = "";
        };
        reader.readAsDataURL(file);
    }

    removeAttachment() {
        this.state.attachment = null;
    }

    _getMediaType(mimeType) {
        if (mimeType.startsWith("image/")) return "image";
        if (mimeType.startsWith("audio/")) return "audio";
        if (mimeType.startsWith("video/")) return "video";
        return "document";
    }

    onComposerKeydown(event) {
        // Quick Reply navigation
        if (this.state.showQuickReplies) {
            const filtered = this.filteredQuickReplies();
            if (event.key === "ArrowDown") {
                event.preventDefault();
                this.state.quickReplyIndex = Math.min(this.state.quickReplyIndex + 1, filtered.length - 1);
                return;
            }
            if (event.key === "ArrowUp") {
                event.preventDefault();
                this.state.quickReplyIndex = Math.max(this.state.quickReplyIndex - 1, 0);
                return;
            }
            if (event.key === "Enter" && filtered.length > 0) {
                event.preventDefault();
                this.selectQuickReply(filtered[this.state.quickReplyIndex]);
                return;
            }
            if (event.key === "Escape") {
                this.state.showQuickReplies = false;
                return;
            }
        }
        if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            this.sendMessage();
        }
    }

    onComposerInput(event) {
        const text = event.target.value;
        // Detect / at start of line for quick replies
        if (text === "/" || text.startsWith("/")) {
            this.state.showQuickReplies = true;
            this.state.quickReplyFilter = text.slice(1).toLowerCase();
            this.state.quickReplyIndex = 0;
        } else {
            this.state.showQuickReplies = false;
        }
    }

    // ──── QUICK REPLIES ────
    async loadQuickReplies() {
        try {
            this.state.quickReplies = await this.orm.searchRead(
                "bader.inbox.template",
                [["active", "=", true]],
                ["id", "name", "shortcut", "content", "category"],
                { order: "use_count desc, sequence asc", limit: 50 }
            );
        } catch (e) {
            console.warn("Quick replies not available:", e);
        }
    }

    filteredQuickReplies() {
        if (!this.state.quickReplyFilter) return this.state.quickReplies;
        const q = this.state.quickReplyFilter;
        return this.state.quickReplies.filter(r =>
            (r.shortcut && r.shortcut.toLowerCase().includes(q)) ||
            r.name.toLowerCase().includes(q) ||
            r.content.toLowerCase().includes(q)
        );
    }

    selectQuickReply(template) {
        this.state.composerText = template.content;
        this.state.showQuickReplies = false;
        this.state.quickReplyFilter = "";
        // Increment use counter
        this.orm.call("bader.inbox.template", "write", [[template.id], { use_count: (template.use_count || 0) + 1 }]).catch(() => { });
        // Focus composer
        setTimeout(() => {
            const ta = this.composerRef.el;
            if (ta) { ta.focus(); ta.selectionStart = ta.value.length; }
        }, 50);
    }

    // ──── EMOJI PICKER ────
    toggleEmojiPicker() {
        this.state.showEmojiPicker = !this.state.showEmojiPicker;
        this.state.emojiSearch = "";
        if (this.state.showEmojiPicker) this.state.showQuickReplies = false;
    }

    insertEmoji(emoji) {
        const ta = this.composerRef.el;
        if (ta) {
            const start = ta.selectionStart || this.state.composerText.length;
            const before = this.state.composerText.slice(0, start);
            const after = this.state.composerText.slice(start);
            this.state.composerText = before + emoji + after;
            setTimeout(() => { ta.focus(); ta.selectionStart = ta.selectionEnd = start + emoji.length; }, 10);
        } else {
            this.state.composerText += emoji;
        }
    }

    getEmojiCategories() {
        return [
            { id: "recent", icon: "🕐", label: "Recentes" },
            { id: "people", icon: "😀", label: "Pessoas" },
            { id: "nature", icon: "🐻", label: "Natureza" },
            { id: "food", icon: "🍕", label: "Comida" },
            { id: "travel", icon: "✈️", label: "Viagem" },
            { id: "objects", icon: "💡", label: "Objetos" },
            { id: "symbols", icon: "❤️", label: "Símbolos" },
        ];
    }

    getEmojisForCategory(catId) {
        const q = this.state.emojiSearch.toLowerCase();
        const data = {
            people: ["😀", "😃", "😄", "😁", "😆", "😅", "🤣", "😂", "🙂", "😊", "😇", "🥰", "😍", "🤩", "😘", "😗", "😚", "😙", "🥲", "😋", "😛", "😜", "🤪", "😝", "🤑", "🤗", "🤭", "🫢", "🤫", "🤔", "🫡", "🤐", "🤨", "😐", "😑", "😶", "🫥", "😏", "😒", "🙄", "😬", "😮‍💨", "🤥", "😌", "😔", "😪", "🤤", "😴", "😷", "🤒", "🤕", "🤢", "🤮", "🥵", "🥶", "🥴", "😵", "🤯", "🤠", "🥳", "🥸", "😎", "🤓", "🧐", "😕", "🫤", "😟", "🙁", "😮", "😯", "😲", "😳", "🥺", "🥹", "😦", "😧", "😨", "😰", "😥", "😢", "😭", "😱", "😖", "😣", "😞", "😓", "😩", "😫", "🥱", "😤", "😡", "😠", "🤬", "👋", "🤚", "🖐️", "✋", "🖖", "🫱", "🫲", "🫳", "🫴", "👌", "🤌", "🤏", "✌️", "🤞", "🫰", "🤟", "🤘", "🤙", "👈", "👉", "👆", "🖕", "👇", "☝️", "🫵", "👍", "👎", "✊", "👊", "🤛", "🤜", "👏", "🙌", "🫶", "👐", "🤲", "🤝", "🙏"],
            nature: ["🐶", "🐱", "🐭", "🐹", "🐰", "🦊", "🐻", "🐼", "🐨", "🐯", "🦁", "🐮", "🐷", "🐸", "🐵", "🐔", "🐧", "🐦", "🦅", "🦆", "🦉", "🐴", "🦄", "🐝", "🐛", "🦋", "🐌", "🐞", "🌸", "💐", "🌹", "🌺", "🌻", "🌼", "🌷", "🌱", "🌲", "🌳", "🌴", "🌵", "🍀", "🍁", "🍂", "🍃", "🌍", "🌙", "⭐", "🌟", "⚡", "🔥", "🌈", "☀️", "🌤️", "⛅", "🌧️", "❄️", "💧", "🌊"],
            food: ["🍏", "🍎", "🍐", "🍊", "🍋", "🍌", "🍉", "🍇", "🍓", "🫐", "🍈", "🍒", "🍑", "🥭", "🍍", "🥥", "🥝", "🍅", "🥑", "🍆", "🥔", "🥕", "🌽", "🌶️", "🫑", "🥒", "🥬", "🥦", "🧄", "🧅", "🍄", "🥜", "🍞", "🥐", "🥖", "🥨", "🧀", "🍖", "🍗", "🥩", "🥓", "🍔", "🍟", "🍕", "🌭", "🥪", "🌮", "🌯", "🥗", "🍝", "🍜", "🍲", "🍛", "🍣", "🍱", "🥟", "🍤", "🍙", "🍰", "🎂", "🧁", "🍫", "🍬", "🍭", "🍮", "🍩", "🍪", "☕", "🍵", "🥤", "🍺", "🍷", "🥂", "🍸", "🍹"],
            travel: ["🚗", "🚕", "🚌", "🏎️", "🚑", "🚒", "✈️", "🚀", "🛸", "🚁", "⛵", "🚢", "🏠", "🏢", "🏥", "🏫", "🏟️", "🗼", "🗽", "⛪", "🕌", "🛕", "🏰", "🏯", "🏝️", "🏖️", "🏔️", "⛰️", "🗻", "🌋", "🏕️", "🎪", "🎡", "🎢", "🎠", "⛲", "🌁", "🌉", "🌅", "🌄"],
            objects: ["⌚", "📱", "💻", "⌨️", "🖥️", "🖨️", "📷", "📹", "🎥", "📞", "📺", "📻", "🎙️", "⏰", "🔋", "💡", "🔦", "🕯️", "📦", "📬", "📝", "📁", "📋", "📌", "📎", "🔑", "🔒", "🔓", "🔨", "🪛", "🔧", "💰", "💳", "💎", "🎁", "🎈", "🎉", "🎊", "🏆", "🥇", "🥈", "🥉", "⚽", "🏀", "🎾", "🎮", "🎯", "🎲", "🧩", "🎭", "🎨", "🎵", "🎶", "🎸", "🎹", "🥁", "🎻", "🎺"],
            symbols: ["❤️", "🧡", "💛", "💚", "💙", "💜", "🖤", "🤍", "🤎", "💔", "❣️", "💕", "💞", "💓", "💗", "💖", "💘", "💝", "✅", "❌", "⭕", "❗", "❓", "💯", "⚠️", "🔴", "🟠", "🟡", "🟢", "🔵", "🟣", "⚫", "⚪", "🟤", "▪️", "▫️", "🔶", "🔷", "🔸", "🔹", "💠", "🔘", "✨", "💫", "🌟", "⚡", "💥", "🔔", "🎵", "🎶"],
        };
        if (catId === "recent") {
            const stored = localStorage.getItem("bader_recent_emojis");
            return stored ? JSON.parse(stored) : ["👍", "❤️", "😂", "🙏", "😊", "👋", "🔥", "✅", "👏", "😍"];
        }
        let emojis = data[catId] || [];
        if (q) {
            // Simple search: not needed for emojis, just return all
            return emojis;
        }
        return emojis;
    }

    onEmojiSelected(emoji) {
        this.insertEmoji(emoji);
        // Save to recents
        try {
            const stored = localStorage.getItem("bader_recent_emojis");
            let recents = stored ? JSON.parse(stored) : [];
            recents = [emoji, ...recents.filter(e => e !== emoji)].slice(0, 30);
            localStorage.setItem("bader_recent_emojis", JSON.stringify(recents));
        } catch (e) { }
    }

    // ──── DESKTOP NOTIFICATIONS ────
    _requestNotificationPermission() {
        if ("Notification" in window && Notification.permission === "default") {
            Notification.requestPermission();
        }
        this._notificationsEnabled = "Notification" in window && Notification.permission === "granted";
    }

    _showDesktopNotification(title, body, conversationId) {
        if (!this._notificationsEnabled || document.hasFocus()) return;
        try {
            const n = new Notification(title, {
                body: body,
                icon: "/bader_inbox/static/src/img/icon.png",
                tag: `bader-inbox-${conversationId}`,
                silent: false,
            });
            n.onclick = () => {
                window.focus();
                if (conversationId) this.openConversationById(conversationId);
                n.close();
            };
            setTimeout(() => n.close(), 8000);
        } catch (e) { }
    }

    // ──── MESSAGE SEARCH ────
    toggleMessageSearch() {
        this.state.showMessageSearch = !this.state.showMessageSearch;
        if (!this.state.showMessageSearch) {
            this.state.messageSearchQuery = "";
            this.state.messageSearchResults = [];
            this.state.messageSearchIndex = -1;
        }
    }

    async searchMessages() {
        const q = this.state.messageSearchQuery.trim();
        if (!q || q.length < 2 || !this.state.selectedConversation) return;
        try {
            this.state.messageSearchResults = await this.orm.searchRead(
                "bader.inbox.message",
                [
                    ["conversation_id", "=", this.state.selectedConversation.id],
                    ["content", "ilike", q],
                    ["message_type", "=", "text"],
                ],
                ["id", "content", "create_date", "direction"],
                { order: "create_date desc", limit: 50 }
            );
            this.state.messageSearchIndex = this.state.messageSearchResults.length > 0 ? 0 : -1;
            if (this.state.messageSearchIndex >= 0) {
                this._scrollToMessage(this.state.messageSearchResults[0].id);
            }
        } catch (e) {
            console.error("Search error:", e);
        }
    }

    navigateSearchResult(direction) {
        if (!this.state.messageSearchResults.length) return;
        if (direction === "next") {
            this.state.messageSearchIndex = Math.min(this.state.messageSearchIndex + 1, this.state.messageSearchResults.length - 1);
        } else {
            this.state.messageSearchIndex = Math.max(this.state.messageSearchIndex - 1, 0);
        }
        const msg = this.state.messageSearchResults[this.state.messageSearchIndex];
        if (msg) this._scrollToMessage(msg.id);
    }

    _scrollToMessage(msgId) {
        setTimeout(() => {
            const container = this.messagesRef.el;
            if (!container) return;
            const el = container.querySelector(`[data-msg-id="${msgId}"]`);
            if (el) {
                el.scrollIntoView({ behavior: "smooth", block: "center" });
                el.classList.add("search-highlight");
                setTimeout(() => el.classList.remove("search-highlight"), 2000);
            }
        }, 100);
    }

    onSearchKeydown(event) {
        if (event.key === "Enter") {
            event.preventDefault();
            this.searchMessages();
        } else if (event.key === "Escape") {
            this.toggleMessageSearch();
        }
    }

    // ──── DRAG & DROP ────
    onDragEnterChat(ev) {
        ev.preventDefault();
        if (!this.state.selectedConversation) return;
        this.state.isDragOver = true;
    }

    onDragOverChat(ev) {
        ev.preventDefault();
    }

    onDragLeaveChat(ev) {
        ev.preventDefault();
        // Only hide if leaving the main container
        if (!ev.currentTarget.contains(ev.relatedTarget)) {
            this.state.isDragOver = false;
        }
    }

    onDropChat(ev) {
        ev.preventDefault();
        this.state.isDragOver = false;
        if (!this.state.selectedConversation) return;
        const files = ev.dataTransfer?.files;
        if (files && files.length > 0) {
            this._processDroppedFile(files[0]);
        }
    }

    _processDroppedFile(file) {
        const reader = new FileReader();
        reader.onload = () => {
            const base64Data = reader.result.split(',')[1];
            this.state.attachment = {
                name: file.name,
                data: base64Data,
                type: this._getMediaType(file.type),
                preview: reader.result
            };
        };
        reader.readAsDataURL(file);
    }

    // ==========================================
    // CONTACT PANEL ACTIONS
    // ==========================================

    toggleContactPanel() {
        this.state.showContactPanel = !this.state.showContactPanel;
    }

    // ==========================================
    // VIEW MODE TOGGLE
    // ==========================================

    setViewMode(mode) {
        this.state.viewMode = mode;
        if (mode === "kanban") {
            if (this.state.pipelines.length && !this.state.selectedPipelineId) {
                this.state.selectedPipelineId = this.state.pipelines[0].id;
            }
            this.loadKanbanData();
        }
    }

    // ==========================================
    // PIPELINE / KANBAN
    // ==========================================

    async loadPipelines() {
        try {
            this.state.pipelines = await this.orm.searchRead(
                "bader.inbox.pipeline",
                [["active", "=", true]],
                ["id", "name", "icon", "color"],
                { order: "sequence, name" }
            );
        } catch (e) {
            console.error("Error loading pipelines:", e);
        }
    }

    async selectPipeline(pipelineId) {
        this.state.selectedPipelineId = pipelineId;
        await this.loadKanbanData();
    }

    onPipelineSelectChange(ev) {
        const val = parseInt(ev.target.value, 10);
        if (val) this.selectPipeline(val);
    }

    onPipelineModalChange(ev) {
        this.state.pipelineModalPipelineId = parseInt(ev.target.value, 10) || null;
    }

    async loadKanbanData() {
        if (!this.state.selectedPipelineId) return;
        this.state.loadingKanban = true;
        try {
            // Load stages for this pipeline
            const stages = await this.orm.searchRead(
                "bader.inbox.pipeline.stage",
                [["pipeline_id", "=", this.state.selectedPipelineId]],
                ["id", "name", "sequence", "fold"],
                { order: "sequence, id" }
            );
            this.state.kanbanStages = stages;

            // Load assignments (cards)
            const cards = await this.orm.searchRead(
                "bader.inbox.conversation.pipeline",
                [["pipeline_id", "=", this.state.selectedPipelineId]],
                [
                    "id", "conversation_id", "stage_id", "contact_name",
                    "phone", "last_message", "unread_count", "priority",
                    "assigned_user_id", "kanban_state", "notes"
                ],
                { order: "priority desc, id" }
            );

            // Group cards by stage
            const grouped = {};
            for (const stage of stages) {
                grouped[stage.id] = [];
            }
            for (const card of cards) {
                const stageId = card.stage_id[0];
                if (grouped[stageId]) {
                    grouped[stageId].push(card);
                }
            }
            this.state.kanbanCards = grouped;
        } catch (e) {
            console.error("Error loading kanban:", e);
        }
        this.state.loadingKanban = false;
    }

    // Drag and Drop
    onDragStart(ev, card) {
        ev.dataTransfer.setData("text/plain", JSON.stringify({
            cardId: card.id,
            fromStageId: card.stage_id[0]
        }));
        ev.dataTransfer.effectAllowed = "move";
        ev.target.classList.add("dragging");
    }

    onDragEnd(ev) {
        ev.target.classList.remove("dragging");
    }

    onDragOver(ev) {
        ev.preventDefault();
        ev.dataTransfer.dropEffect = "move";
        ev.currentTarget.classList.add("drag-over");
    }

    onDragLeave(ev) {
        ev.currentTarget.classList.remove("drag-over");
    }

    async onDrop(ev, targetStageId) {
        ev.preventDefault();
        ev.currentTarget.classList.remove("drag-over");

        try {
            const data = JSON.parse(ev.dataTransfer.getData("text/plain"));
            if (data.fromStageId === targetStageId) return;

            // Optimistic update
            const fromCards = this.state.kanbanCards[data.fromStageId] || [];
            const cardIdx = fromCards.findIndex(c => c.id === data.cardId);
            if (cardIdx >= 0) {
                const [card] = fromCards.splice(cardIdx, 1);
                const targetStage = this.state.kanbanStages.find(s => s.id === targetStageId);
                card.stage_id = [targetStageId, targetStage?.name || ""];
                if (!this.state.kanbanCards[targetStageId]) {
                    this.state.kanbanCards[targetStageId] = [];
                }
                this.state.kanbanCards[targetStageId].push(card);
            }

            // Persist to server
            await this.orm.write(
                "bader.inbox.conversation.pipeline",
                [data.cardId],
                { stage_id: targetStageId }
            );
        } catch (e) {
            console.error("Error moving card:", e);
            this.notification.add(_t("Erro ao mover card"), { type: "danger" });
            await this.loadKanbanData();
        }
    }

    onKanbanCardClick(card) {
        // Switch to list view and open this conversation
        const conv = this.state.conversations.find(c => c.id === card.conversation_id[0]);
        if (conv) {
            this.state.viewMode = "list";
            this.selectConversation(conv);
        } else {
            this.openConversationById(card.conversation_id[0]);
            this.state.viewMode = "list";
        }
    }

    // Pipeline sidebar — load assignments for selected conversation
    async loadConvPipelines(conversationId) {
        if (!conversationId) return;
        this.state.loadingPipelines = true;
        try {
            this.state.convPipelines = await this.orm.searchRead(
                "bader.inbox.conversation.pipeline",
                [["conversation_id", "=", conversationId]],
                ["id", "pipeline_id", "stage_id", "priority"],
                { order: "id" }
            );
        } catch (e) {
            console.error("Error loading conv pipelines:", e);
        }
        this.state.loadingPipelines = false;
    }

    // Add conversation to pipeline
    openAddToPipelineModal() {
        this.state.showPipelineModal = true;
        this.state.pipelineModalPipelineId = null;
    }

    closePipelineModal() {
        this.state.showPipelineModal = false;
    }

    async addToPipeline() {
        if (!this.state.selectedConversation || !this.state.pipelineModalPipelineId) return;
        try {
            // Get first stage of the pipeline
            const stages = await this.orm.searchRead(
                "bader.inbox.pipeline.stage",
                [["pipeline_id", "=", this.state.pipelineModalPipelineId]],
                ["id"],
                { order: "sequence", limit: 1 }
            );
            if (!stages.length) {
                this.notification.add(_t("Este pipeline não tem etapas!"), { type: "warning" });
                return;
            }
            await this.orm.create("bader.inbox.conversation.pipeline", [{
                conversation_id: this.state.selectedConversation.id,
                pipeline_id: this.state.pipelineModalPipelineId,
                stage_id: stages[0].id,
            }]);
            this.notification.add(_t("Adicionado ao pipeline!"), { type: "success" });
            this.state.showPipelineModal = false;
            await this.loadConvPipelines(this.state.selectedConversation.id);
        } catch (e) {
            console.error("Error adding to pipeline:", e);
            if (e.message && e.message.includes("unique")) {
                this.notification.add(_t("Esta conversa já está neste pipeline!"), { type: "warning" });
            } else {
                this.notification.add(_t("Erro ao adicionar"), { type: "danger" });
            }
        }
    }

    async removeFromPipeline(assignmentId) {
        try {
            await this.orm.unlink("bader.inbox.conversation.pipeline", [assignmentId]);
            this.notification.add(_t("Removido do pipeline"), { type: "info" });
            await this.loadConvPipelines(this.state.selectedConversation.id);
        } catch (e) {
            console.error("Error removing from pipeline:", e);
        }
    }

    get availablePipelinesForModal() {
        // Pipelines not already assigned to this conversation
        const assignedIds = this.state.convPipelines.map(cp => cp.pipeline_id[0]);
        return this.state.pipelines.filter(p => !assignedIds.includes(p.id));
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
        // Deserialize UTC string to Luxon DateTime (in local/system zone)
        const date = deserializeDateTime(dateStr);
        const now = DateTime.local();
        // Calculate difference in days (ignoring time)
        const diff = Math.floor(now.diff(date, 'days').days);

        if (date.hasSame(now, 'day')) {
            return date.toFormat("HH:mm");
        } else if (diff < 2 && date.hasSame(now.minus({ days: 1 }), 'day')) {
            return "Ontem";
        } else if (diff < 7) {
            return date.toFormat("ccc"); // Short weekday
        } else {
            return date.toFormat("dd/MM");
        }
    }

    getInitials(name) {
        if (!name) return "?";
        return name.split(" ").map(n => n[0]).join("").substring(0, 2).toUpperCase();
    }

    getAvatarColor(id) {
        return this.avatarColors[(id || 0) % this.avatarColors.length];
    }

    getMediaUrl(msg) {
        // Always use Odoo media endpoint for media messages
        const mediaTypes = ["image", "audio", "video", "document"];
        if (mediaTypes.includes(msg.message_type)) {
            return `/bader-inbox/media/${msg.id}`;
        }
        return msg.media_url || "";
    }

    async _onBusNotification({ detail: notifications }) {
        let refreshConversations = false;
        let refreshMessages = false;

        for (const { payload, type } of notifications) {
            if (type === "bader_inbox_new_message") {
                // New message received
                refreshConversations = true;

                // Desktop notification for incoming messages
                if (payload.message && payload.message.direction === "in") {
                    const convName = payload.contact_name || payload.phone || "Nova mensagem";
                    const body = payload.message.content || payload.message.message_type || "";
                    this._showDesktopNotification(convName, body, payload.conversation_id);
                }

                // If this conversation is currently open, refresh messages immediately
                if (this.state.selectedConversation &&
                    this.state.selectedConversation.id === payload.conversation_id) {

                    // Optimistic update if we have full message payload
                    if (payload.message) {
                        // Check if already exists
                        const exists = this.state.messages.some(m => m.id === payload.message.id);
                        if (!exists) {
                            // Add media URL helper if needed
                            const msg = payload.message;
                            if (!msg.media_url && msg.message_type !== 'text' && msg.id) {
                                msg.media_url = `/bader-inbox/media/${msg.id}`;
                            }
                            this.state.messages.push(msg);
                            setTimeout(() => {
                                const container = this.messagesRef.el;
                                if (container) container.scrollTop = container.scrollHeight;
                            }, 100);
                        }
                    } else {
                        refreshMessages = true;
                    }
                }
            } else if (type === "bader_inbox_status_update") {
                // Message delivery/read status update
                if (this.state.selectedConversation &&
                    this.state.selectedConversation.id === payload.conversation_id) {
                    const msg = this.state.messages.find(m => m.id === payload.message_id);
                    if (msg) {
                        msg.status = payload.status;
                    }
                }
            }
        }

        if (refreshConversations) {
            await this._refreshConversations();
        }
        if (refreshMessages && this.state.selectedConversation) {
            await this._refreshMessages(this.state.selectedConversation.id);
        }
    }

    getUserInitials() {
        return this.getInitials(this.user.name || "U");
    }
}

registry.category("actions").add("bader_inbox_main", BaderInboxMain);
