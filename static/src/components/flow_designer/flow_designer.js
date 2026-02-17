/** @odoo-module **/

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
const { Component, useState, useRef, onMounted, onWillUnmount } = owl;

// ════════════════════════════════════════════════════════════════
//  NODE TYPE DEFINITIONS
// ════════════════════════════════════════════════════════════════

const NODE_TYPES = {
    // ── Triggers ──
    trigger_manual: { label: "Manual Start", icon: "▶", color: "#10B981", category: "trigger" },
    trigger_lead: { label: "Novo Lead", icon: "👤", color: "#10B981", category: "trigger" },
    trigger_lead_stage: { label: "Lead → Etapa", icon: "📊", color: "#10B981", category: "trigger" },
    trigger_opp_won: { label: "Negócio Ganho", icon: "🏆", color: "#10B981", category: "trigger" },
    trigger_opp_lost: { label: "Negócio Perdido", icon: "💔", color: "#10B981", category: "trigger" },
    trigger_order: { label: "Pedido Confirmado", icon: "🛒", color: "#10B981", category: "trigger" },
    trigger_cart: { label: "Carrinho Abandonado", icon: "🛒", color: "#10B981", category: "trigger" },
    trigger_shipped: { label: "Pedido Enviado", icon: "📦", color: "#10B981", category: "trigger" },
    trigger_invoice: { label: "Fatura Vencida", icon: "💰", color: "#10B981", category: "trigger" },
    trigger_tag: { label: "Tag Adicionada", icon: "🏷️", color: "#10B981", category: "trigger" },
    trigger_birthday: { label: "Aniversário", icon: "🎂", color: "#10B981", category: "trigger" },

    // ── Actions ──
    action_send_text: { label: "Enviar Texto", icon: "💬", color: "#3B82F6", category: "action" },
    action_send_media: { label: "Enviar Media", icon: "📷", color: "#3B82F6", category: "action" },
    action_send_template: { label: "Enviar Template", icon: "📋", color: "#3B82F6", category: "action" },
    action_send_product: { label: "Product Card", icon: "🏪", color: "#3B82F6", category: "action" },
    action_add_tag: { label: "Adicionar Tag", icon: "🏷️", color: "#3B82F6", category: "action" },
    action_create_lead: { label: "Criar Lead", icon: "📝", color: "#3B82F6", category: "action" },
    action_create_task: { label: "Criar Tarefa", icon: "✅", color: "#3B82F6", category: "action" },
    action_ai_generate: { label: "IA Gerar Texto", icon: "🤖", color: "#8B5CF6", category: "action" },
    action_webhook: { label: "Webhook", icon: "🔗", color: "#3B82F6", category: "action" },
    action_update_crm: { label: "Atualizar CRM", icon: "💼", color: "#3B82F6", category: "action" },

    // ── Conditions ──
    condition_if: { label: "Se / Senão", icon: "❓", color: "#F59E0B", category: "condition" },
    condition_replied: { label: "Respondeu?", icon: "↩", color: "#F59E0B", category: "condition" },
    condition_read: { label: "Leu?", icon: "👁", color: "#F59E0B", category: "condition" },
    condition_clicked: { label: "Clicou Link?", icon: "🔗", color: "#F59E0B", category: "condition" },
    condition_ab_split: { label: "A/B Split", icon: "🔀", color: "#F59E0B", category: "condition" },

    // ── Delays ──
    delay_fixed: { label: "Esperar Tempo", icon: "⏰", color: "#6B7280", category: "delay" },
    delay_until: { label: "Esperar Até", icon: "📅", color: "#6B7280", category: "delay" },
    delay_reaction: { label: "Esperar Reação", icon: "👀", color: "#6B7280", category: "delay" },
};

const CATEGORIES = [
    { key: "trigger", label: "⚡ Triggers", color: "#10B981" },
    { key: "action", label: "📦 Actions", color: "#3B82F6" },
    { key: "condition", label: "❓ Conditions", color: "#F59E0B" },
    { key: "delay", label: "⏰ Delays", color: "#6B7280" },
];

