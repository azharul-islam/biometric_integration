# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

"""Bulk onboarding for a new site/crew.

One paste creates (or links) the employees, assigns the next free PINs, links
each person to every selected device, and queues the device user creation. The
same page then tracks template capture and fan-out, so a two-terminal site can
be enrolled as: enrol each person on terminal A -> capture -> push to B.

This is deliberately a thin layer over the tested primitives:
``services.user_sync.ensure_on_devices`` for assignment and
``add_command`` for device work.
"""

from __future__ import annotations

import re

import frappe
from frappe.utils import cint, getdate, nowdate

from biometric_integration.biometric_integration.doctype.attendance_device_command.attendance_device_command import (
    add_command,
)
from biometric_integration.services.user_sync import SyncConflict, ensure_on_devices

MAX_ROWS = 300
_GENDER_ALIASES = {
    "m": "Male",
    "male": "Male",
    "f": "Female",
    "female": "Female",
    "o": "Other",
    "other": "Other",
}


# ---------------------------------------------------------------------------
# Context / parsing
# ---------------------------------------------------------------------------


def get_context() -> dict:
    devices = frappe.get_all(
        "Attendance Device",
        filters={"disabled": 0, "brand": "ZKTeco"},
        fields=[
            "name",
            "device_name",
            "company",
            "branch",
            "device_timezone",
            "last_contact",
            "disable_employee_sync",
            "max_users",
        ],
        order_by="device_name asc",
    )
    companies = frappe.get_all("Company", pluck="name", order_by="name asc")
    branches = frappe.get_all("Branch", pluck="name", order_by="name asc")
    return {
        "devices": devices,
        "companies": companies,
        "branches": branches,
        "next_pin": next_free_pin(),
        "today": nowdate(),
    }


def next_free_pin() -> int:
    used = set()
    for value in frappe.get_all("Employee", filters={"attendance_device_id": ["is", "set"]},
                                pluck="attendance_device_id"):
        if str(value).isdigit():
            used.add(int(value))
    for value in frappe.get_all("Attendance Device User", pluck="user_id"):
        if str(value).isdigit():
            used.add(int(value))
    candidate = 1
    while candidate in used:
        candidate += 1
    return candidate


def _split(row: str) -> list[str]:
    if "\t" in row:
        return [p.strip() for p in row.split("\t")]
    return [p.strip() for p in re.split(r"\s*,\s*", row)]


def parse_rows(raw: str) -> list[dict]:
    """Parse pasted rows: ``Name[, PIN[, Gender[, DOB]]]`` one per line.

    Tab-separated input is accepted too, which is what a spreadsheet paste
    produces. Blank lines and ``#`` comments are ignored.
    """
    rows = []
    for number, line in enumerate((raw or "").splitlines(), start=1):
        line = line.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        parts = _split(line)
        name = parts[0].strip() if parts else ""
        if not name:
            continue
        pin = parts[1].strip() if len(parts) > 1 else ""
        gender = parts[2].strip() if len(parts) > 2 else ""
        dob = parts[3].strip() if len(parts) > 3 else ""
        rows.append({"line": number, "name": name, "pin": pin, "gender": gender, "dob": dob})
        if len(rows) >= MAX_ROWS:
            break
    return rows


# ---------------------------------------------------------------------------
# Preview / provision
# ---------------------------------------------------------------------------


def _get_devices(device_names) -> dict:
    if isinstance(device_names, str):
        import json

        device_names = json.loads(device_names)
    names = [n for n in (device_names or []) if n]
    if not names:
        frappe.throw(frappe._("Select at least one device."))
    devices = frappe.get_all(
        "Attendance Device",
        filters={"name": ["in", names], "disabled": 0},
        fields=["name", "brand", "device_name", "company"],
    )
    if len(devices) != len(set(names)):
        frappe.throw(frappe._("One or more devices are missing or disabled."))
    return {d.name: d for d in devices}


