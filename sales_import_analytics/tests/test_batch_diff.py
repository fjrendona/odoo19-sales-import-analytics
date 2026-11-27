from odoo.tests import BaseCase, tagged

from ..tools.batch_diff import (
    MATCH_PARTIAL,
    MATCH_VERSION,
    classify_match,
    diff_by_reference,
)


@tagged("post_install", "-at_install")
class TestBatchDiff(BaseCase):
    """Comparación pura, sin base de datos."""

    def test_diff_categories(self):
        diff = diff_by_reference(
            {"A": (1, 2), "B": (1, 2), "C": (1, 2)},
            {"A": (1, 2), "B": (9, 2), "D": (1, 2)},
        )
        self.assertEqual(diff.unchanged, ["A"])
        self.assertEqual(diff.updated, ["B"])
        self.assertEqual(diff.added, ["D"])
        self.assertEqual(diff.missing, ["C"])
        self.assertTrue(diff.has_changes)

    def test_identical_content_has_no_changes(self):
        self.assertFalse(diff_by_reference({"A": (1,)}, {"A": (1,)}).has_changes)

    def test_classify_match(self):
        cases = [
            # (refs del lote, refs del archivo, comunes, esperado)
            (10, 11, 10, MATCH_VERSION),  # archivo completo con una nueva
            (10, 9, 9, MATCH_VERSION),  # archivo completo con una faltante
            (10, 5, 4, MATCH_PARTIAL),  # pocas filas, casi todas conocidas
            (10, 6, 6, MATCH_VERSION),  # todo conocido pero trae más de la mitad del lote
            (10, 25, 5, None),  # comparte pocas referencias
            (10, 10, 0, None),
            (0, 10, 0, None),
        ]
        for batch_count, file_count, common, expected in cases:
            with self.subTest(batch=batch_count, file=file_count, common=common):
                self.assertEqual(classify_match(batch_count, file_count, common, 0.8), expected)
