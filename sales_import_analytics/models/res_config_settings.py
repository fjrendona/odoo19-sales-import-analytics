from odoo import api, fields, models
from odoo.exceptions import ValidationError

VERSION_MATCH_THRESHOLD_PARAM = "sales_import_analytics.version_match_threshold"
DEFAULT_VERSION_MATCH_THRESHOLD = 0.8


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    sales_import_version_threshold = fields.Float(
        string="Umbral de coincidencia de versiones",
        config_parameter=VERSION_MATCH_THRESHOLD_PARAM,
        default=DEFAULT_VERSION_MATCH_THRESHOLD,
        help="Proporción de referencias de venta que deben coincidir con un lote existente "
        "para tratar un archivo como una nueva versión de ese lote o como una corrección parcial.",
    )

    @api.constrains("sales_import_version_threshold")
    def _check_sales_import_version_threshold(self):
        for settings in self:
            if not 0 < settings.sales_import_version_threshold <= 1:
                raise ValidationError(self.env._(
                    "El umbral de coincidencia de versiones debe estar entre 1 % y 100 %."
                ))
