# Guia de usuario - AbiesZipZap

AbiesZipZap te permite **descargar bibliotecas desde AbiesWeb** y **crearlas en Abies+** desde una consola web local. Esta guia explica como usarla paso a paso.

Repositorio original: https://github.com/clmmakers/AbiesZipZap

Licencia: GNU Affero General Public License v3.0 o posterior (`AGPL-3.0-or-later`).

## Requisitos

- Docker instalado.
- Credenciales de AbiesWeb (usuario adminEDAE y clave) y de Abies+ (admin y clave).
- Un CSV de centros con cabeceras MECD (ver mas abajo).

## Primera puesta en marcha

1. Copia la plantilla de configuracion y rellena tus credenciales:

```bash
cp .env.example .env
```

Edita `.env` y completa al menos:
- `ABIES_BASE_URL`, `ABIES_USERNAME`, `ABIES_PASSWORD`
- `ABIESPLUS_BASE_URL`, `ABIESPLUS_USERNAME`, `ABIESPLUS_PASSWORD`
- `ABIESPLUS_PROVINCE_EQUIVALENCES` (p.ej. `60=extranjero,52=melilla,51=ceuta`)
- `PRELOGIN_CENTER_TEXT` **obligatorio si tu AbiesWeb pide seleccionar centro antes del login** (es el caso de `programasexterior.abiesweb.org`). Indica el texto de un centro que aparezca en el autocompletado de la pantalla de prelogin (p.ej. `C.C. La Inmaculada`). Si lo dejas vacio en una instancia con prelogin, el login abortara con `Timeout esperando #signin_username`.

Si Abies+ corre en local y su HTML genera enlaces/formularios con `http://abiesplus-local:8080`, configura `ABIESPLUS_BASE_URL=http://abiesplus-local:8080`. No mezcles ese origen con `host.docker.internal`, porque la cookie de sesion puede quedar en otro host y Abies+ respondera `No autorizado` tras el login.

2. Construye la imagen:

```bash
docker compose build
```

3. Crea las carpetas de datos runtime con permisos para el contenedor:

```bash
mkdir -p data logs downloads state debug_gestores
```

> Importante: si Docker crea estas carpetas por ti al primer arranque, las crea como `root` y el contenedor (que corre como `appuser`, uid 1000) no podra escribir en ellas. Crealas tu antes con `mkdir -p` para que queden con tu usuario. Si ya las creo Docker como root y obtienes `PermissionError`, borralas (si estan vacias) y recrealas, o haz `sudo chown -R 1000:1000 data logs downloads state debug_gestores`.

4. Inicializa la base de datos (crea el esquema, sin centros):

```bash
docker compose run --rm abies-v2 python v2/db/init_db.py
```

Si ya tienes un CSV de centros y prefieres cargarlo por CLI:

```bash
docker compose run --rm abies-v2 python v2/db/init_db.py v2/data/mi_centros.csv
```

Tambien puedes subirlo despues desde el front (ver mas abajo).

5. Arranca el front:

```bash
docker compose up -d abies-v2-front
```

5. Abre **http://127.0.0.1:8010/centers** en tu navegador.

## El CSV de centros

El archivo CSV puede tener cualquier nombre. Lo importante es la **estructura** (cabeceras MECD, separadas por comas, UTF-8):

```
Provincia,Localidad,Código Centro,Denominación Centro,Nombre de centro,Titularidad,Código Postal,Dirección Postal,Correo Electrónico,Teléfono
```

- **Obligatorias**: `Código Centro` y `Nombre de centro`.
- El resto son recomendables (se usan para rellenar el formulario de Abies+ y validar contra el XML).
- El sistema valida las cabeceras al subir/sincronizar e informa de filas insertadas, actualizadas o con errores.

### Como aportar tu CSV

Tienes dos opciones:

**Desde el navegador** (recomendado):

- **Subir CSV** (pantalla **Centros**): pulsa "Subir CSV de centros", selecciona tu `.csv` y se sincroniza al instante. Se hace upsert por `Código Centro` (inserta nuevos, actualiza existentes).
- **Re-sincronizar** (pantalla **Configuracion**): si ya colocaste el archivo en la carpeta, pulsa "Re-sincronizar CSV". Muestra el CSV activo y un boton.

**Por CLI** (alternativa):

```bash
# Inicializar la base y cargar un CSV en un solo paso:
docker compose run --rm abies-v2 python v2/db/init_db.py v2/data/mi_centros.csv

# O reinicializar la base vacia y cargar el CSV despues:
docker compose run --rm abies-v2 python v2/db/init_db.py
docker compose run --rm abies-v2 python v2/db/init_db.py v2/data/mi_centros.csv
```

> Nota: para que el contenedor vea tu CSV, dejalo en la carpeta `data/` del proyecto (en tu maquina). Esa carpeta se monta en Docker como `/app/v2/data`. El nombre del archivo es libre. El CSV de ejemplo ficticio distribuido esta en `data/centros_example.csv`.

Tras subir, veras un mensaje con cuantos centros se insertaron/actualizaron y si faltan cabeceras.

## Pantalla Centros (`/centers`)

Listado de todos los centros con su ultimo estado de AbiesWeb y de descarga.

