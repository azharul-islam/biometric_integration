// Copyright (c) 2026, Khaled Bin Amir
// SPDX-License-Identifier: MIT
//
// A terminal that stops polling looks identical to one that is simply idle, so
// the state the health job computes has to be visible without opening a form.
// The indicator is driven by the stored health_status rather than recomputed
// here: the list must not do per-row work, and an operator looking at it should
// see what the last scheduled check said, with its timestamp on the form.

frappe.listview_settings["Attendance Device"] = {
	add_fields: ["health_status", "last_contact", "last_health_check", "disabled"],
	get_indicator(doc) {
		if (doc.disabled) {
			return [__("Disabled"), "grey", "disabled,=,1"];
		}
		const indicators = {
			Healthy: [__("Healthy"), "green", "health_status,=,Healthy"],
			Warning: [__("Warning"), "orange", "health_status,=,Warning"],
			Offline: [__("Offline"), "red", "health_status,=,Offline"],
			Unknown: [__("Unknown"), "gray", "health_status,=,Unknown"],
		};
		return indicators[doc.health_status] || indicators.Unknown;
	},
};
