# -*- coding: utf-8 -*-
################################################################################
#
#    Cybrosys Technologies Pvt. Ltd. (Adapted for HERSAN HITECH Schedules)
#
#    Copyright (C) 2026-TODAY Cybrosys Technologies(<https://www.cybrosys.com>).
#    Author: Cybrosys Techno Solutions (odoo@cybrosys.com)
#
#    You can modify it under the terms of the GNU AFFERO
#    GENERAL PUBLIC LICENSE (AGPL v3), Version 3.
#
################################################################################
import json
import logging
import math
import time as time_module
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, time as dt_time
import pytz
import requests
from requests.auth import HTTPDigestAuth
from odoo import _, api, exceptions, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


class HikvisionDevice(models.Model):
    """Model for managing Hikvision biometric devices."""

    _name = 'hikvision.device'
    _inherit = ['mail.thread']
    _description = 'Hikvision Biometric Device'

    name = fields.Char(help='Name of the Biometric Device', required=True)
    ip_address = fields.Char("Device IP", help='The IP address of the Device', required=True)
    username = fields.Char("Username", help="Username of the device", required=True)
    password = fields.Char("Password", help="Password of the device", required=True)
    image = fields.Image()
    device_name = fields.Char("Device Name", help='Name of the Device')
    device_id = fields.Char("Device Id", help="Id of the device")
    device_model = fields.Char("Model", help="Model of the device")
    device_serial_no = fields.Char("Serial No", help="Serial No of the device")
    device_mac_address = fields.Char(string='Device Mac ID', help='Mac ID of the Device')

    # -------------------------------------------------------------------------
    # API & TIMEZONE HELPERS
    # -------------------------------------------------------------------------

    def _get_api_config(self, endpoint=""):
        """Get API configuration for device requests."""
        url = f"http://{self.ip_address}{endpoint}"
        auth = HTTPDigestAuth(self.username, self.password)
        headers = {"Content-Type": "application/json"}
        return url, auth, headers

    def _get_local_tz(self):
        """Return the configured timezone, defaulting to America/Mexico_City."""
        tz_name = self.env.user.tz or self.env.context.get("tz") or "America/Mexico_City"
        try:
            return pytz.timezone(tz_name)
        except pytz.UnknownTimeZoneError:
            return pytz.timezone("America/Mexico_City")

    def _format_hikvision_datetime(self, dt_aware):
        """Format aware datetime with explicit +/-HH:MM offset required by Hikvision ISAPI."""
        raw = dt_aware.strftime("%Y-%m-%dT%H:%M:%S%z")
        if len(raw) >= 5 and raw[-5] in ("+", "-"):
            return f"{raw[:-2]}:{raw[-2:]}"
        return raw

    def _find_employee_by_hikvision_no(self, emp_no):
        """Find active Odoo employee matching hikvision_number (including zero-padded variants)."""
        if not emp_no:
            return self.env["hr.employee"]
        emp_no_str = str(emp_no).strip()
        emp_no_stripped = emp_no_str.lstrip("0") or emp_no_str
        candidates = list({emp_no_str, emp_no_stripped})
        return self.env["hr.employee"].search(
            [("hikvision_number", "in", candidates)], limit=1
        )

    # -------------------------------------------------------------------------
    # SCHEDULE-AWARE HELPERS (HERSAN HITECH CALENDARS)
    # -------------------------------------------------------------------------

    def _get_employee_calendar(self, employee):
        """Return the active resource.calendar for the employee (contract first, then employee)."""
        calendar = False
        if hasattr(employee, "contract_id") and employee.contract_id and employee.contract_id.resource_calendar_id:
            calendar = employee.contract_id.resource_calendar_id
        if not calendar:
            calendar = employee.resource_calendar_id
        return calendar

    def _get_calendar_week_type(self, calendar, target_date):
        """Return '0' (Primera) or '1' (Segundo) if calendar rotates weekly, else False."""
        if not calendar or not getattr(calendar, "two_weeks_calendar", False):
            return False
        if hasattr(calendar, "get_week_type"):
            return str(calendar.get_week_type(target_date))
        return str(int(math.floor((target_date.toordinal() - 1) / 7) % 2))

    def _float_to_local_datetime(self, local_date, hour_float, local_tz):
        """Convert a date and Odoo float hour (e.g. 14.5 -> 14:30, 24.0 -> 23:59:59) to aware datetime."""
        if hour_float >= 24.0:
            naive_dt = datetime.combine(local_date, dt_time(23, 59, 59))
        else:
            hours = int(hour_float)
            minutes = int(round((hour_float - hours) * 60))
            if minutes >= 60:
                hours += 1
                minutes -= 60
            if hours >= 24:
                naive_dt = datetime.combine(local_date, dt_time(23, 59, 59))
            else:
                naive_dt = datetime.combine(local_date, dt_time(hours, minutes, 0))
        return local_tz.localize(naive_dt)

    def _get_scheduled_checkout_for_checkin(self, employee, checkin_utc, new_checkin_utc, local_tz):
        """Compute the expected shift end in UTC from the employee's resource.calendar.

        Handles:
        - Standard 48 hours/week (Mon-Fri 09:00-18:00, Sat 09:00-14:30)
        - Jornada de Limpieza (Mon/Tue/Thu 08:00-18:00, Wed/Fri 08:00-17:00)
        - Rotating 2-week shifts (Turno 1/2/3/4):
          * Day week: Mon-Thu 07:00-17:00 (or 09:00-19:00 on Monday for Mantenimiento/Turno 3/4)
          * Night week: Mon-Thu 19:00-24:00 crossing midnight into 00:00-07:00 (or 05:00 Friday)
        """
        checkin_local = pytz.UTC.localize(checkin_utc).astimezone(local_tz)
        checkin_date = checkin_local.date()
        checkin_hour = checkin_local.hour + checkin_local.minute / 60.0
        dow = str(checkin_date.weekday())

        calendar = self._get_employee_calendar(employee)
        scheduled_end_local = None

        if calendar and calendar.attendance_ids:
            wk_type = self._get_calendar_week_type(calendar, checkin_date)
            day_lines = calendar.attendance_ids.filtered(
                lambda a: a.dayofweek == dow
                and a.hour_to > a.hour_from
                and (not wk_type or a.week_type == wk_type)
            )

            # Check if this shift crosses midnight (any line ending at 24.0, e.g. 19:00 -> 24:00)
            crosses_midnight = any(
                a.hour_to >= 23.99 and a.hour_from >= 12.0 for a in day_lines
            ) and checkin_hour >= 15.0

            if crosses_midnight:
                next_date = checkin_date + timedelta(days=1)
                next_dow = str(next_date.weekday())
                next_wk_type = self._get_calendar_week_type(calendar, next_date)
                next_morning_lines = calendar.attendance_ids.filtered(
                    lambda a: a.dayofweek == next_dow
                    and a.hour_to > a.hour_from
                    and a.hour_to <= 12.0
                    and (not next_wk_type or a.week_type == next_wk_type)
                )
                if next_morning_lines:
                    end_hour = max(next_morning_lines.mapped("hour_to"))
                    scheduled_end_local = self._float_to_local_datetime(next_date, end_hour, local_tz)
                else:
                    scheduled_end_local = self._float_to_local_datetime(next_date, 7.0, local_tz)
            elif day_lines:
                future_lines = day_lines.filtered(lambda a: a.hour_to > checkin_hour)
                target_lines = future_lines or day_lines
                end_hour = max(target_lines.mapped("hour_to"))
                scheduled_end_local = self._float_to_local_datetime(checkin_date, end_hour, local_tz)

        if not scheduled_end_local:
            scheduled_end_local = checkin_local + timedelta(hours=8)

        scheduled_end_utc = scheduled_end_local.astimezone(pytz.UTC).replace(tzinfo=None)

        # Avoid overlapping any subsequent attendance record already stored in Odoo
        next_att = self.env["hr.attendance"].search([
            ("employee_id", "=", employee.id),
            ("check_in", ">", checkin_utc),
        ], order="check_in asc", limit=1)

        upper_bound = new_checkin_utc - timedelta(seconds=1)
        if next_att:
            upper_bound = min(upper_bound, next_att.check_in - timedelta(seconds=1))

        # Clamp strictly between (checkin_utc + 1 min) and upper_bound
        min_end = checkin_utc + timedelta(minutes=1)
        max_end = min(checkin_utc + timedelta(hours=14), upper_bound)
        if max_end < min_end:
            return max(checkin_utc + timedelta(seconds=1), upper_bound)
        return max(min_end, min(scheduled_end_utc, max_end))

    def _close_stale_attendance(self, employee, open_att, new_checkin_utc, local_tz):
        """Close a forgotten open attendance using the employee's scheduled shift end."""
        if not open_att or open_att.check_out:
            return
        if new_checkin_utc <= open_att.check_in:
            return
        safe_checkout = self._get_scheduled_checkout_for_checkin(
            employee, open_att.check_in, new_checkin_utc, local_tz
        )
        open_att.sudo().write({"check_out": safe_checkout})

    def _sanitize_open_attendances(self, employee, reference_time, local_tz):
        """Close any stale or out-of-order open attendances that sit before a newer record."""
        Attendance = self.env["hr.attendance"]
        open_atts = Attendance.search([
            ("employee_id", "=", employee.id),
            ("check_out", "=", False),
        ], order="check_in asc")

        for att in open_atts:
            next_att = Attendance.search([
                ("employee_id", "=", employee.id),
                ("id", "!=", att.id),
                ("check_in", ">", att.check_in),
            ], order="check_in asc", limit=1)

            if next_att:
                self._close_stale_attendance(employee, att, next_att.check_in, local_tz)
            elif (reference_time - att.check_in).total_seconds() / 3600.0 > 15.0:
                self._close_stale_attendance(employee, att, reference_time, local_tz)

    # -------------------------------------------------------------------------
    # DEVICE CONNECTION & EVENT FETCHING
    # -------------------------------------------------------------------------

    def test_connection(self):
        """Test connection and fetch device details."""
        for device in self:
            url = f"http://{device.ip_address}/ISAPI/System/deviceInfo"
            try:
                response = requests.get(url, auth=HTTPDigestAuth(device.username, device.password), timeout=10)
                if response.status_code == 200:
                    root = ET.fromstring(response.content)
                    ns_uri = root.tag[root.tag.find("{") + 1:root.tag.find("}")]
                    ns = {'ns': ns_uri}
                    device.device_name = root.findtext('ns:deviceName', namespaces=ns)
                    device.device_id = root.findtext('ns:deviceID', namespaces=ns)
                    device.device_model = root.findtext('ns:model', namespaces=ns)
                    device.device_serial_no = root.findtext('ns:serialNumber', namespaces=ns)
                    device.device_mac_address = root.findtext('ns:macAddress', namespaces=ns)
                    return {
                        'type': 'ir.actions.client',
                        'tag': 'display_notification',
                        'params': {
                            'message': 'Successfully Connected',
                            'type': 'success',
                            'sticky': False
                        }
                    }
                else:
                    raise ValidationError(f"HTTP {response.status_code}: {response.text}")
            except Exception as error:
                raise ValidationError(f'{error}')

    def fetch_attendance(self, start_date=None, end_date=None):
        """Fetch attendance events within date range (defaults to yesterday + today for night shifts)."""
        if not self.ip_address:
            raise exceptions.UserError("Device IP address is not configured.")
        if not self.username or not self.password:
            raise exceptions.UserError("Device credentials are not configured.")

        local_tz = self._get_local_tz()

        if not start_date or not end_date:
            today = datetime.now(local_tz).date()
            # 48h window so overnight shifts (19:00 yesterday -> 07:00/05:00 today) stay paired
            start_date = today - timedelta(days=1)
            end_date = today

        start_datetime = local_tz.localize(datetime.combine(start_date, dt_time.min))
        end_datetime = local_tz.localize(datetime.combine(end_date, dt_time.max))

        url, auth, headers = self._get_api_config("/ISAPI/AccessControl/AcsEvent?format=json")

        payload_template = {
            "AcsEventCond": {
                "searchID": "1",
                "searchResultPosition": 0,
                "maxResults": 30,
                "major": 5,
                "minor": 0,
                "startTime": self._format_hikvision_datetime(start_datetime),
                "endTime": self._format_hikvision_datetime(end_datetime),
            }
        }

        all_events = []
        position = 0
        total_matches = None

        while True:
            payload_template["AcsEventCond"]["searchResultPosition"] = position
            try:
                response = requests.post(url, auth=auth, json=payload_template, headers=headers, timeout=15)
                if response.status_code != 200:
                    raise exceptions.UserError(
                        f"Hikvision HTTP {response.status_code}: {response.text}"
                    )
                try:
                    data = response.json()
                except json.JSONDecodeError:
                    raise exceptions.UserError(f"Invalid JSON response from device: {response.text}")

                events = data.get('AcsEvent', {}).get('InfoList', [])
                if total_matches is None:
                    total_matches = data.get('AcsEvent', {}).get('totalMatches', 0)

                if not events:
                    break

                all_events.extend(events)

                if len(events) < payload_template["AcsEventCond"]["maxResults"]:
                    break

                position += payload_template["AcsEventCond"]["maxResults"]

                if total_matches and len(all_events) >= total_matches:
                    break

            except requests.exceptions.ConnectionError as e:
                raise exceptions.UserError(f"Failed to connect to the device at {self.ip_address}: {str(e)}")
            except requests.exceptions.RequestException as e:
                raise exceptions.UserError(f"Error communicating with the device: {str(e)}")

        return all_events

    def fetch_all_attendance(self):
        """Fetch historical attendance events in 3-day chunks."""
        if not self.ip_address:
            raise exceptions.UserError("Device IP address is not configured.")
        if not self.username or not self.password:
            raise exceptions.UserError("Device credentials are not configured.")

        local_tz = self._get_local_tz()
        now_local = datetime.now(local_tz)
        start_date = now_local - timedelta(days=90)
        end_date = now_local + timedelta(days=1)

        all_events = []
        current_date = start_date

        job = self.env.context.get('job')
        total_chunks = 0
        tmp = start_date
        while tmp < end_date:
            total_chunks += 1
            tmp = min(tmp + timedelta(days=3), end_date)
        processed_chunks = 0

        while current_date < end_date:
            chunk_end = min(current_date + timedelta(days=3), end_date)

            try:
                chunk_events = self._fetch_attendance_chunk(current_date, chunk_end)
                all_events.extend(chunk_events)
                processed_chunks += 1

                if job:
                    try:
                        if hasattr(job, 'set_progress'):
                            job.set_progress(processed_chunks, total=total_chunks)
                        elif hasattr(job, 'progress'):
                            job.progress = min(100, int(processed_chunks * 100 / total_chunks))
                    except Exception:
                        pass

                time_module.sleep(0.3)

            except Exception as err:
                _logger.warning("Chunk %s -> %s failed: %s", current_date, chunk_end, err)
                continue

            current_date = chunk_end

        return all_events

    def _fetch_attendance_chunk(self, start_date, end_date):
        """Fetch attendance for a date range chunk."""
        url, auth, headers = self._get_api_config("/ISAPI/AccessControl/AcsEvent?format=json")

        payload_template = {
            "AcsEventCond": {
                "searchID": "1",
                "searchResultPosition": 0,
                "maxResults": 30,
                "major": 5,
                "minor": 0,
                "startTime": self._format_hikvision_datetime(start_date),
                "endTime": self._format_hikvision_datetime(end_date),
            }
        }

        chunk_events = []
        position = 0
        total_matches = None

        while True:
            payload_template["AcsEventCond"]["searchResultPosition"] = position
            try:
                response = requests.post(url, auth=auth, json=payload_template, headers=headers, timeout=30)
                if response.status_code == 401:
                    try:
                        test_url, test_auth, _ = self._get_api_config("/ISAPI/System/deviceInfo")
                        requests.get(test_url, auth=test_auth, timeout=10)
                    except Exception:
                        pass
                    response = requests.post(url, auth=auth, json=payload_template, headers=headers, timeout=30)

                if response.status_code != 200:
                    raise exceptions.UserError(
                        f"Hikvision HTTP {response.status_code}: {response.text}"
                    )

                try:
                    data = response.json()
                except json.JSONDecodeError:
                    raise exceptions.UserError(f"Invalid JSON response from device: {response.text}")

                events = data.get('AcsEvent', {}).get('InfoList', [])
                if total_matches is None:
                    total_matches = data.get('AcsEvent', {}).get('totalMatches', 0)

                if not events:
                    break

                chunk_events.extend(events)

                if len(events) < payload_template["AcsEventCond"]["maxResults"]:
                    break

                position += payload_template["AcsEventCond"]["maxResults"]

                if total_matches and len(chunk_events) >= total_matches:
                    break

            except requests.exceptions.ConnectionError as e:
                raise exceptions.UserError(f"Failed to connect to the device at {self.ip_address}: {str(e)}")
            except requests.exceptions.Timeout as e:
                raise exceptions.UserError(f"Request timed out: {str(e)}")
            except requests.exceptions.RequestException as e:
                raise exceptions.UserError(f"Error communicating with the device: {str(e)}")

        return chunk_events

    # -------------------------------------------------------------------------
    # QUEUE JOB TRIGGERS
    # -------------------------------------------------------------------------

    def fetch_and_create_attendance(self):
        """Queue job to download and create attendance records."""
        self.ensure_one()

        self.with_delay(
            description=f"Download Attendance from {self.name} ({self.ip_address})",
            priority=5,
            max_retries=3,
        ).job_download_attendance(self.id)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Job Queued'),
                'message': _(
                    'Attendance download job has been queued. You can monitor its progress in the Job Queue menu.'),
                'type': 'info',
                'sticky': False
            }
        }

    def download_all_attendance(self):
        """Queue job to download all attendance records."""
        self.ensure_one()

        self.with_delay(
            description=f"Download All Attendance from {self.name} ({self.ip_address})",
            priority=5,
            max_retries=3,
        ).job_download_all_attendance(self.id)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Job Queued'),
                'message': _(
                    'All attendance download job has been queued. This may take some time as it downloads all records. You can monitor its progress in the Job Queue menu.'),
                'type': 'info',
                'sticky': False
            }
        }

    # -------------------------------------------------------------------------
    # CORE ATTENDANCE PROCESSING (CHRONOLOGICAL + SCHEDULE-AWARE)
    # -------------------------------------------------------------------------

    def _parse_and_sort_events(self, events):
        """Parse timestamps, filter valid touchscreen punches, and sort chronologically."""
        parsed_events = []
        skipped_no_status = 0

        for event in events:
            emp_no = event.get("employeeNoString")
            pass_time_str = event.get("time")
            attendance_status = event.get("attendanceStatus")

            if not emp_no or not pass_time_str:
                continue

            # Ignore door-only face/card unlocks without an attendance button press
            if attendance_status not in ("checkIn", "checkOut", "breakOut", "breakIn"):
                skipped_no_status += 1
                continue

            try:
                pass_time = datetime.strptime(pass_time_str, "%Y-%m-%dT%H:%M:%S%z")
                pass_time = pass_time.astimezone(pytz.UTC).replace(tzinfo=None)
            except ValueError:
                try:
                    normalized = pass_time_str.replace("Z", "+00:00")
                    pass_time = datetime.fromisoformat(normalized).astimezone(pytz.UTC).replace(tzinfo=None)
                except ValueError:
                    continue

            parsed_events.append((pass_time, emp_no, attendance_status, event))

        parsed_events.sort(key=lambda item: item[0])
        return parsed_events, skipped_no_status

    def _process_single_punch(self, employee, pass_time, attendance_status, local_tz):
        """Apply a single attendance event (checkIn, breakOut, breakIn, checkOut) for an employee.

        Returns 'created', 'updated', or 'skipped'.
        """
        Attendance = self.env["hr.attendance"]

        # Exact timestamp deduplication
        duplicate = Attendance.search([
            ("employee_id", "=", employee.id),
            "|",
            ("check_in", "=", pass_time),
            ("check_out", "=", pass_time),
        ], limit=1)
        if duplicate:
            return "skipped"

        # Clean up any stale or out-of-order open attendance before evaluating current punch
        self._sanitize_open_attendances(employee, pass_time, local_tz)

        # Map 4 Hikvision touchscreen buttons into work-interval transitions:
        # - checkIn / breakIn  -> 'in'  (start of shift or return from Comida)
        # - breakOut / checkOut -> 'out' (start of Comida or end of shift)
        effective_action = "in" if attendance_status in ("checkIn", "breakIn") else "out"

        open_att = Attendance.search([
            ("employee_id", "=", employee.id),
            ("check_out", "=", False),
        ], order="check_in desc", limit=1)

        # Maximum shift span before considering an open session forgotten:
        # Turno 1/2/3/4 overnight is 12h (19:00-07:00), Limpieza is 10h; 15h gives 3h overtime margin
        max_open_hours = 15.0

        if effective_action == "in":
            if open_att:
                elapsed_hours = (pass_time - open_att.check_in).total_seconds() / 3600.0
                if elapsed_hours < 0:
                    return "skipped"
                if elapsed_hours <= max_open_hours:
                    # Already clocked in within the same active shift -> ignore duplicate checkIn/breakIn
                    return "skipped"
                # Stale open attendance from a previous shift -> auto-close using employee's schedule
                self._close_stale_attendance(employee, open_att, pass_time, local_tz)

            # Anti-debounce against accidental double-taps (< 45 seconds after previous check_out)
            last_closed = Attendance.search([
                ("employee_id", "=", employee.id),
                ("check_out", "!=", False),
            ], order="check_out desc", limit=1)
            if last_closed and 0 <= (pass_time - last_closed.check_out).total_seconds() < 45:
                return "skipped"

            try:
                Attendance.sudo().create({
                    "employee_id": employee.id,
                    "check_in": pass_time,
                    "hikvision_in_status": attendance_status,
                })
                return "created"
            except ValidationError:
                still_open = Attendance.search([
                    ("employee_id", "=", employee.id),
                    ("check_out", "=", False),
                ], order="check_in desc", limit=1)
                if still_open and pass_time > still_open.check_in:
                    self._close_stale_attendance(employee, still_open, pass_time, local_tz)
                    Attendance.sudo().create({
                        "employee_id": employee.id,
                        "check_in": pass_time,
                        "hikvision_in_status": attendance_status,
                    })
                    return "created"
                return "skipped"

        else:  # effective_action == "out" (breakOut or checkOut)
            if not open_att:
                # Ignore orphan breakOut/checkOut when no shift is open
                return "skipped"

            elapsed_seconds = (pass_time - open_att.check_in).total_seconds()
            if elapsed_seconds < 45:
                # Accidental double-tap right after checkIn/breakIn (< 45s)
                return "skipped"

            if elapsed_seconds / 3600.0 > max_open_hours:
                # Open session belongs to an earlier forgotten shift; close that one at scheduled end
                self._close_stale_attendance(employee, open_att, pass_time, local_tz)
                return "updated"

            open_att.sudo().write({
                "check_out": pass_time,
                "hikvision_out_status": attendance_status,
            })
            return "updated"

    def job_download_attendance(self, device_id, start_date=None, end_date=None):
        """Job to download and create attendance records in chronological order."""
        device = self.env['hikvision.device'].browse(device_id)
        if not device.exists():
            raise exceptions.UserError(f"Device with ID {device_id} not found")

        try:
            events = device.fetch_attendance(start_date=start_date, end_date=end_date)
            parsed_events, skipped_no_status = device._parse_and_sort_events(events)

            local_tz = device._get_local_tz()
            job = self.env.context.get('job')
            total_events = len(parsed_events)

            created_count = 0
            updated_count = 0
            skipped_count = skipped_no_status
            unmatched_emp_nos = set()

            for idx, (pass_time, emp_no, attendance_status, _raw) in enumerate(parsed_events, start=1):
                if job:
                    try:
                        job.set_progress(idx, total=total_events)
                    except Exception:
                        pass

                employee = device._find_employee_by_hikvision_no(emp_no)
                if not employee:
                    unmatched_emp_nos.add(emp_no)
                    skipped_count += 1
                    continue

                if employee.create_date and pass_time < employee.create_date.replace(tzinfo=None):
                    skipped_count += 1
                    continue

                outcome = device._process_single_punch(employee, pass_time, attendance_status, local_tz)
                if outcome == "created":
                    created_count += 1
                elif outcome == "updated":
                    updated_count += 1
                else:
                    skipped_count += 1

            summary = (
                f"Attendance sync completed: {len(events)} raw events, "
                f"{created_count} check-ins created, {updated_count} check-outs updated, "
                f"{skipped_count} skipped."
            )
            if unmatched_emp_nos:
                summary += f" Unmatched Hikvision IDs (ignored): {sorted(unmatched_emp_nos)}"
            _logger.info(summary)
            return summary

        except Exception as e:
            _logger.exception("Error in job_download_attendance: %s", e)
            raise

    def job_download_all_attendance(self, device_id):
        """Job to download all historical attendance records in chronological order."""
        device = self.env['hikvision.device'].browse(device_id)
        if not device.exists():
            raise exceptions.UserError(f"Device with ID {device_id} not found")

        try:
            device.test_connection()
            events = device.fetch_all_attendance()
            parsed_events, skipped_no_status = device._parse_and_sort_events(events)

            local_tz = device._get_local_tz()
            processed_count = 0
            skipped_count = skipped_no_status
            created_count = 0
            updated_count = 0
            unmatched_emp_nos = set()

            chunk_size = 50
            for chunk_idx in range(0, len(parsed_events), chunk_size):
                chunk = parsed_events[chunk_idx:chunk_idx + chunk_size]

                for pass_time, emp_no, attendance_status, _raw in chunk:
                    processed_count += 1
                    employee = device._find_employee_by_hikvision_no(emp_no)
                    if not employee:
                        unmatched_emp_nos.add(emp_no)
                        skipped_count += 1
                        continue

                    if employee.create_date and pass_time < employee.create_date.replace(tzinfo=None):
                        skipped_count += 1
                        continue

                    outcome = device._process_single_punch(employee, pass_time, attendance_status, local_tz)
                    if outcome == "created":
                        created_count += 1
                    elif outcome == "updated":
                        updated_count += 1
                    else:
                        skipped_count += 1

                try:
                    self.env.cr.commit()
                except Exception:
                    self.env.cr.rollback()
                    continue

                job = self.env.context.get('job')
                if job and parsed_events:
                    try:
                        progress = min(100, int((chunk_idx + chunk_size) * 100 / len(parsed_events)))
                        if hasattr(job, 'set_progress'):
                            job.set_progress(min(chunk_idx + chunk_size, len(parsed_events)), total=len(parsed_events))
                        elif hasattr(job, 'progress'):
                            job.progress = progress
                    except Exception:
                        pass

                time_module.sleep(0.1)

            summary = (
                f"All attendance download completed: {len(events)} raw events, "
                f"{processed_count} valid punches, Created {created_count}, "
                f"Updated {updated_count}, Skipped {skipped_count}."
            )
            if unmatched_emp_nos:
                summary += f" Unmatched Hikvision IDs (ignored): {sorted(unmatched_emp_nos)}"
            _logger.info(summary)
            return summary

        except Exception as e:
            try:
                self.env.cr.rollback()
            except Exception:
                pass
            raise e

    # -------------------------------------------------------------------------
    # EMPLOYEE SYNC (SAFE MODE: UPDATE EXISTING ONLY, NEVER CREATE EXTERNAL USERS)
    # -------------------------------------------------------------------------

    def fetch_employees(self):
        """Sync names of existing Odoo employees with Hikvision terminal without creating new employees."""
        for device in self:
            url, auth, headers = self._get_api_config("/ISAPI/AccessControl/UserInfo/Search?format=json")
            fetched_emp_ids = []
            position = 0

            try:
                while True:
                    payload = {
                        "UserInfoSearchCond": {
                            "searchID": "1",
                            "searchResultPosition": position,
                            "maxResults": 30,
                        }
                    }
                    response = requests.post(url, auth=auth, json=payload, headers=headers, timeout=15)
                    if response.status_code != 200:
                        break
                    data = response.json()
                    search_block = data.get("UserInfoSearch", {})
                    employees = search_block.get("UserInfo", [])
                    if not employees:
                        break

                    for emp in employees:
                        emp_no = emp.get("employeeNo")
                        emp_name = emp.get("name")
                        if not emp_no:
                            continue
                        existing_emp = device._find_employee_by_hikvision_no(emp_no)
                        if existing_emp:
                            if emp_name and existing_emp.name != emp_name:
                                existing_emp.sudo().write({"name": emp_name})
                            fetched_emp_ids.append(existing_emp.id)

                    if len(employees) < 30:
                        break
                    position += 30

                self.env.cr.commit()
                return {
                    "type": "ir.actions.act_window",
                    "name": _("Matched Employees"),
                    "res_model": "hr.employee",
                    "view_mode": "list,form",
                    "domain": [("id", "in", fetched_emp_ids)],
                }
            except requests.exceptions.RequestException as e:
                raise UserError(_("Error fetching employees from device: %s") % str(e))

    # -------------------------------------------------------------------------
    # RAW LOGS & DEVICE TIME / USER MANAGEMENT
    # -------------------------------------------------------------------------

    def fetch_logs(self):
        """Fetch and store raw attendance logs (48h window, supporting breaks and night shifts)."""
        events = self.fetch_attendance()

        minor_to_attendance_type = {
            38: '1',    # Fingerprint
            75: '15',   # Face
            181: '3',   # Card / RFID
        }
        status_to_punch_type = {
            'checkIn': '0',
            'checkOut': '1',
            'breakOut': '2',
            'breakIn': '3',
            'overtimeIn': '4',
            'overtimeOut': '5',
            'duplicate': '255',
        }

        for event in events:
            emp_no = event.get("employeeNoString")
            if not emp_no:
                continue

            emp = self._find_employee_by_hikvision_no(emp_no)
            if not emp:
                continue

            punch_time_str = event.get("time")
            if not punch_time_str:
                continue
            try:
                normalized = punch_time_str.replace('Z', '+00:00')
                punch_dt = datetime.fromisoformat(normalized).astimezone(pytz.UTC).replace(tzinfo=None)
                if emp.create_date and punch_dt < emp.create_date.replace(tzinfo=None):
                    continue
            except Exception:
                try:
                    punch_dt = datetime.strptime(punch_time_str[:19], '%Y-%m-%dT%H:%M:%S')
                except Exception:
                    continue

            minor_raw = event.get("minor")
            try:
                minor_val = int(minor_raw) if minor_raw is not None else None
            except (ValueError, TypeError):
                minor_val = None

            attendance_type = minor_to_attendance_type.get(minor_val, '255')
            punch_type = status_to_punch_type.get(event.get("attendanceStatus"), '255')

            existing_log = self.env['hikvision.logs'].search([
                ('employee_id', '=', emp.id),
                ('punching_time', '=', punch_dt),
                ('punch_type', '=', punch_type),
                ('attendance_type', '=', attendance_type),
            ], limit=1)

            if not existing_log:
                self.env['hikvision.logs'].sudo().create({
                    'date': punch_dt.date(),
                    'employee_id': emp.id,
                    'punch_type': punch_type,
                    'attendance_type': attendance_type,
                    'punching_time': punch_dt,
                })

    def set_time(self):
        """Set device time to system local time."""
        local_tz = self._get_local_tz()
        now_local = datetime.now(local_tz)
        local_time_str = self._format_hikvision_datetime(now_local)

        xml_payload = f"""<?xml version="1.0" encoding="UTF-8"?>
    <Time xmlns="http://www.hikvision.com/ver10/XMLSchema">
        <timeMode>manual</timeMode>
        <localTime>{local_time_str}</localTime>
    </Time>
    """

        url = f"http://{self.ip_address}/ISAPI/System/time"
        auth = HTTPDigestAuth(self.username, self.password)

        response = requests.put(
            url,
            auth=auth,
            headers={"Content-Type": "application/xml"},
            data=xml_payload,
            timeout=10,
        )

        if response.status_code == 200:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'message': 'Successfully Set the Time',
                    'type': 'success',
                    'sticky': False
                }
            }
        else:
            raise UserError(_("Please Check the Connection"))

    def _get_next_hikvision_employee_no(self):
        """Get next available employee number."""
        url, auth, headers = self._get_api_config("/ISAPI/AccessControl/UserInfo/Search?format=json")

        payload = {
            "UserInfoSearchCond": {
                "searchID": "1",
                "maxResults": 1000,
                "searchResultPosition": 0
            }
        }

        used_numbers = set()
        try:
            resp = requests.post(url, auth=auth, json=payload, headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                user_list = data.get("UserInfoSearch", {}).get("UserInfo", [])
                for user in user_list:
                    try:
                        num = int(user.get("employeeNo"))
                        used_numbers.add(num)
                    except (ValueError, TypeError):
                        continue
        except Exception:
            pass

        all_odoo_employees = self.env["hr.employee"].with_context(active_test=False).search([
            ("hikvision_number", "!=", False)
        ])

        for emp in all_odoo_employees:
            try:
                num = int(emp.hikvision_number)
                used_numbers.add(num)
            except (ValueError, TypeError):
                continue

        return (max(used_numbers) + 1) if used_numbers else 1

    def create_hikvision_user(self, employee):
        """Create user on Hikvision device."""
        new_employee_no = self._get_next_hikvision_employee_no()

        employee.hikvision_number = new_employee_no
        self.env.cr.commit()

        url, auth, headers = self._get_api_config("/ISAPI/AccessControl/UserInfo/Record?format=json")

        payload = {
            "UserInfo": {
                "employeeNo": str(new_employee_no),
                "name": employee.name,
                "userType": "normal",
                "valid": {
                    "enable": True,
                    "beginTime": "2020-01-01T00:00:00",
                    "endTime": "2030-01-01T23:59:59"
                },
                "doorRight": "1",
                "RightPlan": [{
                    "doorNo": 1,
                    "planTemplateNo": "1"
                }],
                "faceURL": ""
            }
        }

        response = requests.post(url, auth=auth, json=payload, headers=headers, timeout=10)
        if response.status_code in (200, 201):
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'message': _('User created successfully on Hikvision device with ID: %s') % new_employee_no,
                    'type': 'success',
                    'sticky': False
                }
            }
        else:
            employee.hikvision_number = False
            self.env.cr.commit()
            raise UserError(_("Failed to create user: %s") % response.text)

    def update_hikvision_user(self, employee):
        """Update user on Hikvision device."""
        if not employee.hikvision_number:
            raise UserError(_("This employee does not have a Hikvision number."))

        url, auth, headers = self._get_api_config("/ISAPI/AccessControl/UserInfo/Modify?format=json")

        payload = {
            "UserInfo": {
                "employeeNo": employee.hikvision_number,
                "name": employee.name,
                "userType": "normal",
                "valid": {
                    "enable": True,
                    "beginTime": "2020-01-01T00:00:00",
                    "endTime": "2030-01-01T23:59:59"
                },
                "doorRight": "1",
                "RightPlan": [{
                    "doorNo": 1,
                    "planTemplateNo": "1"
                }],
                "faceURL": ""
            }
        }

        response = requests.put(url, auth=auth, json=payload, headers=headers, timeout=10)
        if response.status_code in (200, 201):
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'message': _('User updated successfully on Hikvision device.'),
                    'type': 'success',
                    'sticky': False
                }
            }
        else:
            raise UserError(_("Failed to update user: %s") % response.text)

    def archive_hikvision_user(self, employee):
        """Delete user from Hikvision device when archived."""
        if not employee.hikvision_number:
            raise UserError(_("This employee does not have a Hikvision number."))

        url, auth, headers = self._get_api_config("/ISAPI/AccessControl/UserInfo/Delete?format=json")

        payload = {
            "UserInfoDelCond": {
                "EmployeeNoList": [
                    {
                        "employeeNo": employee.hikvision_number
                    }
                ]
            }
        }

        try:
            response = requests.put(url, auth=auth, json=payload, headers=headers, timeout=10)
        except requests.RequestException as e:
            raise UserError(_("Connection error: %s") % e)

        if response.status_code in (200, 201):
            employee.active = False
            self.env.cr.commit()
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'message': _(
                        'User deleted from device successfully. ID %s preserved in Odoo.') % employee.hikvision_number,
                    'type': 'success',
                    'sticky': False
                }
            }
        else:
            raise UserError(_("Failed to delete user from device: %s") % response.text)

    def delete_hikvision_user(self, employee):
        """Delete user from Hikvision device."""
        return self.archive_hikvision_user(employee)

    @api.model
    def cron_download_attendance_all_devices(self):
        """Cron job to download attendance from all devices."""
        devices = self.search([])

        for device in devices:
            try:
                device.fetch_and_create_attendance()
                time_module.sleep(2)
            except Exception as err:
                _logger.warning("Cron download failed for device %s: %s", device.name, err)
                continue

        return True