// ════════════════════════════════════════════════════════════════
//  NODE CONFIG SCHEMAS
// ════════════════════════════════════════════════════════════════

const NODE_CONFIG_FIELDS = {
    action_send_text: [{ key: "message", label: "Mensagem", type: "textarea" }],
    action_send_media: [
        { key: "message", label: "Caption", type: "textarea" },
        { key: "media_url", label: "URL da Media", type: "text" },
    ],
    action_send_template: [{ key: "template_id", label: "Template", type: "many2one", model: "bader.inbox.template" }],
    action_add_tag: [{ key: "tag_id", label: "Tag", type: "many2one", model: "res.partner.category" }],
    action_ai_generate: [
        { key: "ai_prompt", label: "Prompt IA", type: "textarea" },
        { key: "variable_name", label: "Guardar em variável", type: "text" },
    ],
    condition_if: [{ key: "condition_expr", label: "Condição (Python)", type: "text" }],
    condition_ab_split: [
        { key: "split_a", label: "% Variante A", type: "number" },
        { key: "split_b", label: "% Variante B", type: "number" },
    ],
    delay_fixed: [
        { key: "delay_value", label: "Valor", type: "number" },
        {
            key: "delay_unit", label: "Unidade", type: "select", options: [
                { value: "minutes", label: "Minutos" },
                { value: "hours", label: "Horas" },
                { value: "days", label: "Dias" },
            ]
        },
    ],
    trigger_cart: [
        { key: "min_minutes", label: "Min. minutos abandono", type: "number" },
    ],
    trigger_invoice: [
        { key: "min_days_overdue", label: "Min. dias vencido", type: "number" },
    ],
    trigger_tag: [{ key: "tag_id", label: "Tag específica", type: "many2one", model: "res.partner.category" }],
};

// ════════════════════════════════════════════════════════════════
//  FLOW DESIGNER COMPONENT
// ════════════════════════════════════════════════════════════════

export class FlowDesigner extends Component {
    static template = "bader_inbox.FlowDesigner";

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.canvasRef = useRef("canvas");

        this.state = useState({
            nodes: [],
            edges: [],
            selectedNode: null,
            connectingFrom: null,
            connectingOutput: null,
            dragNode: null,
            dragOffset: { x: 0, y: 0 },
            pan: { x: 0, y: 0 },
            zoom: 1.0,
            isPanning: false,
            panStart: { x: 0, y: 0 },
            showPalette: true,
            paletteSearch: "",
            configPanel: null,
            flowId: null,
            campaignId: null,
            campaignName: "",
            isSaving: false,
            isDirty: false,
            mousePos: { x: 0, y: 0 },
        });

        this.nodeIdCounter = 0;

        onMounted(() => {
            this._loadFlow();
            this._setupKeyboard();
        });