- **Filtros**: busca por codigo, nombre, localidad o email; filtra por estado (pendiente, descargado, error, no encontrado) y por seleccion.
- **Seleccion**: marca los checkboxes y "Guardar seleccion visible", o usa "Seleccionar activos" / "Limpiar seleccion".
- **Procesar seleccion**: lanza un job de descarga en segundo plano para los centros seleccionados. Puedes indicar reintentos, un limite, y "Forzar comprobacion remota" (ignora el salto rapido si ya hay descarga de hoy).
- Cada fila muestra: estado del check, estado de la descarga, el ZIP si lo hay (con enlace para descargarlo), y un enlace "Detalle".

## Pantalla Detalle de centro (`/centers/<codigo>`)

Historial de un centro: comprobaciones en AbiesWeb, gestores detectados, descargas y logs recientes.

## Pantalla Jobs (`/jobs`)

Listado de jobs de descarga y de Abies+. Muestra el comando CLI equivalente. Un job puede estar:

- `running` (en curso) — solo se permite uno activo a la vez.
- `completed` / `completed_with_errors` / `failed` / `cancelled`.

## Pantalla Detalle de job (`/jobs/<id>`)

Resumen del job (inicio, fin, comando, motivo), tabla de centros con su estado dentro del job, y logs del job. Para jobs de Abies+, las estadisticas muestran procesados / errores / dry-run.

## Pantalla Abies+ (`/abiesplus`)

Lista los centros que ya tienen un ZIP descargado (`downloaded`) y permite crear las bibliotecas en Abies+.

1. Marca los centros en la tabla (o indica codigos en "Solo codigos").
2. Ajusta los checkboxes:
   - **Dry-run**: valida ZIP/XML y rellena el formulario **sin guardar**. Recomendado la primera vez.
   - **Usar registro**: activa "utilizar campos de registro en ejemplares".
   - **Syncusers**: visualizar menus de sincronizacion de usuarios.
   - **Importusers**: bibliotecarios pueden importar usuarios masivamente.
3. Pulsa "Crear bibliotecas en Abies+" (o "Lanzar seleccionados").

El proceso corre en segundo plano. Los resultados aparecen en la columna "Import" de la tabla y en `/jobs`. Estados: `created` (biblioteca creada), `dry_run_ok` (validacion OK sin guardar), `error`.

> **Recomendacion**: haz primero un dry-run sobre 1-2 centros para validar que el XML y los datos coinciden. Solo cuando el dry-run este limpio, desmarca Dry-run y ejecuta en real.

## Pantalla Logs (`/logs`)

Logs globales filtrables por centro y por accion. Util para diagnosticar fallos.

## Pantalla Configuracion (`/settings`)

Edita los defaults operativos (todos persisten en SQLite):

- `export_reuse_max_age_days`: margen de dias para reutilizar un ZIP existente (0 = solo hoy).
- `local_timezone`: zona horaria para comparar fechas de AbiesWeb.
- `orchestrator_*`: reintentos, timeout y salto por descarga de hoy.
- `abiesplus_default_*`: defaults de los checkboxes de Abies+.
- Seccion **CSV de centros**: CSV activo y boton "Re-sincronizar".

## Flujo recomendado completo

1. Sube tu CSV de centros en **Centros**.
2. Selecciona los centros a descargar.
3. "Procesar seleccionados" -> revisa el job en **Jobs**.
4. Cuando los ZIP esten `downloaded`, ve a **Abies+**.
5. Lanza un **dry-run** sobre unos pocos centros y revisa errores en **Logs**.
6. Si todo esta limpio, lanza la creacion en real (Dry-run desmarcado).

## Descargar un ZIP

En **Centros** o **Detalle de centro**, el enlace del ZIP lo descarga directamente desde la base local.

## Problemas frecuentes

- **"Ya existe un job activo"**: espera a que termine o cancela el job en curso (solo uno a la vez).
- **Timeout esperando `#signin_username` en el login**: tu instancia AbiesWeb tiene pantalla de prelogin (pide "Escribe tu centro" antes del login). Rellena `PRELOGIN_CENTER_TEXT` en `.env` con el texto de un centro valido del autocompletado (p.ej. `C.C. La Inmaculada`) y reinicia el front. Obligatorio para `programasexterior.abiesweb.org`.
- **Centro no encontrado en AbiesWeb**: revisa que el `Código Centro` del CSV coincida con la U.O. de AbiesWeb. No es permanente: al volver a seleccionarlo se reintenta.
- **Sin gestores impersonables**: el centro no tiene usuarios con perfil `adminbiblioteca` sin `adminEDAE`.
- **Provincia no coincide DB/XML**: ajusta `ABIESPLUS_PROVINCE_EQUIVALENCES` en `.env` y re-sincroniza.
- **Abies+ responde `No autorizado` despues del login local**: revisa que `ABIESPLUS_BASE_URL` use el mismo origen que emite Abies+ en sus enlaces/formularios. En la instancia local probada debe ser `http://abiesplus-local:8080`, no `http://host.docker.internal:8080`.
- **Timeout de exportacion**: aumenta `orchestrator_export_timeout_seconds` en **Configuracion**.
- **`PermissionError` al lanzar job desde el front**: las carpetas `data logs downloads state debug_gestores` fueron creadas como `root` por Docker. Borralas (si vacias) y recrealas con tu usuario, o `sudo chown -R 1000:1000 data logs downloads state debug_gestores`.
