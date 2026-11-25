# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

"""Fleet health: is every terminal alive, on time, and answering?

A terminal that stops polling is nearly invisible. It keeps accepting punches
locally and uploads them when it comes back, so attendance looks *late* rather
than *broken* — the pilot went quiet for over an hour and nobody noticed until a
punch was inspected by hand.

Everything here is derived from state we already have; the only new thing is the
edge trigger. One notice when a device goes quiet, one when it returns. Never a
reminder on every run, because an alert that repeats is an alert that gets
filtered.

Two conditions are deliberately *not* device health:

- a wrong device clock, which we cannot fix remotely (see
  ``docs/vendor/zkteco/protocol/adms-push.md``), so we surface the drift instead
  of pretending to correct it;
- unmapped punches, which have their own inbox with Retry / Map & Replay.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import frappe

STATUS_UNKNOWN = "Unknown"
STATUS_HEALTHY = "Healthy"
STATUS_WARNING = "Warning"
STATUS_OFFLINE = "Offline"

# Defaults apply when the settings fields are blank, so the job is safe on a
# site that has not opened the settings form yet.
_DEFAULTS = {
    "enable_fleet_health": 1,
    "offline_after_minutes": 15,
    "clock_skew_tolerance_minutes": 5,
    "stale_command_minutes": 30,
}

_ALERT_ROLES = ("System Manager", "HR Manager")

# Re-write the persisted skew only when it moves by more than this, so a device
# on a 10-second poll does not produce a DB write per punch.
_SKEW_WRITE_THRESHOLD_SECONDS = 60


def _now() -> datetime:
    """Server clock, naive.

    ``last_contact`` is written by ``utils.device_cache.touch_device`` with
    ``datetime.now()``; measuring silence against anything else (for example the
    site-local ``frappe.utils.now_datetime()``) would be wrong by the site's UTC
    offset. Same clock on both sides, always.
    """
    return datetime.now()


def _setting(field: str):
    value = frappe.db.get_single_value("Attendance Integration Settings", field)
    if value is None or value == "":
        return _DEFAULTS[field]
    return value


# ── Snapshot ─────────────────────────────────────────────────────────────────


def assess_device(name: str) -> dict:
    """Everything we can say about one device without opening its form."""
    device = frappe.db.get_value(
        "Attendance Device",
        name,
        ["last_contact", "disabled", "clock_skew_seconds", "creation", "company"],
        as_dict=True,
    )
    if not device:
        return {}

    now = _now()
    offline_after = int(_setting("offline_after_minutes"))
    skew_tolerance = int(_setting("clock_skew_tolerance_minutes")) * 60
    stale_after = int(_setting("stale_command_minutes"))

    minutes_silent = None
    if device.last_contact:
        minutes_silent = int((now - device.last_contact).total_seconds() // 60)

    pending, oldest_pending_minutes = _pending_commands(name, now)
    failed_24h = _failed_commands(name, now)
    held_punches = frappe.db.count(
        "Attendance Punch",
        {"attendance_device": name, "status": ["in", ("Unmapped", "Failed")]},
    )

    warnings: list[str] = []
    if device.clock_skew_seconds and abs(int(device.clock_skew_seconds)) > skew_tolerance:
        warnings.append(f"clock {_describe_skew(int(device.clock_skew_seconds))}")
    if pending and oldest_pending_minutes is not None and oldest_pending_minutes > stale_after:
        warnings.append(f"{pending} command(s) pending {oldest_pending_minutes} min")
    if failed_24h:
        warnings.append(f"{failed_24h} command(s) failed in 24h")

    if device.disabled:
        status = STATUS_UNKNOWN
        note = "Disabled — not polled."
    elif minutes_silent is None:
        age_minutes = int((now - device.creation).total_seconds() // 60)
        if age_minutes > offline_after:
            status = STATUS_OFFLINE
            note = "Never contacted the server since it was registered."
        else:
            status = STATUS_UNKNOWN
            note = "Registered; no contact yet."
    elif minutes_silent > offline_after:
        status = STATUS_OFFLINE
        note = f"Silent for {_describe_minutes(minutes_silent)}."
    elif warnings:
        status = STATUS_WARNING
        note = "; ".join(warnings).capitalize() + "."
    else:
        status = STATUS_HEALTHY
        note = ""

    if held_punches:
        extra = f"{held_punches} punch(es) awaiting mapping or retry."
        note = f"{note} {extra}".strip()

    return {
        "device": name,
        "status": status,
        "last_contact": device.last_contact,
        "minutes_silent": minutes_silent,
        "pending_commands": pending,
        "oldest_pending_minutes": oldest_pending_minutes,
        "failed_commands_24h": failed_24h,
        "held_punches": held_punches,
        "clock_skew_seconds": int(device.clock_skew_seconds or 0),
        "note": note,
    }


def _pending_commands(name: str, now: datetime) -> tuple[int, int | None]:
    pending = frappe.db.count(
        "Attendance Device Command", {"attendance_device": name, "status": "Pending"}
    )
    if not pending:
        return 0, None
    oldest = frappe.get_all(
        "Attendance Device Command",
        filters={"attendance_device": name, "status": "Pending"},
        order_by="creation asc",
        limit=1,
        pluck="creation",
    )
    if not oldest:
        return pending, None
    return pending, int((now - oldest[0]).total_seconds() // 60)


def _failed_commands(name: str, now: datetime) -> int:
    """Failures worth a human looking at.

    ``Return=-629`` is this firmware's way of saying the verb does not exist —
    per-user queries and ``Sync User List`` fail that way by design (see
    ``docs/vendor/zkteco/protocol/adms-push.md``). Counting those would leave
    the badge permanently orange, which is how a real warning gets ignored.
    """
    rows = frappe.get_all(
        "Attendance Device Command",
        filters={
            "attendance_device": name,
            "status": "Failed",
            "modified": [">", now - timedelta(hours=24)],
        },
        fields=["device_response"],
    )
    return sum(1 for row in rows if "-629" not in (row.device_response or ""))


# ── Scheduled check ─────────────────────────────────────────────────────────


def check_fleet() -> dict:
    """Assess every enabled device and alert on status transitions.

    Scheduled every 10 minutes. Per-device work is wrapped so one bad row cannot
    stop the rest of the fleet from being assessed.
    """
    if not int(_setting("enable_fleet_health") or 0):
        return {"skipped": "fleet health disabled"}

    checked = alerted = recovered = 0
    for name in frappe.get_all("Attendance Device", filters={"disabled": 0}, pluck="name"):
        try:
            snapshot = assess_device(name)
            if not snapshot:
                continue
            checked += 1
            alerted += _alert_on_transition(name, snapshot)
            recovered += _recover_if_back(name, snapshot)
            _persist(name, snapshot)
        except Exception:
            frappe.log_error(
                title="Fleet health check failed",
                message=f"Device {name}\n{frappe.get_traceback()}",
            )
            continue

    frappe.db.commit()
    if alerted or recovered:
        frappe.logger("biometric_integration").info(
            f"fleet health: {checked} checked, {alerted} alerted, {recovered} recovered"
        )
    return {"checked": checked, "alerted": alerted, "recovered": recovered}


def _persist(name: str, snapshot: dict) -> None:
    frappe.db.set_value(
        "Attendance Device",
        name,
        {
            "health_status": snapshot["status"],
            "last_health_check": _now(),
            "health_note": snapshot["note"],
        },
        update_modified=False,
    )


def _alert_on_transition(name: str, snapshot: dict) -> int:
    """One notice per outage: the flag is the guard, not the stored status."""
    if snapshot["status"] != STATUS_OFFLINE:
        return 0
    if frappe.db.get_value("Attendance Device", name, "offline_alerted_on"):
        return 0

    _notify(
        name,
        subject=f"Biometric terminal offline: {name}",
        body=(
            f"<p><b>{name}</b> has not contacted the server for "
            f"{_describe_minutes(snapshot['minutes_silent'] or 0)}.</p>"
            f"<p>{frappe.utils.escape_html(snapshot['note'])}</p>"
            "<p>Punches are still being recorded on the terminal and will upload on "
            "reconnect; attendance for that period will appear late, not lost. "
            "Check power, network, and the Cloud Server / ADMS address on the device.</p>"
        ),
        log_type="Error",
    )
    frappe.db.set_value(
        "Attendance Device", name, "offline_alerted_on", _now(), update_modified=False
    )
    return 1


def _recover_if_back(name: str, snapshot: dict) -> int:
    if snapshot["status"] == STATUS_OFFLINE:
        return 0
    if not frappe.db.get_value("Attendance Device", name, "offline_alerted_on"):
        return 0

    _notify(
        name,
        subject=f"Biometric terminal back online: {name}",
        body=(
            f"<p><b>{name}</b> is reporting again "
            f"(last contact: {frappe.utils.format_datetime(snapshot['last_contact'])}).</p>"
            "<p>Any punches recorded while it was offline should now have uploaded. "
            "Check <b>Attendance Punch</b> for anything that could not be matched, "
            "and the device clock if the gap was long enough to have drifted.</p>"
        ),
        log_type="Handshake",
    )
    frappe.db.set_value(
        "Attendance Device", name, "offline_alerted_on", None, update_modified=False
    )
    return 1


def _notify(name: str, subject: str, body: str, log_type: str) -> None:
    """Bell notification for the people who can act, plus a device-log line.

    Notification Log rather than email: it needs no SMTP configuration and is the
    trail an operator will actually look at.
    """
    for user in _recipients():
        try:
            frappe.get_doc(
                {
                    "doctype": "Notification Log",
                    "for_user": user,
                    "type": "Alert",
                    "subject": subject,
                    "email_content": body,
                    "document_type": "Attendance Device",
                    "document_name": name,
                }
            ).insert(ignore_permissions=True)
        except Exception:
            frappe.log_error(
                title="Fleet health notification failed",
                message=f"user={user} device={name}\n{frappe.get_traceback()}",
            )

    try:
        from biometric_integration.biometric_integration.doctype.attendance_device_log.attendance_device_log import (
            maybe_log,
        )

        maybe_log(name, log_type, "IN", subject, raw_data=body)
    except Exception:
        # The device log is optional (it is behind a setting); never let it stop
        # the alert itself.
        pass


def _recipients() -> list[str]:
    from frappe.utils.user import get_users_with_role

    users: set[str] = set()
    for role in _ALERT_ROLES:
        users.update(get_users_with_role(role))
    if not users:
        return []
    return frappe.get_all(
        "User",
        filters={"name": ["in", sorted(users)], "enabled": 1, "user_type": "System User"},
        pluck="name",
    )


# ── Clock drift, recorded at the only moment we can observe it ──────────────


def record_clock_skew(sn: str, device_local_time: datetime) -> None:
    """Compare a *real-time* punch to the clock the device should be showing.

    Only call this for `rtlog` records. A batched `ATTLOG` upload is a backlog, so
    its timestamps say nothing about the device's clock now.

    The expected time is computed in the device's own configured zone
    (``Attendance Device.device_timezone``), so a terminal that legitimately runs
    on another zone does not raise a false alarm.
    """
    try:
        tz_name = (
            frappe.db.get_value("Attendance Device", sn, "device_timezone")
            or frappe.utils.get_system_timezone()
        )
        expected = _now_in(tz_name)
        if expected is None:
            return
        skew = int((device_local_time - expected).total_seconds())

        cached = frappe.cache.get_value(f"biometric:clock_skew:{sn}")
        if cached is not None and abs(int(cached) - skew) < _SKEW_WRITE_THRESHOLD_SECONDS:
            return
        frappe.cache.set_value(f"biometric:clock_skew:{sn}", skew, expires_in_sec=3600)

        frappe.db.set_value(
            "Attendance Device", sn, "clock_skew_seconds", skew, update_modified=False
        )
        if abs(skew) > 60:
            frappe.logger("biometric_integration").warning(
                f"{sn}: device clock is {_describe_skew(skew)} — the terminal's own "
                "time zone setting is the only fix"
            )
    except Exception:
        frappe.log_error(
            title="Clock skew measurement failed", message=f"device={sn}\n{frappe.get_traceback()}"
        )


def _now_in(tz_name: str) -> datetime | None:
    from zoneinfo import ZoneInfo

    try:
        return datetime.now(ZoneInfo(tz_name)).replace(tzinfo=None)
    except Exception:
        return None


# ── Helpers ─────────────────────────────────────────────────────────────────


def get_fleet_health() -> list[dict]:
    """Snapshot for a page, a report, or a console check."""
    return [
        assess_device(name)
        for name in frappe.get_all(
            "Attendance Device", filters={"disabled": 0}, order_by="serial asc", pluck="name"
        )
    ]


def _describe_minutes(minutes: int) -> str:
    if minutes < 90:
        return f"{minutes} min"
    if minutes < 60 * 48:
        return f"{minutes // 60} h {minutes % 60} min"
    return f"{minutes // (60 * 24)} days"


def _describe_skew(seconds: int) -> str:
    sign = "+" if seconds > 0 else "-"
    seconds = abs(seconds)
    if seconds < 120:
        return f"{sign}{seconds}s off"
    return f"{sign}{seconds // 60} min {seconds % 60}s off"
