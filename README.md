# AbiesZipZap - AbiesWeb a Abies+

AbiesZipZap es un flujo local y contenerizado para localizar bibliotecas en AbiesWeb, impersonar un gestor valido, exportar/descargar el ZIP y registrar el resultado en SQLite, y luego crear las bibliotecas en Abies+.

AbiesZipZap es autonomo: no depende de ningun proyecto padre. Todo el runtime, los scripts y `abies_backup.py` viven dentro de este directorio.

Repositorio original: https://github.com/clmmakers/AbiesZipZap

Licencia: GNU Affero General Public License v3.0 o posterior (`AGPL-3.0-or-later`). Consulta `LICENSE` y `NOTICE.md`.

## Requisitos

- Docker con soporte de Compose v2.
- Credenciales de AbiesWeb (adminEDAE) y de Abies+ (admin).
- Un CSV de centros con cabeceras MECD para cargar (recomendado, ver seccion 2.1).

El navegador Chromium lo instala la imagen Docker (`Dockerfile`), no hace falta instalarlo en local.

## 1. Puesta En Marcha (obligatorio, sea CLI o GUI)

Estos pasos se ejecutan una sola vez por maquina (o tras un cambio mayor de codigo). Son identicos si luego vas a trabajar por CLI o por GUI.

1. Copia la plantilla de configuracion y rellena las credenciales:

   ```bash
   cp .env.example .env
   # edita .env: ABIES_BASE_URL, ABIES_USERNAME, ABIES_PASSWORD,
   # ABIESPLUS_BASE_URL, ABIESPLUS_USERNAME, ABIESPLUS_PASSWORD,
   # ABIESPLUS_PROVINCE_EQUIVALENCES y PRELOGIN_CENTER_TEXT si aplica
   ```

   `PRELOGIN_CENTER_TEXT` es obligatorio si tu AbiesWeb pide seleccionar centro antes del login (caso tipico: `programasexterior.abiesweb.org`). Indica el texto de un centro que aparezca en el autocompletado de la pantalla de prelogin. Si lo dejas vacio en una instancia con prelogin, el login abortara con `Timeout esperando #signin_username`.

2. Construye la imagen:

   ```bash
   docker compose build
   ```

3. Las carpetas de datos runtime (`data/`, `logs/`, `downloads/`, `state/`, `debug_gestores/`) ya vienen creadas en el repositorio. No borres los archivos `.gitkeep`.

4. Inicializa la base SQLite (crea el esquema, sin centros):

   ```bash
   docker compose run --rm abies-v2 python v2/db/init_db.py
   ```

   Tras esto, la base operativa queda en `data/v2.sqlite3` y SQLite es la unica fuente operativa: estados, logs, usuarios, descargas y configuracion se guardan aqui.

5. (Opcional, solo si vas a usar GUI) Arranca el front:

   ```bash
   docker compose up -d abies-v2-front
   ```

   Abrir `http://127.0.0.1:8010/centers`. Para pararlo: `docker compose stop abies-v2-front`.

## 2. Operaciones

Esta seccion lista las operaciones que puedes hacer tras la puesta en marcha. Cada operacion indica el procedimiento **Por CLI** y **Por GUI** (cuando existe). Si solo hay un camino, se dice explicitamente.

Las operaciones estan en orden de uso tipico: primero cargas datos (2.1, 2.3), luego ejecutas (2.2, 2.4, 2.5), luego ajustas configuracion (2.6) e inspeccionas (2.7, 2.8).

### 2.1 Cargar / sincronizar el CSV de centros

El CSV solo actua como fuente inicial/sincronizacion de centros. A partir de ahi, SQLite es la unica fuente operativa.

Cabeceras MECD obligatorias: `Codigo Centro` y `Nombre de centro`. Recomendadas: `Provincia`, `Localidad`, `Denominacion Centro`, `Titularidad`, `Codigo Postal`, `Direccion Postal`, `Correo Electronico`, `Telefono`. El CSV de ejemplo ficticio distribuido esta en `data/centros_example.csv`.

