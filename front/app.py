import importlib.util
import os
import subprocess
import sys
from pathlib import Path

from flask import Flask, abort, flash, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename

from db import connect, db_path


V2_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = V2_DIR.parent
UPLOAD_CSV_PATH = V2_DIR / "data" / "centros_upload.csv"
DEFAULT_CSV_PATH = V2_DIR / "data" / "centros_example.csv"


def _load_init_db_module():
    spec = importlib.util.spec_from_file_location("v2_db_init", V2_DIR / "db" / "init_db.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_init_db_module = _load_init_db_module()
init_db = _init_db_module.init_db
sync_centers_csv = _init_db_module.sync_centers_csv
normalize_center_type = _init_db_module.normalize_center_type
CENTER_TYPE_OPTIONS = _init_db_module.ABIESPLUS_CENTER_TYPES


def _active_csv_path(conn) -> Path:
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = 'centers_csv_path'"
    ).fetchone()
    if row and str(row["value"] or "").strip():
        return Path(str(row["value"]).strip())
    return DEFAULT_CSV_PATH


def _flash_sync_report(report: dict) -> None:
    if report["errors"]:
        for err in report["errors"]:
            flash(err, "error")
    if report["total"]:
        flash(
            f"Sincronizacion CSV: {report['total']} centros "
            f"({report['inserted']} nuevos, {report['updated']} actualizados).",
            "success",
        )
    if report["missing_expected"]:
        flash(
            "Cabeceras opcionales ausentes: " + ", ".join(report["missing_expected"]),
            "warning",
        )



