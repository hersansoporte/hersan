import pytz
# -*- coding: utf-8 -*-
################################################################################
#
#    Cybrosys Technologies Pvt. Ltd.
#
#    Copyright (C) 2026-TODAY Cybrosys Technologies(<https://www.cybrosys.com>).
#    Author: Cybrosys Techno Solutions (odoo@cybrosys.com)
#
#    You can modify it under the terms of the GNU AFFERO
#    GENERAL PUBLIC LICENSE (AGPL v3), Version 3.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU AFFERO GENERAL PUBLIC LICENSE (AGPL v3) for more details.
#
#    You should have received a copy of the GNU AFFERO GENERAL PUBLIC LICENSE
#    (AGPL v3) along with this program.
#    If not, see <http://www.gnu.org/licenses/>.
#
################################################################################
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

    @api.depends('check_in', 'check_out', 'almuerzo_out', 'almuerzo_in', 'comida_out', 'comida_in')
    def _compute_worked_hours(self):
        super()._compute_worked_hours()
        for attendance in self:
            if attendance.check_in and attendance.check_out and (
                (attendance.almuerzo_out and attendance.almuerzo_in)
                or (attendance.comida_out and attendance.comida_in)
            ):
                total_seconds = (attendance.check_out - attendance.check_in).total_seconds()
                if attendance.almuerzo_out and attendance.almuerzo_in and attendance.almuerzo_in > attendance.almuerzo_out:
                    total_seconds -= (attendance.almuerzo_in - attendance.almuerzo_out).total_seconds()
                if attendance.comida_out and attendance.comida_in and attendance.comida_in > attendance.comida_out:
                    total_seconds -= (attendance.comida_in - attendance.comida_out).total_seconds()
                attendance.worked_hours = max(0.0, total_seconds / 3600.0)

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

    def _classify_break_slot(self, employee, punch_dt_utc, existing_att=None):
        local_tz = self._get_employee_local_tz(employee)
        local_dt = pytz.UTC.localize(punch_dt_utc).astimezone(local_tz)
        dow = str(local_dt.weekday())
        punch_hour = local_dt.hour + local_dt.minute / 60.0 + local_dt.second / 3600.0

        has_alm_out = bool(existing_att and existing_att.almuerzo_out)
        has_com_out = bool(existing_att and existing_att.comida_out)

        # Turno nocturno (19:00 - 06:30): solo tiene Cena (01:00 - 02:30)
        if punch_hour >= 19.0 or punch_hour < 6.5:
            return 'comida' if not has_com_out else ('almuerzo' if not has_alm_out else 'comida')

        contract = getattr(employee, 'contract_id', False)
        calendar = employee.resource_calendar_id or (contract and contract.resource_calendar_id)
        lunch_lines = calendar.attendance_ids.filtered(lambda l: l.day_period == 'lunch') if calendar else self.env['resource.calendar.attendance']
        day_lunch = lunch_lines.filtered(lambda l: l.dayofweek == dow) or lunch_lines
        daytime_breaks = day_lunch.filtered(lambda l: 6.5 <= l.hour_from < 19.0)

        if daytime_breaks:
            almuerzo_slots = daytime_breaks.filtered(
                lambda l: 'almuerzo' in (l.name or '').lower() or l.hour_from < 12.5
            )
            comida_slots = daytime_breaks - almuerzo_slots

            # Horarios sin Almuerzo (ej. Standard 48 hours/week 14:00 - 14:30)
            if not almuerzo_slots and comida_slots:
                return 'comida' if not has_com_out else ('almuerzo' if not has_alm_out else 'comida')

            # Horarios con Almuerzo (10:00 - 10:15) y Comida (14:45 - 15:30)
            if almuerzo_slots and comida_slots:
                alm_mid = sum((l.hour_from + l.hour_to) / 2.0 for l in almuerzo_slots) / len(almuerzo_slots)
                com_mid = sum((l.hour_from + l.hour_to) / 2.0 for l in comida_slots) / len(comida_slots)
                cutoff = (alm_mid + com_mid) / 2.0
                if punch_hour < cutoff:
                    return 'almuerzo' if not has_alm_out else ('comida' if not has_com_out else 'almuerzo')
                return 'comida' if not has_com_out else ('almuerzo' if not has_alm_out else 'comida')

        if 6.5 <= punch_hour < 12.5:
            return 'almuerzo' if not has_alm_out else ('comida' if not has_com_out else 'almuerzo')
        return 'comida' if not has_com_out else ('almuerzo' if not has_alm_out else 'comida')

    @api.model
    def _process_hikvision_punch(self, employee, punch_dt, status):
        open_att = self.search([
            ('employee_id', '=', employee.id),
            ('check_out', '=', False),
        ], order='check_in desc', limit=1)

        if open_att and (punch_dt - open_att.check_in).total_seconds() > 16 * 3600:
            fallback_out = open_att.comida_in or open_att.comida_out or open_att.almuerzo_in or open_att.almuerzo_out or open_att.check_in
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
            slot = self._classify_break_slot(employee, punch_dt, existing_att=open_att)
            if slot == 'almuerzo':
                if not open_att.almuerzo_out:
                    open_att.write({'almuerzo_out': punch_dt})
            else:
                if not open_att.comida_out:
                    open_att.write({'comida_out': punch_dt})
            return open_att

        if status == 'breakIn':
            if not open_att:
                open_att = self.create({
                    'employee_id': employee.id,
                    'check_in': punch_dt,
                })
            if open_att.almuerzo_out and not open_att.almuerzo_in and not (open_att.comida_out and not open_att.comida_in):
                open_att.write({'almuerzo_in': punch_dt})
            elif open_att.comida_out and not open_att.comida_in:
                open_att.write({'comida_in': punch_dt})
            else:
                slot = self._classify_break_slot(employee, punch_dt, existing_att=open_att)
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
            return False
