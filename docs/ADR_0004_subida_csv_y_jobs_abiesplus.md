# ADR 0004 - Subida De CSV Y Trazabilidad De Jobs Abies+

## Estado

Aceptado.

## Contexto

V2 usaba un CSV fijo montado read-only en Docker como unica via de entrada de centros. Un tercero que quisiera aportar su propio CSV de centros tenia que reemplazar el fichero a mano, sin validacion ni informe.

Por otro lado, el Paso 3 (`pullup2Ab+.py`) registraba resultados en `abiesplus_imports` y `session_logs` con un `session_id` fijo `"abiesplus-import"`, sin integracion con la tabla `jobs`/`job_centers`. El front no podia mostrar el progreso de una importacion Abies+ como lo hace con los jobs de descarga.

## Decision

### Subida de CSV

- Se anade `db/init_db.py::sync_centers_csv(db_path, csv_path)` que valida cabeceras MECD (obligatorias: `Código Centro`, `Nombre de centro`), hace upsert por `center_code` y devuelve un informe `{total, inserted, updated, missing_expected, errors}`.
- Se anade el setting `app_settings.centers_csv_path` (migracion 7) que recuerda el CSV activo.
- El front expone `POST /centers/upload-csv` (multipart) que guarda el CSV en `data/`, actualiza el setting y sincroniza. Tambien `POST /centers/resync-csv` que re-sincroniza el CSV activo.
- UI: formulario de subida en `/centers` y seccion "CSV de centros" en `/settings` con el path activo y boton re-sync.

### Trazabilidad Abies+

- `pullup2Ab+.py` crea un `jobs` row (`mode='abiesplus'`, `source='cli'`) y registros en `job_centers` por centro, igual que el orquestador de descargas.
- Se genera un `session_id` UUID por ejecucion (`abiesplus-<uuid>`) en `session_logs` y `abiesplus_imports`, sustituyendo al string fijo.
- `LibraryRow` incluye `download_id` (leido en `load_libraries_from_sqlite`), eliminando la re-consulta `_lookup_download_id` y la posible divergencia.
- Al finalizar, `finish_abiesplus_job` computa `processed_centers`/`error_count` y el estado final (`completed`/`completed_with_errors`/`failed`).
- El front `/jobs/<id>` muestra estadisticas conscientes del modo (`abiesplus` muestra procesados/errores/dry-run en vez de descargados/saltados).

## Consecuencias

- Un tercero puede subir su CSV desde el navegador sin tocar ficheros, con validacion y反馈.
- Las importaciones Abies+ son trazables como jobs y consultables en `/jobs` y `/jobs/<id>`.
- El guard de job activo (un solo `queued`/`running`) ahora cubre tambien jobs Abies+, evitando solapar descargas e importaciones.
- `abiesplus_imports` y `job_centers` pueden tener estados `created`/`dry_run_ok` no contemplados en los contadores especificos de descargas; el conteo de Abies+ usa `processed_centers` y `error_count` genericos.

## Alternativas Consideradas

### Subida solo por reemplazo de fichero

Rechazado: no valida cabeceras ni da informe; peor experiencia para un tercero.

### Integrar Paso 3 dentro del orquestador de descargas

Rechazado por ahora: el orquestador de descargas es secuencial y por subprocess; Abies+ tiene su propio flujo de sesion Playwright. Se mantiene como script independiente con su propio job.

### Mover toda la configuracion no-secreta a SQLite ya

Aplazado (ver ADR-0002): se mantiene `.env` para credenciales y `app_settings` para defaults operativos.
