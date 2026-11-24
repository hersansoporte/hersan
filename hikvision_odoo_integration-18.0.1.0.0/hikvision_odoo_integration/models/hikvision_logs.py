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
from odoo import fields, models

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

    hikvision_in_status = fields.Selection([
        ('checkIn', 'Entrada'),
        ('breakIn', 'Regreso Comida'),
    ], string='Tipo Entrada', readonly=True)

    hikvision_out_status = fields.Selection([
        ('breakOut', 'Salida Comida'),
        ('checkOut', 'Salida'),
    ], string='Tipo Salida', readonly=True)

    break_start = fields.Datetime(
        string='Salida Comida (Hora)',
        compute='_compute_hikvision_break_info',
        store=True,
    )
    break_end = fields.Datetime(
        string='Regreso Comida (Hora)',
        compute='_compute_hikvision_break_info',
        store=True,
    )
    break_duration = fields.Float(
        string='Duración Comida',
        compute='_compute_hikvision_break_info',
        store=True,
    )

    @api.depends('employee_id', 'check_in', 'hikvision_in_status')
    def _compute_hikvision_break_info(self):
        for rec in self:
            rec.break_start = False
            rec.break_end = False
            rec.break_duration = 0.0
            if not rec.employee_id or not rec.check_in:
                continue
            prev_att = self.search([
                ('employee_id', '=', rec.employee_id.id),
                ('id', '!=', rec.id),
                ('check_out', '!=', False),
                ('check_out', '<=', rec.check_in),
            ], order='check_out desc', limit=1)
            if prev_att:
                gap_seconds = (rec.check_in - prev_att.check_out).total_seconds()
                if 60 <= gap_seconds <= 3 * 3600 and (
                    rec.hikvision_in_status == 'breakIn' or prev_att.hikvision_out_status == 'breakOut'
                ):
                    rec.break_start = prev_att.check_out
                    rec.break_end = rec.check_in
                    rec.break_duration = gap_seconds / 3600.0