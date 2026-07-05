import argparse
import csv
import os
import re
import sqlite3
import unicodedata
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
V2_DIR = SCRIPT_DIR.parent
DEFAULT_DB_PATH = V2_DIR / "data" / "v2.sqlite3"
DEFAULT_CSV_PATH = V2_DIR / "data" / "centros_example.csv"
SCHEMA_PATH = SCRIPT_DIR / "schema.sql"


def clean_text(value: str | None) -> str:
    return " ".join((value or "").split()).strip()


ABIESPLUS_CENTER_TYPES = (
    "A.E.P.A.",
    "C.E.E.",
    "C.E.I.P.",
    "C.E.P.A.",
    "C.F.M.R.",
    "C.H.",
    "C.I.F.P.",
    "C.O.D.",
    "C.O.M.",
    "C.P.A.P.D.",
    "C.P.R.",
    "C.R.A.",
    "C.R.I.E.",
    "COL.",
    "COL.E.D.",
    "COL.F.P.",
    "COL.E.E.",
    "COL.E.I.",
    "E.A.O.",
    "E.E.I.",
    "E.F.A.",
    "E.H.",
    "E.O.E.P.",
    "E.O.I.",
    "E.S.A.D.",
    "I.E.S.",
    "I.E.S.O.",
    "SECC.I.E.S.",
    "Biblioteca",
)
ABIESPLUS_DEFAULT_CENTER_TYPE = "Biblioteca"


def _center_type_key(value: str | None) -> str:
    normalized = unicodedata.normalize("NFD", clean_text(value).upper())
    ascii_text = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^A-Z0-9]", "", ascii_text)


_CENTER_TYPE_BY_KEY = {_center_type_key(value): value for value in ABIESPLUS_CENTER_TYPES}
_CENTER_TYPE_ALIASES = {
    "AEPA": "A.E.P.A.",
    "CEE": "C.E.E.",
    "CEIP": "C.E.I.P.",
    "CEPA": "C.E.P.A.",
    "CFMR": "C.F.M.R.",
    "CH": "C.H.",
    "CIFP": "C.I.F.P.",
    "COD": "C.O.D.",
    "COM": "C.O.M.",
    "CPAPD": "C.P.A.P.D.",
    "CPR": "C.P.R.",
    "CRA": "C.R.A.",
    "CRIE": "C.R.I.E.",
    "COL": "COL.",
    "COLED": "COL.E.D.",
    "COLFP": "COL.F.P.",
    "COLEE": "COL.E.E.",
    "COLEI": "COL.E.I.",
    "EAO": "E.A.O.",
    "EEI": "E.E.I.",
    "EFA": "E.F.A.",
    "EH": "E.H.",
    "EOEP": "E.O.E.P.",
    "EOI": "E.O.I.",
    "ESAD": "E.S.A.D.",
    "IES": "I.E.S.",
    "IESO": "I.E.S.O.",
    "SECCIES": "SECC.I.E.S.",
    "SECCIONIES": "SECC.I.E.S.",
    "BIBLIOTECA": "Biblioteca",
}