Durante la carga, `Denominacion Centro` se normaliza al valor esperado por Abies+ (`CEIP` -> `C.E.I.P.`, `IES` -> `I.E.S.`, etc.). Si no hay coincidencia, se guarda `Biblioteca`.

**Por CLI:**

```bash
# Inicializar base y cargar CSV en un solo paso:
docker compose run --rm abies-v2 python v2/db/init_db.py v2/data/mi_centros.csv

# O reinicializar la base vacia y cargar el CSV despues:
docker compose run --rm abies-v2 python v2/db/init_db.py
docker compose run --rm abies-v2 python v2/db/init_db.py v2/data/mi_centros.csv
```

El CSV debe estar dentro de la carpeta `data/` del proyecto (se monta en Docker como `/app/v2/data`). El nombre del archivo es libre.

**Por GUI:**

`/centers` > boton "Subir CSV" o `/settings` > seccion "Re-sincronizar CSV". Tras subir, aparece un mensaje con cuantos centros se insertaron/actualizaron y si faltan cabeceras.

### 2.2 Probar un centro suelto (debug) — solo CLI

Esta operacion procesa un unico centro, sin crear job ni pasar por el orquestador. Sirve para depurar antes de lanzar un lote. **No hay equivalente en el front**: el boton "Procesar seleccion" del front siempre va por el orquestador (seccion 2.4).

**Por CLI:**

```bash
# Listar gestores sin impersonar ni descargar:
docker compose run --rm abies-v2 python v2/buscar_gestores_abiesweb.py --center-code 60001066 --list-only

# Procesar un centro por codigo:
docker compose run --rm abies-v2 python v2/buscar_gestores_abiesweb.py --center-code 60001066

# Procesar un centro por indice del CSV (orden de insercion):
docker compose run --rm abies-v2 python v2/buscar_gestores_abiesweb.py --index 0

# Aumentar el tiempo maximo de espera de exportacion:
docker compose run --rm abies-v2 python v2/buscar_gestores_abiesweb.py --center-code 60001066 --export-timeout-seconds 1200

# Guardar HTML y captura de diagnostico:
docker compose run --rm abies-v2 python v2/buscar_gestores_abiesweb.py --center-code 60001066 --debug
```

Los diagnosticos se guardan en `v2/debug_gestores/`.

### 2.3 Seleccionar centros a procesar

El orquestador (2.4) y la subida a Abies+ (2.5) pueden trabajar con tres modos: centros marcados en SQLite (`--selected-only`), codigos concretos (`--only-codes X,Y,Z`) o todos los activos (`--all-enabled`).

**Por CLI:**

```bash
# Opcion A: pasar codigos concretos al orquestador (no toca centers.selected):
docker compose run --rm abies-v2 python v2/orchestrator.py --only-codes 60000116,60001066

# Opcion B: marcar en SQLite y luego usar --selected-only:
sqlite3 v2/data/v2.sqlite3 "UPDATE centers SET selected=1, updated_at=CURRENT_TIMESTAMP WHERE center_code IN ('60000116','60001066');"
docker compose run --rm abies-v2 python v2/orchestrator.py --selected-only

# Limpiar la seleccion:
sqlite3 v2/data/v2.sqlite3 "UPDATE centers SET selected=0, updated_at=CURRENT_TIMESTAMP;"

# Procesar todos los centros activos sin marcar:
docker compose run --rm abies-v2 python v2/orchestrator.py --all-enabled
```

**Por GUI:**

`/centers` marca los checkboxes de los centros que quieras procesar, pulsa "Guardar seleccion visible". Atajos: "Seleccionar activos" (marca todos los `enabled=1`) o "Limpiar seleccion" (los deja a 0). Tambien puedes editar el campo "Seleccionado" en `/centers/<codigo>`. La seleccion vive en `centers.selected` y es lo que consume `--selected-only`.

