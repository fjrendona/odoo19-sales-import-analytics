from odoo import fields, models

LINE_CHANGE_TYPES = [
    ("updated", "Actualizada"),
    ("added", "Nueva"),
    ("missing", "Falta en el archivo"),
]


class SalesImportWizardLine(models.TransientModel):
    """Detalle de la comparación con el lote existente: solo las ventas que cambian.

    Las que no cambian solo se cuentan en el asistente, para no crear miles de
    líneas con un archivo grande.
    """

    _name = "sales.import.wizard.line"
    _description = "Cambio detectado al comparar con un lote"
    _order = "id"  # el asistente las crea en orden: actualizadas, nuevas y faltantes

    wizard_id = fields.Many2one(
        "sales.import.wizard", string="Asistente", required=True, ondelete="cascade", index=True
    )
    reference = fields.Char(string="Referencia", readonly=True)
    change_type = fields.Selection(LINE_CHANGE_TYPES, string="Cambio", readonly=True)
    summary_before = fields.Char(string="En el lote", readonly=True)
    summary_after = fields.Char(string="En el archivo", readonly=True)
    confirm_removal = fields.Boolean(
        string="Eliminar",
        help="Marque para que la venta no pase a la nueva versión. Sin marcar, se conserva.",
    )
