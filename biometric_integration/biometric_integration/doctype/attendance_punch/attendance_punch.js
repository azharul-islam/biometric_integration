// Copyright (c) 2026, Khaled Bin Amir
// SPDX-License-Identifier: MIT

frappe.ui.form.on("Attendance Punch", {
	refresh(frm) {
		if (frm.is_new() || frm.doc.status === "Ingested") return;

		frm.add_custom_button(__("Retry"), () => {
			frappe.call({
				method: "biometric_integration.api.retry_punches",
				args: { names: [frm.doc.name] },
				freeze: true,
				callback: () => frm.reload_doc(),
			});
		});

		if (frm.doc.status === "Unmapped") {
			frm.add_custom_button(__("Map PIN & Replay"), () => {
				const d = new frappe.ui.Dialog({
					title: __("Map PIN {0} to an Employee", [frm.doc.pin]),
					fields: [
						{
							fieldname: "employee",
							fieldtype: "Link",
							options: "Employee",
							label: __("Employee"),
							reqd: 1,
							get_query: () => ({
								filters: { status: "Active", attendance_device_id: ["in", ["", null]] },
							}),
						},
					],
					primary_action_label: __("Map & Replay"),
					primary_action(values) {
						frappe.call({
							method: "biometric_integration.api.map_pin_and_replay",
							args: { pin: frm.doc.pin, employee: values.employee },
							freeze: true,
							callback(r) {
								d.hide();
								frappe.show_alert({
									message: __("Replayed {0} punch(es)", [r.message.replayed]),
									indicator: "green",
								});
								frm.reload_doc();
							},
						});
					},
				});
				d.show();
			});
		}
	},
});
