# Arquitectura AbiesZipZap - AbiesWeb a Abies+

## Vision General

AbiesZipZap automatiza dos fases secuenciales y trazables:

1. **Descarga (Paso 1)**: localiza una biblioteca en AbiesWeb, impersona un gestor valido, cambia al perfil Administrador AbiesWeb, exporta y descarga el ZIP. Orquestada por lotes desde `orchestrator.py`.
2. **Importacion (Paso 3)**: crea bibliotecas en Abies+ usando los ZIP descargados. Ejecutada por `pullup2Ab+.py`.

Repositorio original: https://github.com/clmmakers/AbiesZipZap

Licencia: GNU Affero General Public License v3.0 o posterior (`AGPL-3.0-or-later`).

SQLite es la **unica fuente operativa**: seleccion de centros, estados, jobs, logs, descargas y correcciones manuales de datos. El CSV de centros es solo fuente inicial/sincronizacion.

## Componentes

```
v2/
├── buscar_gestores_abiesweb.py   Paso 1: 1 centro/ejecucion (Playwright)
├── abies_backup.py               Vendorizado: Config, init_browser, login, new_context, save_auth_state
├── orchestrator.py               Orquestador secuencial de descargas (jobs/job_centers)
├── pullup2Ab+.py                 Paso 3: crea bibliotecas en Abies+ (jobs/job_centers)
├── data/
│   ├── centros_example.csv       CSV ficticio de ejemplo (cabeceras MECD)
│   └── v2.sqlite3                BD operativa
├── db/
│   ├── init_db.py                init_db, seed_centers, sync_centers_csv (migraciones idempotentes)
│   └── schema.sql                Esquema SQLite
├── front/                        Flask en :8010 (centros, jobs, abies+, logs, settings)
├── data/v2.sqlite3               BD operativa
├── Dockerfile / docker-compose   2 servicios: abies-v2 (worker) + abies-v2-front
└── docs/                         PRD, ADRs, arquitectura, guia usuario
```

## Flujo De Descarga (Paso 1)

`orchestrator.py` lee centros seleccionados de SQLite, crea un `jobs` row y por cada centro lanza `buscar_gestores_abiesweb.py` por subprocess:

1. Sincroniza el CSV de `data/` contra SQLite (`sync_centers_csv`, solo si se pasa `--csv`).
2. Login AbiesWeb (reutiliza `AUTH_STATE_FILE` si existe).
3. Gestion > Gestores > filtra U.O. por codigo de centro + perfil `adminbiblioteca`.
4. Elige el primer usuario sin perfil `adminEDAE`; "Ver como...".
5. Cambia a perfil "Administrador AbiesWeb".
6. Herramientas > Exportacion.
7. Politica de reutilizacion: lee "generado el dia: DD/MM/YYYY" y `export_reuse_max_age_days`. Si cumple, descarga el ZIP existente; si no, relanza y espera al 100%.
8. Descarga el ZIP a `downloads/<codigo>_<nombre>/`.
9. Registra `abiesweb_checks`, `abiesweb_users`, `downloads`, `session_logs`.
10. Logout.

Codigos de salida: `0` OK, `1` error, `2` centro no encontrado, `3` sin gestores impersonables.

## Flujo De Importacion (Paso 3)

`pullup2Ab+.py` lee centros con ultima descarga `downloaded` de SQLite:

1. Crea un `jobs` row (`mode='abiesplus'`) y `job_centers` por centro.
2. Login Abies+, navega a Bibliotecas.
3. Por cada centro: extrae el XML del ZIP, valida DB vs XML (nombre y provincia son fallos graves; CP y direccion son avisos), rellena el formulario "Nueva biblioteca", sube el XML.
4. Dry-run: rellena y cancela (no guarda). Real: "Guardar y nuevo" / "Guardar".
5. Registra `abiesplus_imports` y `session_logs` con `session_id` UUID por ejecucion.
6. Actualiza `job_centers` y finaliza el job.

## Esquema SQLite

Tablas principales (ver `db/schema.sql`):

| Tabla | Clave | Proposito |
|---|---|---|
| `centers` | `center_code` (PK) | Centros. `selected`, `enabled`, `priority` controlan seleccion. |
| `app_settings` | `key` (PK) | Configuracion operativa (timeouts, reutilizacion, defaults Abies+). |
| `abiesweb_checks` | `id` | Cada comprobacion de un centro en AbiesWeb. `status`: users_found, no_users_found, center_not_found, error. |
| `abiesweb_users` | `id` | Gestores detectados por check. |
| `downloads` | `id` | Descargas de ZIP. `status`: downloaded, error. `download_mode`: reused_existing_export, reexported_*, waited_*. |
| `abiesplus_imports` | `id` | Intentos de creacion en Abies+. `status`: created, dry_run_ok, error. |
| `jobs` | `id` | Jobs (descarga o Abies+). `mode`: selected_only, all_enabled, only_codes, abiesplus. |
| `job_centers` | (`job_id`, `center_code`) | Resultado por centro dentro de un job. |
| `session_logs` | `id` | Log audit por sesion/job. |
| `schema_migrations` | `version` | Versiones de migracion aplicadas. |

