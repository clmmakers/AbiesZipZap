# AbiesZipZap - AbiesWeb a Abies+

AbiesZipZap es un flujo local y contenerizado para localizar bibliotecas en AbiesWeb, impersonar un gestor valido, exportar/descargar el ZIP y registrar el resultado en SQLite, y luego crear las bibliotecas en Abies+.

AbiesZipZap es autonomo: no depende de ningun proyecto padre. Todo el runtime, los scripts y `abies_backup.py` viven dentro de este directorio.

Repositorio original: https://github.com/clmmakers/AbiesZipZap

Licencia: GNU Affero General Public License v3.0 o posterior (`AGPL-3.0-or-later`). Consulta `LICENSE` y `NOTICE.md`.

## Instalacion (para un tercero)

Requisitos: Docker con soporte de Compose v2.

1. Copiar la plantilla de configuracion y rellenar credenciales:

```bash
cp .env.example .env
# edita .env: ABIES_BASE_URL, ABIES_USERNAME, ABIES_PASSWORD,
# ABIESPLUS_BASE_URL, ABIESPLUS_USERNAME, ABIESPLUS_PASSWORD,
# ABIESPLUS_PROVINCE_EQUIVALENCES y PRELOGIN_CENTER_TEXT si aplica
```

2. (Opcional) Prepara tu CSV de centros (cabeceras MECD, cualquier nombre de archivo). El CSV de ejemplo ficticio distribuido esta en `data/centros_example.csv`. Puedes subirlo despues desde el front o cargarlo por CLI.

3. Construir la imagen:

```bash
docker compose build
```

4. Crea las carpetas de datos runtime:

```bash
mkdir -p data logs downloads state debug_gestores
```

> Si Docker las crea como root, el contenedor no podra escribir. Crealas tu antes del primer arranque.

5. Inicializar la base SQLite (crea el esquema, sin centros):

```bash
docker compose run --rm abies-v2 python v2/db/init_db.py
```

Si ya tienes un CSV y quieres cargarlo por CLI:

```bash
docker compose run --rm abies-v2 python v2/db/init_db.py v2/data/mi_centros.csv
```

6. Arrancar el front:

```bash
docker compose up -d abies-v2-front
```

Abrir `http://127.0.0.1:8010/centers`.

## Preparacion

Construir la imagen de AbiesZipZap:

```bash
docker compose build abies-v2
```

Inicializar la base SQLite (esquema, sin centros):

```bash
docker compose run --rm abies-v2 python v2/db/init_db.py
```

Sincronizar un CSV de centros (cualquier nombre de archivo, valida cabeceras MECD):

```bash
docker compose run --rm abies-v2 python v2/db/init_db.py v2/data/mi_centros.csv
```

Tambien se puede subir el CSV desde el front (`/centers` > "Subir CSV" o `/settings` > "Re-sincronizar CSV").

La base queda en:

```text
data/v2.sqlite3
```

El CSV solo actua como fuente inicial/sincronizacion de centros. Los estados, logs, usuarios y descargas se guardan en SQLite. A partir de ahi, SQLite es la unica fuente operativa.

AbiesZipZap usa configuracion local en:

```text
.env
```

Se distribuye una plantilla `.env.example` sin secretos: copiala a `.env` y rellena las credenciales de NIVEL 1 (AbiesWeb y Abies+). El archivo `.env` queda ignorado por Git mediante `.gitignore` y se monta en Docker como `/app/.env`.

## Probar Un Centro

Listar gestores sin impersonar ni descargar:

```bash
docker compose run --rm abies-v2 python v2/buscar_gestores_abiesweb.py --index 0 --list-only
```

Procesar un centro por indice del CSV:

```bash
docker compose run --rm abies-v2 python v2/buscar_gestores_abiesweb.py --index 0
```

Procesar un centro por codigo:

```bash
docker compose run --rm abies-v2 python v2/buscar_gestores_abiesweb.py --center-code 60001066
```

Procesar un centro aumentando el tiempo maximo de espera de exportacion:

```bash
docker compose run --rm abies-v2 python v2/buscar_gestores_abiesweb.py --index 1 --export-timeout-seconds 1200
```

Guardar HTML y captura de diagnostico:

```bash
docker compose run --rm abies-v2 python v2/buscar_gestores_abiesweb.py --index 1 --debug
```

Los diagnosticos se guardan en:

```text
v2/debug_gestores/
```

## Orquestador Secuencial

El orquestador procesa centros desde SQLite, crea un registro en `jobs`, registra cada centro en `job_centers` y ejecuta el script individual de un centro en cada paso.

Procesar los centros seleccionados en el front:

```bash
docker compose run --rm abies-v2 python v2/orchestrator.py --selected-only
```

