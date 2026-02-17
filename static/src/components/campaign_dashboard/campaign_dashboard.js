/** @odoo-module **/

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
const { Component, useState, onMounted } = owl;

const STATUS_MAP = {
    draft: { label: "Rascunho", color: "#64748B", icon: "📝" },
    active: { label: "Ativa", color: "#10B981", icon: "▶" },
    paused: { label: "Pausada", color: "#F59E0B", icon: "⏸" },
    completed: { label: "Concluída", color: "#3B82F6", icon: "✅" },
    cancelled: { label: "Cancelada", color: "#EF4444", icon: "❌" },
};

export class CampaignDashboard extends Component {
    static template = "bader_inbox.CampaignDashboard";

    setup() {
        this.rpc = useService("rpc");
        this.action = useService("action");
        this.notification = useService("notification");

        this.state = useState({
            loading: true,
            kpis: {},
            campaigns: [],
            channelHealth: [],
            dailyVolume: [],
            optout: {},
            executions: {},
            activeTab: "overview",
            filterStatus: "all",
        });

        onMounted(() => this.loadData());
    }

    async loadData() {
        this.state.loading = true;
        try {
            const data = await this.rpc("/bader-inbox/campaign/dashboard", {});
            this.state.kpis = data.kpis || {};
            this.state.campaigns = data.campaigns || [];
            this.state.channelHealth = data.channel_health || [];
            this.state.dailyVolume = data.daily_volume || [];
            this.state.optout = data.optout || {};
            this.state.executions = data.executions || {};
        } catch (e) {
            console.error("Dashboard load failed:", e);
        }
        this.state.loading = false;
    }

    // ── Tabs ──
    setTab(tab) { this.state.activeTab = tab; }

    // ── Filters ──
    setFilter(status) { this.state.filterStatus = status; }

    get filteredCampaigns() {
        if (this.state.filterStatus === "all") return this.state.campaigns;
        return this.state.campaigns.filter(c => c.status === this.state.filterStatus);
    }

    // ── Status helpers ──
    getStatusInfo(status) { return STATUS_MAP[status] || STATUS_MAP.draft; }
    getStatusLabel(status) { return (STATUS_MAP[status] || STATUS_MAP.draft).label; }
    getStatusColor(status) { return (STATUS_MAP[status] || STATUS_MAP.draft).color; }
    getStatusIcon(status) { return (STATUS_MAP[status] || STATUS_MAP.draft).icon; }

    // ── Chart helpers ──
    get maxVolume() {
        return Math.max(1, ...this.state.dailyVolume.map(d => d.sent));
    }

    getBarHeight(sent) {
        return Math.max(4, (sent / this.maxVolume) * 120);
    }

    getFailBarHeight(d) {
        return Math.max(0, (d.failed / this.maxVolume) * 120);
    }

    // ── Health Score ──
    getHealthClass(score) {
        if (score >= 90) return "cd-health-good";
        if (score >= 70) return "cd-health-warn";
        return "cd-health-bad";
    }

    // ── Actions ──
    async campaignAction(campaignId, actionName) {
        try {
            await this.rpc("/bader-inbox/campaign/action", {
                campaign_id: campaignId,
                action: actionName,
            });
            this.notification.add("✅ Ação executada!", { type: "success" });
            await this.loadData();
        } catch (e) {
            this.notification.add(`❌ Erro: ${e.message}`, { type: "danger" });
        }
    }

    openCampaign(campaignId) {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "bader.inbox.campaign",
            res_id: campaignId,
            views: [[false, "form"]],
        });
    }

    openFlowDesigner(campaignId) {
        this.action.doAction({
            type: "ir.actions.client",
            tag: "bader_inbox_flow_designer",
            params: { campaign_id: campaignId },
        });
    }

    createCampaign() {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "bader.inbox.campaign",
            views: [[false, "form"]],
            target: "current",
        });
    }

    // ── KPI formatting ──
    formatNumber(n) {
        if (!n && n !== 0) return "0";
        if (n >= 1000000) return (n / 1000000).toFixed(1) + "M";
        if (n >= 1000) return (n / 1000).toFixed(1) + "K";
        return n.toString();
    }
}

CampaignDashboard.props = { action: { type: Object, optional: true } };

registry.category("actions").add("bader_inbox_campaign_dashboard", CampaignDashboard);