def normalize_center_type(value: str | None) -> str:
    key = _center_type_key(value)
    if not key:
        return ABIESPLUS_DEFAULT_CENTER_TYPE
    return _CENTER_TYPE_BY_KEY.get(key) or _CENTER_TYPE_ALIASES.get(key) or ABIESPLUS_DEFAULT_CENTER_TYPE


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        ensure_columns(conn)


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def add_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if not column_exists(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def ensure_columns(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_settings (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL,
          description TEXT,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO app_settings (key, value, description)
        VALUES (?, ?, ?)
        """,
        (
            "export_reuse_max_age_days",
            "0",
            "Maximo de dias de antiguedad permitidos para reutilizar un ZIP de exportacion existente; 0 significa solo hoy.",
        ),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO app_settings (key, value, description)
        VALUES (?, ?, ?)
        """,
        (
            "local_timezone",
            "Europe/Madrid",
            "Zona horaria usada para comparar fechas DD/MM/YYYY mostradas por AbiesWeb.",
        ),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO app_settings (key, value, description)
        VALUES (?, ?, ?)
        """,
        (
            "orchestrator_default_retries",
            "0",
            "Numero de reintentos por defecto para errores tecnicos en el orquestador.",
        ),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO app_settings (key, value, description)
        VALUES (?, ?, ?)
        """,
        (
            "orchestrator_export_timeout_seconds",
            "2400",
            "Timeout por defecto en segundos para esperar exportaciones AbiesWeb desde el orquestador.",
        ),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO app_settings (key, value, description)
        VALUES (?, ?, ?)
        """,
        (
            "orchestrator_skip_downloaded_today",
            "true",
            "Si es true, el orquestador salta centros con descarga registrada hoy salvo --force-remote-check.",
        ),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO app_settings (key, value, description)
        VALUES (?, ?, ?)
        """,
        (
            "abiesplus_default_usar_registro",
            "false",
            "Valor por defecto del checkbox usar registro en ejemplares para creacion de bibliotecas en Abies+.",
        ),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO app_settings (key, value, description)
        VALUES (?, ?, ?)
        """,
        (
            "abiesplus_default_syncusers",
            "false",
            "Valor por defecto del checkbox visualizar menus de sincronizacion de usuarios en Abies+.",
        ),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO app_settings (key, value, description)
        VALUES (?, ?, ?)
        """,
        (
            "abiesplus_default_importusers",
            "false",
            "Valor por defecto del checkbox bibliotecarios pueden importar usuarios masivamente en Abies+.",
        ),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO app_settings (key, value, description)
        VALUES (?, ?, ?)
        """,
        (
            "abiesplus_default_dry_run",
            "true",
            "Si es true, el lanzamiento desde el front de Abies+ usa dry-run por defecto.",
        ),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO app_settings (key, value, description)
        VALUES (?, ?, ?)
        """,
        (
            "centers_csv_path",
            str(V2_DIR / "data" / "centros_example.csv"),
            "Ruta del CSV de centros activo usado para sincronizar centers desde el front.",
        ),
    )

    center_columns = {
        "selected": "INTEGER NOT NULL DEFAULT 0",
        "enabled": "INTEGER NOT NULL DEFAULT 1",
        "priority": "INTEGER NOT NULL DEFAULT 100",
        "notes": "TEXT",
    }
    for column, definition in center_columns.items():
        add_column(conn, "centers", column, definition)

    check_columns = {
        "search_text": "TEXT",
        "abiesweb_uo_value": "TEXT",
        "abiesweb_uo_label": "TEXT",
        "selected_username": "TEXT",
        "selected_full_name": "TEXT",
    }
    for column, definition in check_columns.items():
        add_column(conn, "abiesweb_checks", column, definition)

    download_columns = {
        "check_id": "INTEGER",
        "started_at": "TEXT",
        "completed_at": "TEXT",
        "file_dir": "TEXT",
        "file_name": "TEXT",
        "relative_file_path": "TEXT",
        "suggested_filename": "TEXT",
        "source_url": "TEXT",
        "file_size_bytes": "INTEGER",
        "sha256": "TEXT",
        "impersonated_username": "TEXT",
        "impersonated_full_name": "TEXT",
        "export_date_detected": "TEXT",
        "export_age_days": "INTEGER",
        "export_reuse_max_age_days": "INTEGER",
        "export_date_matches_policy": "INTEGER",
        "download_mode": "TEXT",
        "export_progress_initial": "INTEGER",
        "export_progress_final": "INTEGER",
    }
    for column, definition in download_columns.items():
        add_column(conn, "downloads", column, definition)

    abiesplus_imports_columns = {
        "download_id": "INTEGER",
        "zip_file": "TEXT",
        "xml_entry": "TEXT",
        "dry_run": "INTEGER NOT NULL DEFAULT 0",
        "started_at": "TEXT",
        "completed_at": "TEXT",
    }
    for column, definition in abiesplus_imports_columns.items():
        add_column(conn, "abiesplus_imports", column, definition)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_downloads_check_id ON downloads(check_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_downloads_downloaded_at ON downloads(downloaded_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_job_centers_job_id ON job_centers(job_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_job_centers_center_code ON job_centers(center_code)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_job_centers_job_center ON job_centers(job_id, center_code)")
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, description) VALUES (?, ?)",
        (2, "download and abiesweb check metadata columns"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, description) VALUES (?, ?)",
        (3, "export reuse policy settings and download decision metadata"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, description) VALUES (?, ?)",
        (4, "center selection fields for v2 front"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, description) VALUES (?, ?)",
        (5, "orchestrator jobs and settings"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, description) VALUES (?, ?)",
        (6, "abiesplus imports metadata and abiesplus settings"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, description) VALUES (?, ?)",
        (7, "centers_csv_path setting for front csv upload and resync"),
    )
    normalize_existing_center_types(conn)
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, description) VALUES (?, ?)",
        (8, "normalize center_type values to Abies+ options"),
    )


def normalize_existing_center_types(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT center_code, center_type FROM centers").fetchall()
    updates = []
    for center_code, center_type in rows:
        normalized = normalize_center_type(center_type)
        if normalized != (center_type or ""):
            updates.append((normalized, center_code))
    if updates:
        conn.executemany(
            "UPDATE centers SET center_type = ?, updated_at = CURRENT_TIMESTAMP WHERE center_code = ?",
            updates,
        )


EXPECTED_CSV_HEADERS = (
    "Provincia",
    "Localidad",
    "Código Centro",
    "Denominación Centro",
    "Nombre de centro",
    "Titularidad",
    "Código Postal",
    "Dirección Postal",
    "Correo Electrónico",
    "Teléfono",
)
REQUIRED_CSV_HEADERS = ("Código Centro", "Nombre de centro")


def _parse_center_rows(csv_path: Path) -> list[dict]:
    rows = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            center_code = clean_text(row.get("Código Centro"))
            center_name = clean_text(row.get("Nombre de centro"))
            if not center_code or not center_name:
                continue
            rows.append(
                {
                    "center_code": center_code,
                    "province": clean_text(row.get("Provincia")),
                    "city": clean_text(row.get("Localidad")),
                    "center_type": normalize_center_type(row.get("Denominación Centro")),
                    "center_name": center_name,
                    "ownership": clean_text(row.get("Titularidad")),
                    "postal_code": clean_text(row.get("Código Postal")),
                    "postal_address": clean_text(row.get("Dirección Postal")),
                    "email": clean_text(row.get("Correo Electrónico")),
                    "phone": clean_text(row.get("Teléfono")),
                    "csv_source": str(csv_path),
                }
            )
    return rows


def _upsert_center_rows(conn: sqlite3.Connection, rows: list[dict]) -> None:
    conn.executemany(
        """
        INSERT INTO centers (
          center_code, province, city, center_type, center_name, ownership,
          postal_code, postal_address, email, phone, csv_source, updated_at
        ) VALUES (
          :center_code, :province, :city, :center_type, :center_name, :ownership,
          :postal_code, :postal_address, :email, :phone, :csv_source, CURRENT_TIMESTAMP
        )
        ON CONFLICT(center_code) DO UPDATE SET
          province = excluded.province,
          city = excluded.city,
          center_type = excluded.center_type,
          center_name = excluded.center_name,
          ownership = excluded.ownership,
          postal_code = excluded.postal_code,
          postal_address = excluded.postal_address,
          email = excluded.email,
          phone = excluded.phone,
          csv_source = excluded.csv_source,
          updated_at = CURRENT_TIMESTAMP
        """,
        rows,
    )


def seed_centers(db_path: Path, csv_path: Path) -> int:
    if not csv_path.exists():
        return 0
    rows = _parse_center_rows(csv_path)
    if not rows:
        return 0
    with sqlite3.connect(db_path) as conn:
        _upsert_center_rows(conn, rows)
    return len(rows)


def sync_centers_csv(db_path: Path, csv_path: Path) -> dict:
    """Sincroniza un CSV de centros contra SQLite con validacion de cabeceras.

    Devuelve un informe:
      {"total": int, "inserted": int, "updated": int,
       "missing_expected": [...], "errors": [...], "csv_path": str}
    """
    report: dict = {
        "total": 0,
        "inserted": 0,
        "updated": 0,
        "missing_expected": [],
        "errors": [],
        "csv_path": str(csv_path),
    }

    if not csv_path.exists():
        report["errors"].append(f"No se encuentra el archivo: {csv_path}")
        return report

    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames or []
            missing_required = [h for h in REQUIRED_CSV_HEADERS if h not in fieldnames]
            if missing_required:
                report["errors"].append(
                    f"Faltan cabeceras obligatorias: {', '.join(missing_required)}"
                )
                return report
            report["missing_expected"] = [h for h in EXPECTED_CSV_HEADERS if h not in fieldnames]
            rows = []
            skipped = 0
            for row in reader:
                center_code = clean_text(row.get("Código Centro"))
                center_name = clean_text(row.get("Nombre de centro"))
                if not center_code or not center_name:
                    skipped += 1
                    continue
                rows.append(
                    {
                        "center_code": center_code,
                        "province": clean_text(row.get("Provincia")),
                        "city": clean_text(row.get("Localidad")),
                        "center_type": normalize_center_type(row.get("Denominación Centro")),
                        "center_name": center_name,
                        "ownership": clean_text(row.get("Titularidad")),
                        "postal_code": clean_text(row.get("Código Postal")),
                        "postal_address": clean_text(row.get("Dirección Postal")),
                        "email": clean_text(row.get("Correo Electrónico")),
                        "phone": clean_text(row.get("Teléfono")),
                        "csv_source": str(csv_path),
                    }
                )
    except (OSError, UnicodeDecodeError) as exc:
        report["errors"].append(f"No se pudo leer el CSV: {exc}")
        return report

    if skipped:
        report["errors"].append(f"{skipped} filas sin codigo o nombre; ignoradas.")

    report["total"] = len(rows)
    if not rows:
        return report

    existing_codes: set[str] = set()
    with sqlite3.connect(db_path) as conn:
        existing_codes = {
            r[0] for r in conn.execute("SELECT center_code FROM centers").fetchall()
        }
        _upsert_center_rows(conn, rows)
        conn.commit()

    report["inserted"] = sum(1 for r in rows if r["center_code"] not in existing_codes)
    report["updated"] = report["total"] - report["inserted"]
    return report



def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inicializa la base SQLite de AbiesZipZap. Opcionalmente sincroniza un CSV de centros.",
        usage="python v2/db/init_db.py [csv_path] [--db DB] [--no-seed]",
    )
    parser.add_argument("csv", nargs="?", default=None, help="Ruta del CSV de centros a sincronizar (opcional)")
    parser.add_argument("--db", default=os.getenv("V2_DB_PATH", str(DEFAULT_DB_PATH)))
    parser.add_argument("--no-seed", action="store_true", help="No sincronizar CSV aunque se indique")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    db_path = Path(args.db)
    init_db(db_path)
    print(f"SQLite ready: {db_path}")
    if args.no_seed or not args.csv:
        print("No se indico CSV; la base queda sin centros. Sube uno desde el front o usa: python v2/db/init_db.py <archivo.csv>")
        return
    report = sync_centers_csv(db_path, Path(args.csv))
    print(f"CSV: {report['csv_path']}")
    if report["errors"]:
        for err in report["errors"]:
            print(f"  ERROR: {err}")
    if report["total"]:
        print(f"Centros sincronizados: {report['total']} ({report['inserted']} nuevos, {report['updated']} actualizados)")
    if report["missing_expected"]:
        print(f"  Cabeceras opcionales ausentes: {', '.join(report['missing_expected'])}")


if __name__ == "__main__":
    main()