`centers.center_type` se normaliza desde `Denominación Centro` del CSV contra la lista de tipos admitidos por Abies+. Valores no reconocidos se guardan como `Biblioteca`.

### Settings clave (`app_settings`)

| Key | Default | Significado |
|---|---|---|
| `export_reuse_max_age_days` | `0` | Margen de dias para reutilizar ZIP existente; 0 = solo hoy. |
| `local_timezone` | `Europe/Madrid` | Zona para comparar fechas DD/MM/YYYY de AbiesWeb. |
| `orchestrator_default_retries` | `0` | Reintentos por error tecnico. |
| `orchestrator_export_timeout_seconds` | `2400` | Timeout de exportacion por centro. |
| `orchestrator_skip_downloaded_today` | `true` | Saltar centros descargados hoy. |
| `abiesplus_default_dry_run` | `true` | Dry-run por defecto desde el front. |
| `abiesplus_default_usar_registro` | `false` | Checkbox "usar registro en ejemplares". |
| `abiesplus_default_syncusers` | `false` | Checkbox sincronizacion de usuarios. |
| `abiesplus_default_importusers` | `false` | Checkbox importacion masiva de usuarios. |
| `centers_csv_path` | `v2/data/centros_example.csv` | CSV activo para sincronizar. |

## Politica De Reutilizacion De Exportaciones

Con `export_reuse_max_age_days = 0`, solo se reutiliza un ZIP si la fecha detectada en AbiesWeb ("generado el dia: DD/MM/YYYY") es hoy segun `local_timezone`. En otro caso se relanza la exportacion y se vigila la barra de progreso. Si AbiesWeb muestra "error" en `#resultadoEvolucion`, se aborta inmediatamente (no espera al timeout).

## Configuracion

- `.env` (credenciales y selectores; ver `.env.example`). Solo NIVEL 1 es obligatorio para el usuario.
- `app_settings` (SQLite): defaults operativos, editables desde `/settings`.
- Selectores de menu AbiesWeb ahora overridables por env (`SELECTOR_GESTION_MENU`, `SELECTOR_GESTORES_SUBMENU`, `SELECTOR_TOOLS_MENU`, `SELECTOR_EXPORT_SUBMENU`).
- `DOWNLOAD_TIMEOUT_MS` (default 120000): timeout de descarga del ZIP.
- Abies+ local: si la aplicacion genera enlaces o formularios con `abiesplus-local`, `ABIESPLUS_BASE_URL` debe usar ese mismo origen para compartir la cookie de sesion.

## Operaciones (CLI)

Construir e inicializar (desde `v2/`):

```bash
docker compose build
docker compose run --rm abies-v2 python v2/db/init_db.py
```

Descargar centros seleccionados:

```bash
docker compose run --rm abies-v2 python v2/orchestrator.py --selected-only
```

Importar a Abies+ (dry-run):

```bash
docker compose run --rm abies-v2 python v2/pullup2Ab+.py --selected-only --non-interactive --dry-run
```

Front:

```bash
docker compose up -d abies-v2-front   # http://127.0.0.1:8010
```

### Consultas SQLite utiles

```bash
docker compose run --rm abies-v2 sqlite3 /app/v2/data/v2.sqlite3 "SELECT key,value FROM app_settings ORDER BY key;"
docker compose run --rm abies-v2 sqlite3 /app/v2/data/v2.sqlite3 "SELECT id,mode,status,total_centers,processed_centers,error_count FROM jobs ORDER BY id DESC LIMIT 10;"
docker compose run --rm abies-v2 sqlite3 /app/v2/data/v2.sqlite3 "SELECT center_code,status,download_mode,file_name FROM downloads ORDER BY id DESC LIMIT 10;"
```

## Migraciones

Idempotentes en `db/init_db.py::ensure_columns` (ALTER TABLE + INSERT OR IGNORE). Ejecutar `init_db.py` tras actualizar. Version actual: 7.

## Limitaciones Conocidas

- Un solo job activo a la vez (descarga o Abies+).
- El salto por "descargado hoy" no verifica la existencia fisica del ZIP en disco.
- `abies_backup.py` esta duplicado respecto al padre; sincronizar manualmente si el padre cambia.
- El mapeo de provincia por defecto usa `53` para Extranjero; configurar `ABIESPLUS_PROVINCE_EQUIVALENCES` si tu instancia usa otro codigo (p.ej. `60=extranjero`).