def _normalise_gender(value: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    key = value.lower()
    if key in _GENDER_ALIASES:
        return _GENDER_ALIASES[key]
    # Accept the exact Gender record name too ("Male", "Female", "Other").
    if frappe.db.exists("Gender", value):
        return value
    return None


def _parse_dob(value: str):
    if not value:
        return None
    try:
        return getdate(value)
    except Exception:
        return None


def build_plan(raw: str, device_names, company: str, allow_incomplete: bool = False) -> dict:
    """Resolve each pasted row into an action without writing anything."""
    devices = _get_devices(device_names)
    rows = parse_rows(raw)
    if not rows:
        frappe.throw(frappe._("Paste at least one person."))
    if not company:
        frappe.throw(frappe._("Choose a company."))

    used_pins = set()
    for value in frappe.get_all("Employee", filters={"attendance_device_id": ["is", "set"]},
                                pluck="attendance_device_id"):
        if str(value).isdigit():
            used_pins.add(int(value))
    for value in frappe.get_all("Attendance Device User", pluck="user_id"):
        if str(value).isdigit():
            used_pins.add(int(value))

    plan = []
    seen_pins = set()
    seen_names = set()
    next_pin = max(used_pins | {0}) + 1

    for row in rows:
        name = row["name"]
        pin = row["pin"] or str(next_pin)
        gender = _normalise_gender(row["gender"])
        dob = _parse_dob(row["dob"])
        action = "create"
        message = ""
        employee = None
        employee_pin = None

        matches = frappe.get_all(
            "Employee",
            filters={"employee_name": name, "company": company},
            fields=["name", "attendance_device_id", "status"],
            limit_page_length=2,
        )
        existing = matches[0] if len(matches) == 1 else None
        if len(matches) > 1:
            action, message = "conflict", "More than one Employee has this name."
        elif existing:
            employee = existing.name
            employee_pin = str(existing.attendance_device_id or "").strip()
            if (existing.status or "Active") != "Active":
                action, message = "conflict", f"Employee is {existing.status}."
            elif employee_pin and row["pin"] and employee_pin != row["pin"]:
                action, message = (
                    "conflict",
                    f"Employee already has PIN {employee_pin}; remove the pasted PIN.",
                )
            else:
                action = "link"
                pin = employee_pin or pin

        if re.search(r"[,\t\r\n]", name):
            action, message = "conflict", "Name contains a separator."
        if not pin.isdigit():
            action, message = "conflict", "PIN must be numeric."
        elif pin in seen_pins:
            action, message = "conflict", f"PIN {pin} is repeated in this list."
        elif not employee and int(pin) in used_pins:
            action, message = "conflict", f"PIN {pin} is already assigned."
        elif action not in ("conflict",) and frappe.db.get_value(
            "Employee", {"attendance_device_id": pin}, "name"
        ) not in (None, employee):
            action, message = "conflict", f"PIN {pin} is already assigned."

        if name.lower() in seen_names:
            action, message = "conflict", "Name is repeated in this list."
        if action == "create" and not employee:
            if not gender:
                action, message = "needs_details", "Gender is required (M/F/Other)."
            elif not dob:
                action, message = "needs_details", "Date of birth is required (YYYY-MM-DD)."
            elif not allow_incomplete:
                # Both present, nothing to block; keep create.
                pass
        if action == "needs_details" and allow_incomplete:
            action, message = "create", ""

        seen_pins.add(pin)
        seen_names.add(name.lower())
        if pin.isdigit() and int(pin) >= next_pin:
            next_pin = int(pin) + 1

        plan.append(
            {
                **row,
                "pin": pin,
                "gender": gender,
                "dob": str(dob) if dob else "",
                "action": action,
                "employee": employee,
                "message": message,
            }
        )

    summary = {}
    for r in plan:
        summary[r["action"]] = summary.get(r["action"], 0) + 1
    return {
        "rows": plan,
        "summary": summary,
        "devices": [{"name": d.name, "label": d.device_name or d.name} for d in devices.values()],
    }


def provision(raw: str, device_names, company: str, date_of_joining: str | None = None,
              branch: str | None = None, allow_incomplete: bool = False) -> dict:
    """Create/link employees and put each on every selected device."""
    plan = build_plan(raw, device_names, company, allow_incomplete)
    devices = _get_devices(device_names)
    targets = {name: d.brand for name, d in devices.items()}
    joining = getdate(date_of_joining) if date_of_joining else getdate(nowdate())
    results = []

    for row in plan["rows"]:
        if row["action"] not in ("create", "link"):
            results.append({**row, "ok": False})
            continue
        frappe.db.savepoint("bi_provision_row")
        try:
            if row["action"] == "create":
                first, _, last = row["name"].partition(" ")
                emp = frappe.get_doc(
                    {
                        "doctype": "Employee",
                        "naming_series": "HR-EMP-",
                        "first_name": first.strip() or row["name"],
                        "last_name": last.strip() or None,
                        "company": company,
                        "branch": branch or None,
                        "gender": row["gender"] or None,
                        "date_of_birth": row["dob"] or None,
                        "date_of_joining": joining,
                        "status": "Active",
                        "attendance_device_id": row["pin"],
                    }
                )
                emp.insert(ignore_mandatory=allow_incomplete)
            else:
                emp = frappe.get_doc("Employee", row["employee"])
                if not str(emp.attendance_device_id or "").strip():
                    emp.attendance_device_id = row["pin"]
                    emp.save()
                if branch and not emp.branch:
                    emp.branch = branch
                    emp.save()

            added = ensure_on_devices(emp, targets=targets)
            frappe.db.commit()
            results.append(
                {
                    **row,
                    "ok": True,
                    "employee": emp.name,
                    "employee_name": emp.employee_name,
                    "devices_added": added,
                }
            )
        except SyncConflict as exc:
            frappe.db.rollback(save_point="bi_provision_row")
            results.append({**row, "ok": False, "message": str(exc)})
        except Exception as exc:
            frappe.db.rollback(save_point="bi_provision_row")
            frappe.log_error(
                title="Team provisioning row failed",
                message=f"Row {row['line']} {row['name']}\n{frappe.get_traceback()}",
            )
            results.append({**row, "ok": False, "message": str(exc)})

    return {
        "results": results,
        "provisioned": sum(1 for r in results if r.get("ok")),
        "failed": sum(1 for r in results if not r.get("ok")),
        "devices": [{"name": d.name, "label": d.device_name or d.name} for d in devices.values()],
    }


# ---------------------------------------------------------------------------
# Roster / template movement
# ---------------------------------------------------------------------------


def _assigned_users(device_names: list[str]) -> list[dict]:
    links = frappe.get_all(
        "Attendance Device Link",
        filters={"parenttype": "Attendance Device User", "attendance_device": ["in", device_names]},
        fields=["parent", "attendance_device", "enroll_data_source"],
    )
    by_user: dict[str, set[str]] = {}
    source: dict[str, str] = {}
    for link in links:
        by_user.setdefault(link.parent, set()).add(link.attendance_device)
        if link.enroll_data_source:
            source[link.parent] = link.attendance_device

    in_all = set(
        frappe.get_all(
            "Attendance Device User",
            filters={"allow_in_all_devices": 1},
            pluck="name",
        )
    )
    names = list(set(by_user) | in_all)
    if not names:
        return []
    users = frappe.get_all(
        "Attendance Device User",
        filters={"name": ["in", names], "user_id": ["!=", ""]},
        fields=[
            "name",
            "user_id",
            "display_name",
            "employee",
            "employee_name",
            "device_user_name",
            "zkteco_enroll_data",
            "allow_in_all_devices",
        ],
        limit_page_length=0,
    )

    out = []
    for user in users:
        devices = set(device_names) if user.allow_in_all_devices else by_user.get(user.name, set())
        if not devices & set(device_names):
            continue
        out.append(
            {
                "name": user.name,
                "pin": user.user_id,
                "display_name": user.display_name or user.device_user_name or user.employee_name or user.user_id,
                "employee": user.employee,
                "employee_name": user.employee_name,
                "has_template": bool(user.zkteco_enroll_data),
                "template_source": source.get(user.name),
                "devices": sorted(devices),
            }
        )
    return out


def _latest_commands(device_names: list[str], user_names: list[str]) -> dict:
    if not device_names or not user_names:
        return {}
    rows = frappe.get_all(
        "Attendance Device Command",
        filters={
            "attendance_device": ["in", device_names],
            "attendance_device_user": ["in", user_names],
            "command_type": ["in", ["Update User", "Enroll User", "Delete User"]],
        },
        fields=["attendance_device", "attendance_device_user", "command_type", "status", "creation"],
        order_by="creation desc",
        limit_page_length=0,
    )
    latest = {}
    for row in rows:
        key = (row.attendance_device_user, row.attendance_device, row.command_type)
        if key not in latest:
            latest[key] = row.status
    return latest


def _device_status(user: dict, device: str, latest: dict) -> dict:
    deleted = latest.get((user["name"], device, "Delete User"))
    enrolled = latest.get((user["name"], device, "Enroll User"))
    updated = latest.get((user["name"], device, "Update User"))
    if deleted == "Success":
        return {"label": "Removed", "color": "red"}
    if enrolled == "Success":
        return {"label": "Template pushed", "color": "green"}
    if enrolled in ("Pending", "Sent") or updated in ("Pending", "Sent"):
        return {"label": "Queued", "color": "orange"}
    if enrolled == "Failed" or updated == "Failed":
        return {"label": "Failed", "color": "red"}
    if updated == "Success":
        return {"label": "User created", "color": "blue"}
    return {"label": "—", "color": "grey"}


def get_roster(device_names) -> dict:
    devices = _get_devices(device_names)
    names = list(devices)
    users = _assigned_users(names)
    latest = _latest_commands(names, [u["name"] for u in users])
    for user in users:
        user["status"] = {
            d: _device_status(user, d, latest) for d in names
        }
    return {
        "devices": [{"name": d.name, "label": d.device_name or d.name} for d in devices.values()],
        "rows": users,
    }


def _add_device_command(device: str, command_type: str, user: str | None = None) -> str:
    brand = frappe.db.get_value("Attendance Device", device, "brand")
    if not brand:
        frappe.throw(frappe._("Device {0} not found.").format(device))
    cmd = frappe.new_doc("Attendance Device Command")
    cmd.attendance_device = device
    cmd.brand = brand
    cmd.command_type = command_type
    cmd.attendance_device_user = user
    cmd.status = "Pending"
    cmd.insert(ignore_permissions=True)
    return cmd.name


def capture_all(device: str) -> dict:
    """Ask the terminal to replay its whole user + template table (CHECK)."""
    name = _add_device_command(device, "Pull From Device")
    return {"command": name}


def capture_missing(device: str) -> dict:
    """Per-user Get Enroll Data for assigned users with no stored template."""
    from biometric_integration.services.command_processor import _uses_new_user_verb

    if _uses_new_user_verb(device):
        frappe.throw(
            frappe._(
                "This SpeedFace firmware rejects per-user template queries (-629). "
                "Enrol the person on the terminal — the template uploads automatically — "
                "then use Push to copy it to the other terminal."
            )
        )
    brand = frappe.db.get_value("Attendance Device", device, "brand")
    users = _assigned_users([device])
    queued = 0
    for user in users:
        if user["has_template"] or device not in user["devices"]:
            continue
        add_command(device, user["name"], brand, "Get Enroll Data")
        queued += 1
    return {"queued": queued}


def push_templates(device: str, only_missing: bool = True) -> dict:
    """Queue Enroll User for everyone with a template, to the target device."""
    brand, sync_off = frappe.db.get_value(
        "Attendance Device", device, ["brand", "disable_employee_sync"]
    )
    if sync_off:
        frappe.throw(
            frappe._(
                "Employee sync is disabled on {0}. Enable it (or use a normal "
                "attendance terminal) before pushing templates."
            ).format(device)
        )
    users = _assigned_users([device])
    latest = _latest_commands([device], [u["name"] for u in users])
    queued = skipped = 0
    for user in users:
        if not user["has_template"] or device not in user["devices"]:
            continue
        if only_missing and latest.get((user["name"], device, "Enroll User")) == "Success":
            skipped += 1
            continue
        add_command(device, user["name"], brand, "Enroll User")
        queued += 1
    return {"queued": queued, "skipped": skipped}
