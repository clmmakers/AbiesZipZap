import argparse
import json
import os
import shlex
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from db.init_db import init_db, sync_centers_csv


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DEFAULT_DB_PATH = SCRIPT_DIR / "data" / "v2.sqlite3"
SINGLE_CENTER_SCRIPT = SCRIPT_DIR / "buscar_gestores_abiesweb.py"
ACTIVE_JOB_STATUSES = ("queued", "running")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def connect_db(db_path: Path, csv_path: Path | None = None) -> sqlite3.Connection:
    init_db(db_path)
    if csv_path:
        sync_centers_csv(db_path, csv_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_setting(conn: sqlite3.Connection, key: str, default: str) -> str:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    if not row:
        return default
    value = str(row["value"] or "").strip()
    return value or default


def get_int_setting(conn: sqlite3.Connection, key: str, default: int) -> int:
    try:
        return int(get_setting(conn, key, str(default)))
    except ValueError:
        return default


def get_bool_setting(conn: sqlite3.Connection, key: str, default: bool) -> bool:
    value = get_setting(conn, key, "true" if default else "false").lower()
    return value in {"1", "true", "yes", "si", "on"}


def timezone_from_setting(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError:
        return ZoneInfo("Europe/Madrid")


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def is_downloaded_today(row: sqlite3.Row | None, tz: ZoneInfo) -> bool:
    if not row or row["status"] != "downloaded":
        return False
    downloaded_at = parse_timestamp(row["downloaded_at"])
    if not downloaded_at:
        return False
    return downloaded_at.astimezone(tz).date() == datetime.now(tz).date()


def latest_download(conn: sqlite3.Connection, center_code: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM downloads WHERE center_code = ? ORDER BY id DESC LIMIT 1",
        (center_code,),
    ).fetchone()


def latest_check(conn: sqlite3.Connection, center_code: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM abiesweb_checks WHERE center_code = ? ORDER BY id DESC LIMIT 1",
        (center_code,),
    ).fetchone()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, object] | None:
    return dict(row) if row else None


def split_codes(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def selected_centers(conn: sqlite3.Connection, args: argparse.Namespace) -> tuple[str, list[sqlite3.Row]]:
    params: list[object] = []
    limit_clause = ""
    if args.limit:
        limit_clause = " LIMIT ?"
        params.append(args.limit)

    if args.only_codes:
        codes = split_codes(args.only_codes)
        if not codes:
            return "only_codes", []
        placeholders = ",".join("?" for _ in codes)
        rows = conn.execute(
            f"""
            SELECT * FROM centers
            WHERE enabled = 1 AND center_code IN ({placeholders})
            ORDER BY priority ASC, center_code ASC
            {limit_clause}
            """,
            [*codes, *params],
        ).fetchall()
        found = {row["center_code"] for row in rows}
        missing = [code for code in codes if code not in found]
        if missing:
            raise RuntimeError(f"Codigos no encontrados o no activos: {', '.join(missing)}")
        return "only_codes", rows

    if args.all_enabled:
        rows = conn.execute(
            f"""
            SELECT * FROM centers
            WHERE enabled = 1
            ORDER BY priority ASC, center_code ASC
            {limit_clause}
            """,
            params,
        ).fetchall()
        return "all_enabled", rows

    rows = conn.execute(
        f"""
        SELECT * FROM centers
        WHERE enabled = 1 AND selected = 1
        ORDER BY priority ASC, center_code ASC
        {limit_clause}
        """,
        params,
    ).fetchall()
    return "selected_only", rows


def ensure_no_active_job(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT id, status, started_at FROM jobs WHERE status IN (?, ?) ORDER BY id DESC LIMIT 1",
        ACTIVE_JOB_STATUSES,
    ).fetchone()
    if row:
        raise RuntimeError(f"Ya existe un job activo: #{row['id']} ({row['status']})")


def create_job(conn: sqlite3.Connection, mode: str, centers: list[sqlite3.Row], args: argparse.Namespace, retries: int) -> int:
    command = " ".join(shlex.quote(part) for part in sys.argv)
    details = {
        "center_codes": [row["center_code"] for row in centers],
        "limit": args.limit,
        "export_timeout_seconds": args.export_timeout_seconds,
    }
    try:
        conn.execute("BEGIN IMMEDIATE")
        ensure_no_active_job(conn)
        cursor = conn.execute(
            """
            INSERT INTO jobs (
              status, source, mode, total_centers, retries, force_remote_check,
              dry_run, command, started_at, details_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "running",
                "cli",
                mode,
                len(centers),
                retries,
                1 if args.force_remote_check else 0,
                0,
                command,
                utc_now(),
                json_dumps(details),
                utc_now(),
            ),
        )
        job_id = int(cursor.lastrowid)
        conn.executemany(
            """
            INSERT INTO job_centers (job_id, center_code, status, details_json, updated_at)
            VALUES (?, ?, 'queued', ?, ?)
            """,
            [
                (job_id, row["center_code"], json_dumps({"center_name": row["center_name"]}), utc_now())
                for row in centers
            ],
        )
        insert_session_log(conn, f"job-{job_id}", "orchestrator_start", None, "running", f"Job #{job_id} iniciado")
        conn.commit()
        return job_id
    except Exception:
        conn.rollback()
        raise


def insert_session_log(
    conn: sqlite3.Connection,
    session_id: str,
    action: str,
    center_code: str | None,
    status: str,
    message: str,
    details: dict[str, object] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO session_logs (session_id, action, center_code, status, message, details_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (session_id, action, center_code, status, message, json_dumps(details or {}), utc_now()),
    )


def update_job_center(
    conn: sqlite3.Connection,
    job_id: int,
    center_code: str,
    status: str,
    attempt_count: int,
    last_exit_code: int | None,
    reason: str,
    details: dict[str, object] | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> None:
    check = latest_check(conn, center_code)
    download = latest_download(conn, center_code)
    conn.execute(
        """
        UPDATE job_centers
        SET status = ?, attempt_count = ?, last_exit_code = ?, check_id = ?, download_id = ?,
            started_at = COALESCE(started_at, ?), finished_at = ?, reason = ?, details_json = ?, updated_at = ?
        WHERE job_id = ? AND center_code = ?
        """,
        (
            status,
            attempt_count,
            last_exit_code,
            check["id"] if check else None,
            download["id"] if download else None,
            started_at,
            finished_at,
            reason,
            json_dumps(details or {}),
            utc_now(),
            job_id,
            center_code,
        ),
    )


def refresh_job_counts(conn: sqlite3.Connection, job_id: int, final_status: str | None = None, reason: str = "") -> None:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS count FROM job_centers WHERE job_id = ? GROUP BY status",
        (job_id,),
    ).fetchall()
    counts = {row["status"]: int(row["count"]) for row in rows}
    processed = sum(count for status, count in counts.items() if status not in {"queued", "running"})
    status_value = final_status or "running"
    finished_at = utc_now() if final_status else None
    conn.execute(
        """
        UPDATE jobs
        SET status = ?, processed_centers = ?, downloaded_count = ?, skipped_count = ?,
            error_count = ?, center_not_found_count = ?, no_users_found_count = ?,
            finished_at = COALESCE(?, finished_at), reason = COALESCE(NULLIF(?, ''), reason), updated_at = ?
        WHERE id = ?
        """,
        (
            status_value,
            processed,
            counts.get("downloaded", 0),
            counts.get("skipped_downloaded_today", 0),
            counts.get("error", 0),
            counts.get("center_not_found", 0),
            counts.get("no_users_found", 0),
            finished_at,
            reason,
            utc_now(),
            job_id,
        ),
    )


def single_center_command(args: argparse.Namespace, center_code: str, export_timeout_seconds: int) -> list[str]:
    command = [
        sys.executable,
        str(SINGLE_CENTER_SCRIPT),
        "--db",
        str(Path(args.db)),
        "--center-code",
        center_code,
        "--export-timeout-seconds",
        str(export_timeout_seconds),
    ]
    if args.csv:
        command.extend(["--csv", str(Path(args.csv))])
    if args.timeout_ms:
        command.extend(["--timeout-ms", str(args.timeout_ms)])
    if args.headful:
        command.append("--headful")
    if args.debug:
        command.append("--debug")
    if args.verbose:
        command.append("--verbose")
    return command


def run_single_center(args: argparse.Namespace, center_code: str, export_timeout_seconds: int) -> tuple[int, dict[str, object]]:
    command = single_center_command(args, center_code, export_timeout_seconds)
    process_timeout = max(export_timeout_seconds + 600, 900)
    try:
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            timeout=process_timeout,
            check=False,
        )
        return completed.returncode, {
            "command": command,
            "stdout_tail": completed.stdout[-4000:],
            "stderr_tail": completed.stderr[-4000:],
        }
    except subprocess.TimeoutExpired as exc:
        return 124, {
            "command": command,
            "timeout_seconds": process_timeout,
            "stdout_tail": (exc.stdout or "")[-4000:] if isinstance(exc.stdout, str) else "",
            "stderr_tail": (exc.stderr or "")[-4000:] if isinstance(exc.stderr, str) else "",
        }


def classify_result(conn: sqlite3.Connection, center_code: str, exit_code: int) -> tuple[str, str]:
    check = latest_check(conn, center_code)
    download = latest_download(conn, center_code)
    if exit_code == 2:
        return "center_not_found", "Centro no encontrado en AbiesWeb"
    if exit_code == 3 or (check and check["status"] == "no_users_found"):
        return "no_users_found", "No se encontraron gestores impersonables"
    if exit_code == 0 and download and download["status"] == "downloaded":
        return "downloaded", "ZIP descargado"
    if exit_code == 0:
        return "error", "El script termino sin descarga registrada"
    if exit_code == 124:
        return "error", "Timeout del proceso individual"
    return "error", f"El script individual termino con codigo {exit_code}"


def process_center(
    conn: sqlite3.Connection,
    job_id: int,
    center: sqlite3.Row,
    args: argparse.Namespace,
    retries: int,
    export_timeout_seconds: int,
    skip_downloaded_today: bool,
    tz: ZoneInfo,
) -> str:
    center_code = center["center_code"]
    session_id = f"job-{job_id}"
    started_at = utc_now()
    update_job_center(conn, job_id, center_code, "running", 0, None, "Procesando", started_at=started_at)
    insert_session_log(conn, session_id, "orchestrator_center", center_code, "running", "Procesando centro")
    conn.commit()

    existing_download = latest_download(conn, center_code)
    if skip_downloaded_today and not args.force_remote_check and is_downloaded_today(existing_download, tz):
        details = {"download": row_to_dict(existing_download), "local_timezone": str(tz)}
        update_job_center(
            conn,
            job_id,
            center_code,
            "skipped_downloaded_today",
            0,
            None,
            "Descarga registrada hoy; se omite comprobacion remota",
            details,
            started_at=started_at,
            finished_at=utc_now(),
        )
        insert_session_log(
            conn,
            session_id,
            "orchestrator_skip",
            center_code,
            "skipped_downloaded_today",
            "Descarga registrada hoy; se omite comprobacion remota",
            details,
        )
        refresh_job_counts(conn, job_id)
        conn.commit()
        return "skipped_downloaded_today"

    last_status = "error"
    last_reason = ""
    last_details: dict[str, object] = {}
    last_exit_code = None
    max_attempts = retries + 1
    for attempt in range(1, max_attempts + 1):
        insert_session_log(
            conn,
            session_id,
            "orchestrator_attempt",
            center_code,
            "running",
            f"Intento {attempt}/{max_attempts}",
        )
        conn.commit()
        last_exit_code, run_details = run_single_center(args, center_code, export_timeout_seconds)
        last_status, last_reason = classify_result(conn, center_code, last_exit_code)
        last_details = {"attempt": attempt, "max_attempts": max_attempts, **run_details}
        if last_status != "error":
            break
        if attempt < max_attempts:
            insert_session_log(
                conn,
                session_id,
                "orchestrator_retry",
                center_code,
                "error",
                last_reason,
                last_details,
            )
            conn.commit()

    update_job_center(
        conn,
        job_id,
        center_code,
        last_status,
        max(1, int(last_details.get("attempt", 1))),
        last_exit_code,
        last_reason,
        last_details,
        started_at=started_at,
        finished_at=utc_now(),
    )
    insert_session_log(conn, session_id, "orchestrator_center", center_code, last_status, last_reason, last_details)
    refresh_job_counts(conn, job_id)
    conn.commit()
    return last_status


def final_job_status(conn: sqlite3.Connection, job_id: int) -> str:
    row = conn.execute(
        """
        SELECT
          SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS errors,
          SUM(CASE WHEN status = 'center_not_found' THEN 1 ELSE 0 END) AS not_found,
          SUM(CASE WHEN status = 'no_users_found' THEN 1 ELSE 0 END) AS no_users,
          SUM(CASE WHEN status IN ('queued', 'running') THEN 1 ELSE 0 END) AS unfinished
        FROM job_centers
        WHERE job_id = ?
        """,
        (job_id,),
    ).fetchone()
    if row["unfinished"]:
        return "failed"
    if row["errors"] or row["not_found"] or row["no_users"]:
        return "completed_with_errors"
    return "completed"


def run(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    csv_path = Path(args.csv) if args.csv else None
    with connect_db(db_path, csv_path) as conn:
        retries = args.retries if args.retries is not None else get_int_setting(conn, "orchestrator_default_retries", 0)
        export_timeout_seconds = (
            args.export_timeout_seconds
            if args.export_timeout_seconds is not None
            else get_int_setting(conn, "orchestrator_export_timeout_seconds", 2400)
        )
        skip_downloaded_today = get_bool_setting(conn, "orchestrator_skip_downloaded_today", True)
        tz = timezone_from_setting(get_setting(conn, "local_timezone", "Europe/Madrid"))
        mode, centers = selected_centers(conn, args)
        if args.dry_run:
            print(json.dumps({"mode": mode, "total_centers": len(centers), "centers": [dict(row) for row in centers]}, ensure_ascii=False, indent=2))
            return 0
        if not centers:
            print("No hay centros para procesar.")
            return 0

        job_id = create_job(conn, mode, centers, args, retries)
        print(f"Job #{job_id} iniciado con {len(centers)} centros.")
        exit_code = 0
        try:
            for center in centers:
                status = process_center(conn, job_id, center, args, retries, export_timeout_seconds, skip_downloaded_today, tz)
                print(f"{center['center_code']} {center['center_name']}: {status}")
            status = final_job_status(conn, job_id)
            refresh_job_counts(conn, job_id, status, "Job finalizado")
            insert_session_log(conn, f"job-{job_id}", "orchestrator_finish", None, status, f"Job #{job_id} finalizado")
            conn.commit()
            exit_code = 0 if status == "completed" else 1
            print(f"Job #{job_id} finalizado: {status}")
        except KeyboardInterrupt:
            conn.execute("UPDATE jobs SET status = 'cancelled', finished_at = ?, reason = ?, updated_at = ? WHERE id = ?", (utc_now(), "Cancelado por usuario", utc_now(), job_id))
            conn.execute("UPDATE job_centers SET status = 'cancelled', finished_at = ?, reason = ?, updated_at = ? WHERE job_id = ? AND status IN ('queued', 'running')", (utc_now(), "Cancelado por usuario", utc_now(), job_id))
            insert_session_log(conn, f"job-{job_id}", "orchestrator_cancel", None, "cancelled", "Job cancelado por usuario")
            conn.commit()
            print(f"Job #{job_id} cancelado.")
            exit_code = 130
        except Exception as exc:
            conn.execute("UPDATE jobs SET status = 'failed', finished_at = ?, reason = ?, updated_at = ? WHERE id = ?", (utc_now(), str(exc), utc_now(), job_id))
            insert_session_log(conn, f"job-{job_id}", "orchestrator_error", None, "failed", str(exc))
            conn.commit()
            raise
        return exit_code


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Orquestador secuencial AbiesZipZap para descargas AbiesWeb")
    parser.add_argument("--db", default=os.getenv("V2_DB_PATH", str(DEFAULT_DB_PATH)), help="SQLite de seguimiento AbiesZipZap")
    parser.add_argument("--csv", default=None, help="CSV de centros para sincronizar antes de procesar (opcional)")
    parser.add_argument("--selected-only", action="store_true", help="Procesa centros seleccionados; es el modo por defecto")
    parser.add_argument("--all-enabled", action="store_true", help="Procesa todos los centros activos")
    parser.add_argument("--only-codes", default="", help="Codigos concretos separados por coma")
    parser.add_argument("--limit", type=int, default=0, help="Limita el numero de centros")
    parser.add_argument("--retries", type=int, default=None, help="Reintentos por error tecnico; por defecto lee app_settings")
    parser.add_argument("--force-remote-check", action="store_true", help="No salta centros descargados hoy; entra en AbiesWeb")
    parser.add_argument("--export-timeout-seconds", type=int, default=None, help="Timeout de exportacion por centro")
    parser.add_argument("--timeout-ms", type=int, default=30000, help="Timeout Playwright para el script individual")
    parser.add_argument("--dry-run", action="store_true", help="Muestra centros seleccionados sin crear job ni procesar")
    parser.add_argument("--headful", action="store_true", help="Muestra navegador en el script individual")
    parser.add_argument("--debug", action="store_true", help="Guarda diagnosticos del script individual")
    parser.add_argument("--verbose", action="store_true", help="Activa log detallado del script individual")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 0:
        parser.error("--limit debe ser >= 0")
    if args.retries is not None and args.retries < 0:
        parser.error("--retries debe ser >= 0")
    if args.all_enabled and args.only_codes:
        parser.error("--all-enabled y --only-codes son incompatibles")
    return args


def main() -> None:
    try:
        raise SystemExit(run(parse_args()))
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
