# PRD AbiesZipZap - AbiesWeb a Abies+

## Estado

Documento vivo. Se ira completando con futuras implementaciones.

## Objetivo

AbiesZipZap automatiza de forma local y trazable el flujo de descarga de bibliotecas desde AbiesWeb para preparar su posterior subida a Abies+.

## Alcance Actual

- Mantener el CSV de centros en `data/` solo como fuente inicial y sincronizacion de centros.
- Usar SQLite como fuente operativa de seleccion, estados, jobs, logs y descargas.
- Permitir que el usuario seleccione centros desde el front local.
- Ejecutar un orquestador secuencial desde CLI o desde el front para procesar los centros seleccionados.
- Ahorrar tiempo saltando centros con descarga local registrada hoy segun `Europe/Madrid`.
- Registrar el resultado por job y por centro.
- Crear bibliotecas en Abies+ usando la ultima descarga `downloaded` de cada centro (Paso 3 adaptado a SQLite).
- Registrar resultados de Abies+ en `abiesplus_imports` y `session_logs`.
- Permitir dry-run y ejecucion real de Abies+ desde CLI y front.
- Usar `v2/.env` como configuracion runtime de AbiesZipZap, sin depender del proyecto padre.
- AbiesZipZap autonomo: `abies_backup.py` vendorizado en `v2/`; sin imports del proyecto padre.

## Fuera De Alcance Actual

- Gestionar varios jobs concurrentes.
- Validar la existencia fisica del ZIP antes de saltar una descarga registrada hoy.
- Integracion del Paso 3 (Abies+) con el modelo de jobs (`jobs`/`job_centers`).

## Alcance Futuro Proximo

- Integrar el Paso 3 con el orquestador y la tabla `jobs`/`job_centers`.
- Subida de CSV de centros desde el front (upload + re-sincronizacion).
- Distribucion Docker autónoma (Dockerfile con contexto propio de v2).
- Cancelacion de jobs en curso.

## Usuarios

- Operador local que prepara la seleccion de centros y lanza el proceso por CLI.
- Operador local que revisa resultados, errores, logs y descargas desde el front.

## Requisitos Funcionales

- El front debe permitir seleccionar centros con checkbox.
- El orquestador debe poder procesar los centros seleccionados.
- El orquestador debe poder procesar una lista concreta de codigos.
- El front debe poder lanzar un job en segundo plano con los centros seleccionados.
- El front no debe bloquear la peticion HTTP mientras se ejecuta Playwright.
- El orquestador debe registrar un job global.
- El orquestador debe registrar el resultado de cada centro del job.
- El orquestador debe impedir mas de un job activo simultaneo.
- El orquestador debe saltar por defecto centros con ultima descarga `downloaded` de hoy.
- El usuario debe poder forzar comprobacion remota con una opcion CLI.
- El usuario debe poder elegir numero de reintentos.
- `center_not_found` no debe considerarse estado permanente: si se selecciona otra vez, se comprueba otra vez.
- Si varios usuarios son impersonables, se mantiene la seleccion del primero sin `adminEDAE`.

## Lanzamiento Desde Front

- La pantalla `/centers` muestra un formulario para procesar seleccionados.
- El endpoint `POST /jobs/start` valida que hay centros seleccionados y que no hay otro job activo.
- El endpoint lanza `v2/orchestrator.py --selected-only` con `subprocess.Popen`.
- La peticion HTTP redirige inmediatamente a `/jobs`.
- La salida del proceso lanzado desde front se guarda en `v2/logs/orchestrator_front.log`.

## Requisitos No Funcionales

- Todo se ejecuta dentro del contenedor de AbiesZipZap.
- Los cambios se limitan a `v2/`.
- AbiesZipZap usa `v2/.env`; el proyecto padre no se modifica.
- El flujo debe ser secuencial y robusto.
- Los logs deben ser suficientes para diagnostico posterior.
- Las migraciones deben ser idempotentes.

## Estados De Job

- `queued`
- `running`
- `completed`
- `completed_with_errors`
- `failed`
- `cancelled`

## Estados Por Centro

- `queued`
- `running`
- `skipped_downloaded_today`
- `downloaded`
- `center_not_found`
- `no_users_found`
- `error`
- `cancelled`

## Criterio De Salto Por Descarga Local

Un centro se salta si se cumple todo:

- La ultima descarga registrada del centro tiene `status = downloaded`.
- `downloaded_at`, convertido a la zona `local_timezone`, cae en la fecha local actual.
- No se ha usado `--force-remote-check`.

No se comprueba si el archivo existe fisicamente en disco.

## Preguntas Futuras

- Como se cancelara un job en curso.
- Como se integrara la subida a Abies+.
- Que resumen de errores necesita el operador para reintentos masivos.