        onWillUnmount(() => {
            this._removeKeyboard();
        });
    }

    // ── Data Loading ──────────────────────────────────────────

    async _loadFlow() {
        const params = this.props.action?.params || {};
        this.state.flowId = params.flow_id;
        this.state.campaignId = params.campaign_id;

        if (!this.state.flowId) return;

        const flow = await this.orm.read("bader.inbox.campaign.flow", [this.state.flowId], ["name", "canvas_data"]);
        if (flow.length) {
            this.state.campaignName = flow[0].name;
            if (flow[0].canvas_data) {
                try {
                    const data = JSON.parse(flow[0].canvas_data);
                    this.state.nodes = data.nodes || [];
                    this.state.edges = data.edges || [];
                    this.state.pan = data.pan || { x: 0, y: 0 };
                    this.state.zoom = data.zoom || 1.0;
                    this.nodeIdCounter = Math.max(0, ...this.state.nodes.map(n => parseInt(n.id.replace("node_", "")) || 0)) + 1;
                } catch (e) {
                    console.error("Failed to load canvas data", e);
                }
            }
        }
    }

    async saveFlow() {
        if (!this.state.flowId) return;

        this.state.isSaving = true;
        try {
            const canvasData = JSON.stringify({
                nodes: this.state.nodes,
                edges: this.state.edges,
                pan: this.state.pan,
                zoom: this.state.zoom,
            });

            await this.orm.write("bader.inbox.campaign.flow", [this.state.flowId], {
                canvas_data: canvasData,
            });

            // Also sync nodes/edges to Odoo models
            await this.orm.call("bader.inbox.campaign.flow", "action_save_from_canvas", [[this.state.flowId]], {
                canvas_json: canvasData,
            });

            this.state.isDirty = false;
            this.notification.add("✅ Fluxo guardado!", { type: "success" });
        } catch (e) {
            this.notification.add(`❌ Erro ao guardar: ${e.message}`, { type: "danger" });
        }
        this.state.isSaving = false;
    }

    // ── Node Management ──────────────────────────────────────

    addNode(subtype, x, y) {
        const def = NODE_TYPES[subtype];
        if (!def) return;

        const id = `node_${this.nodeIdCounter++}`;
        const node = {
            id,
            subtype,
            label: def.label,
            icon: def.icon,
            color: def.color,
            category: def.category,
            x: (x - this.state.pan.x) / this.state.zoom,
            y: (y - this.state.pan.y) / this.state.zoom,
            width: 200,
            height: 60,
            config: {},
            outputs: def.category === "condition" ? ["true", "false"] : ["default"],
        };

        this.state.nodes.push(node);
        this.state.isDirty = true;
        return node;
    }

    deleteNode(nodeId) {
        this.state.nodes = this.state.nodes.filter(n => n.id !== nodeId);
        this.state.edges = this.state.edges.filter(e => e.source !== nodeId && e.target !== nodeId);
        if (this.state.selectedNode === nodeId) {
            this.state.selectedNode = null;
            this.state.configPanel = null;
        }
        this.state.isDirty = true;
    }

    duplicateNode(nodeId) {
        const orig = this.state.nodes.find(n => n.id === nodeId);
        if (!orig) return;

        const newNode = { ...orig };
        newNode.id = `node_${this.nodeIdCounter++}`;
        newNode.x = orig.x + 30;
        newNode.y = orig.y + 30;
        newNode.config = { ...orig.config };
        this.state.nodes.push(newNode);
        this.state.isDirty = true;
    }

    getSelectedNode() {
        return this.state.nodes.find(n => n.id === this.state.selectedNode);
    }

    // ── Edge Management ──────────────────────────────────────

    startConnection(nodeId, output) {
        this.state.connectingFrom = nodeId;
        this.state.connectingOutput = output || "default";
    }

    completeConnection(targetId) {
        if (!this.state.connectingFrom || this.state.connectingFrom === targetId) {
            this.state.connectingFrom = null;
            return;
        }

        const existing = this.state.edges.find(
            e => e.source === this.state.connectingFrom && e.target === targetId && e.output === this.state.connectingOutput
        );
        if (existing) {
            this.state.connectingFrom = null;
            return;
        }

        this.state.edges.push({
            id: `edge_${Date.now()}`,
            source: this.state.connectingFrom,
            target: targetId,
            output: this.state.connectingOutput,
        });

        this.state.connectingFrom = null;
        this.state.connectingOutput = null;
        this.state.isDirty = true;
    }

    deleteEdge(edgeId) {
        this.state.edges = this.state.edges.filter(e => e.id !== edgeId);
        this.state.isDirty = true;
    }

    // ── Canvas Interaction ───────────────────────────────────

    onCanvasMouseDown(ev) {
        if (ev.target === this.canvasRef.el || ev.target.classList.contains("fd-canvas-bg")) {
            this.state.isPanning = true;
            this.state.panStart = { x: ev.clientX - this.state.pan.x, y: ev.clientY - this.state.pan.y };
            this.state.selectedNode = null;
            this.state.configPanel = null;
        }
    }

    onCanvasMouseMove(ev) {
        this.state.mousePos = { x: ev.clientX, y: ev.clientY };

        if (this.state.isPanning) {
            this.state.pan.x = ev.clientX - this.state.panStart.x;
            this.state.pan.y = ev.clientY - this.state.panStart.y;
        }

        if (this.state.dragNode) {
            const node = this.state.nodes.find(n => n.id === this.state.dragNode);
            if (node) {
                node.x = (ev.clientX - this.state.dragOffset.x - this.state.pan.x) / this.state.zoom;
                node.y = (ev.clientY - this.state.dragOffset.y - this.state.pan.y) / this.state.zoom;
            }
        }
    }

    onCanvasMouseUp(ev) {
        if (this.state.isPanning) {
            this.state.isPanning = false;
        }
        if (this.state.dragNode) {
            this.state.dragNode = null;
            this.state.isDirty = true;
        }
    }

    onCanvasWheel(ev) {
        ev.preventDefault();
        const factor = ev.deltaY > 0 ? 0.9 : 1.1;
        const newZoom = Math.max(0.2, Math.min(3, this.state.zoom * factor));
        this.state.zoom = newZoom;
    }

    // ── Node Drag ────────────────────────────────────────────

    onNodeMouseDown(ev, nodeId) {
        ev.stopPropagation();
        this.state.dragNode = nodeId;
        const node = this.state.nodes.find(n => n.id === nodeId);
        if (node) {
            this.state.dragOffset = {
                x: ev.clientX - node.x * this.state.zoom - this.state.pan.x,
                y: ev.clientY - node.y * this.state.zoom - this.state.pan.y,
            };
        }
        this.state.selectedNode = nodeId;
    }

    onNodeClick(ev, nodeId) {
        ev.stopPropagation();
        this.state.selectedNode = nodeId;
    }

    onNodeDblClick(ev, nodeId) {
        ev.stopPropagation();
        this.state.selectedNode = nodeId;
        this.state.configPanel = nodeId;
    }

    // ── Port Interaction ─────────────────────────────────────

    onOutputClick(ev, nodeId, output) {
        ev.stopPropagation();
        if (this.state.connectingFrom) {
            this.completeConnection(nodeId);
        } else {
            this.startConnection(nodeId, output);
        }
    }

    onInputClick(ev, nodeId) {
        ev.stopPropagation();
        if (this.state.connectingFrom) {
            this.completeConnection(nodeId);
        }
    }

    // ── Palette ──────────────────────────────────────────────

    get filteredNodeTypes() {
        const search = this.state.paletteSearch.toLowerCase();
        const result = {};
        for (const cat of CATEGORIES) {
            const items = Object.entries(NODE_TYPES)
                .filter(([key, def]) => def.category === cat.key)
                .filter(([key, def]) => !search || def.label.toLowerCase().includes(search) || key.includes(search));
            if (items.length) {
                result[cat.key] = { ...cat, items };
            }
        }
        return result;
    }

    onPaletteDragStart(ev, subtype) {
        ev.dataTransfer.setData("nodeType", subtype);
    }

    onCanvasDrop(ev) {
        ev.preventDefault();
        const subtype = ev.dataTransfer.getData("nodeType");
        if (subtype) {
            const rect = this.canvasRef.el.getBoundingClientRect();
            this.addNode(subtype, ev.clientX - rect.left, ev.clientY - rect.top);
        }
    }

    onCanvasDragOver(ev) {
        ev.preventDefault();
    }

    // ── Edge Path Calculation ────────────────────────────────

    getEdgePath(edge) {
        const src = this.state.nodes.find(n => n.id === edge.source);
        const tgt = this.state.nodes.find(n => n.id === edge.target);
        if (!src || !tgt) return "";

        const srcOutputs = src.outputs || ["default"];
        const outputIdx = srcOutputs.indexOf(edge.output || "default");
        const spacing = src.width / (srcOutputs.length + 1);

        const sx = src.x + spacing * (outputIdx + 1);
        const sy = src.y + src.height;
        const tx = tgt.x + tgt.width / 2;
        const ty = tgt.y;

        const dy = Math.abs(ty - sy);
        const cp = Math.max(50, dy * 0.5);

        return `M ${sx} ${sy} C ${sx} ${sy + cp}, ${tx} ${ty - cp}, ${tx} ${ty}`;
    }

    getEdgeColor(edge) {
        if (edge.output === "true") return "#10B981";
        if (edge.output === "false") return "#EF4444";
        return "#6B7280";
    }

    // ── Config Panel ─────────────────────────────────────────

    getConfigFields() {
        const node = this.state.nodes.find(n => n.id === this.state.configPanel);
        if (!node) return [];
        return NODE_CONFIG_FIELDS[node.subtype] || [];
    }

    onConfigChange(ev, fieldKey) {
        const node = this.state.nodes.find(n => n.id === this.state.configPanel);
        if (node) {
            node.config[fieldKey] = ev.target.value;
            this.state.isDirty = true;
        }
    }

    closeConfig() {
        this.state.configPanel = null;
    }

    // ── Toolbar Actions ──────────────────────────────────────

    goBack() {
        if (this.state.campaignId) {
            this.action.doAction({
                type: "ir.actions.act_window",
                res_model: "bader.inbox.campaign",
                res_id: this.state.campaignId,
                views: [[false, "form"]],
            });
        }
    }

    zoomIn() {
        this.state.zoom = Math.min(3, this.state.zoom * 1.2);
    }

    zoomOut() {
        this.state.zoom = Math.max(0.2, this.state.zoom / 1.2);
    }

    zoomReset() {
        this.state.zoom = 1.0;
        this.state.pan = { x: 0, y: 0 };
    }

    togglePalette() {
        this.state.showPalette = !this.state.showPalette;
    }

    deleteSelected() {
        if (this.state.selectedNode) {
            this.deleteNode(this.state.selectedNode);
        }
    }

    // ── Keyboard ─────────────────────────────────────────────

    _setupKeyboard() {
        this._keyHandler = (ev) => {
            if (ev.key === "Delete" && this.state.selectedNode) {
                this.deleteNode(this.state.selectedNode);
            }
            if (ev.key === "Escape") {
                this.state.connectingFrom = null;
                this.state.selectedNode = null;
                this.state.configPanel = null;
            }
            if (ev.ctrlKey && ev.key === "s") {
                ev.preventDefault();
                this.saveFlow();
            }
            if (ev.ctrlKey && ev.key === "d" && this.state.selectedNode) {
                ev.preventDefault();
                this.duplicateNode(this.state.selectedNode);
            }
        };
        document.addEventListener("keydown", this._keyHandler);
    }

    _removeKeyboard() {
        if (this._keyHandler) {
            document.removeEventListener("keydown", this._keyHandler);
        }
    }

    // ── Computed for template ────────────────────────────────

    get nodeTypes() {
        return NODE_TYPES;
    }

    get categories() {
        return CATEGORIES;
    }

    get connectingLine() {
        if (!this.state.connectingFrom) return null;
        const src = this.state.nodes.find(n => n.id === this.state.connectingFrom);
        if (!src) return null;

        const canvasRect = this.canvasRef.el?.getBoundingClientRect();
        if (!canvasRect) return null;

        const sx = src.x + src.width / 2;
        const sy = src.y + src.height;
        const mx = (this.state.mousePos.x - canvasRect.left - this.state.pan.x) / this.state.zoom;
        const my = (this.state.mousePos.y - canvasRect.top - this.state.pan.y) / this.state.zoom;

        return `M ${sx} ${sy} L ${mx} ${my}`;
    }
}

FlowDesigner.props = { action: { type: Object, optional: true } };

registry.category("actions").add("bader_inbox_flow_designer", FlowDesigner);
