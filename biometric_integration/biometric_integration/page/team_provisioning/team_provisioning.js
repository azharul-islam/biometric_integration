// Copyright (c) 2026, Khaled Bin Amir
// SPDX-License-Identifier: MIT
//
// Day-one onboarding: paste a crew, provision them on every selected terminal,
// then capture templates from one and push them to the other.

const TP_TEMPLATE = `
<div class="tp">
	<div class="row">
		<div class="col-md-4">
			<div class="tp-card">
				<h5>1 · Site devices</h5>
				<p class="text-muted small">Everyone below will be created on every selected terminal.</p>
				<div class="tp-devices"></div>
			</div>

			<div class="tp-card">
				<h5>2 · People</h5>
				<p class="text-muted small">
					One person per line: <b>Name, PIN, Gender, DOB</b>.
					PIN is optional (next free number is assigned), Gender is M/F/Other,
					DOB is YYYY-MM-DD. Paste straight from a spreadsheet.
				</p>
				<textarea class="form-control tp-rows" rows="10"
					placeholder="Ahmed Ali, , M, 1990-05-12&#10;Sara Khan, 21, F, 1994-11-03"></textarea>
				<div class="mt-2">
					<label class="small text-muted">Company</label>
					<select class="form-control tp-company"></select>
				</div>
				<div class="row mt-2">
					<div class="col-6">
						<label class="small text-muted">Joining date</label>
						<input type="date" class="form-control tp-joining">
					</div>
					<div class="col-6">
						<label class="small text-muted">Branch (optional)</label>
						<select class="form-control tp-branch"></select>
					</div>
				</div>
				<label class="d-flex align-items-center mt-2 small">
					<input type="checkbox" class="tp-incomplete mr-2">
					Create with minimum details (fill gender/DOB later)
				</label>
				<div class="mt-3">
					<button class="btn btn-default btn-sm tp-preview">Preview</button>
					<button class="btn btn-primary btn-sm tp-provision" disabled>Provision</button>
				</div>
			</div>
		</div>

		<div class="col-md-8">
			<div class="tp-card">
				<h5>3 · Preview</h5>
				<div class="tp-preview text-muted small">Nothing previewed yet.</div>
			</div>

			<div class="tp-card">
				<div class="d-flex align-items-center justify-content-between">
					<h5 class="mb-0">4 · Roster</h5>
					<button class="btn btn-xs btn-default tp-refresh">Refresh</button>
				</div>
				<p class="text-muted small mt-2 mb-2">
					<b>Capture</b> pulls templates off a terminal after you enrol people on it.
					<b>Push</b> copies stored templates to the other terminal.
				</p>
				<div class="tp-actions mb-3"></div>
				<div class="tp-roster text-muted small">Provision someone to see the roster.</div>
			</div>
		</div>
	</div>
</div>
<style>
	.tp-card { border: 1px solid var(--border-color); border-radius: 8px; padding: 14px; margin-bottom: 14px; }
	.tp-card h5 { margin-bottom: 8px; }
	.tp-badge { display: inline-block; padding: 1px 7px; border-radius: 10px; color: #fff; font-size: 11px; }
	.tp-table td, .tp-table th { vertical-align: middle !important; }
</style>
`;

frappe.pages["team-provisioning"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Team Provisioning"),
		single_column: true,
	});
	const tp = frappe.pages["team-provisioning"];
	tp.page = page;
	tp.ctx = null;
	tp.plan = null;

	page.set_primary_action(__("Refresh Roster"), () => tp.load_roster());
	page.main.html(TP_TEMPLATE);

	page.main.on("click", ".tp-preview", () => tp.preview());
	page.main.on("click", ".tp-provision", () => tp.provision());
	page.main.on("click", ".tp-refresh", () => tp.load_roster());
	page.main.on("change", ".tp-device", () => {
		tp.render_actions();
		tp.load_roster();
	});
	page.main.on("click", ".tp-capture-all", () =>
		tp.command("capture_device_templates", tp.pick_device(".tp-capture-device"))
	);
	page.main.on("click", ".tp-capture-missing", () =>
		tp.command("capture_missing_templates", tp.pick_device(".tp-capture-device"))
	);
	page.main.on("click", ".tp-push-missing", () =>
		tp.command("push_templates", tp.pick_device(".tp-push-device"), { only_missing: 1 })
	);
	page.main.on("click", ".tp-push-all", () =>
		tp.command("push_templates", tp.pick_device(".tp-push-device"), { only_missing: 0 })
	);

	tp.load_context();
};

frappe.pages["team-provisioning"].on_page_show = function () {
	const tp = frappe.pages["team-provisioning"];
	if (tp && tp.ctx) tp.load_roster();
};

