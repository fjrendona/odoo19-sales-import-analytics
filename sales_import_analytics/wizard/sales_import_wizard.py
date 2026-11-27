import base64
import binascii

from odoo import Command, api, fields, models
from odoo.exceptions import UserError
from odoo.fields import Domain
from odoo.tools import escape_psql, float_round, format_date, formatLang

from ..models.res_config_settings import (
    DEFAULT_VERSION_MATCH_THRESHOLD,
    VERSION_MATCH_THRESHOLD_PARAM,
)
from ..models.sales_import_batch import MANAGER_GROUP
from ..tools.batch_diff import (
    MATCH_PARTIAL,
    MATCH_VERSION,
    classify_match,
    diff_by_reference,
)
from ..tools.sales_excel_parser import (
    COLUMN_LABELS,
    MAX_DATA_ROWS,
    AmountRules,
    ParseError,
    content_fingerprint,
    normalize_name,
    parse_sales_workbook,
)

MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB
ALLOWED_EXTENSION = ".xlsx"
MAX_REPORTED_ERRORS = 50


class SalesImportWizard(models.TransientModel):
    """Orquesta la importación: lee, valida todo y solo entonces crea (todo o nada).

    La lectura y validación del archivo la hace el parser puro; aquí se resuelven
    las referencias contra la base de datos y se crean el lote y las ventas.

    Si el archivo trae la columna Referencia y se parece a un lote vigente, el
    asistente pasa a un segundo paso: muestra la comparación y espera a que el
    usuario elija qué hacer. Hasta entonces no se crean ni cambian ventas ni
    lotes. Al confirmar se vuelve a leer y comparar el archivo, porque el lote
    pudo cambiar mientras tanto.
    """

    _name = "sales.import.wizard"
    _description = "Asistente de importación de ventas"

    file = fields.Binary(string="Archivo Excel", required=True, attachment=False)
    file_name = fields.Char(string="Nombre del archivo")
    force_reimport = fields.Boolean(
        string="Importar de todas formas",
        help="Importa el archivo aunque ya se haya importado antes con el mismo contenido.",
    )
    duplicate_batch_id = fields.Many2one(
        "sales.import.batch",
        string="Importado antes en",
        compute="_compute_duplicate_batch_id",
    )
    limits_note = fields.Char(compute="_compute_limits_note")
    # Paso de comparación con un lote existente (versionamiento)
    step = fields.Selection(
        [("upload", "Archivo"), ("compare", "Comparación")],
        default="upload",
        required=True,
    )
    match_type = fields.Selection(
        [(MATCH_VERSION, "Nueva versión"), (MATCH_PARTIAL, "Corrección parcial")],
        string="Tipo de archivo",
        readonly=True,
    )
    candidate_batch_id = fields.Many2one(
        "sales.import.batch", string="Lote existente", readonly=True
    )
    match_summary = fields.Char(string="Motivo", readonly=True)
    unchanged_count = fields.Integer(string="Sin cambios", readonly=True)
    updated_count = fields.Integer(string="Actualizadas", readonly=True)
    added_count = fields.Integer(string="Nuevas", readonly=True)
    missing_count = fields.Integer(string="Faltan en el archivo", readonly=True)
    line_ids = fields.One2many("sales.import.wizard.line", "wizard_id", string="Detalle")

    def _compute_limits_note(self):
        self.limits_note = self.env._(
            "Máximo %(size)s MB y %(rows)s filas.",
            size=MAX_FILE_SIZE // 1024 // 1024,
            rows=f"{MAX_DATA_ROWS:,}".replace(",", "."),
        )

    @api.depends("file")
    def _compute_duplicate_batch_id(self):
        """Aviso previo en el formulario; ``action_import`` lo vuelve a comprobar."""
        for wizard in self:
            batch = self.env["sales.import.batch"]
            if wizard.file:
                try:
                    parsed = parse_sales_workbook(base64.b64decode(wizard.file), wizard._amount_rules())
                except (binascii.Error, ValueError):
                    parsed = None  # el error de lectura se reporta al importar
                if parsed and parsed.rows and not parsed.errors:
                    batch = wizard._find_batch_by_hash(content_fingerprint(parsed.rows))
            wizard.duplicate_batch_id = batch

    def action_import(self):
        self.ensure_one()
        rows, references = self._read_sales()
        file_hash = content_fingerprint(rows)
        forced_duplicate = self._check_not_imported(file_hash)
        candidate, match_type = (None, None) if forced_duplicate else self._find_version_match(rows)
        if candidate:
            self._check_can_version(candidate)
            self._prepare_comparison(candidate, match_type, rows, references)
            return self._reopen_action()
        return self._import_new_batch(rows, references, file_hash)

    def action_save_version(self):
        """Crea la versión siguiente con todas las ventas del lote: las del archivo
        más las que faltan en él y no se marcaron para eliminar. La anterior deja de
        ser vigente."""
        rows, references, diff = self._recompare(MATCH_VERSION)
        previous = self.candidate_batch_id
        removed = set(self.line_ids.filtered("confirm_removal").mapped("reference")) & set(diff.missing)
        kept_references = set(diff.missing) - removed
        kept = previous.record_ids.filtered(lambda record: record.reference in kept_references)

        references.create_missing()
        change_by_reference = (
            dict.fromkeys(diff.unchanged, "unchanged")
            | dict.fromkeys(diff.updated, "updated")
            | dict.fromkeys(diff.added, "added")
        )
        batch = self._create_batch(content_fingerprint(rows), len(rows) + len(kept), references, {
            # La siguiente de la cadena, no de la anterior: tras restaurar una versión
            # antigua, la nueva no debe repetir un número ya usado.
            "version": max(previous.version_ids.mapped("version")) + 1,
            "previous_batch_id": previous.id,
            "origin_batch_id": (previous.origin_batch_id or previous).id,
            "unchanged_count": len(diff.unchanged) + len(kept),
            "updated_count": len(diff.updated),
            "added_count": len(diff.added),
            "removed_count": len(removed),
        })
        previous.is_current = False
        self.env["sales.import.record"].create(
            [references.sale_values(row, batch) | {"change_type": change_by_reference[row.reference]}
             for row in rows]
            + kept.copy_data({"batch_id": batch.id, "change_type": "unchanged"})
        )
        return self._success_action(batch, self.env._(
            "Versión %(version)s guardada: %(updated)s ventas actualizadas, %(added)s nuevas "
            "y %(removed)s eliminadas.",
            version=batch.version,
            updated=batch.updated_count,
            added=batch.added_count,
            removed=batch.removed_count,
        ))

    def action_replace_batch(self):
        """Borra el lote y todas sus versiones (y sus ventas, en cascada) y lo
        importa de nuevo como versión 1, sin historial."""
        rows, references, _diff = self._recompare(MATCH_VERSION)
        self.candidate_batch_id.version_ids.unlink()
        return self._import_new_batch(rows, references, content_fingerprint(rows))

    def action_apply_changes(self):
        """Corrección parcial: actualiza y agrega las ventas del archivo en el mismo
        lote, sin versión nueva y sin tocar las que el archivo no trae."""
        rows, references, diff = self._recompare(MATCH_PARTIAL)
        batch = self.candidate_batch_id
        references.create_missing()
        rows_by_reference = {row.reference: row for row in rows}
        records_by_reference = {record.reference: record for record in batch.record_ids}
        # Un write por venta: cada una recibe valores distintos, y una corrección
        # parcial trae pocas filas. Sin lote ni compañía, que no cambian: escribir
        # company_id recalcula la moneda y la restricción del total se evaluaría
        # antes de aplicar el importe nuevo.
        for reference in diff.updated:
            values = references.sale_values(rows_by_reference[reference], batch)
            del values["batch_id"], values["company_id"]
            records_by_reference[reference].write(values | {"change_type": "updated"})
        added = self.env["sales.import.record"].create([
            references.sale_values(rows_by_reference[reference], batch) | {"change_type": "added"}
            for reference in diff.added
        ])
        if not added:  # create ya avisa a las listas abiertas; un write no
            self.env["sales.import.record"]._notify_new_records()
        batch.write({
            "record_count": batch.record_count + len(added),
            "updated_count": batch.updated_count + len(diff.updated),
            "added_count": batch.added_count + len(added),
        })
        return self._success_action(batch, self.env._(
            "Lote %(batch)s corregido: %(updated)s ventas actualizadas y %(added)s nuevas.",
            batch=batch.display_name,
            updated=len(diff.updated),
            added=len(added),
        ))

    # Lectura y comprobaciones previas

    def _read_sales(self):
        """Lee y valida el archivo; con un solo error no sigue (todo o nada)."""
        parsed = parse_sales_workbook(self._read_file_content(), self._amount_rules())
        references = _SaleReferences(self.env, parsed.rows)
        errors = parsed.errors + references.missing_master_errors()
        if errors:
            raise UserError(self._format_errors(errors))
        return parsed.rows, references

    def _decode_file(self) -> bytes:
        try:
            return base64.b64decode(self.file)
        except (binascii.Error, ValueError):
            raise UserError(self.env._("No se pudo leer el archivo subido."))

    def _read_file_content(self) -> bytes:
        if self.file_name and not self.file_name.lower().endswith(ALLOWED_EXTENSION):
            raise UserError(self.env._(
                "El archivo debe ser un Excel %(extension)s.", extension=ALLOWED_EXTENSION
            ))
        content = self._decode_file()
        if len(content) > MAX_FILE_SIZE:
            raise UserError(self.env._(
                "El archivo pesa %(size).1f MB y el máximo es %(max)s MB.",
                size=len(content) / 1024 / 1024,
                max=MAX_FILE_SIZE // 1024 // 1024,
            ))
        return content

    def _find_batch_by_hash(self, file_hash):
        """Solo lotes vigentes: volver a subir el contenido de una versión reemplazada
        no es un duplicado (sus ventas no cuentan), sino una nueva versión."""
        return self.env["sales.import.batch"].search(
            [("file_hash", "=", file_hash), ("is_current", "=", True)], limit=1
        )

    def _check_not_imported(self, file_hash):
        """Devuelve el lote con la misma huella si el usuario fuerza la reimportación."""
        previous = self._find_batch_by_hash(file_hash)
        if previous and not self.force_reimport:
            raise UserError(self.env._(
                "Este archivo ya se importó el %(date)s en el lote «%(batch)s». "
                "Marque «Importar de todas formas» si quiere importarlo otra vez.",
                date=fields.Datetime.to_string(previous.create_date),
                batch=previous.display_name,
            ))
        return previous

    def _amount_rules(self) -> AmountRules:
        """Las mismas reglas que aplicará el modelo al guardar: la cantidad se
        redondea a la precisión del campo y los importes se comparan con la
        tolerancia de la moneda."""
        currency = self.env.company.currency_id
        _precision, quantity_digits = self.env["sales.import.record"]._fields["quantity"].get_digits(self.env)
        return AmountRules(
            round_quantity=lambda quantity: float_round(quantity, precision_digits=quantity_digits),
            amounts_match=lambda expected, received: not currency.compare_amounts(expected, received),
        )

    def _format_errors(self, errors) -> str:
        lines = [error.render(self.env._) for error in errors[:MAX_REPORTED_ERRORS]]
        if len(errors) > MAX_REPORTED_ERRORS:
            lines.append(self.env._(
                "… y %(count)s errores más.", count=len(errors) - MAX_REPORTED_ERRORS
            ))
        return self.env._(
            "No se importó ninguna venta. Corrija el archivo y vuelva a intentarlo:\n\n%(errors)s",
            errors="\n".join(lines),
        )

    # Versionamiento: detección y comparación

    def _version_match_threshold(self) -> float:
        # sudo: leer parámetros del sistema exige ser administrador, y el umbral no
        # es un dato sensible. Solo se lee este parámetro.
        value = self.env["ir.config_parameter"].sudo().get_param(VERSION_MATCH_THRESHOLD_PARAM)
        try:
            return float(value)
        except (TypeError, ValueError):
            return DEFAULT_VERSION_MATCH_THRESHOLD

    def _find_version_match(self, rows):
        """Busca el lote vigente del que el archivo parece una versión o una corrección.

        Dos consultas agregadas en total, sea cual sea el número de lotes: cuántas
        referencias del archivo tiene cada lote, y cuántas referencias y qué meses
        tienen esos lotes. Gana el lote con más referencias en común.
        """
        file_references = {row.reference for row in rows if row.reference}
        if not file_references:
            return None, None
        Record = self.env["sales.import.record"]
        current_domain = [
            ("is_current", "=", True),
            ("batch_id", "!=", False),
            ("company_id", "=", self.env.company.id),
        ]
        common_counts = dict(Record._read_group(
            current_domain + [("reference", "in", list(file_references))], ["batch_id"], ["__count"]
        ))
        if not common_counts:
            return None, None
        batch_totals, batch_months = {}, {}
        for batch, month, count in Record._read_group(
            [("batch_id", "in", [batch.id for batch in common_counts]), ("reference", "!=", False)],
            ["batch_id", "date:month"],
            ["__count"],
        ):
            batch_totals[batch] = batch_totals.get(batch, 0) + count
            batch_months.setdefault(batch, set()).add(month)

        file_months = {row.date.replace(day=1) for row in rows}
        threshold = self._version_match_threshold()
        for batch, common in sorted(common_counts.items(), key=lambda item: (-item[1], -item[0].id)):
            if not batch_months[batch] & file_months:
                continue
            match_type = classify_match(batch_totals[batch], len(file_references), common, threshold)
            if match_type:
                return batch, match_type
        return None, None

    def _check_can_version(self, batch):
        if not self.env.user.has_group(MANAGER_GROUP):
            raise UserError(self.env._(
                "Este archivo parece una nueva versión o una corrección del lote «%(batch)s». "
                "Solo un Responsable puede crear versiones o corregir lotes.",
                batch=batch.display_name,
            ))

    def _compare(self, batch, rows, references):
        previous = {record.reference: record for record in batch.record_ids if record.reference}
        incoming = {row.reference: row for row in rows}
        diff = diff_by_reference(
            {reference: _record_signature(record) for reference, record in previous.items()},
            {reference: _row_signature(row, references, batch.company_id.currency_id)
             for reference, row in incoming.items()},
        )
        return diff, previous, incoming

    def _prepare_comparison(self, batch, match_type, rows, references):
        diff, previous, incoming = self._compare(batch, rows, references)
        missing = diff.missing if match_type == MATCH_VERSION else []
        if not (diff.updated or diff.added or missing):
            raise UserError(self.env._(
                "El archivo no trae cambios respecto al lote vigente «%(batch)s».",
                batch=batch.display_name,
            ))
        lines = (
            [{"reference": ref, "change_type": "updated", "summary_before": self._record_summary(previous[ref]),
              "summary_after": self._row_summary(incoming[ref])} for ref in diff.updated]
            + [{"reference": ref, "change_type": "added", "summary_after": self._row_summary(incoming[ref])}
               for ref in diff.added]
            + [{"reference": ref, "change_type": "missing", "summary_before": self._record_summary(previous[ref])}
               for ref in missing]
        )
        self.write({
            "step": "compare",
            "match_type": match_type,
            "candidate_batch_id": batch.id,
            "match_summary": self._match_summary(batch, len(rows), len(diff.unchanged) + len(diff.updated)),
            "unchanged_count": len(diff.unchanged),
            "updated_count": len(diff.updated),
            "added_count": len(diff.added),
            "missing_count": len(missing),
            "line_ids": [Command.clear()] + [Command.create(line) for line in lines],
        })

    def _recompare(self, expected_match_type):
        """Al confirmar se vuelve a leer y comparar el archivo: el formulario solo
        guarda el resumen, y el lote pudo cambiar mientras el usuario lo revisaba.
        Cada acción solo vale para el tipo detectado (los botones son públicos por RPC)."""
        self.ensure_one()
        if self.step != "compare" or self.match_type != expected_match_type:
            raise UserError(self.env._("Esta acción no corresponde a la comparación en curso."))
        batch = self.candidate_batch_id
        if not batch.is_current:
            raise UserError(self.env._(
                "El lote «%(batch)s» cambió mientras revisaba la comparación. Vuelva a importar el archivo.",
                batch=batch.display_name,
            ))
        self._check_can_version(batch)
        rows, references = self._read_sales()
        diff, _previous, _incoming = self._compare(batch, rows, references)
        return rows, references, diff

    def _match_summary(self, batch, file_count, common_count):
        return self.env._(
            "El archivo trae %(file)s ventas; %(common)s ya están en el lote «%(batch)s», "
            "que tiene %(batch_count)s.",
            file=file_count,
            common=common_count,
            batch=batch.display_name,
            batch_count=batch.record_count,
        )

    def _row_summary(self, row):
        return self._sale_summary(
            row.date, row.partner, row.salesperson, row.product, row.quantity, row.amount_total, row.state
        )

    def _record_summary(self, record):
        return self._sale_summary(
            record.date, record.partner_id.name, record.salesperson_id.name, record.product_id.display_name,
            record.quantity, record.amount_total, record.state,
        )

    def _sale_summary(self, date, partner, salesperson, product, quantity, amount_total, state):
        state_labels = dict(self.env["sales.import.record"]._fields["state"]._description_selection(self.env))
        return " · ".join([
            format_date(self.env, date),
            partner,
            salesperson,
            f"{quantity:g} × {product}",
            formatLang(self.env, amount_total, currency_obj=self.env.company.currency_id),
            state_labels[state],
        ])

    def _reopen_action(self):
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Comparar con el lote existente"),
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "views": [(False, "form")],
            "target": "new",
            "context": {"dialog_size": "extra-large"},  # los resúmenes antes/después son anchos
        }

    # Creación

    def _import_new_batch(self, rows, references, file_hash):
        references.create_missing()
        batch = self._create_batch(file_hash, len(rows), references)
        self.env["sales.import.record"].create([references.sale_values(row, batch) for row in rows])
        return self._success_action(batch)

    def _create_batch(self, file_hash, record_count, references, extra_values=None):
        return self.env["sales.import.batch"].create({
            "name": self.file_name or self.env._("Importación sin nombre"),
            "file_hash": file_hash,
            "record_count": record_count,
            "new_partner_count": len(references.new_partners),
            "new_product_count": len(references.new_products),
            "new_salesperson_count": len(references.new_salespersons),
            **(extra_values or {}),
        })

    def _success_action(self, batch, message=None):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "title": self.env._("Importación completada"),
                "message": message or self.env._(
                    "%(sales)s ventas importadas (%(partners)s clientes, %(products)s productos "
                    "y %(salespersons)s vendedores nuevos).",
                    sales=batch.record_count,
                    partners=batch.new_partner_count,
                    products=batch.new_product_count,
                    salespersons=batch.new_salesperson_count,
                ),
                "next": batch.action_open_records(),
            },
        }