Procesar codigos concretos:

```bash
docker compose run --rm abies-v2 python v2/orchestrator.py --only-codes 60000116,60001066
```

Probar la seleccion sin crear job ni descargar:

```bash
docker compose run --rm abies-v2 python v2/orchestrator.py --selected-only --dry-run
```

Permitir un reintento por error tecnico:

```bash
docker compose run --rm abies-v2 python v2/orchestrator.py --selected-only --retries 1
```

Forzar entrada en AbiesWeb aunque haya descarga local registrada hoy:

```bash
docker compose run --rm abies-v2 python v2/orchestrator.py --selected-only --force-remote-check
```

Por defecto, el orquestador salta centros con ultima descarga `downloaded` cuya `downloaded_at` sea hoy segun `local_timezone`. No comprueba si el ZIP existe fisicamente en disco.

Solo se permite un job activo a la vez. Si existe un job `queued` o `running`, el orquestador termina con error.

Settings relacionadas:

```text
orchestrator_default_retries = 0
orchestrator_export_timeout_seconds = 2400
orchestrator_skip_downloaded_today = true
```

## Salidas

Los ZIP descargados se guardan en:

```text
v2/downloads/<codigo>_<nombre_normalizado>/
```

Ejemplo:

```text
v2/downloads/60001066_COLEGIO_ESPA_OL_DE_RABAT/exportacionAbiesWeb_40.zip
```

## Politica De Reutilizacion De Exportaciones

La fecha se lee desde el texto de AbiesWeb:

```text
El fichero de exportacion fue generado el dia: DD/MM/YYYY
```

El formato esperado es `DD/MM/YYYY`.

Configuracion por defecto en SQLite:

```text
export_reuse_max_age_days = 0
local_timezone = Europe/Madrid
```

Con `export_reuse_max_age_days = 0`, solo se reutiliza un ZIP existente si la fecha detectada es hoy segun `Europe/Madrid`. Si no cumple, el script relanza la exportacion y vigila la barra de progreso; no espera que AbiesWeb refresque el parrafo de fecha.

Cambiar margen de reutilizacion a 10 dias:

```bash
sqlite3 v2/data/v2.sqlite3 "UPDATE app_settings SET value='10', updated_at=CURRENT_TIMESTAMP WHERE key='export_reuse_max_age_days';"
```

Volver a solo hoy:

```bash
sqlite3 v2/data/v2.sqlite3 "UPDATE app_settings SET value='0', updated_at=CURRENT_TIMESTAMP WHERE key='export_reuse_max_age_days';"
```

Ver configuracion actual:

```bash
sqlite3 v2/data/v2.sqlite3 "SELECT key, value FROM app_settings ORDER BY key;"
```

## Consultas SQLite Utiles

Resumen de tablas principales:

```bash
sqlite3 v2/data/v2.sqlite3 "SELECT 'centers', COUNT(*) FROM centers UNION ALL SELECT 'checks', COUNT(*) FROM abiesweb_checks UNION ALL SELECT 'users', COUNT(*) FROM abiesweb_users UNION ALL SELECT 'downloads', COUNT(*) FROM downloads UNION ALL SELECT 'logs', COUNT(*) FROM session_logs;"
```

Ultimas descargas:

```bash
sqlite3 v2/data/v2.sqlite3 "SELECT id, center_code, status, download_mode, export_date_detected, export_age_days, export_reuse_max_age_days, file_name, file_size_bytes, downloaded_at FROM downloads ORDER BY id DESC LIMIT 10;"
```

Logs recientes de un centro:

```bash
sqlite3 v2/data/v2.sqlite3 "SELECT action, status, message, created_at FROM session_logs WHERE center_code='60001066' ORDER BY id DESC LIMIT 20;"
```

Centros disponibles:

```bash
sqlite3 v2/data/v2.sqlite3 "SELECT center_code, center_name, city, email FROM centers ORDER BY center_code;"
```

## Front Local

El front es una consola Flask simple para revisar centros, seleccionar centros, consultar detalles, modificar configuracion y ver logs.

Construir imagen con Flask:

```bash
docker compose build abies-v2-front
```

Arrancar front:

```bash
docker compose up -d abies-v2-front
```

Abrir en el navegador:

```text
http://127.0.0.1:8010/centers
```

Parar front:

```bash
docker compose stop abies-v2-front
```

Pantallas disponibles:

```text
/centers              listado, filtros y seleccion por checkbox
/centers/<codigo>     detalle de centro, checks, usuarios, descargas y logs
/jobs                 listado de jobs y comando recomendado
/jobs/<id>            detalle de job, centros y logs
/settings             configuracion app_settings
/logs                 logs globales filtrables
```