### 2.4 Descargar lotes (Paso 1, orquestador)

El orquestador procesa los centros seleccionados, crea un registro en `jobs`, registra cada centro en `job_centers` y ejecuta `buscar_gestores_abiesweb.py` por subprocess. Solo se permite **un job activo a la vez**: si existe un job `queued` o `running`, el orquestador termina con error.

Por defecto, el orquestador salta centros con ultima descarga `downloaded` cuya `downloaded_at` sea hoy segun `local_timezone` (no comprueba si el ZIP existe fisicamente en disco). Para forzar la entrada en AbiesWeb en esos casos, usar `--force-remote-check`.

**Por CLI:**

```bash
# Procesar la seleccion (centers.selected = 1):
docker compose run --rm abies-v2 python v2/orchestrator.py --selected-only

# Procesar codigos concretos:
docker compose run --rm abies-v2 python v2/orchestrator.py --only-codes 60000116,60001066

# Procesar todos los centros activos:
docker compose run --rm abies-v2 python v2/orchestrator.py --all-enabled

# Probar la seleccion sin crear job ni descargar (dry-run):
docker compose run --rm abies-v2 python v2/orchestrator.py --selected-only --dry-run

# Permitir un reintento por error tecnico:
docker compose run --rm abies-v2 python v2/orchestrator.py --selected-only --retries 1

# Forzar entrada en AbiesWeb aunque haya descarga local registrada hoy:
docker compose run --rm abies-v2 python v2/orchestrator.py --selected-only --force-remote-check
```

Salida: los ZIPs quedan en `v2/downloads/<codigo>_<nombre_normalizado>/exportacionAbiesWeb_<n>.zip`, por ejemplo `v2/downloads/60001066_COLEGIO_ESPA_OL_DE_RABAT/exportacionAbiesWeb_40.zip`.

**Por GUI:**

`/centers` > boton "Procesar seleccion" lanza un job de descarga en segundo plano para los centros con checkbox marcado. Acepta reintentos, un limite y "Forzar comprobacion remota". `/jobs` lista los jobs y muestra el comando CLI equivalente. `/jobs/<id>` da el detalle del job (centros procesados, logs, estado). La salida stdout/stderr de los jobs lanzados desde el front se guarda en `v2/logs/orchestrator_front.log`.

### 2.5 Crear bibliotecas en Abies+ (Paso 3)

`pullup2Ab+.py` lee los centros con ultima descarga `downloaded` desde la tabla `downloads` (no usa CSV de mapeo), extrae el XML del propio ZIP, valida DB vs XML y rellena el formulario "Nueva biblioteca" en Abies+. Los checkboxes del lote (`usar_registro`, `syncusers`, `importusers`) se resuelven por orden: prompt interactivo > `app_settings` (`abiesplus_default_*`) > `.env` (`ABIESPLUS_DEFAULT_*`).

Recomendacion: lanzar primero un dry-run sobre 1-2 centros para validar que el XML y los datos coinciden. Solo cuando el dry-run este limpio, ejecutar en real (sin `--dry-run`).

**Por CLI:**

```bash
# Dry-run sobre la seleccion (valida y rellena sin guardar):
docker compose run --rm abies-v2 python v2/pullup2Ab+.py --selected-only --non-interactive --dry-run

# Creacion en real sobre la seleccion:
docker compose run --rm abies-v2 python v2/pullup2Ab+.py --selected-only --non-interactive

# Codigos concretos:
docker compose run --rm abies-v2 python v2/pullup2Ab+.py --only-codes 60000165,60001066 --non-interactive

# Modo interactivo (pregunta checkboxes; no usar en scripts):
docker compose run --rm abies-v2 python v2/pullup2Ab+.py --selected-only
```

Estados `abiesplus_imports.status`: `created` (biblioteca creada), `dry_run_ok` (validacion OK sin guardar), `error`.

