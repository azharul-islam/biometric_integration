// Copyright (c) 2026, Khaled Bin Amir
// SPDX-License-Identifier: MIT

frappe.listview_settings["Attendance Punch"] = {
	add_fields: ["status", "employee", "pin"],
	get_indicator(doc) {
		const map = {
			Unmapped: ["Unmapped", "orange", "status,=,Unmapped"],
			Failed: ["Failed", "red", "status,=,Failed"],
			Ingested: ["Ingested", "green", "status,=,Ingested"],
			Ignored: ["Ignored", "grey", "status,=,Ignored"],
		};
		return map[doc.status] || [__(doc.status), "grey", "status,=," + doc.status];
	},
	onload(listview) {
		listview.page.add_action_item(__("Retry Selected"), () => {
			const names = listview.get_checked_items().map((d) => d.name);
			if (!names.length) return;
			frappe.call({
				method: "biometric_integration.api.retry_punches",
				args: { names },
				freeze: true,
				callback(r) {
					const done = Object.values(r.message || {}).filter((s) => s === "Ingested").length;
					frappe.show_alert({
						message: __("{0} of {1} punch(es) ingested", [done, names.length]),
						indicator: done ? "green" : "orange",
					});
					listview.refresh();
				},
			});
		});

		listview.page.add_action_item(__("Map PIN & Replay"), () => {
			const rows = listview.get_checked_items().filter((d) => d.status === "Unmapped");
			if (rows.length !== 1) {
				frappe.msgprint(__("Select exactly one Unmapped punch."));
				return;
			}
			const d = new frappe.ui.Dialog({
				title: __("Map PIN {0} to an Employee", [rows[0].pin]),
				fields: [
					{
						fieldname: "employee",
						fieldtype: "Link",
						options: "Employee",
						label: __("Employee"),
						reqd: 1,
						get_query: () => ({ filters: { status: "Active" } }),
					},
				],
				primary_action_label: __("Map & Replay"),
				primary_action(values) {
					frappe.call({
						method: "biometric_integration.api.map_pin_and_replay",
						args: { pin: rows[0].pin, employee: values.employee },
						freeze: true,
						callback(r) {
							d.hide();
							frappe.show_alert({
								message: __("Replayed {0} punch(es)", [r.message.replayed]),
								indicator: "green",
							});
							listview.refresh();
						},
					});
				},
			});
			d.show();
		});
	},
};
