# ADR 0001 - Orquestador Secuencial Con SQLite

## Estado

Aceptado.

## Contexto

AbiesZipZap ya dispone de un script fiable para procesar un unico centro: busca la U.O. en AbiesWeb, elige un gestor valido, impersona, entra en exportacion, descarga el ZIP y registra checks, usuarios, descargas y logs en SQLite.

El siguiente paso es procesar lotes elegidos por el usuario desde el front, manteniendo trazabilidad y evitando trabajo repetido.

## Decision

Se implementa un orquestador secuencial en `v2/orchestrator.py` que:

- Usa SQLite como fuente operativa.
- Lee centros seleccionados desde `centers.selected` o desde `--only-codes`.
- Crea registros en `jobs` y `job_centers`.
- Permite solo un job activo a la vez.
- Invoca el script individual por subprocess para cada centro.
- Salta por defecto centros con descarga `downloaded` registrada hoy.
- Permite reintentos configurables por CLI.

El front local puede lanzar el orquestador con `POST /jobs/start`:

- Valida que hay centros seleccionados.
- Valida que no hay otro job `queued` o `running`.
- Ejecuta `v2/orchestrator.py --selected-only` con `subprocess.Popen`.
- Redirige a `/jobs` sin esperar a que Playwright termine.
- Registra stdout/stderr en `v2/logs/orchestrator_front.log`.

## Consecuencias

- Cada centro se procesa en un proceso Python aislado, reduciendo contaminacion entre sesiones Playwright.
- El flujo es mas lento que una integracion importable en memoria, pero mas robusto y simple.
- El front puede preparar seleccion, lanzar jobs en background y consultar progreso sin ejecutar Playwright dentro de la request HTTP.
- El estado queda auditable en SQLite.

## Alternativas Consideradas

### Refactorizar El Script Individual A Funcion Importable

Rechazado por ahora. Reduciria overhead, pero requiere mas cambios en un flujo que ya funciona y podria compartir estado Playwright entre centros.

### Ejecutar Jobs Desde Flask Directamente

Rechazado como ejecucion sincrona dentro de la request. Se acepta lanzar un proceso en background con `subprocess.Popen`, porque la request vuelve inmediatamente y el progreso queda en SQLite.

### Permitir Jobs Concurrentes

Rechazado. AbiesWeb, Playwright y el estado de autenticacion compartido hacen mas seguro un solo job activo.