**Por GUI:**

`/abiesplus` lista los centros con descarga `downloaded`. Marca los centros en la tabla (o indica codigos en "Solo codigos"), ajusta los checkboxes del lote (Dry-run, Usar registro, Syncusers, Importusers) y pulsa "Crear bibliotecas en Abies+". El proceso corre en segundo plano y los resultados aparecen en `/jobs`. La misma pantalla de `/abiesplus` es equivalente al comando CLI: si arrancas un job desde ahi, se usa el flag `--selected-only --non-interactive --dry-run` por defecto.

### 2.6 Configurar `app_settings` (timeouts, defaults, reutilizacion)

Toda la configuracion operativa vive en `app_settings` (SQLite). Las claves mas usadas:

| Key | Default | Significado |
|---|---|---|
| `export_reuse_max_age_days` | `0` | Margen de dias para reutilizar un ZIP existente (0 = solo hoy). |
| `local_timezone` | `Europe/Madrid` | Zona para comparar fechas DD/MM/YYYY de AbiesWeb. |
| `orchestrator_default_retries` | `0` | Reintentos por error tecnico. |
| `orchestrator_export_timeout_seconds` | `2400` | Timeout de exportacion por centro. |
| `orchestrator_skip_downloaded_today` | `true` | Saltar centros descargados hoy. |
| `abiesplus_default_dry_run` | `true` | Dry-run por defecto desde el front. |
| `abiesplus_default_usar_registro` | `false` | Checkbox "usar registro en ejemplares". |
| `abiesplus_default_syncusers` | `false` | Checkbox sincronizacion de usuarios. |
| `abiesplus_default_importusers` | `false` | Checkbox importacion masiva de usuarios. |
| `centers_csv_path` | `v2/data/centros_example.csv` | CSV activo para sincronizar. |

Politica de reutilizacion por defecto (`export_reuse_max_age_days = 0`): solo se reutiliza un ZIP existente si la fecha detectada en AbiesWeb ("generado el dia: DD/MM/YYYY") es hoy segun `local_timezone`. Si no cumple, el script relanza la exportacion y vigila la barra de progreso; no espera que AbiesWeb refresque el parrafo de fecha.

**Por CLI:**

```bash
# Ver configuracion actual:
docker compose run --rm abies-v2 sqlite3 /app/v2/data/v2.sqlite3 "SELECT key, value FROM app_settings ORDER BY key;"

# Cambiar margen de reutilizacion a 10 dias:
docker compose run --rm abies-v2 sqlite3 /app/v2/data/v2.sqlite3 \
  "UPDATE app_settings SET value='10', updated_at=CURRENT_TIMESTAMP WHERE key='export_reuse_max_age_days';"

# Volver a solo hoy:
docker compose run --rm abies-v2 sqlite3 /app/v2/data/v2.sqlite3 \
  "UPDATE app_settings SET value='0', updated_at=CURRENT_TIMESTAMP WHERE key='export_reuse_max_age_days';"
```

**Por GUI:**

`/settings` permite editar las mismas claves con formularios y persistirlas en SQLite. Tambien permite re-sincronizar el CSV desde la seccion "CSV de centros".

### 2.7 Inspeccionar resultados

**Por CLI:**