def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.getenv("V2_FRONT_SECRET", "v2-local-dev-secret")

    @app.context_processor
    def inject_globals():
        return {"db_path": db_path()}

    @app.get("/")
    def index():
        return redirect(url_for("centers"))

    @app.get("/centers")
    def centers():
        q = (request.args.get("q") or "").strip()
        status = (request.args.get("status") or "all").strip()
        selected = (request.args.get("selected") or "all").strip()

        where = []
        params = []
        if q:
            like = f"%{q}%"
            where.append("(c.center_code LIKE ? OR c.center_name LIKE ? OR c.city LIKE ? OR c.email LIKE ?)")
            params.extend([like, like, like, like])
        if selected == "yes":
            where.append("c.selected = 1")
        elif selected == "no":
            where.append("c.selected = 0")
        if status != "all":
            if status == "pending":
                where.append("ld.status IS NULL")
            elif status == "downloaded":
                where.append("ld.status = 'downloaded'")
            elif status == "error":
                where.append("(ld.status = 'error' OR lc.status = 'error')")
            elif status == "not_found":
                where.append("lc.status = 'center_not_found'")

        sql_where = "WHERE " + " AND ".join(where) if where else ""
        with connect() as conn:
            summary = dict(conn.execute(
                """
                SELECT
                  COUNT(*) AS total,
                  SUM(CASE WHEN selected = 1 THEN 1 ELSE 0 END) AS selected_count,
                  SUM(CASE WHEN enabled = 1 THEN 1 ELSE 0 END) AS enabled_count
                FROM centers
                """
            ).fetchone())
            rows = conn.execute(
                f"""
                WITH latest_download AS (
                  SELECT d.* FROM downloads d
                  JOIN (SELECT center_code, MAX(id) AS id FROM downloads GROUP BY center_code) x ON x.id = d.id
                ),
                latest_check AS (
                  SELECT c2.* FROM abiesweb_checks c2
                  JOIN (SELECT center_code, MAX(id) AS id FROM abiesweb_checks GROUP BY center_code) x ON x.id = c2.id
                )
                SELECT
                  c.*,
                  ld.id AS download_id,
                  ld.status AS download_status,
                  ld.download_mode,
                  ld.file_name,
                  ld.file_size_bytes,
                  ld.downloaded_at,
                  ld.export_date_detected,
                  ld.export_age_days,
                  lc.id AS check_id,
                  lc.status AS check_status,
                  lc.checked_at,
                  lc.users_found,
                  lc.reason AS check_reason
                FROM centers c
                LEFT JOIN latest_download ld ON ld.center_code = c.center_code
                LEFT JOIN latest_check lc ON lc.center_code = c.center_code
                {sql_where}
                ORDER BY c.selected DESC, c.priority ASC, c.center_code ASC
                """,
                params,
            ).fetchall()
        command = "docker compose run --rm abies-v2 python v2/orchestrator.py --selected-only"
        return render_template("centers.html", centers=rows, summary=summary, q=q, status=status, selected=selected, command=command)

    @app.post("/centers/selection")
    def update_selection():
        visible_codes = request.form.getlist("visible_codes")
        selected_codes = set(request.form.getlist("selected_codes"))
        with connect() as conn:
            for code in visible_codes:
                conn.execute("UPDATE centers SET selected = ?, updated_at = CURRENT_TIMESTAMP WHERE center_code = ?", (1 if code in selected_codes else 0, code))
        flash("Seleccion actualizada", "success")
        return redirect(url_for("centers", q=request.form.get("q", ""), status=request.form.get("status", "all"), selected=request.form.get("selected", "all")))

    @app.post("/centers/select-all")
    def select_all():
        with connect() as conn:
            conn.execute("UPDATE centers SET selected = 1, updated_at = CURRENT_TIMESTAMP WHERE enabled = 1")
        flash("Centros activos seleccionados", "success")
        return redirect(url_for("centers"))

    @app.post("/centers/clear-selection")
    def clear_selection():
        with connect() as conn:
            conn.execute("UPDATE centers SET selected = 0, updated_at = CURRENT_TIMESTAMP")
        flash("Seleccion limpiada", "success")
        return redirect(url_for("centers"))

    @app.post("/centers/upload-csv")
    def upload_csv():
        uploaded = request.files.get("csv_file")
        if not uploaded or not uploaded.filename:
            flash("Selecciona un archivo CSV.", "error")
            return redirect(url_for("centers"))
        if not uploaded.filename.lower().endswith(".csv"):
            flash("El archivo debe tener extension .csv", "error")
            return redirect(url_for("centers"))

        UPLOAD_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
        filename = secure_filename(uploaded.filename) or "centros_upload.csv"
        save_path = UPLOAD_CSV_PATH.parent / filename
        uploaded.save(str(save_path))

        init_db(db_path())
        report = sync_centers_csv(db_path(), save_path)
        with connect() as conn:
            conn.execute(
                "UPDATE app_settings SET value = ?, updated_at = CURRENT_TIMESTAMP WHERE key = 'centers_csv_path'",
                (str(save_path),),
            )
        _flash_sync_report(report)
        return redirect(url_for("centers"))

    @app.post("/centers/resync-csv")
    def resync_csv():
        with connect() as conn:
            csv_path = _active_csv_path(conn)
        init_db(db_path())
        report = sync_centers_csv(db_path(), csv_path)
        _flash_sync_report(report)
        return redirect(url_for("settings"))

    @app.get("/centers/<center_code>")
    def center_detail(center_code: str):
        with connect() as conn:
            center = conn.execute("SELECT * FROM centers WHERE center_code = ?", (center_code,)).fetchone()
            if not center:
                abort(404)
            checks = conn.execute("SELECT * FROM abiesweb_checks WHERE center_code = ? ORDER BY id DESC LIMIT 20", (center_code,)).fetchall()
            users = conn.execute("SELECT * FROM abiesweb_users WHERE center_code = ? ORDER BY id DESC LIMIT 50", (center_code,)).fetchall()
            downloads = conn.execute("SELECT * FROM downloads WHERE center_code = ? ORDER BY id DESC LIMIT 20", (center_code,)).fetchall()
            logs = conn.execute("SELECT * FROM session_logs WHERE center_code = ? ORDER BY id DESC LIMIT 50", (center_code,)).fetchall()
        return render_template(
            "center_detail.html",
            center=center,
            checks=checks,
            users=users,
            downloads=downloads,
            logs=logs,
            center_type_options=CENTER_TYPE_OPTIONS,
        )

    @app.post("/centers/<center_code>/update")
    def update_center_detail(center_code: str):
        action = (request.form.get("action") or "data").strip()
        with connect() as conn:
            center = conn.execute("SELECT center_code FROM centers WHERE center_code = ?", (center_code,)).fetchone()
            if not center:
                abort(404)

            if action == "notes":
                conn.execute(
                    "UPDATE centers SET notes = ?, updated_at = CURRENT_TIMESTAMP WHERE center_code = ?",
                    ((request.form.get("notes") or "").strip(), center_code),
                )
                flash("Notas actualizadas", "success")
            else:
                center_type = normalize_center_type(request.form.get("center_type"))
                selected = 1 if request.form.get("selected") == "1" else 0
                enabled = 1 if request.form.get("enabled") == "1" else 0
                conn.execute(
                    """
                    UPDATE centers
                    SET province = ?, city = ?, center_type = ?, center_name = ?, ownership = ?,
                        postal_code = ?, postal_address = ?, email = ?, phone = ?,
                        selected = ?, enabled = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE center_code = ?
                    """,
                    (
                        (request.form.get("province") or "").strip(),
                        (request.form.get("city") or "").strip(),
                        center_type,
                        (request.form.get("center_name") or "").strip(),
                        (request.form.get("ownership") or "").strip(),
                        (request.form.get("postal_code") or "").strip(),
                        (request.form.get("postal_address") or "").strip(),
                        (request.form.get("email") or "").strip(),
                        (request.form.get("phone") or "").strip(),
                        selected,
                        enabled,
                        center_code,
                    ),
                )
                flash("Datos del centro actualizados", "success")
        return redirect(url_for("center_detail", center_code=center_code))

    @app.post("/jobs/start")
    def start_job():
        retries_raw = (request.form.get("retries") or "").strip()
        limit_raw = (request.form.get("limit") or "").strip()
        force_remote_check = request.form.get("force_remote_check") == "1"

        command = [sys.executable, "v2/orchestrator.py", "--selected-only"]
        if retries_raw:
            try:
                retries = max(0, int(retries_raw))
            except ValueError:
                flash("Reintentos debe ser un numero entero >= 0", "error")
                return redirect(url_for("centers"))
            command.extend(["--retries", str(retries)])
        if limit_raw:
            try:
                limit = max(0, int(limit_raw))
            except ValueError:
                flash("Limite debe ser un numero entero >= 0", "error")
                return redirect(url_for("centers"))
            if limit:
                command.extend(["--limit", str(limit)])
        if force_remote_check:
            command.append("--force-remote-check")

        with connect() as conn:
            active = conn.execute("SELECT id, status FROM jobs WHERE status IN ('queued', 'running') ORDER BY id DESC LIMIT 1").fetchone()
            if active:
                flash(f"Ya hay un job activo: #{active['id']} ({active['status']})", "error")
                return redirect(url_for("jobs"))
            selected_count = conn.execute("SELECT COUNT(*) AS count FROM centers WHERE enabled = 1 AND selected = 1").fetchone()["count"]
            if not selected_count:
                flash("No hay centros seleccionados para procesar", "error")
                return redirect(url_for("centers", selected="yes"))

        log_dir = V2_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "orchestrator_front.log"
        child_env = os.environ.copy()
        child_env.pop("PYTHONPATH", None)
        with log_file.open("ab") as stream:
            subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                env=child_env,
                stdout=stream,
                stderr=stream,
                start_new_session=True,
            )
        import time as _time
        _time.sleep(2)
        flash("Job lanzado en segundo plano. Revisa la pantalla Jobs para ver el progreso.", "success")
        return redirect(url_for("jobs"))

    @app.get("/jobs")
    def jobs():
        with connect() as conn:
            summary = dict(conn.execute(
                """
                SELECT
                  COUNT(*) AS total,
                  SUM(CASE WHEN status IN ('queued', 'running') THEN 1 ELSE 0 END) AS active_count,
                  SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed_count,
                  SUM(CASE WHEN status = 'completed_with_errors' THEN 1 ELSE 0 END) AS completed_with_errors_count
                FROM jobs
                """
            ).fetchone())
            rows = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 100").fetchall()
        command = "docker compose run --rm abies-v2 python v2/orchestrator.py --selected-only"
        return render_template("jobs.html", jobs=rows, summary=summary, command=command)

    @app.get("/jobs/<int:job_id>")
    def job_detail(job_id: int):
        with connect() as conn:
            job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if not job:
                abort(404)
            centers = conn.execute(
                """
                SELECT jc.*, c.center_name, c.city, c.email, d.file_name, d.file_size_bytes, d.downloaded_at
                FROM job_centers jc
                JOIN centers c ON c.center_code = jc.center_code
                LEFT JOIN downloads d ON d.id = jc.download_id
                WHERE jc.job_id = ?
                ORDER BY jc.id ASC
                """,
                (job_id,),
            ).fetchall()
            logs = conn.execute(
                """
                SELECT l.*, c.center_name
                FROM session_logs l
                LEFT JOIN centers c ON c.center_code = l.center_code
                WHERE l.session_id = ?
                ORDER BY l.id DESC
                LIMIT 200
                """,
                (f"job-{job_id}",),
            ).fetchall()
        return render_template("job_detail.html", job=job, centers=centers, logs=logs)

    @app.get("/settings")
    def settings():
        with connect() as conn:
            rows = conn.execute("SELECT * FROM app_settings ORDER BY key").fetchall()
            active_csv = _active_csv_path(conn)
        return render_template("settings.html", settings=rows, active_csv=active_csv)

    @app.post("/settings")
    def update_settings():
        allowed = {
            "export_reuse_max_age_days",
            "local_timezone",
            "orchestrator_default_retries",
            "orchestrator_export_timeout_seconds",
            "orchestrator_skip_downloaded_today",
            "abiesplus_default_usar_registro",
            "abiesplus_default_syncusers",
            "abiesplus_default_importusers",
            "abiesplus_default_dry_run",
        }
        with connect() as conn:
            for key in allowed:
                if key in request.form:
                    value = (request.form.get(key) or "").strip()
                    if key in {"export_reuse_max_age_days", "orchestrator_default_retries", "orchestrator_export_timeout_seconds"}:
                        try:
                            value = str(max(0, int(value)))
                        except ValueError:
                            flash(f"{key} debe ser un numero entero >= 0", "error")
                            return redirect(url_for("settings"))
                    if key in {"orchestrator_skip_downloaded_today", "abiesplus_default_usar_registro", "abiesplus_default_syncusers", "abiesplus_default_importusers", "abiesplus_default_dry_run"}:
                        value = "true" if value.lower() in {"1", "true", "yes", "si", "on"} else "false"
                    conn.execute("UPDATE app_settings SET value = ?, updated_at = CURRENT_TIMESTAMP WHERE key = ?", (value, key))
        flash("Configuracion guardada", "success")
        return redirect(url_for("settings"))

    @app.get("/logs")
    def logs():
        center_code = (request.args.get("center_code") or "").strip()
        action = (request.args.get("action") or "").strip()
        where = []
        params = []
        if center_code:
            where.append("l.center_code = ?")
            params.append(center_code)
        if action:
            where.append("l.action = ?")
            params.append(action)
        sql_where = "WHERE " + " AND ".join(where) if where else ""
        with connect() as conn:
            rows = conn.execute(
                f"""
                SELECT l.*, c.center_name
                FROM session_logs l
                LEFT JOIN centers c ON c.center_code = l.center_code
                {sql_where}
                ORDER BY l.id DESC
                LIMIT 300
                """,
                params,
            ).fetchall()
            actions = conn.execute("SELECT DISTINCT action FROM session_logs ORDER BY action").fetchall()
        return render_template("logs.html", logs=rows, actions=actions, center_code=center_code, action=action)

    @app.get("/downloads/<int:download_id>")
    def download_file(download_id: int):
        with connect() as conn:
            row = conn.execute("SELECT * FROM downloads WHERE id = ?", (download_id,)).fetchone()
        if not row or not row["file_path"]:
            abort(404)
        path = Path(row["file_path"])
        if not path.exists():
            abort(404)
        return send_file(path, as_attachment=True, download_name=row["file_name"] or path.name)

    @app.get("/abiesplus")
    def abiesplus():
        with connect() as conn:
            summary = dict(conn.execute(
                """
                SELECT
                  COUNT(*) AS total_eligible,
                  SUM(CASE WHEN c.selected = 1 THEN 1 ELSE 0 END) AS selected_eligible
                FROM centers c
                WHERE EXISTS (
                    SELECT 1 FROM downloads d
                    WHERE d.center_code = c.center_code AND d.status = 'downloaded'
                )
                """
            ).fetchone())
            rows = conn.execute(
                """
                SELECT c.center_code, c.center_name, c.city, c.selected,
                       d.id AS download_id, d.file_name, d.file_size_bytes, d.downloaded_at,
                       (SELECT ai.status FROM abiesplus_imports ai
                        WHERE ai.center_code = c.center_code ORDER BY ai.id DESC LIMIT 1) AS import_status,
                       (SELECT ai.imported_at FROM abiesplus_imports ai
                        WHERE ai.center_code = c.center_code ORDER BY ai.id DESC LIMIT 1) AS imported_at
                FROM centers c
                JOIN downloads d ON d.id = (
                    SELECT id FROM downloads
                    WHERE center_code = c.center_code AND status = 'downloaded'
                    ORDER BY id DESC LIMIT 1
                )
                ORDER BY c.selected DESC, c.priority ASC, c.center_code ASC
                """
            ).fetchall()
            defaults = {
                row["key"]: str(row["value"] or "").lower() in {"1", "true", "yes", "on", "si"}
                for row in conn.execute(
                    "SELECT key, value FROM app_settings WHERE key IN (?, ?, ?, ?)",
                    ("abiesplus_default_usar_registro", "abiesplus_default_syncusers", "abiesplus_default_importusers", "abiesplus_default_dry_run"),
                ).fetchall()
            }
        command = "docker compose run --rm abies-v2 python v2/pullup2Ab+.py --selected-only --non-interactive --dry-run"
        return render_template("abiesplus.html", centers=rows, summary=summary, command=command, defaults=defaults)

    @app.post("/abiesplus/start")
    def start_abiesplus():
        dry_run = request.form.get("dry_run") == "1"
        usar_registro = request.form.get("usar_registro") == "1"
        syncusers = request.form.get("syncusers") == "1"
        importusers = request.form.get("importusers") == "1"
        only_codes = (request.form.get("only_codes") or "").strip()
        selected_codes = request.form.getlist("selected_codes")

        codes_to_process = only_codes
        if selected_codes:
            codes_to_process = ",".join(selected_codes)

        if not codes_to_process:
            flash("Marca al menos un centro en la tabla o indica codigos en 'Solo codigos'", "error")
            return redirect(url_for("abiesplus"))

        command = [sys.executable, "v2/pullup2Ab+.py", "--non-interactive", "--only-codes", codes_to_process]
        if dry_run:
            command.append("--dry-run")

        env_overrides = {
            "ABIESPLUS_ASK_BATCH_OPTIONS": "false",
            "ABIESPLUS_DEFAULT_USAR_REGISTRO": "true" if usar_registro else "false",
            "ABIESPLUS_DEFAULT_SYNCUSERS": "true" if syncusers else "false",
            "ABIESPLUS_DEFAULT_IMPORTUSERS": "true" if importusers else "false",
        }

        with connect() as conn:
            active = conn.execute(
                "SELECT id, status FROM jobs WHERE status IN ('queued', 'running') ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if active:
                flash(f"Ya hay un job activo: #{active['id']} ({active['status']})", "error")
                return redirect(url_for("abiesplus"))

        log_dir = V2_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "abiesplus_front.log"
        child_env = os.environ.copy()
        child_env.pop("PYTHONPATH", None)
        child_env.update(env_overrides)
        with log_file.open("ab") as stream:
            subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                env=child_env,
                stdout=stream,
                stderr=stream,
                start_new_session=True,
            )
        import time as _time
        _time.sleep(2)
        flash("Import Abies+ lanzado en segundo plano. Revisa los resultados abajo.", "success")
        return redirect(url_for("abiesplus"))

    return app


app = create_app()


if __name__ == "__main__":
    host = os.getenv("V2_FRONT_HOST", "0.0.0.0")
    port = int(os.getenv("V2_FRONT_PORT", "8010"))
    app.run(host=host, port=port)
