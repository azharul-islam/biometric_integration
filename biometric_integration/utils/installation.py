import os
import frappe
import logging
import shutil
from frappe.utils import get_sites

_ZKTECO_DEFAULTS = {
    "device_poll_delay": 10,
    "device_error_delay": 30,
    "trans_times": "00:00;14:05",
    "trans_interval": 1,
}


def after_migrate():
    """Keep settings sane and repair indexes Frappe cannot declare.

    ``tabSingles`` has no unique key on (doctype, field), so an
    ``ON DUPLICATE KEY`` insert appended a duplicate row on every migrate. Two
    rows with different values make ``get_single_value`` return whichever the
    engine happens to read first. Collapse duplicates, then set defaults with
    the proper API.
    """
    try:
        _dedupe_singles("Attendance Integration Settings")
        for field, default in _ZKTECO_DEFAULTS.items():
            if frappe.db.get_single_value("Attendance Integration Settings", field) in (None, ""):
                frappe.db.set_single_value("Attendance Integration Settings", field, default)
        frappe.db.commit()
    except Exception as exc:
        logging.warning(f"biometric_integration after_migrate: could not set defaults: {exc}")

    _ensure_command_poll_index()


def _dedupe_singles(doctype: str) -> None:
    rows = frappe.db.sql(
        """select field, count(*) c from `tabSingles` where doctype=%s group by field having c > 1""",
        doctype,
        as_dict=True,
    )
    for row in rows:
        value = frappe.db.get_single_value(doctype, row.field)
        frappe.db.sql(
            "delete from `tabSingles` where doctype=%s and field=%s", (doctype, row.field)
        )
        frappe.db.sql(
            "insert into `tabSingles` (doctype, field, value) values (%s, %s, %s)",
            (doctype, row.field, value),
        )


def _ensure_command_poll_index() -> None:
    """The device poll filters (attendance_device, status, creation).

    A patch created this index on the reference deployment, yet the index was
    absent (the patch is recorded as run). Re-assert it on every migrate; the
    check is a cheap metadata lookup.
    """
    table = "tabAttendance Device Command"
    index = "adc_device_status_creation"
    try:
        if not frappe.db.has_index(table, index):
            frappe.db.sql_ddl(
                f"ALTER TABLE `{table}` ADD INDEX {index} "
                "(attendance_device, status, creation)"
            )
    except Exception as exc:
        logging.warning(f"biometric_integration: could not ensure {index}: {exc}")

def after_uninstall():
    """Cleanup assets directory created by the biometric_integration app only if no site uses it."""
    app_name = "biometric_integration"
    try:
        # Check all sites to see if the app is still installed elsewhere
        found_app_in_site = False
        for site in get_sites():
            try:
                frappe.init(site=site)
                frappe.connect()
                if app_name in frappe.get_installed_apps():
                    found_app_in_site = True
                    break
            finally:
                if frappe.local.db:
                    frappe.destroy()

        if not found_app_in_site:
            assets_dir = os.path.join(frappe.utils.get_bench_path(), "sites", "assets", "biometric_assets")
            if os.path.exists(assets_dir):
                shutil.rmtree(assets_dir)
                logging.info("biometric_assets directory removed successfully.")
            else:
                logging.info("biometric_assets directory does not exist, no cleanup needed.")
        else:
             logging.info(f"App {app_name} is still installed in another site, not removing assets.")

    except Exception as e:
        logging.error(f"Error while cleaning up biometric_assets directory: {str(e)}")