```bash
# Resumen de tablas principales:
docker compose run --rm abies-v2 sqlite3 /app/v2/data/v2.sqlite3 \
  "SELECT 'centers', COUNT(*) FROM centers
   UNION ALL SELECT 'checks', COUNT(*) FROM abiesweb_checks
   UNION ALL SELECT 'users', COUNT(*) FROM abiesweb_users
   UNION ALL SELECT 'downloads', COUNT(*) FROM downloads
   UNION ALL SELECT 'logs', COUNT(*) FROM session_logs;"

# Ultimas descargas:
docker compose run --rm abies-v2 sqlite3 /app/v2/data/v2.sqlite3 \
  "SELECT id, center_code, status, download_mode, export_date_detected,
          export_age_days, export_reuse_max_age_days, file_name, file_size_bytes,
          downloaded_at
   FROM downloads ORDER BY id DESC LIMIT 10;"

# Logs recientes de un centro:
docker compose run --rm abies-v2 sqlite3 /app/v2/data/v2.sqlite3 \
  "SELECT action, status, message, created_at
   FROM session_logs WHERE center_code='60001066'
   ORDER BY id DESC LIMIT 20;"

# Centros disponibles:
docker compose run --rm abies-v2 sqlite3 /app/v2/data/v2.sqlite3 \
  "SELECT center_code, center_name, city, email
   FROM centers ORDER BY center_code;"
```

**Por GUI:**

- `/centers` — listado de centros con su ultimo estado de AbiesWeb y de descarga, filtros por codigo/nombre/email/estado/seleccion.
- `/centers/<codigo>` — historial del centro: datos editables, checks en AbiesWeb, gestores detectados, descargas y logs.
- `/jobs` — listado de jobs (descarga y Abies+). Muestra el comando CLI equivalente.
- `/jobs/<id>` — detalle del job (centros, estado por centro, logs).
- `/logs` — logs globales filtrables por centro y accion.

### 2.8 Bajar un ZIP al disco

**Por CLI:**

Los ZIPs ya estan en el sistema de archivos del proyecto. Para llevarlos a otra maquina:

```bash
# Ver los ZIPs descargados:
ls -1 downloads/

# Copiar uno concreto a una ruta de la maquina:
cp downloads/60001066_COLEGIO_ESPA_OL_DE_RABAT/exportacionAbiesWeb_40.zip /tmp/
```

**Por GUI:**

`/centers` y `/centers/<codigo>` muestran un enlace "ZIP" por centro. Pulsandolo se descarga el archivo desde la base local.

## 3. Pantallas Del Front (referencia)

| Pantalla | Sirve para | Operacion relacionada |
|---|---|---|
| `/centers` | Listado, filtros, seleccion por checkbox, lanzar descarga | 2.1, 2.3, 2.4, 2.7, 2.8 |
| `/centers/<codigo>` | Detalle y edicion de un centro, checks, usuarios, descargas, logs | 2.7, 2.8 |
| `/jobs` | Listado de jobs, comando CLI equivalente | 2.4, 2.5 |
| `/jobs/<id>` | Detalle de job y sus centros | 2.4, 2.5 |
| `/abiesplus` | Crear bibliotecas en Abies+ (incluido dry-run) | 2.5 |
| `/settings` | Configuracion `app_settings` y re-sincronizar CSV | 2.1, 2.6 |
| `/logs` | Logs globales filtrables | 2.7 |

Para parar el front: `docker compose stop abies-v2-front`. Para volver a arrancarlo: `docker compose up -d abies-v2-front`.

## 4. Estados Y Settings (referencia)

### Estados en `abiesweb_checks.status`

```text
users_found
no_users_found
center_not_found
error
```

### Estados en `downloads.status` y `downloads.download_mode`

```text
status:           downloaded | error
download_mode:    reused_existing_export
                  reexported_old_export
                  reexported_unknown_date
                  reexported_no_existing_export
                  waited_running_export
                  waited_running_export_unknown_initial_progress
```

### Estados en `jobs.status`

```text
queued
running
completed
completed_with_errors
failed
cancelled
```

### Estados en `job_centers.status`

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

### Estados en `abiesplus_imports.status`

```text
created      biblioteca creada en Abies+
dry_run_ok   validacion OK sin guardar (dry-run)
error        fallo en validacion o creacion
```

Lista completa de `app_settings` y sus defaults: ver seccion 2.6 y `docs/arquitectura.md#esquema-sqlite`.

## 5. Flags Utiles (referencia rapida)

