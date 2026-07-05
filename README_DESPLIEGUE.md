# Despliegue y distribucion de AbiesZipZap

Esta guia define como preparar una copia distribuible de AbiesZipZap sin secretos ni datos runtime.

Repositorio original: https://github.com/clmmakers/AbiesZipZap

Licencia: GNU Affero General Public License v3.0 o posterior (`AGPL-3.0-or-later`). Consulta `LICENSE` y `NOTICE.md`.

## Que debe incluir el repositorio

- Codigo fuente: scripts Python, `front/`, `db/`, `docs/`, `Dockerfile`, `docker-compose.yml`.
- `requirements.txt`.
- `.env.example`, sin credenciales reales.
- `data/centros_example.csv` como CSV de ejemplo ficticio.
- Documentacion: `README.md`, `docs/guia_usuario.md`, `docs/arquitectura.md` y este archivo.
- `NOTICE.md`, con referencia al repositorio original.
- `LICENSE`, con la licencia AGPLv3.

## Que no debe incluir el repositorio

- `.env`.
- `data/v2.sqlite3` ni ninguna SQLite runtime.
- `logs/`.
- `downloads/`.
- `state/`.
- `debug_gestores/`.
- `__pycache__/` y `*.pyc`.
- ZIPs reales descargados de AbiesWeb.
- Capturas o HTML de diagnostico con datos reales.

## SQLite vacio

La opcion recomendada es no distribuir SQLite. La base se crea vacia en la primera puesta en marcha:

```bash
docker compose run --rm abies-v2 python v2/db/init_db.py
```

Ventajas:

- Evita distribuir datos reales por error.
- Evita problemas de permisos o versiones de esquema.
- Permite que `init_db.py` aplique siempre las migraciones idempotentes.

Si se quiere cargar el CSV de ejemplo por CLI:

```bash
docker compose run --rm abies-v2 python v2/db/init_db.py v2/data/centros_example.csv
```

Alternativamente, el usuario puede arrancar el front y subir su propio CSV desde `/centers` o re-sincronizar desde `/settings#csv`.

## Preparar una copia limpia para entregar

Desde el directorio del proyecto (`v2/` o `v3/`):

```bash
cp .env.example .env
docker compose build
docker compose run --rm abies-v2 python v2/db/init_db.py
docker compose up -d abies-v2-front
```

Despues abre:

```text
http://127.0.0.1:8010/centers
```

## Checklist antes de publicar

- `.env` no existe en el paquete o esta ignorado.
- `data/v2.sqlite3` no esta versionado.
- `downloads/`, `logs/`, `state/` y `debug_gestores/` no estan versionados.
- `data/.gitkeep`, `logs/.gitkeep`, `downloads/.gitkeep`, `state/.gitkeep` y `debug_gestores/.gitkeep` existen para que las carpetas vengan creadas tras clonar.
- `data/centros_example.csv` existe como ejemplo ficticio.
- `docker compose config --quiet` no falla.
- Compilacion Python OK:

```bash
python3 -m py_compile abies_backup.py buscar_gestores_abiesweb.py orchestrator.py 'pullup2Ab+.py' db/init_db.py front/app.py
```

## Notas Abies+ local

Si Abies+ local genera enlaces/formularios con `http://abiesplus-local:8080`, configura:

```env
ABIESPLUS_BASE_URL=http://abiesplus-local:8080
```

No mezcles ese origen con `host.docker.internal`, porque las cookies de sesion pueden quedar asociadas a otro host y Abies+ respondera `No autorizado` tras el login.