La seleccion se guarda en `centers.selected`. El front puede lanzar un job en segundo plano con `POST /jobs/start` y tambien muestra el comando CLI equivalente. El proceso escribe progreso en SQLite y se consulta desde `/jobs`.

La salida stdout/stderr de jobs lanzados desde el front se guarda en:

```text
v2/logs/orchestrator_front.log
```

## Comportamiento Del Script Actual

El script `v2/buscar_gestores_abiesweb.py` procesa un unico centro cada vez.

Flujo:

1. Sincroniza el CSV de `data/` contra SQLite con `center_code` como clave primaria.
2. Busca la U.O. en AbiesWeb con `Nombre de centro (Codigo Centro)`.
3. Filtra perfil `adminbiblioteca`.
4. Elige el primer usuario que no tenga perfil `adminEDAE`.
5. Hace `Ver como...`.
6. Cambia al perfil `Administrador AbiesWeb`.
7. Entra en `Herramientas > Exportacion`.
8. Decide si reutiliza ZIP existente o relanza exportacion segun `export_reuse_max_age_days`.
9. Descarga el ZIP.
10. Guarda centro, check, usuarios, descarga y logs en SQLite.
11. Pulsa `salir` y registra `logout_ok`, `logout_unavailable` o `logout_error`.

## Estados Registrados

Estados habituales en `abiesweb_checks.status`:

```text
users_found
no_users_found
center_not_found
error
```

Estados habituales en `downloads.status`:

```text
downloaded
error
```

Modos habituales en `downloads.download_mode`:

```text
reused_existing_export
reexported_old_export
reexported_unknown_date
reexported_no_existing_export
waited_running_export
waited_running_export_unknown_initial_progress
```

Estados habituales en `jobs.status`:

```text
queued
running
completed
completed_with_errors
failed
cancelled
```

Estados habituales en `job_centers.status`:

```text
queued
running
skipped_downloaded_today
downloaded
center_not_found
no_users_found
error
cancelled
```

## Documentacion De Producto Y Arquitectura

PRD vivo:

```text
docs/PRD_v2.md
```

Arquitectura, esquema SQLite y operaciones:

```text
docs/arquitectura.md
```

Guia de usuario final (frontend):

```text
docs/guia_usuario.md
```

ADRs:

```text
docs/ADR_0001_orquestador_secuencial.md
docs/ADR_0002_env_y_step03_locales.md
docs/ADR_0003_autonomia_v2.md
docs/ADR_0004_subida_csv_y_jobs_abiesplus.md
```

## Abies+

El script de Step03 vive en:

```text
v2/pullup2Ab+.py
```

Ya esta adaptado a SQLite: lee los centros con ultima descarga `downloaded` desde la tabla `downloads` (no usa CSV de mapeo) y registra cada intento en `abiesplus_imports` y `session_logs`. El ZIP se toma de `downloads.file_path` y el XML se extrae del propio ZIP en un directorio temporal.

Lanzamiento por CLI:

```bash
docker compose run --rm abies-v2 python v2/pullup2Ab+.py --selected-only --non-interactive --dry-run
```

Modos y flags utiles:

```text
--selected-only        Solo centros con centers.selected = 1 y descarga downloaded
--only-codes 60000165,60001066   Codigos concretos separados por coma
--dry-run              Valida ZIP/XML y rellena el formulario sin guardar (no crea la biblioteca)
--non-interactive      No pregunta opciones de checkboxes; usa ABIESPLUS_DEFAULT_* / app_settings
```

Los checkboxes del lote (`usar_registro`, `syncusers`, `importusers`) se resuelven por orden: prompt interactivo > `app_settings` (`abiesplus_default_*`) > `.env` (`ABIESPLUS_DEFAULT_*`).

Resultados registrados en `abiesplus_imports.status`:

```text
created      biblioteca creada en Abies+
dry_run_ok   validacion OK sin guardar (dry-run)
error        fallo en validacion o creacion
```

## Autonomia De AbiesZipZap

AbiesZipZap es autonomo: no depende en tiempo de ejecucion del proyecto padre.

- `abies_backup.py` (Config, init_browser, login, new_context, save_auth_state) esta vendorizado en `v2/abies_backup.py`.
- `buscar_gestores_abiesweb.py` importa `abies_backup` localmente y carga `v2/.env`.
- La configuracion base se distribuye como `v2/.env.example` (copia a `v2/.env` y rellena credenciales).

## Notas

No instalar dependencias Python en el sistema local. Todo se ejecuta desde el contenedor `abies-v2`.

El `.env` de V2 se monta como solo lectura:

```text
./.env:/app/.env:ro
```

La plantilla sin secretos es `v2/.env.example`.