### `buscar_gestores_abiesweb.py` (Paso 1, un centro)

```text
--center-code 60001066       Centro por codigo
--index 0                    Centro por indice del CSV
--list-only                  Lista gestores sin impersonar ni descargar
--export-timeout-seconds N   Timeout de exportacion (default lee app_settings)
--timeout-ms N               Timeout Playwright (default 30000)
--debug                      Guarda HTML/captura de diagnostico
--headful                    Muestra navegador
--verbose                    Log detallado
```

### `orchestrator.py` (Paso 1, lote)

```text
--selected-only         Procesa centros con centers.selected = 1 (modo por defecto)
--all-enabled           Procesa todos los centros con enabled = 1
--only-codes X,Y,Z      Codigos concretos separados por coma
--limit N               Limita el numero de centros
--retries N             Reintentos por error tecnico (default lee app_settings)
--export-timeout-seconds N   Timeout de exportacion por centro
--force-remote-check    No salta centros descargados hoy; entra en AbiesWeb
--dry-run               Muestra centros sin crear job ni procesar
--headful               Muestra navegador en el script individual
--debug                 Guarda diagnosticos del script individual
```

### `pullup2Ab+.py` (Paso 3, Abies+)

```text
--selected-only         Centros con centers.selected = 1 y descarga downloaded
--only-codes X,Y,Z      Codigos concretos separados por coma
--non-interactive       No pregunta checkboxes; usa ABIESPLUS_DEFAULT_* / app_settings
--dry-run               Valida ZIP/XML y rellena el formulario sin guardar
```

`--all-enabled` y `--only-codes` son incompatibles en el orquestador.

## 6. Comportamiento Del Script Individual

`v2/buscar_gestores_abiesweb.py` procesa un unico centro cada vez. Flujo:

1. Sincroniza el CSV de `data/` contra SQLite con `center_code` como clave primaria (solo si se pasa `--csv`).
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

Codigos de salida: `0` OK, `1` error, `2` centro no encontrado, `3` sin gestores impersonables.

## 7. Documentacion Complementaria

- `docs/guia_usuario.md` — guia de usuario final (flujo GUI, pantalla a pantalla).
- `docs/arquitectura.md` — arquitectura, esquema SQLite completo y notas tecnicas.
- `docs/PRD_v2.md` — PRD vivo.
- ADRs:
  - `docs/ADR_0001_orquestador_secuencial.md`
  - `docs/ADR_0002_env_y_step03_locales.md`
  - `docs/ADR_0003_autonomia_v2.md`
  - `docs/ADR_0004_subida_csv_y_jobs_abiesplus.md`
- `README_DESPLIEGUE.md` — preparar una copia distribuible sin secretos.
- `LICENSE`, `NOTICE.md` — licencia y aviso.

## 8. Autonomia De AbiesZipZap

AbiesZipZap es autonomo: no depende en tiempo de ejecucion del proyecto padre.

- `abies_backup.py` (Config, init_browser, login, new_context, save_auth_state) esta vendorizado en `v2/abies_backup.py`.
- `buscar_gestores_abiesweb.py` importa `abies_backup` localmente y carga `v2/.env`.
- La configuracion base se distribuye como `v2/.env.example` (copia a `v2/.env` y rellena credenciales).

## 9. Notas

No instalar dependencias Python en el sistema local. Todo se ejecuta desde el contenedor `abies-v2`.

El `.env` se monta como solo lectura: `./.env:/app/.env:ro`.

La plantilla sin secretos es `v2/.env.example` (mismas claves que `.env.example` en la raiz del repo).

Si Abies+ local genera enlaces/formularios con `http://abiesplus-local:8080`, configura `ABIESPLUS_BASE_URL=http://abiesplus-local:8080`. No mezcles ese origen con `host.docker.internal`, porque las cookies de sesion pueden quedar asociadas a otro host y Abies+ respondera `No autorizado` tras el login.