def _record_signature(record) -> tuple:
    """Valores que definen "la misma venta" al comparar versiones (ids, no nombres)."""
    currency = record.company_id.currency_id
    return (
        record.date, record.partner_id.id, record.salesperson_id.id, record.product_id.id,
        record.quantity, currency.round(record.price_unit), currency.round(record.amount_total), record.state,
    )


def _row_signature(row, references, currency) -> tuple:
    """Como :func:`_record_signature`, para una fila del archivo. Un cliente,
    producto o vendedor que aún no existe da ``None``, que nunca coincide."""
    def resolved_id(records_by_name, name):
        record = records_by_name.get(normalize_name(name))
        return record.id if record else None

    return (
        row.date,
        resolved_id(references.partners, row.partner),
        resolved_id(references.salespersons, row.salesperson),
        resolved_id(references.products, row.product),
        row.quantity, currency.round(row.price_unit), currency.round(row.amount_total), row.state,
    )


class _SaleReferences:
    """Resuelve clientes, productos y vendedores del archivo contra la base de datos.

    Hace una consulta por modelo (no una por fila) y guarda el resultado en
    diccionarios indexados por el nombre normalizado.
    """

    def __init__(self, env, rows):
        self.env = env
        self.rows = rows
        self.partners = self._search_by_names("res.partner", "name", {row.partner for row in rows})
        self.products = self._search_products({row.product for row in rows})
        # Incluye archivados: el nombre es único también entre ellos, y crearlo de
        # nuevo violaría el índice.
        self.salespersons = self._search_by_names(
            "sales.import.salesperson", "name", {row.salesperson for row in rows}, active_test=False
        )
        self.new_partners = self._missing_names(self.partners, (row.partner for row in rows))
        self.new_products = self._missing_names(self.products, (row.product for row in rows))
        self.new_salespersons = self._missing_names(
            self.salespersons, (row.salesperson for row in rows)
        )

    def missing_master_errors(self) -> list[ParseError]:
        """Solo un Responsable puede crear clientes y productos (constitución V)."""
        if self.env.user.has_group(MANAGER_GROUP):
            return []
        errors = []
        for row in self.rows:
            if normalize_name(row.partner) in self.new_partners:
                errors.append(ParseError(row.row_number, COLUMN_LABELS["partner"], self.env._(
                    "El cliente «%(name)s» no existe; solo un Responsable puede crearlo.",
                    name=row.partner,
                )))
            if normalize_name(row.product) in self.new_products:
                errors.append(ParseError(row.row_number, COLUMN_LABELS["product"], self.env._(
                    "El producto «%(name)s» no existe; solo un Responsable puede crearlo.",
                    name=row.product,
                )))
        return errors

    def create_missing(self):
        self.partners.update(self._create_missing_partners())
        self.products.update(self._create_missing_products())
        self.salespersons.update(self._create_missing_salespersons())

    def sale_values(self, row, batch) -> dict:
        return {
            "date": row.date,
            "partner_id": self.partners[normalize_name(row.partner)].id,
            "salesperson_id": self.salespersons[normalize_name(row.salesperson)].id,
            "product_id": self.products[normalize_name(row.product)].id,
            "quantity": row.quantity,
            "price_unit": row.price_unit,
            "amount_total": row.amount_total,
            "state": row.state,
            "reference": row.reference,
            "batch_id": batch.id,
            "company_id": batch.company_id.id,
        }

    # Búsqueda

    def _search_by_names(self, model, field_name, names, active_test=True) -> dict:
        if not names:
            return {}
        domain = Domain.OR([(field_name, "=ilike", escape_psql(name))] for name in names)
        records = self.env[model].with_context(active_test=active_test).search_fetch(
            domain, [field_name], order="id"
        )
        found = {}
        for record in records:
            found.setdefault(normalize_name(record[field_name]), record)  # el más antiguo gana
        return found

    def _search_products(self, names) -> dict:
        by_code = self._search_by_names("product.product", "default_code", names)
        remaining = {name for name in names if normalize_name(name) not in by_code}
        return self._search_by_names("product.product", "name", remaining) | by_code

    @staticmethod
    def _missing_names(found, names) -> dict:
        """``{clave normalizada: nombre tal como aparece por primera vez}``."""
        missing = {}
        for name in names:
            key = normalize_name(name)
            if key not in found:
                missing.setdefault(key, name)
        return missing

    # Creación de maestros faltantes

    def _check_can_create_masters(self):
        if not self.env.user.has_group(MANAGER_GROUP):
            raise UserError(self.env._("Solo un Responsable puede crear clientes y productos."))

    def _create_missing_partners(self) -> dict:
        """EXCEPCIÓN DOCUMENTADA a la regla de sudo() (constitución V, v1.1.0).

        Un usuario interno no puede crear contactos. Condiciones: solo ``create``,
        lista blanca de campos {name}, usuario Responsable verificado y creación
        contada en el lote (``new_partner_count``).
        """
        if not self.new_partners:
            return {}
        self._check_can_create_masters()
        partners = self.env["res.partner"].sudo().create(
            [{"name": name} for name in self.new_partners.values()]
        ).sudo(False)
        return dict(zip(self.new_partners, partners))

    def _create_missing_products(self) -> dict:
        """EXCEPCIÓN DOCUMENTADA a la regla de sudo() (constitución V, v1.1.0).

        Un usuario interno no puede crear productos. Condiciones: solo ``create``,
        lista blanca de campos {name, type} (bien consumible, sin precio), usuario
        Responsable verificado y creación contada en el lote (``new_product_count``).
        """
        if not self.new_products:
            return {}
        self._check_can_create_masters()
        products = self.env["product.product"].sudo().create(
            [{"name": name, "type": "consu"} for name in self.new_products.values()]
        ).sudo(False)
        return dict(zip(self.new_products, products))

    def _create_missing_salespersons(self) -> dict:
        if not self.new_salespersons:
            return {}
        users = self._search_by_names("res.users", "name", set(self.new_salespersons.values()))
        salespersons = self.env["sales.import.salesperson"].create([
            {"name": name, "user_id": users.get(key, self.env["res.users"]).id}
            for key, name in self.new_salespersons.items()
        ])
        return dict(zip(self.new_salespersons, salespersons))

