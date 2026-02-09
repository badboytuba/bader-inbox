/** @odoo-module **/

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, useState } from "@odoo/owl";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

/**
 * PhoneWhatsAppWidget - Extends phone field with WhatsApp integration
 * Shows WhatsApp icon on hover, clicking opens Bader Inbox with conversation
 */
export class PhoneWhatsAppWidget extends Component {
    setup() {
        this.actionService = useService("action");
        this.orm = useService("orm");
        this.notification = useService("notification");

        this.state = useState({
            showWhatsApp: false,
            loading: false,
        });
    }

    get phoneNumber() {
        return this.props.record.data[this.props.name] || "";
    }

    get formattedPhone() {
        // Clean phone number for display
        let phone = this.phoneNumber;
        if (!phone) return "";
        return phone;
    }

    get cleanPhone() {
        // Clean phone for WhatsApp (remove spaces, dashes, etc)
        let phone = this.phoneNumber;
        if (!phone) return "";
        return phone.replace(/[\s\-\(\)\.]/g, "").replace(/^\+/, "");
    }

    get hasPhone() {
        return !!this.phoneNumber && this.phoneNumber.trim().length > 0;
    }

    get partnerData() {
        // Get partner/contact data from the record
        const record = this.props.record;
        const data = record.data;

        return {
            partner_id: data.id || null,
            name: data.name || data.display_name || "",
            email: data.email || "",
            model: record.resModel,
        };
    }

    onMouseEnter() {
        if (this.hasPhone) {
            this.state.showWhatsApp = true;
        }
    }

    onMouseLeave() {
        this.state.showWhatsApp = false;
    }

    async onWhatsAppClick(ev) {
        ev.preventDefault();
        ev.stopPropagation();

        if (!this.hasPhone || this.state.loading) return;

        this.state.loading = true;

        try {
            const phone = this.cleanPhone;
            const partnerData = this.partnerData;

            // Call controller to find/create conversation
            const result = await this.orm.call(
                "bader.inbox.conversation",
                "open_or_create_by_phone",
                [phone],
                {
                    partner_id: partnerData.partner_id,
                    contact_name: partnerData.name,
                    model: partnerData.model,
                }
            );

            if (result && result.conversation_id) {
                // Open Bader Inbox with this conversation
                await this.actionService.doAction({
                    type: "ir.actions.client",
                    tag: "bader_inbox_main",
                    params: {
                        conversation_id: result.conversation_id,
                        phone: phone,
                    },
                });
            } else {
                // Fallback: just open inbox
                await this.actionService.doAction({
                    type: "ir.actions.client",
                    tag: "bader_inbox_main",
                });
            }

        } catch (error) {
            console.error("WhatsApp widget error:", error);
            this.notification.add("Erro ao abrir WhatsApp: " + error.message, {
                type: "danger"
            });
        } finally {
            this.state.loading = false;
        }
    }

    onPhoneClick(ev) {
        // Allow normal phone click behavior (call)
        if (this.hasPhone) {
            window.location.href = `tel:${this.phoneNumber}`;
        }
    }
}

PhoneWhatsAppWidget.template = "bader_inbox.PhoneWhatsAppWidget";
PhoneWhatsAppWidget.props = {
    ...standardFieldProps,
};

// Register the widget
registry.category("fields").add("phone_whatsapp", {
    component: PhoneWhatsAppWidget,
    supportedTypes: ["char"],
});
