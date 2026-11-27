import { onWillDestroy, status } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { ListController } from "@web/views/list/list_controller";
import { listView } from "@web/views/list/list_view";

import { createTrailingThrottle } from "../../utils/trailing_throttle";

/** Debe coincidir con NEW_RECORDS_NOTIFICATION en models/sales_import_record.py. */
export const NEW_RECORDS_NOTIFICATION = "sales_import_analytics/new_records";
export const RELOAD_INTERVAL_MS = 2000;

/**
 * Lista nativa que se recarga sola cuando el servidor avisa de ventas nuevas.
 *
 * Solo cambia el comportamiento del controlador: el renderer, el modelo y el
 * template son los de la lista estándar. La recarga usa `model.load()`, que
 * conserva filtros, agrupaciones, orden y paginación.
 */
export class SalesRealtimeListController extends ListController {
    setup() {
        super.setup();
        this.busService = useService("bus_service");
        this.reloadThrottle = createTrailingThrottle(() => this.reloadList(), RELOAD_INTERVAL_MS);
        // Función propia de esta instancia: el servicio guarda un mapa
        // callback -> wrapper, así que no se puede compartir entre instancias.
        this.onNewRecords = () => this.reloadThrottle.call();
        this.busService.subscribe(NEW_RECORDS_NOTIFICATION, this.onNewRecords);

        // onWillDestroy y no onWillUnmount: el componente puede destruirse sin
        // haber llegado a montarse, y la suscripción de setup() quedaría huérfana.
        onWillDestroy(() => {
            this.busService.unsubscribe(NEW_RECORDS_NOTIFICATION, this.onNewRecords);
            this.reloadThrottle.cancel();
        });
    }

    async reloadList() {
        if (status(this) === "destroyed") {
            return;
        }
        // Primera carga aún en curso (el aviso llegó mientras se abría la lista, p. ej.
        // las ventas de un lote recién guardado): la lista se monta antes de tener
        // datos y model.load() descarta la carga anterior (keepLast), así que la
        // carga inicial no terminaría nunca y la lista quedaría en blanco.
        // Tampoco se descarta una edición en línea ni una selección de filas.
        // En ambos casos se reintenta en la siguiente ventana del throttle.
        if (!this.model.isReady || this.isUserBusy()) {
            this.reloadThrottle.call();
            return;
        }
        await this.model.load();
    }

    isUserBusy() {
        const { root } = this.model;
        return Boolean(root.editedRecord || root.selection.length);
    }
}

export const salesRealtimeListView = {
    ...listView,
    Controller: SalesRealtimeListController,
};

registry.category("views").add("sales_import_realtime_list", salesRealtimeListView);
