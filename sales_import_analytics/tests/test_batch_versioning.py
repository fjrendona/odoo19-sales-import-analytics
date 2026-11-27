import base64
from unittest.mock import patch

from odoo.exceptions import UserError, ValidationError
from odoo.tests import Form, tagged

from ..models.res_config_settings import VERSION_MATCH_THRESHOLD_PARAM
from .common import SALES_HEADERS, SalesImportCommon, build_xlsx

REFERENCE_HEADERS = [*SALES_HEADERS, "Referencia"]
JUNE_REFERENCES = [f"FV-{number:03d}" for number in range(1, 11)]


def ref_row(reference, day=1, month=6, quantity=2, price_unit=50000, state="Confirmada"):
    return [f"2026-{month:02d}-{day:02d}", "SIA Cliente A", "SIA Juan Pérez", "SIA Producto 1",
            quantity, price_unit, quantity * price_unit, state, reference]


def june_rows(references=JUNE_REFERENCES, **changes):
    """Filas de junio; ``changes`` = {referencia: {campo: valor}} para las que cambian."""
    return [ref_row(reference, day=int(reference[3:]) % 28 + 1, **changes.get(reference, {}))
            for reference in references]


@tagged("post_install", "-at_install")
class TestBatchVersioning(SalesImportCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Batch = cls.env["sales.import.batch"]
        cls.Record = cls.env["sales.import.record"]

    def setUp(self):
        super().setUp()
        # La BD puede tener lotes de pruebas manuales: los tests solo miran los suyos.
        self.preexisting = self.Batch.search([])
        self.wizard_for(june_rows()).action_import()
        self.june = self.test_batches()

    def wizard_for(self, rows, user=None, file_name="junio.xlsx", **values):
        return self.env["sales.import.wizard"].with_user(user or self.manager_importer).create({
            "file": base64.b64encode(build_xlsx(rows, REFERENCE_HEADERS)),
            "file_name": file_name,
            **values,
        })

    def compare(self, rows, **values):
        """Importa y comprueba que el asistente pasa al paso de comparación."""
        wizard = self.wizard_for(rows, **values)
        action = wizard.action_import()
        self.assertEqual(action["res_model"], "sales.import.wizard")
        self.assertEqual(wizard.step, "compare")
        return wizard

    def test_batches(self, domain=()):
        return self.Batch.search([("id", "not in", self.preexisting.ids), *domain])

    def current_records(self):
        """Lo que cuenta el tablero (el dominio de sus acciones), en los lotes del test."""
        return self.Record.search([("is_current", "=", True), ("batch_id", "in", self.test_batches().ids)])

    def assert_counts(self, wizard, unchanged, updated, added, missing):
        self.assertEqual(
            (wizard.unchanged_count, wizard.updated_count, wizard.added_count, wizard.missing_count),
            (unchanged, updated, added, missing),
        )

    # Comparación y nueva versión

    def test_comparison_writes_nothing_until_confirmed(self):
        wizard = self.compare(june_rows([*JUNE_REFERENCES, "FV-011"], **{"FV-002": {"quantity": 3}}))
        self.assertEqual(wizard.match_type, "version")
        self.assertEqual(wizard.candidate_batch_id, self.june)
        self.assert_counts(wizard, unchanged=9, updated=1, added=1, missing=0)
        self.assertEqual(wizard.line_ids.mapped("change_type"), ["updated", "added"])
        self.assertIn("2 × [SIA-P-001] SIA Producto 1", wizard.line_ids[0].summary_before)
        self.assertIn("3 × SIA Producto 1", wizard.line_ids[0].summary_after)
        # Cancelar es cerrar el asistente: el lote y sus ventas siguen igual.
        self.assertEqual(len(self.test_batches()), 1)
        self.assertEqual(len(self.june.record_ids), 10)
        self.assertTrue(self.june.is_current)

    def test_save_version_with_unchanged_updated_and_new_sales(self):
        wizard = self.compare(june_rows([*JUNE_REFERENCES, "FV-011"], **{"FV-002": {"quantity": 3}}))
        wizard.action_save_version()

        version = self.june.version_ids[0]
        self.assertEqual((version.version, version.previous_batch_id, version.origin_batch_id), (2, self.june, self.june))
        self.assertTrue(version.is_current)
        self.assertFalse(self.june.is_current)
        self.assertEqual(
            (version.unchanged_count, version.updated_count, version.added_count, version.removed_count), (9, 1, 1, 0)
        )
        by_reference = {record.reference: record for record in version.record_ids}
        self.assertEqual(by_reference["FV-002"].change_type, "updated")
        self.assertEqual(by_reference["FV-002"].quantity, 3)
        self.assertEqual(by_reference["FV-011"].change_type, "added")
        self.assertEqual(by_reference["FV-001"].change_type, "unchanged")
        # El tablero solo cuenta la versión vigente: 11 ventas, no 21.
        self.assertEqual(self.current_records().batch_id, version)
        self.assertEqual(len(self.current_records()), 11)
        self.assertEqual(self.june.display_name, "junio.xlsx")
        self.assertEqual(version.display_name, "junio.xlsx (v2)")

    def test_dashboard_totals_only_count_current_versions(self):
        self.compare(june_rows(**{"FV-001": {"quantity": 4}})).action_save_version()
        [(total,)] = self.Record._read_group(
            [("is_current", "=", True), ("batch_id", "in", self.test_batches().ids)], aggregates=["amount_total:sum"]
        )
        self.assertEqual(total, 9 * 100000 + 200000)

    def test_missing_sale_is_kept_unless_confirmed(self):
        wizard = self.compare(june_rows(JUNE_REFERENCES[:-1], **{"FV-001": {"quantity": 3}}))
        self.assert_counts(wizard, unchanged=8, updated=1, added=0, missing=1)
        missing_line = wizard.line_ids.filtered(lambda line: line.change_type == "missing")
        self.assertEqual(missing_line.reference, "FV-010")
        wizard.action_save_version()

        version = self.june.version_ids[0]
        self.assertIn("FV-010", version.record_ids.mapped("reference"))
        self.assertEqual(len(version.record_ids), 10)
        self.assertEqual((version.unchanged_count, version.removed_count), (9, 0))

    def test_confirmed_missing_sale_is_removed_from_the_new_version(self):
        wizard = self.compare(june_rows(JUNE_REFERENCES[:-1]))
        wizard.line_ids.filtered(lambda line: line.change_type == "missing").confirm_removal = True
        wizard.action_save_version()

        version = self.june.version_ids[0]
        self.assertNotIn("FV-010", version.record_ids.mapped("reference"))
        self.assertEqual((len(version.record_ids), version.removed_count), (9, 1))
        # La versión anterior conserva la venta: el historial no se pierde.
        self.assertIn("FV-010", self.june.record_ids.mapped("reference"))

    def test_replace_batch_deletes_all_versions(self):
        self.compare(june_rows(**{"FV-001": {"quantity": 3}})).action_save_version()
        wizard = self.compare(june_rows(**{"FV-001": {"quantity": 5}}))
        wizard.action_replace_batch()

        (batch,) = self.test_batches()
        self.assertEqual((batch.version, batch.previous_batch_id), (1, self.Batch))
        self.assertEqual(len(batch.record_ids), 10)
        self.assertEqual(
            batch.record_ids.filtered(lambda record: record.reference == "FV-001").quantity, 5
        )

    def test_restore_previous_version(self):
        self.compare(june_rows(**{"FV-001": {"quantity": 3}})).action_save_version()
        version = self.june.version_ids[0]
        self.june.with_user(self.manager_importer).action_restore()

        self.assertTrue(self.june.is_current)
        self.assertFalse(version.is_current)
        self.assertEqual(self.current_records().batch_id, self.june)
        with self.assertRaises(UserError):
            version.with_user(self.user_importer).action_restore()

    def test_reuploading_a_superseded_version_proposes_a_new_version(self):
        rows = june_rows(**{"FV-001": {"quantity": 3}})
        self.compare(rows).action_save_version()
        self.june.with_user(self.manager_importer).action_restore()
        wizard = self.compare(rows)  # misma huella que la v2, que ya no es vigente
        self.assertEqual(wizard.candidate_batch_id, self.june)
        self.assert_counts(wizard, unchanged=9, updated=1, added=0, missing=0)
        wizard.action_save_version()
        self.assertEqual(self.june.version_ids.mapped("version"), [3, 2, 1])

    def test_deleting_current_version_restores_the_previous_one(self):
        self.compare(june_rows(**{"FV-001": {"quantity": 3}})).action_save_version()
        self.june.version_ids[0].unlink()
        self.assertTrue(self.june.is_current)
        self.assertEqual(len(self.current_records()), 10)

    def test_saving_a_version_sends_a_single_notification(self):
        wizard = self.compare(june_rows([*JUNE_REFERENCES, "FV-011"], **{"FV-002": {"quantity": 3}}))
        with patch.object(type(self.env["bus.bus"]), "_sendone", autospec=True) as sendone:
            wizard.action_save_version()
        self.assertEqual(sendone.call_count, 1)

    # Corrección parcial

    def partial_rows(self):
        """5 ventas: 4 del lote (una corregida) y una nueva. Cubre el 80 % del archivo
        y solo el 40 % del lote."""
        return june_rows(["FV-002", "FV-003", "FV-004", "FV-005", "FV-011"], **{"FV-002": {"quantity": 3}})

    def test_partial_correction_is_detected(self):
        wizard = self.compare(self.partial_rows())
        self.assertEqual(wizard.match_type, "partial")
        self.assert_counts(wizard, unchanged=3, updated=1, added=1, missing=0)
        self.assertNotIn("missing", wizard.line_ids.mapped("change_type"))
        self.assertIn("5 ventas; 4 ya están", wizard.match_summary)

    def test_apply_partial_correction_to_the_existing_batch(self):
        fv_010 = self.june.record_ids.filtered(lambda record: record.reference == "FV-010")
        self.compare(self.partial_rows()).action_apply_changes()

        self.assertEqual(len(self.test_batches()), 1)  # sin versión nueva
        self.assertEqual((self.june.version, self.june.record_count), (1, 11))
        self.assertEqual((self.june.updated_count, self.june.added_count), (1, 1))
        by_reference = {record.reference: record for record in self.june.record_ids}
        self.assertEqual((by_reference["FV-002"].quantity, by_reference["FV-002"].change_type), (3, "updated"))
        self.assertEqual(by_reference["FV-011"].change_type, "added")
        self.assertEqual(by_reference["FV-010"], fv_010)  # las que no vienen no se tocan
        self.assertFalse(fv_010.change_type)

    def test_full_file_after_partial_correction_has_no_changes(self):
        self.compare(self.partial_rows()).action_apply_changes()
        corrected = june_rows([*JUNE_REFERENCES, "FV-011"], **{"FV-002": {"quantity": 3}})
        with self.assertRaises(UserError) as error:
            self.wizard_for(corrected, file_name="junio_corregido.xlsx").action_import()
        self.assertIn("no trae cambios", str(error.exception))

    def test_known_file_with_half_the_batch_is_a_version_not_a_partial_correction(self):
        wizard = self.compare(june_rows(JUNE_REFERENCES[:6]))
        self.assertEqual(wizard.match_type, "version")
        self.assert_counts(wizard, unchanged=6, updated=0, added=0, missing=4)

    # Casos que no son versiones

    def test_below_threshold_is_imported_as_a_new_batch(self):
        references = JUNE_REFERENCES[:5] + [f"FV-1{number:02d}" for number in range(20)]
        action = self.wizard_for(june_rows(references), file_name="otro.xlsx").action_import()
        self.assertEqual(action["tag"], "display_notification")
        self.assertEqual(len(self.test_batches()), 2)
        self.assertTrue(self.june.is_current)

    def test_threshold_is_read_from_settings(self):
        rows = june_rows([*JUNE_REFERENCES[:8], "FV-011", "FV-012"])  # 80 % del lote y del archivo
        self.assertEqual(self.wizard_for(rows).action_import()["res_model"], "sales.import.wizard")

        settings = Form(self.env["res.config.settings"])
        settings.sales_import_version_threshold = 0.95
        settings.save().execute()
        self.assertEqual(self.env["ir.config_parameter"].get_param(VERSION_MATCH_THRESHOLD_PARAM), "0.95")
        action = self.wizard_for(rows, file_name="otro.xlsx").action_import()
        self.assertEqual(action["tag"], "display_notification")

    def test_threshold_must_be_a_valid_proportion(self):
        for value in (0, 1.5):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.env["res.config.settings"].create({"sales_import_version_threshold": value})

    def test_sales_from_another_month_are_a_new_batch(self):
        july = [ref_row(reference, month=7) for reference in JUNE_REFERENCES]
        action = self.wizard_for(july, file_name="julio.xlsx").action_import()
        self.assertEqual(action["tag"], "display_notification")
        self.assertEqual(len(self.test_batches([("is_current", "=", True)])), 2)

    def test_file_without_reference_column_is_never_a_version(self):
        rows = [row[:-1] for row in june_rows(**{"FV-001": {"quantity": 3}})]
        content = build_xlsx(rows)
        wizard = self.env["sales.import.wizard"].with_user(self.manager_importer).create({
            "file": base64.b64encode(content), "file_name": "sin_referencia.xlsx",
        })
        self.assertEqual(wizard.action_import()["tag"], "display_notification")
        self.assertEqual(len(self.test_batches()), 2)

    def test_exact_duplicate_is_blocked_and_forcing_imports_a_new_batch(self):
        with self.assertRaises(UserError) as error:
            self.wizard_for(june_rows()).action_import()
        self.assertIn("ya se importó", str(error.exception))

        action = self.wizard_for(june_rows(), force_reimport=True).action_import()
        self.assertEqual(action["tag"], "display_notification")
        self.assertEqual(len(self.test_batches([("file_hash", "=", self.june.file_hash)])), 2)

    def test_user_cannot_create_versions(self):
        with self.assertRaises(UserError) as error:
            self.wizard_for(june_rows(**{"FV-001": {"quantity": 3}}), user=self.user_importer).action_import()
        self.assertIn("Solo un Responsable", str(error.exception))

    def test_batch_changed_during_review_is_rejected(self):
        wizard = self.compare(june_rows(**{"FV-001": {"quantity": 3}}))
        self.compare(june_rows(**{"FV-001": {"quantity": 4}})).action_save_version()
        with self.assertRaises(UserError) as error:
            wizard.action_save_version()
        self.assertIn("cambió mientras revisaba", str(error.exception))

    def test_actions_only_apply_to_the_detected_match_type(self):
        version = self.compare(june_rows(**{"FV-001": {"quantity": 3}}))
        with self.assertRaises(UserError):
            version.action_apply_changes()
        partial = self.compare(self.partial_rows())
        for action in (partial.action_save_version, partial.action_replace_batch):
            with self.subTest(action=action.__name__), self.assertRaises(UserError):
                action()
        self.assertEqual(len(self.test_batches()), 1)

    def test_existing_batches_default_to_current_first_version(self):
        # Equivale a los lotes creados antes de esta funcionalidad: sin valores de versión.
        batch = self.Batch.create({"name": "antiguo.xlsx", "file_hash": "sia-legacy"})
        self.assertEqual((batch.version, batch.is_current, batch.version_count), (1, True, 1))
        record = self.Record.create(self._sale_values())  # venta creada a mano, sin lote
        self.assertTrue(record.is_current)
