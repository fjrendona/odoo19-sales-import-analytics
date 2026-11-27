from odoo import api, fields, models
from odoo.exceptions import UserError

MANAGER_GROUP = "sales_import_analytics.group_manager"


class SalesImportBatch(models.Model):
    """Una importación de un archivo Excel, o una versión de ella.

    Da trazabilidad (de qué archivo salió cada venta), permite deshacer una
    importación completa borrando el lote y detecta reimportaciones por la
    huella de su contenido. El usuario y la fecha son ``create_uid`` y ``create_date``.

    Cada versión es un lote completo (una foto de todas sus ventas) enlazado con
    la anterior. Solo una versión de cada cadena es vigente, y el tablero solo
    cuenta las ventas vigentes. Restaurar una versión es mover esa marca.
    """

    _name = "sales.import.batch"
    _description = "Lote de importación de ventas"
    _order = "create_date desc, id desc"

    name = fields.Char(string="Archivo", required=True)
    file_hash = fields.Char(
        string="Huella del contenido",
        required=True,
        index=True,
        readonly=True,
        help="SHA-256 de las ventas del archivo (no de sus bytes), usada para detectar "
        "reimportaciones aunque el archivo se haya vuelto a guardar.",
    )
    record_ids = fields.One2many(
        "sales.import.record", "batch_id", string="Ventas", readonly=True
    )
    record_count = fields.Integer(string="Ventas importadas", readonly=True)
    new_partner_count = fields.Integer(string="Clientes nuevos", readonly=True)
    new_product_count = fields.Integer(string="Productos nuevos", readonly=True)
    new_salesperson_count = fields.Integer(string="Vendedores nuevos", readonly=True)
    company_id = fields.Many2one(
        "res.company",
        string="Compañía",
        required=True,
        index=True,
        default=lambda self: self.env.company,
    )
    # Versionamiento. Los defaults dejan los lotes anteriores a esta funcionalidad
    # como versión 1 vigente al actualizar el módulo, sin script de migración.
    version = fields.Integer(string="Versión", default=1, readonly=True)
    is_current = fields.Boolean(string="Vigente", default=True, index=True, readonly=True)
    previous_batch_id = fields.Many2one(
        "sales.import.batch",
        string="Versión anterior",
        index=True,
        ondelete="set null",
        readonly=True,
    )
    origin_batch_id = fields.Many2one(
        "sales.import.batch",
        string="Primera versión",
        index=True,
        ondelete="set null",
        readonly=True,
        help="Vacío en la primera versión. Agrupa todas las versiones de un mismo lote.",
    )
    later_version_ids = fields.One2many(
        "sales.import.batch", "origin_batch_id", string="Versiones posteriores", readonly=True
    )
    version_ids = fields.Many2many(
        "sales.import.batch",
        string="Versiones",
        compute="_compute_version_ids",
    )
    version_count = fields.Integer(string="Número de versiones", compute="_compute_version_ids")
    unchanged_count = fields.Integer(string="Sin cambios", readonly=True)
    updated_count = fields.Integer(string="Actualizadas", readonly=True)
    added_count = fields.Integer(string="Nuevas", readonly=True)
    removed_count = fields.Integer(string="Eliminadas", readonly=True)

    @api.depends("origin_batch_id.later_version_ids", "later_version_ids")
    def _compute_version_ids(self):
        for batch in self:
            origin = batch.origin_batch_id or batch
            batch.version_ids = (origin | origin.later_version_ids).sorted("version", reverse=True)
            batch.version_count = len(batch.version_ids)

    @api.depends("name", "version")
    def _compute_display_name(self):
        for batch in self:
            batch.display_name = (
                self.env._("%(name)s (v%(version)s)", name=batch.name, version=batch.version)
                if batch.version > 1
                else batch.name
            )

    @api.ondelete(at_uninstall=False)
    def _unlink_promote_previous_version(self):
        """Borrar la versión vigente devuelve la vigencia a la anterior, para que
        el tablero no pierda las ventas de ese lote."""
        previous = self.filtered("is_current").previous_batch_id - self
        previous.is_current = True

    def action_open_records(self):
        """``views`` explícito: esta acción también se usa anidada en ``params.next``
        de la notificación del asistente, y ahí el servidor no la completa a partir
        de ``view_mode`` (``clean_action`` solo procesa la acción de primer nivel).
        Sin ``views`` el cliente falla con un TypeError al redirigir."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Ventas de %(batch)s", batch=self.display_name),
            "res_model": "sales.import.record",
            "view_mode": "list,form",
            "views": [(False, "list"), (False, "form")],
            "domain": [("batch_id", "=", self.id)],
        }

    def action_open_changes(self):
        """Las ventas de esta versión agrupadas por el cambio respecto a la anterior.
        Las eliminadas no están en esta versión: se cuentan en ``removed_count`` y
        se ven en la versión anterior."""
        action = self.action_open_records()
        action["name"] = self.env._("Cambios de %(batch)s", batch=self.display_name)
        action["context"] = {"group_by": ["change_type"]}
        return action

    def action_restore(self):
        """Hace vigente esta versión y deja como reemplazada la que lo era."""
        self.ensure_one()
        self._check_manager()
        if self.is_current:
            return False
        (self.version_ids - self).filtered("is_current").is_current = False
        self.is_current = True
        # La lista en tiempo real recarga ante este aviso: cambian las ventas vigentes.
        self.env["sales.import.record"]._notify_new_records()
        return True

    def _check_manager(self):
        if not self.env.user.has_group(MANAGER_GROUP):
            raise UserError(self.env._(
                "Solo un Responsable puede crear, reemplazar o restaurar versiones de un lote."
            ))
