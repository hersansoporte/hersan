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
import pytz
from odoo import api, fields, models


class HikvisionLogs(models.Model):
    """Stores Hikvision attendance logs for auditing."""
    _name = 'hikvision.logs'
    _description = 'Hikvision Logs'

    date = fields.Date(
        string='Date',
        help="Date on which the attendance punch was recorded."
    )

    employee_id = fields.Many2one(
        'hr.employee',
        string='Employee',
        help="Employee associated with this Hikvision attendance log."
    )
    punch_type = fields.Selection([('0', 'Check In'), ('1', 'Check Out'),
                                   ('2', 'Break Out'), ('3', 'Break In'),
                                   ('4', 'Overtime In'), ('5', 'Overtime Out'),
                                   ('255', 'Duplicate')],
                                  string='Punching Type',
                                  help='Punching type of the attendance')
    attendance_type = fields.Selection([('1', 'Finger'), ('15', 'Face'),
                                        ('2', 'Type_2'), ('3', 'Password'),
                                        ('4', 'Card'), ('255', 'Duplicate')],
                                       string='Category',
                                       help="Attendance detecting methods")
    punching_time = fields.Datetime(string='Punching Time',
                                    help="Punching time in the device")


class HrAttendance(models.Model):
    _inherit = 'hr.attendance'

    almuerzo_out = fields.Datetime(string='Salida Almuerzo', readonly=True)
    almuerzo_in = fields.Datetime(string='Regreso Almuerzo', readonly=True)
    comida_out = fields.Datetime(string='Salida Comida', readonly=True)
    comida_in = fields.Datetime(string='Regreso Comida', readonly=True)

    break_hours = fields.Float(
        string='Tiempo de descanso',
        compute='_compute_break_and_total_hours',
        store=True,
        readonly=True,
    )
    worked_hours = fields.Float(
        string='Tiempo trabajado',
        compute='_compute_worked_hours',
        store=True,
        readonly=True,
    )
    total_hours = fields.Float(
        string='Tiempo total',
        compute='_compute_break_and_total_hours',
        store=True,
        readonly=True,
    )

    def _get_attendance_duration_seconds(self):
        """Return (break_seconds, worked_seconds, total_seconds) using actual punches."""
        self.ensure_one()
        break_seconds = 0.0
        if (
            self.almuerzo_out
            and self.almuerzo_in
            and self.almuerzo_in > self.almuerzo_out
        ):
            break_seconds += (self.almuerzo_in - self.almuerzo_out).total_seconds()
        if (
            self.comida_out
            and self.comida_in
            and self.comida_in > self.comida_out
        ):
            break_seconds += (self.comida_in - self.comida_out).total_seconds()

        if self.check_in and self.check_out and self.check_out >= self.check_in:
            total_seconds = (self.check_out - self.check_in).total_seconds()
            worked_seconds = max(0.0, total_seconds - break_seconds)
        else:
            total_seconds = 0.0
            worked_seconds = 0.0

        return break_seconds, worked_seconds, total_seconds

    @api.depends('check_in', 'check_out', 'almuerzo_out', 'almuerzo_in', 'comida_out', 'comida_in')
    def _compute_worked_hours(self):
        super()._compute_worked_hours()
        for attendance in self:
            if attendance.check_in and attendance.check_out:
                _, worked_seconds, _ = attendance._get_attendance_duration_seconds()
                worked_val = worked_seconds / 3600.0
                attendance.worked_hours = worked_val
                if 'hik_worked_hours' in attendance._fields:
                    attendance.hik_worked_hours = worked_val

    @api.depends('check_in', 'check_out', 'almuerzo_out', 'almuerzo_in', 'comida_out', 'comida_in')
    def _compute_break_and_total_hours(self):
        for attendance in self:
            break_seconds, _, total_seconds = attendance._get_attendance_duration_seconds()
            attendance.break_hours = max(0.0, break_seconds / 3600.0)
            attendance.total_hours = max(0.0, total_seconds / 3600.0)

    def _get_employee_local_tz(self, employee):
        contract = getattr(employee, 'contract_id', False)
        calendar = employee.resource_calendar_id or (contract and contract.resource_calendar_id)
        tz_name = (
            employee.tz
            or (calendar and calendar.tz)
            or self.env.user.tz
            or 'America/Mexico_City'
        )
        try:
            return pytz.timezone(tz_name)
        except pytz.UnknownTimeZoneError:
            return pytz.timezone('America/Mexico_City')

    def _classify_break_slot(self, employee, punch_dt_utc):
        local_tz = self._get_employee_local_tz(employee)
        local_dt = pytz.UTC.localize(punch_dt_utc).astimezone(local_tz)
        dow = str(local_dt.weekday())
        punch_hour = local_dt.hour + local_dt.minute / 60.0 + local_dt.second / 3600.0

        # Turno nocturno (19:00 - 06:30): solo tiene Cena
        if punch_hour >= 19.0 or punch_hour < 6.5:
            return 'comida'

        contract = getattr(employee, 'contract_id', False)
        calendar = employee.resource_calendar_id or (contract and contract.resource_calendar_id)
        lunch_lines = (
            calendar.attendance_ids.filtered(lambda l: l.day_period == 'lunch')
            if calendar
            else self.env['resource.calendar.attendance']
        )
        day_lunch = lunch_lines.filtered(lambda l: l.dayofweek == dow) or lunch_lines
        daytime_breaks = day_lunch.filtered(lambda l: 6.5 <= l.hour_from < 19.0)

        if daytime_breaks:
            almuerzo_slots = daytime_breaks.filtered(
                lambda l: 'almuerzo' in (l.name or '').lower() or l.hour_from < 12.5
            )
            comida_slots = daytime_breaks - almuerzo_slots

            # Horarios con Almuerzo (10:00 - 10:15) y Comida (14:45 - 15:30)
            if almuerzo_slots and comida_slots:
                alm_mid = sum((l.hour_from + l.hour_to) / 2.0 for l in almuerzo_slots) / len(almuerzo_slots)
                com_mid = sum((l.hour_from + l.hour_to) / 2.0 for l in comida_slots) / len(comida_slots)
                cutoff = (alm_mid + com_mid) / 2.0
                return 'almuerzo' if punch_hour < cutoff else 'comida'

            # Horarios sin Almuerzo (ej. Standard 48 hours/week 14:00 - 14:30)
            if not almuerzo_slots and comida_slots:
                return 'almuerzo' if punch_hour < 11.5 else 'comida'

        # Sin contrato ni calendario asignado: antes de 12:30 es Almuerzo, desde 12:30 es Comida
        return 'almuerzo' if punch_hour < 12.5 else 'comida'

    @api.model
    def _process_hikvision_punch(self, employee, punch_dt, status):
        open_att = self.search([
            ('employee_id', '=', employee.id),
            ('check_out', '=', False),
        ], order='check_in desc', limit=1)

        if open_att and (punch_dt - open_att.check_in).total_seconds() > 16 * 3600:
            fallback_out = (
                open_att.comida_in
                or open_att.comida_out
                or open_att.almuerzo_in
                or open_att.almuerzo_out
                or open_att.check_in
            )
            open_att.write({'check_out': fallback_out})
            open_att = self.env['hr.attendance']

        if status == 'checkIn':
            if not open_att:
                return self.create({
                    'employee_id': employee.id,
                    'check_in': punch_dt,
                })
            return open_att

        if status == 'breakOut':
            if not open_att:
                open_att = self.create({
                    'employee_id': employee.id,
                    'check_in': punch_dt,
                })
            slot = self._classify_break_slot(employee, punch_dt)
            if slot == 'almuerzo':
                if not open_att.almuerzo_out:
                    open_att.write({'almuerzo_out': punch_dt})
                elif not open_att.comida_out and punch_dt > (open_att.almuerzo_in or open_att.almuerzo_out):
                    open_att.write({'comida_out': punch_dt})
            else:
                if not open_att.comida_out:
                    open_att.write({'comida_out': punch_dt})
                elif not open_att.almuerzo_out and punch_dt > (open_att.comida_in or open_att.comida_out):
                    # Orden cronologico: si hay 2 descansos en el mismo turno, el primero pasa a Almuerzo y el segundo queda en Comida
                    open_att.write({
                        'almuerzo_out': open_att.comida_out,
                        'almuerzo_in': open_att.comida_in,
                        'comida_out': punch_dt,
                        'comida_in': False,
                    })
            return open_att

        if status == 'breakIn':
            if not open_att:
                # Apertura automatica si olvido checar Entrada y Salida de descanso
                return self.create({
                    'employee_id': employee.id,
                    'check_in': punch_dt,
                })
            if open_att.almuerzo_out and not open_att.almuerzo_in and not (open_att.comida_out and not open_att.comida_in):
                open_att.write({'almuerzo_in': punch_dt})
            elif open_att.comida_out and not open_att.comida_in:
                open_att.write({'comida_in': punch_dt})
            else:
                slot = self._classify_break_slot(employee, punch_dt)
                if slot == 'almuerzo' and not open_att.almuerzo_in:
                    open_att.write({'almuerzo_in': punch_dt})
                elif not open_att.comida_in:
                    open_att.write({'comida_in': punch_dt})
            return open_att

        if status == 'checkOut':
            if open_att:
                if punch_dt >= open_att.check_in:
                    open_att.write({'check_out': punch_dt})
                return open_att
            # Apertura automatica si olvido checar Entrada y su primer toque es Salida
            return self.create({
                'employee_id': employee.id,
                'check_in': punch_dt,
                'check_out': punch_dt,
            })