Object.assign(frappe.pages["team-provisioning"], {
	load_context() {
		const tp = this;
		frappe.call("biometric_integration.api.get_provisioning_context").then((r) => {
			tp.ctx = r.message;
			tp.render_context();
		});
	},

	render_context() {
		const tp = this;
		const $m = tp.page.main;
		const ctx = tp.ctx;

		$m.find(".tp-devices").html(
			ctx.devices
				.map(
					(d, i) => `
				<label class="d-flex align-items-center small mb-1">
					<input type="checkbox" class="tp-device mr-2" value="${frappe.utils.escape_html(d.name)}" ${i < 2 ? "checked" : ""}>
					<span>${frappe.utils.escape_html(d.device_name || d.name)}
						<span class="text-muted">· ${frappe.utils.escape_html(d.company || "")}</span>
						${d.disable_employee_sync ? `<span class="text-danger">· sync off</span>` : ""}</span>
				</label>`
				)
				.join("") || `<div class="text-danger small">No enabled ZKTeco devices.</div>`
		);

		$m.find(".tp-company").html(
			ctx.companies.map((c) => `<option>${frappe.utils.escape_html(c)}</option>`).join("")
		);
		$m.find(".tp-branch").html(
			`<option value=""></option>` +
				ctx.branches.map((b) => `<option>${frappe.utils.escape_html(b)}</option>`).join("")
		);
		$m.find(".tp-joining").val(ctx.today);
		tp.render_actions();
		tp.load_roster();
	},

	render_actions() {
		const tp = this;
		const $m = tp.page.main;
		const devices = tp.selected_devices();
		if (!devices.length) {
			$m.find(".tp-actions").html(`<div class="text-muted small">Select at least one device.</div>`);
			return;
		}
		const opts = devices
			.map((d) => `<option value="${frappe.utils.escape_html(d)}">${frappe.utils.escape_html(d)}</option>`)
			.join("");
		$m.find(".tp-actions").html(`
			<div class="row">
				<div class="col-md-6 mb-2">
					<div class="small text-muted mb-1">Capture from</div>
					<div class="input-group input-group-sm">
						<select class="form-control tp-capture-device">${opts}</select>
						<div class="input-group-append">
							<button class="btn btn-default tp-capture-all" title="Ask the terminal to replay all users and templates">Bulk</button>
							<button class="btn btn-default tp-capture-missing" title="One query per user without a stored template">Missing</button>
						</div>
					</div>
				</div>
				<div class="col-md-6 mb-2">
					<div class="small text-muted mb-1">Push stored templates to</div>
					<div class="input-group input-group-sm">
						<select class="form-control tp-push-device">${opts}</select>
						<div class="input-group-append">
							<button class="btn btn-default tp-push-missing">Missing</button>
							<button class="btn btn-default tp-push-all">All</button>
						</div>
					</div>
				</div>
			</div>
		`);
		// Two-terminal flow: enrol on A, push to B.
		$m.find(".tp-push-device").val(devices[1] || devices[0]);
	},

	selected_devices() {
		return this.page.main
			.find(".tp-device:checked")
			.map((_, el) => el.value)
			.get();
	},

	pick_device(selector) {
		return this.page.main.find(selector).val();
	},

	preview() {
		const tp = this;
		const devices = tp.selected_devices();
		const raw = tp.page.main.find(".tp-rows").val();
		if (!devices.length) return frappe.msgprint(__("Select at least one device."));
		if (!raw.trim()) return frappe.msgprint(__("Paste at least one person."));

		frappe.call({
			method: "biometric_integration.api.preview_provisioning",
			args: {
				raw,
				devices,
				company: tp.page.main.find(".tp-company").val(),
				allow_incomplete: tp.page.main.find(".tp-incomplete").is(":checked") ? 1 : 0,
			},
			freeze: true,
			callback(r) {
				tp.plan = r.message;
				tp.render_plan();
			},
		});
	},

	render_plan() {
		const tp = this;
		const plan = tp.plan;
		const colors = { create: "#28a745", link: "#2490ef", conflict: "#dc3545", needs_details: "#fd7e14" };
		const rows = plan.rows
			.map(
				(r) => `
			<tr>
				<td>${r.line}</td>
				<td>${frappe.utils.escape_html(r.name)}</td>
				<td>${frappe.utils.escape_html(r.pin)}</td>
				<td><span class="tp-badge" style="background:${colors[r.action] || "#8d99a6"}">${r.action}</span></td>
				<td>${frappe.utils.escape_html(r.employee || "")}</td>
				<td class="small text-muted">${frappe.utils.escape_html(r.message || "")}</td>
			</tr>`
			)
			.join("");
		tp.page.main.find(".tp-preview").html(`
			<div class="small mb-2">${Object.entries(plan.summary)
				.map(([k, v]) => `<b>${v}</b> ${k.replace("_", " ")}`)
				.join(" · ")}</div>
			<div class="table-responsive"><table class="table table-sm tp-table">
				<thead><tr><th>#</th><th>Name</th><th>PIN</th><th>Action</th><th>Employee</th><th></th></tr></thead>
				<tbody>${rows}</tbody>
			</table></div>`);

		const ready = (plan.summary.create || 0) + (plan.summary.link || 0);
		tp.page.main.find(".tp-provision").prop("disabled", !ready);
	},

	provision() {
		const tp = this;
		const plan = tp.plan;
		if (!plan) return;
		const ready = (plan.summary.create || 0) + (plan.summary.link || 0);
		frappe.confirm(
			__("Create/link {0} people on {1} device(s)?", [ready, tp.selected_devices().length]),
			() => {
				frappe.call({
					method: "biometric_integration.api.provision_team",
					args: {
						raw: tp.page.main.find(".tp-rows").val(),
						devices: tp.selected_devices(),
						company: tp.page.main.find(".tp-company").val(),
						date_of_joining: tp.page.main.find(".tp-joining").val(),
						branch: tp.page.main.find(".tp-branch").val() || null,
						allow_incomplete: tp.page.main.find(".tp-incomplete").is(":checked") ? 1 : 0,
					},
					freeze: true,
					callback(r) {
						const res = r.message;
						frappe.show_alert({
							message: __("{0} provisioned, {1} failed", [res.provisioned, res.failed]),
							indicator: res.failed ? "orange" : "green",
						});
						tp.render_result(res);
						tp.load_roster();
					},
				});
			}
		);
	},

	render_result(res) {
		const tp = this;
		const rows = res.results
			.map(
				(r) => `
			<tr class="${r.ok ? "" : "table-danger"}">
				<td>${r.line}</td>
				<td>${frappe.utils.escape_html(r.name)}</td>
				<td>${frappe.utils.escape_html(r.pin || "")}</td>
				<td>${r.ok ? frappe.utils.escape_html(r.employee || "") : ""}</td>
				<td class="small">${frappe.utils.escape_html(r.message || (r.ok ? __("On {0} device(s)", [(r.devices_added || []).length]) : ""))}</td>
			</tr>`
			)
			.join("");
		tp.page.main.find(".tp-preview").html(`
			<div class="table-responsive"><table class="table table-sm tp-table">
				<thead><tr><th>#</th><th>Name</th><th>PIN</th><th>Employee</th><th>Result</th></tr></thead>
				<tbody>${rows}</tbody>
			</table></div>`);
	},

	load_roster() {
		const tp = this;
		const devices = tp.selected_devices();
		if (!devices.length || !tp.ctx) return;
		frappe.call({
			method: "biometric_integration.api.get_provisioning_roster",
			args: { devices },
			callback(r) {
				tp.roster = r.message;
				tp.render_roster();
			},
		});
	},

	render_roster() {
		const tp = this;
		const roster = tp.roster;
		if (!roster) return;
		if (!roster.rows.length) {
			tp.page.main.find(".tp-roster").html(`<div class="text-muted small">No one assigned yet.</div>`);
			return;
		}
		const colors = { green: "#28a745", red: "#dc3545", orange: "#fd7e14", blue: "#2490ef", grey: "#8d99a6" };
		const head = roster.devices
			.map((d) => `<th>${frappe.utils.escape_html(d.label)}</th>`)
			.join("");
		const body = roster.rows
			.map((u) => {
				const cells = roster.devices
					.map((d) => {
						const s = (u.status && u.status[d.name]) || { label: "—", color: "grey" };
						return `<td><span class="tp-badge" style="background:${colors[s.color] || "#8d99a6"}">${s.label}</span></td>`;
					})
					.join("");
				const template = u.has_template
					? `<span class="text-success">${__("Yes")}</span> <span class="text-muted small">${frappe.utils.escape_html(u.template_source || "")}</span>`
					: `<span class="text-muted">${__("No")}</span>`;
				return `<tr>
					<td>${frappe.utils.escape_html(u.display_name || u.pin)}</td>
					<td>${frappe.utils.escape_html(u.pin)}</td>
					<td>${template}</td>
					${cells}
				</tr>`;
			})
			.join("");
		tp.page.main.find(".tp-roster").html(`
			<div class="table-responsive"><table class="table table-sm tp-table">
				<thead><tr><th>Person</th><th>PIN</th><th>Template</th>${head}</tr></thead>
				<tbody>${body}</tbody>
			</table></div>`);
	},

	command(method, device, extra) {
		const tp = this;
		if (!device) return frappe.msgprint(__("Choose a device first."));
		frappe.call({
			method: "biometric_integration.api." + method,
			args: Object.assign({ device }, extra || {}),
			freeze: true,
			callback(r) {
				const msg = r.message || {};
				frappe.show_alert({
					message: Object.entries(msg)
						.map(([k, v]) => `${k}: ${v}`)
						.join(" · "),
					indicator: "green",
				});
				setTimeout(() => tp.load_roster(), 3000);
			},
		});
	},
});
