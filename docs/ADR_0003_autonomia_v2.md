# ADR 0003 - Autonomia De AbiesZipZap Y Vendorizacion De abies_backup

## Estado

Aceptado.

## Contexto

AbiesZipZap dependia en tiempo de ejecucion del proyecto padre: `buscar_gestores_abiesweb.py` importaba `abies_backup` desde `../step01_abiesweb/` mediante `sys.path.insert`, y el `Dockerfile` construia con `context: ..` copiando `step01_abiesweb` y `v2` desde el padre.

Esto impedia distribuir AbiesZipZap de forma autonoma a un tercero: el receptor necesitaba el arbol completo del proyecto padre.

## Decision

- Se vendoriza `step01_abiesweb/abies_backup.py` en `v2/abies_backup.py`. AbiesZipZap solo necesita las funciones `Config, init_browser, login, new_context, save_auth_state`, todas presentes en la copia.
- `buscar_gestores_abiesweb.py` elimina `PROJECT_ROOT`, `STEP01_DIR` y `sys.path.insert`; importa `abies_backup` localmente y carga `v2/.env` (`load_dotenv(SCRIPT_DIR / ".env")`).
- Se crea `v2/.env.example` (plantilla sin secretos) como punto de partida para un tercero.
- El `Dockerfile` pasa a `context: .` (v2) con `COPY . /app/v2`; se elimina `COPY step01_abiesweb`. Se anade `v2/.dockerignore` para excluir `.env` y artefactos runtime.
- `docker-compose.yml` pasa a `context: .`, `dockerfile: Dockerfile`.
- `pullup2Ab+.py` se limpia de restos CSV: se elimina `DEFAULT_CSV_PATH`, `result_csv`, se renombran parametros `csv_*` a `db_*` y cadenas "CSV/XML" a "DB/XML".

## Consecuencias

- AbiesZipZap es distribuible standalone: solo requiere Docker y un `.env` con credenciales.
- El proyecto padre no se modifica; el archivo original `step01_abiesweb/abies_backup.py` queda intacto.
- `abies_backup.py` queda duplicado en v2; si el padre evoluciona, la copia de v2 debe sincronizarse manualmente (aceptado por la regla de independencia).
- Las rutas relativas de salida (`OUTPUT_DIR`) se resuelven contra `SCRIPT_DIR` cuando son relativas, para que AbiesZipZap funcione fuera de Docker sin dispersar ficheros.

## Alternativas Consideradas

### Seguir dependiendo del padre

Rechazado: impide la distribucion autonoma solicitada.

### Refactorizar abies_backup a un paquete instalable compartido

Rechazado por ahora: mayor complejidad y acopla la evolucion de V2 a un paquete comun.
