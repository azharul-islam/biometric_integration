# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

"""Durable inbox for punches that did not become Employee Checkins.

Devices are told ``OK`` and move their upload cursor forward even when a punch
cannot be attributed (unknown PIN) or the insert fails (deadlock, validation,
deleted Employee). Those punches used to exist only in an Error Log. This module
keeps them as an ``Attendance Punch`` row so HR can map the PIN and replay, or
a scheduled retry can clear a transient failure.

Only exceptions are stored: a punch that becomes an Employee Checkin is not
duplicated here, so this table grows with problems, not with headcount.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

import frappe
from frappe.utils import add_days, now_datetime

from biometric_integration.services.checkin import (
    RESULT_DUPLICATE,
    RESULT_FAILED,
    RESULT_STORED,
    RESULT_UNMAPPED,
    create_employee_checkin,
)
from biometric_integration.utils.device_cache import invalidate_employee_pin

MAX_RETRY_ATTEMPTS = 10
RETRY_WINDOW_DAYS = 14
KEEP_INGESTED_DAYS = 30


def _dedupe_key(device_sn: str, pin: str, raw_time: str) -> str:
    """Stable across re-sends: the device's own wall-clock string, not the
    server-localised value (which changes if device_timezone is corrected)."""
    return hashlib.sha1(f"{device_sn}|{pin}|{raw_time}".encode("utf-8")).hexdigest()


def record_unprocessed_punch(
    *,
    device_sn: str,
    pin: str,
    punch_time: datetime,
    raw_time: str,
    log_type: str | None = None,
    biometric_method: str | None = None,
    raw_line: str | None = None,
    outcome: str = RESULT_FAILED,
) -> None:
    """Store a punch that did not become an Employee Checkin. Never raises."""
    if not device_sn or not pin:
        return
    key = _dedupe_key(device_sn, str(pin), str(raw_time))
    try:
        if frappe.db.exists("Attendance Punch", key):
            return
        device = device_sn if frappe.db.exists("Attendance Device", device_sn) else None
        frappe.get_doc(
            {
                "doctype": "Attendance Punch",
                "dedupe_key": key,
                "attendance_device": device,
                "device_serial": device_sn,
                "pin": str(pin),
                "punch_time": punch_time,
                "log_type": log_type,
                "biometric_method": biometric_method,
                "status": "Unmapped" if outcome == RESULT_UNMAPPED else "Failed",
                "raw_line": (raw_line or "")[:60000],
            }
        ).insert(ignore_permissions=True)
    except Exception:
        # The inbox must never break device communication; the Error Log keeps
        # the traceback if the row could not be written.
        frappe.log_error(
            title="Attendance Punch inbox write failed",
            message=f"SN={device_sn} PIN={pin} time={raw_time}\n{frappe.get_traceback()}",
        )


def retry_punch(name: str) -> str:
    """Re-run one inbox punch. Returns its new status."""
    doc = frappe.get_doc("Attendance Punch", name)
    if doc.status == "Ingested":
        return doc.status

    outcome = create_employee_checkin(
        device_pin=doc.pin,
        timestamp=doc.punch_time,
        # Only a real Attendance Device can be linked; a serial whose device
        # record was deleted would fail link validation on the Checkin.
        device_id=doc.attendance_device or None,
        log_type=doc.log_type,
        biometric_method=doc.biometric_method,
    )
    doc.attempts = (doc.attempts or 0) + 1
    doc.last_attempt = now_datetime()

    if outcome in (RESULT_STORED, RESULT_DUPLICATE):
        doc.status = "Ingested"
        doc.error_message = None
        doc.employee = frappe.db.get_value(
            "Employee", {"attendance_device_id": str(doc.pin)}, "name"
        ) or doc.employee
        if doc.employee:
            doc.employee_checkin = frappe.db.get_value(
                "Employee Checkin", {"employee": doc.employee, "time": doc.punch_time}, "name"
            )
    elif outcome == RESULT_UNMAPPED:
        doc.status = "Unmapped"
        doc.employee = None
        doc.error_message = None
    else:
        doc.status = "Failed"
        doc.error_message = "Insert failed — see Error Log for the traceback."

    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return doc.status


def map_pin_and_replay(pin: str, employee: str) -> dict:
    """Map a device PIN to an Employee, then replay its Unmapped punches.

    Saving the Employee (rather than db.set_value) runs the normal mapping
    hooks, so the matching Attendance Device User is linked and the PIN cache is
    invalidated before any punch is retried.
    """
    pin = str(pin).strip()
    if not pin:
        frappe.throw(frappe._("PIN is required."))

    emp = frappe.get_doc("Employee", employee)
    if (emp.status or "Active") != "Active":
        frappe.throw(frappe._("Employee {0} is not Active.").format(employee))

    other = frappe.db.get_value("Employee", {"attendance_device_id": pin}, "name")
    if other and other != employee:
        frappe.throw(
            frappe._("PIN {0} is already assigned to Employee {1}.").format(pin, other)
        )
    own = str(emp.attendance_device_id or "").strip()
    if own and own != pin:
        frappe.throw(
            frappe._("Employee {0} already has Attendance Device ID {1}.").format(
                employee, own
            )
        )

    if own != pin:
        emp.attendance_device_id = pin
        emp.save()
        invalidate_employee_pin(pin)

    names = frappe.get_all(
        "Attendance Punch",
        filters={"pin": pin, "status": "Unmapped"},
        pluck="name",
        order_by="punch_time asc",
    )
    results = {}
    for name in names:
        results[name] = retry_punch(name)
    return {
        "pin": pin,
        "employee": employee,
        "replayed": len(names),
        "results": results,
    }


def retry_failed_punches() -> None:
    """Scheduled: retry transient failures with a bounded attempt count."""
    cutoff = add_days(now_datetime(), -RETRY_WINDOW_DAYS)
    names = frappe.get_all(
        "Attendance Punch",
        filters={
            "status": "Failed",
            "attempts": ["<", MAX_RETRY_ATTEMPTS],
            "punch_time": [">=", cutoff],
        },
        pluck="name",
        order_by="punch_time asc",
        limit_page_length=200,
    )
    for name in names:
        try:
            retry_punch(name)
        except Exception:
            frappe.log_error(
                title="Attendance Punch retry failed",
                message=f"Punch={name}\n{frappe.get_traceback()}",
            )


def purge_ingested_punches() -> None:
    """Scheduled: resolved punches are only kept as a short audit trail."""
    cutoff = add_days(now_datetime(), -KEEP_INGESTED_DAYS)
    frappe.db.delete(
        "Attendance Punch",
        {"status": "Ingested", "punch_time": ["<", cutoff]},
    )
    frappe.db.commit()
