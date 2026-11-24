# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

"""Keep the two sides of a PIN ↔ Employee mapping in sync.

The check-in hot path looks up ``Employee.attendance_device_id``. The
``Attendance Device User`` record is the device-side view of the same PIN.
Historically the two had to be edited separately; these helpers let either
form be the single place a person is mapped.
"""

from __future__ import annotations

import frappe

from biometric_integration.utils.device_cache import invalidate_employee_pin


def _pin_of(employee: str | None) -> str | None:
    if not employee:
        return None
    return frappe.db.get_value("Employee", employee, "attendance_device_id")


def sync_employee_from_device_user(user_doc) -> None:
    """Write the Device User's PIN to its linked Employee (and clear old links).

    Called after an Attendance Device User is saved. Uses ``db.set_value`` for
    the Employee so this does not re-enter the Employee save hooks.
    """
    before = user_doc.get_doc_before_save()
    old_employee = before.employee if before else None
    old_pin = str(before.user_id).strip() if before and before.user_id else None
    pin = str(user_doc.user_id).strip()

    # The PIN was renamed on an existing record: move the employee's field too.
    if old_employee and old_pin and old_pin != pin and _pin_of(old_employee) == old_pin:
        frappe.db.set_value("Employee", old_employee, "attendance_device_id", pin)
        invalidate_employee_pin(old_pin)
        invalidate_employee_pin(pin)

    # The employee was unlinked or replaced: clear the old employee's field.
    if old_employee and old_employee != user_doc.employee and _pin_of(old_employee) == pin:
        frappe.db.set_value("Employee", old_employee, "attendance_device_id", None)
        invalidate_employee_pin(pin)

    if not user_doc.employee:
        return

    current = _pin_of(user_doc.employee)
    if current and str(current) != pin:
        frappe.throw(
            frappe._("Employee {0} already has Attendance Device ID {1}.").format(
                user_doc.employee, current
            )
        )
    if str(current or "") != pin:
        frappe.db.set_value("Employee", user_doc.employee, "attendance_device_id", pin)
        invalidate_employee_pin(pin)


def clear_employee_for_device_user(user_doc) -> None:
    """Unmap the Employee when its Device User is deleted."""
    if user_doc.employee and _pin_of(user_doc.employee) == str(user_doc.user_id):
        frappe.db.set_value("Employee", user_doc.employee, "attendance_device_id", None)
        invalidate_employee_pin(user_doc.user_id)


def sync_device_user_from_employee(employee_doc, old_pin=None, new_pin=None) -> None:
    """Link/clear the matching Device User when an Employee's PIN changes."""
    old_pin = str(old_pin or "").strip()
    new_pin = str(new_pin or "").strip()

    if old_pin and old_pin != new_pin:
        device_user = frappe.db.get_value(
            "Attendance Device User", {"user_id": old_pin}, "name"
        )
        if device_user and (
            frappe.db.get_value("Attendance Device User", device_user, "employee")
            == employee_doc.name
        ):
            frappe.db.set_value(
                "Attendance Device User",
                device_user,
                {
                    "employee": None,
                    "employee_name": None,
                    "display_name": (
                        frappe.db.get_value(
                            "Attendance Device User", device_user, "device_user_name"
                        )
                        or old_pin
                    ),
                },
            )

    if not new_pin:
        return

    device_user = frappe.db.get_value(
        "Attendance Device User", {"user_id": new_pin}, "name"
    )
    if not device_user:
        return

    linked = frappe.db.get_value("Attendance Device User", device_user, "employee")
    if linked and linked != employee_doc.name:
        frappe.throw(
            frappe._("PIN {0} is already linked to Employee {1}.").format(
                new_pin, linked
            )
        )
    if linked != employee_doc.name:
        frappe.db.set_value(
            "Attendance Device User",
            device_user,
            {
                "employee": employee_doc.name,
                "employee_name": employee_doc.employee_name,
                "display_name": (
                    frappe.db.get_value(
                        "Attendance Device User", device_user, "device_user_name"
                    )
                    or employee_doc.employee_name
                    or new_pin
                ),
            },
        )
