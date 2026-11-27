"""Comparación de un archivo con un lote ya importado, por la referencia de cada venta.

Módulo puro, como el parser: trabaja con referencias y valores comparables, sin
ORM, así que se prueba sin base de datos. El asistente decide qué hacer con el
resultado.
"""

from dataclasses import dataclass

# "La mayoría de las ventas del lote no vienen en el archivo": por debajo de esta
# cobertura del lote, un archivo conocido es una corrección parcial, no una versión.
MAJORITY_SHARE = 0.5

MATCH_VERSION = "version"
MATCH_PARTIAL = "partial"


@dataclass(frozen=True)
class BatchDiff:
    """Referencias por categoría (ordenadas, para un resultado estable)."""

    unchanged: list[str]
    updated: list[str]
    added: list[str]
    missing: list[str]

    @property
    def has_changes(self) -> bool:
        return bool(self.updated or self.added or self.missing)


def diff_by_reference(previous: dict, incoming: dict) -> BatchDiff:
    """Compara ``{referencia: valores comparables}`` del lote y del archivo.

    Los valores deben ser comparables con ``==`` (tuplas de ids, fechas e importes
    ya redondeados): qué cuenta como "mismo dato" lo decide quien los construye.
    """
    common = previous.keys() & incoming.keys()
    return BatchDiff(
        unchanged=sorted(ref for ref in common if previous[ref] == incoming[ref]),
        updated=sorted(ref for ref in common if previous[ref] != incoming[ref]),
        added=sorted(incoming.keys() - previous.keys()),
        missing=sorted(previous.keys() - incoming.keys()),
    )


def classify_match(batch_reference_count: int, file_reference_count: int, common_count: int,
                   threshold: float) -> str | None:
    """Decide si el archivo es una versión nueva del lote, una corrección parcial o ninguna.

    - El archivo cubre casi todo el lote: versión nueva.
    - Casi todo el archivo ya está en el lote, pero trae menos de la mitad del
      lote: corrección parcial (se aplica sobre el mismo lote).
    - Casi todo el archivo ya está en el lote y trae al menos la mitad: versión
      nueva, porque importarlo como lote aparte duplicaría esas ventas.
    """
    if not batch_reference_count or not file_reference_count:
        return None
    batch_coverage = common_count / batch_reference_count
    file_coverage = common_count / file_reference_count
    if batch_coverage >= threshold:
        return MATCH_VERSION
    if file_coverage < threshold:
        return None
    return MATCH_PARTIAL if batch_coverage < MAJORITY_SHARE else MATCH_VERSION
