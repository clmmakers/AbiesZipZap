import argparse
import csv
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from playwright.sync_api import Error as PlaywrightError, Page, TimeoutError as PlaywrightTimeoutError


SCRIPT_DIR = Path(__file__).resolve().parent

from abies_backup import Config, init_browser, login, new_context, save_auth_state  # noqa: E402
from db.init_db import init_db, normalize_center_type, sync_centers_csv  # noqa: E402


GESTION_SELECTOR = "#menu000_txt"
GESTORES_SELECTOR = "#submenu001_txt"
TOOLS_SELECTOR = "#menu001_txt"
EXPORT_SUBMENU_SELECTOR = "#submenu001_txt"
UNIT_INPUT_SELECTOR = "input.custom-combobox-input.ui-autocomplete-input"
ROLE_SELECT_SELECTOR = "select[ng-model='Buscar.roles']"
DEFAULT_ROLE_VALUE = "adminbiblioteca"
DEFAULT_DB_PATH = SCRIPT_DIR / "data" / "v2.sqlite3"
DEFAULT_DOWNLOAD_TIMEOUT_MS = 120000


def _env_selector(env_key: str, default: str) -> str:
    return os.getenv(env_key, default)


class CenterNotFoundInAbiesWeb(RuntimeError):
    pass


def clean_text(value: str) -> str:
    return " ".join((value or "").split()).strip()


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def load_centers(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        return [{key: clean_text(value) for key, value in row.items()} for row in csv.DictReader(f)]


def load_centers_from_sqlite(conn: sqlite3.Connection) -> list[dict[str, str]]:
    rows = conn.execute(
        "SELECT center_code, center_name, province, city, center_type, ownership, postal_code, postal_address, email, phone FROM centers ORDER BY center_code"
    ).fetchall()
    return [
        {
            "Código Centro": r["center_code"] or "",
            "Nombre de centro": r["center_name"] or "",
            "Provincia": r["province"] or "",
            "Localidad": r["city"] or "",
            "Denominación Centro": r["center_type"] or "",
            "Titularidad": r["ownership"] or "",
            "Código Postal": r["postal_code"] or "",
            "Dirección Postal": r["postal_address"] or "",
            "Correo Electrónico": r["email"] or "",
            "Teléfono": r["phone"] or "",
        }
        for r in rows
    ]


def choose_center(rows: list[dict[str, str]], center_code: str, center_name: str, index: int) -> dict[str, str]:
    if center_code:
        for row in rows:
            if row.get("Código Centro") == center_code:
                return row
        raise ValueError(f"No existe Código Centro={center_code} en el CSV")

    if center_name:
        needle = clean_text(center_name).lower()
        for row in rows:
            if needle in clean_text(row.get("Nombre de centro", "")).lower():
                return row
        raise ValueError(f"No existe un centro que contenga: {center_name}")

    if index < 0 or index >= len(rows):
        raise ValueError(f"Indice fuera de rango: {index}. Total centros: {len(rows)}")
    return rows[index]


def center_code(center: dict[str, str]) -> str:
    return clean_text(center.get("Código Centro", ""))


def center_name(center: dict[str, str]) -> str:
    return clean_text(center.get("Nombre de centro", ""))


def center_search_text(center: dict[str, str]) -> str:
    return f"{center_name(center)} ({center_code(center)})"


def connect_db(db_path: Path, csv_path: Path | None = None) -> sqlite3.Connection:
    init_db(db_path)
    if csv_path:
        sync_centers_csv(db_path, csv_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_int_setting(conn: sqlite3.Connection, key: str, default: int) -> int:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    if not row:
        return default
    try:
        return int(str(row["value"]).strip())
    except (TypeError, ValueError):
        return default


def get_text_setting(conn: sqlite3.Connection, key: str, default: str) -> str:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    if not row:
        return default
    value = str(row["value"] or "").strip()
    return value or default


def upsert_center(conn: sqlite3.Connection, center: dict[str, str], csv_path: Path) -> None:
    conn.execute(
        """
        INSERT INTO centers (
          center_code, province, city, center_type, center_name, ownership,
          postal_code, postal_address, email, phone, csv_source, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
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
        (
            center_code(center),
            clean_text(center.get("Provincia", "")),
            clean_text(center.get("Localidad", "")),
            normalize_center_type(center.get("Denominación Centro", "")),
            center_name(center),
            clean_text(center.get("Titularidad", "")),
            clean_text(center.get("Código Postal", "")),
            clean_text(center.get("Dirección Postal", "")),
            clean_text(center.get("Correo Electrónico", "")),
            clean_text(center.get("Teléfono", "")),
            str(csv_path),
        ),
    )


def insert_check(
    conn: sqlite3.Connection,
    center: dict[str, str],
    status: str,
    exists_in_abiesweb: int | None,
    users_found: int,
    search_text: str,
    selected_unit: dict[str, str] | None,
    selected_user: dict[str, object] | None,
    reason: str,
    details: dict[str, object],
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO abiesweb_checks (
          center_code, checked_at, status, exists_in_abiesweb, users_found,
          search_text, abiesweb_uo_value, abiesweb_uo_label,
          selected_username, selected_full_name, reason, details_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            center_code(center),
            utc_now(),
            status,
            exists_in_abiesweb,
            users_found,
            search_text,
            (selected_unit or {}).get("value"),
            (selected_unit or {}).get("text"),
            (selected_user or {}).get("username"),
            (selected_user or {}).get("name"),
            reason,
            json_dumps(details),
        ),
    )
    return int(cursor.lastrowid)


def insert_users(conn: sqlite3.Connection, check_id: int, center: dict[str, str], users: list[dict[str, object]]) -> None:
    for user in users:
        raw_count = clean_text(str(user.get("profile_count", "")))
        profile_count = int(raw_count) if raw_count.isdigit() else None
        conn.execute(
            """
            INSERT INTO abiesweb_users (
              check_id, center_code, full_name, username, document,
              profiles, profile_count, raw_json, detected_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                check_id,
                center_code(center),
                clean_text(str(user.get("name", ""))),
                clean_text(str(user.get("username", ""))),
                clean_text(str(user.get("document", ""))),
                clean_text(str(user.get("profiles", ""))),
                profile_count,
                json_dumps(user),
                utc_now(),
            ),
        )


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def insert_download(
    conn: sqlite3.Connection,
    check_id: int | None,
    center: dict[str, str],
    status: str,
    started_at: str,
    completed_at: str | None,
    file_path: str | None,
    selected_user: dict[str, object] | None,
    progress_initial: int | None,
    progress_final: int | None,
    reason: str,
    details: dict[str, object],
) -> int:
    file_dir = file_name_value = relative_path = suggested_filename = source_url = sha256 = None
    file_size_bytes = None
    if file_path:
        path = Path(file_path)
        file_dir = str(path.parent)
        file_name_value = path.name
        suggested_filename = str(details.get("suggested_filename") or path.name)
        file_size_bytes = path.stat().st_size if path.exists() else None
        sha256 = file_sha256(path) if path.exists() else None
        try:
            relative_path = str(path.relative_to(SCRIPT_DIR))
        except ValueError:
            relative_path = str(path)

    cursor = conn.execute(
        """
        INSERT INTO downloads (
          check_id, center_code, status, started_at, requested_at, completed_at, downloaded_at,
          file_dir, file_name, file_path, relative_file_path, suggested_filename, source_url,
          file_size_bytes, sha256, impersonated_username, impersonated_full_name,
          export_date_detected, export_age_days, export_reuse_max_age_days,
          export_date_matches_policy, download_mode, export_progress_initial, export_progress_final,
          reason, details_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        (
            check_id,
            center_code(center),
            status,
            started_at,
            started_at,
            completed_at,
            completed_at if status == "downloaded" else None,
            file_dir,
            file_name_value,
            file_path,
            relative_path,
            suggested_filename,
            str(details.get("source_url") or "") or None,
            file_size_bytes,
            sha256,
            (selected_user or {}).get("username"),
            (selected_user or {}).get("name"),
            details.get("export_date_detected"),
            details.get("export_age_days"),
            details.get("export_reuse_max_age_days"),
            details.get("export_date_matches_policy"),
            details.get("download_mode"),
            progress_initial,
            progress_final,
            reason,
            json_dumps(details),
        ),
    )
    return int(cursor.lastrowid)


def insert_session_log(
    conn: sqlite3.Connection,
    session_id: str,
    action: str,
    center: dict[str, str],
    status: str,
    message: str,
    details: dict[str, object] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO session_logs (session_id, action, center_code, status, message, details_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (session_id, action, center_code(center), status, message, json_dumps(details or {}), utc_now()),
    )


def logout(page: Page, timeout_ms: int) -> tuple[str, str]:
    try:
        logout_link = page.locator("a[href='/signout']").first
        if logout_link.count() == 0:
            logout_link = page.locator("a", has_text="salir").first
        if logout_link.count() == 0:
            return "logout_unavailable", "No se encontro enlace de salida"

        logout_link.click(timeout=timeout_ms)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            logging.debug("Timeout esperando domcontentloaded tras logout; se comprueba URL actual")
        page.wait_for_timeout(500)
        return "logout_ok", f"Sesion cerrada. URL actual: {page.url}"
    except Exception as exc:
        logging.exception("Error cerrando sesion")
        return "logout_error", str(exc)


def logout_and_log(conn: sqlite3.Connection, page: Page, session_id: str, center: dict[str, str], timeout_ms: int) -> None:
    status, message = logout(page, timeout_ms)
    insert_session_log(conn, session_id, "logout", center, status, message, {"url": getattr(page, "url", "")})
    conn.commit()


def click_text_or_parent(page: Page, selector: str, label: str, timeout_ms: int) -> None:
    locator = page.locator(selector).first
    locator.wait_for(state="visible", timeout=timeout_ms)
    locator.scroll_into_view_if_needed(timeout=timeout_ms)
    try:
        locator.click(timeout=timeout_ms)
        return
    except Exception:
        logging.debug("Click directo fallido para %s, probando enlace padre", label, exc_info=True)

    locator.evaluate("node => (node.closest('a') || node).click()")


def open_gestores(page: Page, timeout_ms: int) -> None:
    logging.info("Abriendo Gestion > Gestores")
    gestion = _env_selector("SELECTOR_GESTION_MENU", GESTION_SELECTOR)
    gestores = _env_selector("SELECTOR_GESTORES_SUBMENU", GESTORES_SELECTOR)
    unit_input = _env_selector("SELECTOR_GESTORES_INPUT", UNIT_INPUT_SELECTOR)
    click_text_or_parent(page, gestion, "Gestion", timeout_ms)
    page.wait_for_timeout(700)
    click_text_or_parent(page, gestores, "Gestores", timeout_ms)
    page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    page.locator(unit_input).first.wait_for(state="visible", timeout=timeout_ms)


def select_unit_by_combobox_option(page: Page, center_code: str, center_name: str, search_text: str) -> dict[str, str] | None:
    return page.evaluate(
        r"""
        ({ centerCode, centerName, searchText }) => {
          const clean = (value) => (value || '').normalize('NFD')
            .replace(/[\u0300-\u036f]/g, '')
            .replace(/[\"']/g, '')
            .replace(/\s+/g, ' ')
            .trim()
            .toLowerCase();
          const options = Array.from(document.querySelectorAll('#combobox option'));
          const normalizedName = clean(centerName);
          const option = options.find((item) => centerCode && item.textContent.includes(`(${centerCode})`))
            || options.find((item) => clean(item.textContent).includes(normalizedName));
          if (!option || !option.value) {
            return null;
          }

          const select = document.querySelector('#combobox');
          const hidden = document.querySelector('#buscaUO');
          const visible = document.querySelector('.custom-combobox-input');
          select.value = option.value;
          hidden.value = option.value;
          if (visible) {
            visible.value = searchText;
          }

          if (window.jQuery) {
            window.jQuery('#combobox').val(option.value).trigger('change');
            window.jQuery('#buscaUO').val(option.value).trigger('input').trigger('change');
            window.jQuery('.custom-combobox-input').val(searchText);
          } else {
            hidden.dispatchEvent(new Event('input', { bubbles: true }));
            hidden.dispatchEvent(new Event('change', { bubbles: true }));
            select.dispatchEvent(new Event('change', { bubbles: true }));
          }

          if (window.angular) {
            const scope = window.angular.element(hidden).scope();
            if (scope) {
              scope.Buscar = scope.Buscar || {};
              scope.Buscar.uos = option.value;
              scope.$evalAsync();
            }
          }

          return { value: option.value, text: option.textContent.trim() };
        }
        """,
        {"centerCode": center_code, "centerName": center_name, "searchText": search_text},
    )


def apply_filters(page: Page, center_code: str, center_name: str, search_text: str, role_value: str, timeout_ms: int) -> dict[str, str]:
    logging.info("Filtrando unidad organizativa: %s", search_text)
    selected_unit = select_unit_by_combobox_option(page, center_code, center_name, search_text)
    if selected_unit:
        logging.info("U.O. seleccionada: %s -> %s", selected_unit["value"], selected_unit["text"])
        page.wait_for_timeout(1000)
    else:
        raise CenterNotFoundInAbiesWeb(f"No se encontro la U.O. en AbiesWeb: {search_text}")

    role_select = page.locator(ROLE_SELECT_SELECTOR).first
    role_select.wait_for(state="visible", timeout=timeout_ms)
    role_select.select_option(value=role_value, timeout=timeout_ms)
    page.evaluate(
        """
        (roleValue) => {
          const select = document.querySelector("select[ng-model='Buscar.roles']");
          if (!select) return;
          select.value = roleValue;
          if (window.jQuery) {
            window.jQuery(select).trigger('input').trigger('change');
          } else {
            select.dispatchEvent(new Event('input', { bubbles: true }));
            select.dispatchEvent(new Event('change', { bubbles: true }));
          }
          if (window.angular) {
            const scope = window.angular.element(select).scope();
            if (scope) {
              scope.Buscar = scope.Buscar || {};
              scope.Buscar.roles = roleValue;
              scope.$evalAsync();
            }
          }
        }
        """,
        role_value,
    )
    page.wait_for_timeout(500)

    search_selectors = [
        "button:has-text('Buscar')",
        "input[type='submit'][value*='Buscar']",
        "input[type='button'][value*='Buscar']",
        "button:has-text('Filtrar')",
        "input[type='submit'][value*='Filtrar']",
        "input[type='button'][value*='Filtrar']",
    ]
    for selector in search_selectors:
        button = page.locator(selector).first
        if button.count() > 0 and button.is_visible():
            logging.info("Ejecutando busqueda con selector: %s", selector)
            button.click(timeout=timeout_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=timeout_ms)
            except PlaywrightTimeoutError:
                logging.debug("networkidle no alcanzado tras buscar; continuo con la extraccion")
            page.wait_for_timeout(1500)
            return selected_unit

    logging.info("No se detecto boton Buscar/Filtrar; se espera actualizacion automatica")
    page.keyboard.press("Enter")
    page.wait_for_timeout(2000)
    return selected_unit


def extract_table_rows(page: Page) -> list[dict[str, object]]:
    extracted = []
    rows = page.locator("table tr")
    for i in range(rows.count()):
        row = rows.nth(i)
        try:
            if not row.is_visible():
                continue
            cells = [clean_text(text) for text in row.locator("td").all_inner_texts()]
        except Exception:
            continue
        cells = [cell for cell in cells if cell]
        if not cells:
            continue
        extracted.append({"index": i, "cells": cells, "text": " | ".join(cells)})
    return extracted


def extract_user_rows(page: Page) -> list[dict[str, object]]:
    extracted = []
    rows = page.locator("tr[ng-repeat*='item in data']")
    for i in range(rows.count()):
        row = rows.nth(i)
        try:
            if not row.is_visible():
                continue
            name = clean_text(row.locator(".sf_admin_list_td_nombre").nth(1).inner_text())
            username = clean_text(row.locator(".sf_admin_list_td_sfusername").first.inner_text())
            document = clean_text(row.locator(".sf_admin_list_td_docidentificacion").first.inner_text())
            profiles = clean_text(row.locator(".sf_admin_list_td_perfiles").first.inner_text())
            profile_count = clean_text(row.locator("a.perfiles").first.inner_text()).strip("()")
        except Exception:
            continue
        if not name or not username:
            continue
        extracted.append(
            {
                "user_row_index": i,
                "name": name,
                "username": username,
                "document": document,
                "profiles": profiles,
                "profile_count": profile_count,
                "text": " | ".join(x for x in [name, username, document, profiles, profile_count] if x),
            }
        )
    return extracted


def likely_user_rows(rows: list[dict[str, object]], center_name: str) -> list[dict[str, object]]:
    ignored_fragments = {
        "gestion",
        "gestores",
        "catalogos",
        "prestamos",
        "lectores",
        "herramientas",
    }
    center_token = clean_text(center_name).lower()
    result = []
    for row in rows:
        text = str(row["text"])
        lowered = text.lower()
        if len(row["cells"]) < 2:
            continue
        if lowered in ignored_fragments:
            continue
        if "admin. biblioteca" in lowered or "adminbiblioteca" in lowered or center_token in lowered:
            result.append(row)
    return result or rows


def choose_impersonation_user(users: list[dict[str, object]]) -> dict[str, object]:
    for user in users:
        profiles = str(user.get("profiles", "")).lower()
        if "adminedae" not in profiles:
            return user
    raise RuntimeError("No hay usuarios impersonables: todos tienen perfil adminEDAE o no se detectaron perfiles")


def click_view_as_for_user(page: Page, user: dict[str, object], timeout_ms: int) -> None:
    row_index = int(user["user_row_index"])
    row = page.locator("tr[ng-repeat*='item in data']").nth(row_index)
    row.scroll_into_view_if_needed(timeout=timeout_ms)
    action = row.locator("a:has(img[title='Ver como...']), a:has(img[alt='Ver como...'])").first
    if action.count() == 0:
        raise RuntimeError(f"El usuario {user.get('username')} no tiene boton 'Ver como...'")

    logging.info("Impersonando usuario: %s (%s)", user.get("name"), user.get("username"))
    action.click(timeout=timeout_ms)
    page.wait_for_selector("#tabla_cambiar_perfil", timeout=timeout_ms)


def switch_to_admin_abiesweb_profile(page: Page, timeout_ms: int) -> None:
    profile_link = page.locator("#tabla_cambiar_perfil a", has_text="Administrador AbiesWeb").first
    profile_link.wait_for(state="visible", timeout=timeout_ms)
    href = profile_link.get_attribute("href") or ""
    logging.info("Cambiando al perfil Administrador AbiesWeb: %s", href)
    profile_link.click(timeout=timeout_ms)
    page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    page.wait_for_timeout(1000)


def open_exportacion(page: Page, cfg: Config, timeout_ms: int) -> None:
    logging.info("Abriendo Herramientas > Exportacion")
    tools_sel = _env_selector("SELECTOR_TOOLS_MENU", TOOLS_SELECTOR)
    export_submenu_sel = _env_selector("SELECTOR_EXPORT_SUBMENU", EXPORT_SUBMENU_SELECTOR)
    tools = page.locator(tools_sel).first
    if tools.count() > 0 and tools.is_visible():
        click_text_or_parent(page, tools_sel, "Herramientas", timeout_ms)
        page.wait_for_timeout(700)
    else:
        page.goto(f"{cfg.base_url}{cfg.tools_path}", wait_until="domcontentloaded")

    submenu = page.locator(export_submenu_sel).first
    export_link = page.locator("a", has_text="Exportación").first
    if submenu.count() > 0 and submenu.is_visible() and "Export" in clean_text(submenu.inner_text()):
        click_text_or_parent(page, export_submenu_sel, "Exportacion", timeout_ms)
    elif export_link.count() > 0:
        export_link.click(timeout=timeout_ms)
    else:
        page.goto(f"{cfg.base_url}{cfg.export_path}", wait_until="domcontentloaded")

    page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    page.wait_for_selector("#salidaImport, #FormExportacion", timeout=timeout_ms)
    page.wait_for_timeout(1500)


def parse_progress_percent(text: str) -> int:
    match = re.search(r"(\d{1,3})\s*%", text or "")
    if not match:
        return -1
    value = int(match.group(1))
    return max(0, min(value, 100))


def read_progress_percent(page: Page) -> int:
    if page.locator("#resultadoEvolucion").count() == 0:
        return -1
    return parse_progress_percent(page.locator("#resultadoEvolucion").inner_text(timeout=30000))


def read_export_date(page: Page) -> date | None:
    texts = []
    if page.locator("#FormExportacion").count() > 0:
        texts.append(page.locator("#FormExportacion").inner_text(timeout=30000))
    texts.append(page.locator("body").inner_text(timeout=30000))
    text = "\n".join(texts)
    match = re.search(r"generado\s+el\s+d[ií]a:\s*(\d{2}/\d{2}/\d{4})", text, re.IGNORECASE)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%d/%m/%Y").date()
    except ValueError:
        return None


def local_today(timezone_name: str) -> date:
    try:
        return datetime.now(ZoneInfo(timezone_name)).date()
    except ZoneInfoNotFoundError:
        logging.warning("Zona horaria no encontrada: %s. Se usa fecha local del sistema", timezone_name)
        return date.today()


def export_date_policy(page: Page, max_age_days: int, timezone_name: str) -> dict[str, object]:
    max_age_days = int(max_age_days or 0)
    export_date = read_export_date(page)
    today = local_today(timezone_name)
    age_days = None
    matches_policy = False
    if export_date:
        age_days = (today - export_date).days
        matches_policy = 0 <= age_days <= max_age_days
    return {
        "export_date": export_date,
        "export_date_detected": export_date.isoformat() if export_date else None,
        "export_age_days": age_days,
        "export_reuse_max_age_days": max_age_days,
        "export_date_matches_policy": 1 if matches_policy else 0,
        "today": today.isoformat(),
        "timezone": timezone_name,
    }


def start_export(page: Page, cfg: Config, timeout_ms: int) -> None:
    export_button = page.locator(cfg.selector_export_trigger).first
    try:
        export_button.wait_for(state="attached", timeout=timeout_ms)
        export_button.click(timeout=timeout_ms, force=True)
        logging.info("Click force=True en boton exportacion (formulario oculto por AbiesWeb)")
        page.wait_for_timeout(2000)
        return
    except Exception:
        logging.debug("Click force en Exportacion fallido; probando submit directo", exc_info=True)
    submit_export_form_direct(page, timeout_ms)


def submit_export_form_direct(page: Page, timeout_ms: int) -> None:
    submitted = page.evaluate(
        """
        () => {
          const form = document.querySelector('#FormExportacion');
          if (!form) return false;
          const button = document.querySelector('#Exportacion');
          if (button) {
            button.click();
          } else if (form.requestSubmit) {
            form.requestSubmit();
          } else {
            form.submit();
          }
          return true;
        }
        """
    )
    if not submitted:
        raise RuntimeError("No se encontro formulario de exportacion para relanzar")
    logging.info("Submit directo del formulario de exportacion enviado")
    page.wait_for_timeout(2000)


def wait_for_export_ready(
    page: Page,
    center_code: str,
    timeout_seconds: int,
    error_marker: str = "error",
) -> int:
    start = time.time()
    last_pct = -2
    last_has_link = None
    last_heartbeat = -1
    error_marker_norm = (error_marker or "").strip().lower()
    while True:
        has_link = page.locator("#enlaceZip").count() > 0
        pct_text = ""
        if page.locator("#resultadoEvolucion").count() > 0:
            pct_text = clean_text(page.locator("#resultadoEvolucion").inner_text(timeout=30000))
        pct = parse_progress_percent(pct_text)
        elapsed = int(time.time() - start)

        if pct != last_pct or has_link != last_has_link:
            logging.info("[%s] Progreso exportacion: %s (link=%s, t=%ss)", center_code, pct_text or "?", has_link, elapsed)
            last_pct = pct
            last_has_link = has_link

        heartbeat = elapsed // 30
        if heartbeat != last_heartbeat:
            logging.info("[%s] HEARTBEAT 30s: progreso=%s (link=%s, t=%ss)", center_code, pct_text or "?", has_link, elapsed)
            last_heartbeat = heartbeat

        if error_marker_norm and error_marker_norm in (pct_text or "").lower() and not (has_link and pct >= 100):
            logging.error("[%s] AbiesWeb reporto estado de error: %s", center_code, pct_text)
            raise RuntimeError(f"AbiesWeb reporto error de exportacion: {pct_text}")

        if has_link and pct >= 100:
            logging.info("[%s] Exportacion lista para descargar", center_code)
            return pct
        if elapsed > timeout_seconds:
            raise RuntimeError(f"Timeout esperando exportacion lista ({timeout_seconds}s)")
        page.wait_for_timeout(10_000)


def wait_for_export_start_observed(page: Page, center_code: str, timeout_seconds: int) -> None:
    start = time.time()
    last_state = None
    while True:
        elapsed = int(time.time() - start)
        try:
            has_link = page.locator("#enlaceZip").count() > 0
            pct_text = ""
            if page.locator("#resultadoEvolucion").count() > 0:
                pct_text = clean_text(page.locator("#resultadoEvolucion").inner_text(timeout=30000))
            pct = parse_progress_percent(pct_text)
        except PlaywrightError as exc:
            logging.info("[%s] Navegacion durante espera de inicio de exportacion: %s", center_code, exc)
            if elapsed > timeout_seconds:
                raise RuntimeError(f"No se observo inicio de exportacion tras relanzar ({timeout_seconds}s)") from exc
            page.wait_for_timeout(1000)
            continue

        state = (pct, has_link)

        if state != last_state:
            logging.info("[%s] Esperando inicio de exportacion: progreso=%s link=%s t=%ss", center_code, pct_text or "?", has_link, elapsed)
            last_state = state

        if pct >= 0 and pct < 100:
            logging.info("[%s] Inicio de exportacion observado por progreso < 100%%", center_code)
            return
        if not has_link:
            logging.info("[%s] Inicio de exportacion observado por desaparicion del enlace ZIP", center_code)
            return
        if elapsed > timeout_seconds:
            raise RuntimeError(f"No se observo inicio de exportacion tras relanzar ({timeout_seconds}s)")
        page.wait_for_timeout(5_000)


def wait_for_export_start_after_relaunch(page: Page, center_code: str, timeout_ms: int, timeout_seconds: int) -> None:
    try:
        wait_for_export_start_observed(page, center_code, min(timeout_seconds, 120))
    except RuntimeError:
        logging.warning("[%s] No se observo inicio tras click; reintentando submit directo del formulario", center_code)
        submit_export_form_direct(page, timeout_ms)
        page.wait_for_timeout(1000)
        wait_for_export_start_observed(page, center_code, min(timeout_seconds, 120))


def safe_folder_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return cleaned.strip("_") or "centro"


def download_export(page: Page, cfg: Config, center: dict[str, str], timeout_ms: int) -> dict[str, object]:
    center_code = center.get("Código Centro", "")
    center_name = clean_text(center.get("Nombre de centro", ""))
    output_base = Path(cfg.output_dir)
    if not output_base.is_absolute():
        output_base = SCRIPT_DIR / output_base
    output_dir = output_base / safe_folder_name(f"{center_code}_{center_name}")
    output_dir.mkdir(parents=True, exist_ok=True)

    download_link = page.locator(cfg.selector_download_link).first
    if download_link.count() == 0:
        download_link = page.locator("#enlaceZip").first
    download_link.wait_for(state="visible", timeout=timeout_ms)
    source_url = download_link.get_attribute("href") or ""

    download_timeout_ms = int(os.getenv("DOWNLOAD_TIMEOUT_MS", str(DEFAULT_DOWNLOAD_TIMEOUT_MS)))
    with page.expect_download(timeout=download_timeout_ms) as dl_info:
        download_link.click(timeout=timeout_ms)
    download = dl_info.value
    destination = output_dir / download.suggested_filename
    download.save_as(str(destination))
    logging.info("[%s] ZIP descargado: %s", center_code, destination)
    return {
        "file_path": str(destination),
        "source_url": source_url,
        "suggested_filename": download.suggested_filename,
    }


def export_and_download(
    page: Page,
    cfg: Config,
    center: dict[str, str],
    timeout_ms: int,
    timeout_seconds: int,
    export_reuse_max_age_days: int,
    local_timezone: str,
) -> dict[str, object]:
    center_code = center.get("Código Centro", "")
    page.wait_for_timeout(1500)
    current_pct = read_progress_percent(page)
    has_link = page.locator("#enlaceZip").count() > 0
    export_button = page.locator(cfg.selector_export_trigger).first
    can_launch = export_button.count() > 0 and export_button.is_visible()
    progress_visible = page.locator("#salidaImport").count() > 0 and page.locator("#salidaImport").first.is_visible()
    initial_policy = export_date_policy(page, export_reuse_max_age_days, local_timezone)
    logging.info(
        "[%s] Estado inicial exportacion: progreso=%s enlace=%s boton_exportar_visible=%s fecha=%s edad=%s margen=%s cumple=%s",
        center_code,
        current_pct,
        has_link,
        can_launch,
        initial_policy["export_date_detected"] or "?",
        initial_policy["export_age_days"],
        export_reuse_max_age_days,
        bool(initial_policy["export_date_matches_policy"]),
    )

    if has_link and current_pct >= 100:
        if initial_policy["export_date_matches_policy"]:
            logging.info("[%s] Exportacion existente dentro de margen; se descarga sin relanzar", center_code)
            final_pct = current_pct
            download_mode = "reused_existing_export"
        else:
            download_mode = "reexported_old_export" if initial_policy["export_date_detected"] else "reexported_unknown_date"
            logging.info("[%s] Exportacion existente fuera de margen o sin fecha; se relanza (%s)", center_code, download_mode)
            start_export(page, cfg, timeout_ms)
            page.wait_for_timeout(1000)
            wait_for_export_start_after_relaunch(page, center_code, timeout_ms, timeout_seconds)
            final_pct = wait_for_export_ready(page, center_code, timeout_seconds, cfg.status_error)
    elif current_pct >= 0 and current_pct < 100:
        logging.info("[%s] Exportacion ya en curso; se espera a 100%%", center_code)
        download_mode = "waited_running_export"
        final_pct = wait_for_export_ready(page, center_code, timeout_seconds, cfg.status_error)
    elif progress_visible and not can_launch:
        logging.info("[%s] Hay una exportacion activa sin porcentaje legible inicial; se espera al progreso", center_code)
        download_mode = "waited_running_export_unknown_initial_progress"
        final_pct = wait_for_export_ready(page, center_code, timeout_seconds, cfg.status_error)
    else:
        logging.info("[%s] Lanzando exportacion", center_code)
        download_mode = "reexported_no_existing_export"
        start_export(page, cfg, timeout_ms)
        page.wait_for_timeout(1000)
        final_pct = wait_for_export_ready(page, center_code, timeout_seconds, cfg.status_error)

    final_policy = export_date_policy(page, export_reuse_max_age_days, local_timezone)
    download_info = download_export(page, cfg, center, timeout_ms)
    download_info["export_progress_initial"] = current_pct
    download_info["export_progress_final"] = final_pct
    download_info["download_mode"] = download_mode
    download_info["export_date_detected"] = final_policy["export_date_detected"] or initial_policy["export_date_detected"]
    download_info["initial_export_date_detected"] = initial_policy["export_date_detected"]
    download_info["final_export_date_detected"] = final_policy["export_date_detected"]
    download_info["export_age_days"] = final_policy["export_age_days"] if final_policy["export_age_days"] is not None else initial_policy["export_age_days"]
    download_info["export_reuse_max_age_days"] = export_reuse_max_age_days
    download_info["local_timezone"] = local_timezone
    download_info["policy_today"] = final_policy["today"]
    download_info["export_date_matches_policy"] = final_policy["export_date_matches_policy"] or initial_policy["export_date_matches_policy"]
    return download_info


def save_debug(page: Page, debug_dir: Path, prefix: str) -> None:
    debug_dir.mkdir(parents=True, exist_ok=True)
    html_path = debug_dir / f"{prefix}.html"
    screenshot_path = debug_dir / f"{prefix}.png"
    html_path.write_text(page.content(), encoding="utf-8")
    page.screenshot(path=str(screenshot_path), full_page=True)
    logging.info("Diagnostico guardado en %s y %s", html_path, screenshot_path)


def run(args: argparse.Namespace) -> int:
    load_dotenv(SCRIPT_DIR / ".env")
    cfg = Config()
    if args.headful:
        cfg.headless = False

    csv_path = Path(args.csv) if args.csv else None
    db_path = Path(args.db)
    conn = connect_db(db_path, csv_path)
    if csv_path:
        centers = load_centers(csv_path)
    else:
        centers = load_centers_from_sqlite(conn)
    center = choose_center(centers, args.center_code, args.center_name, args.index)
    name = center_name(center)
    code = center_code(center)
    search_text = center_search_text(center)
    if not name:
        raise ValueError("La fila seleccionada no tiene valor en 'Nombre de centro'")

    conn = connect_db(db_path, csv_path if csv_path else None)
    export_reuse_max_age_days = get_int_setting(conn, "export_reuse_max_age_days", 0)
    local_timezone = get_text_setting(conn, "local_timezone", "Europe/Madrid")
    session_id = str(uuid.uuid4())
    insert_session_log(conn, session_id, "start", center, "started", f"Inicio procesamiento {search_text}")
    conn.commit()

    playwright, browser = init_browser(cfg)
    context = new_context(browser, cfg, accept_downloads=True)
    page = context.new_page()
    selected_unit = None
    users: list[dict[str, object]] = []
    selected_user = None
    check_id = None

    try:
        login(page, cfg)
        save_auth_state(context, cfg)
        open_gestores(page, args.timeout_ms)
        selected_unit = apply_filters(page, code, name, search_text, args.role, args.timeout_ms)
        users = extract_user_rows(page)
        if not users:
            rows = extract_table_rows(page)
            users = likely_user_rows(rows, name)

        if users and not args.list_only:
            selected_user = choose_impersonation_user(users)

        check_status = "users_found" if users else "no_users_found"
        check_id = insert_check(
            conn,
            center,
            check_status,
            1,
            len(users),
            search_text,
            selected_unit,
            selected_user,
            "" if users else "No se encontraron gestores para la U.O. y perfil indicados",
            {
                "role_filter": args.role,
                "session_id": session_id,
                "list_only": args.list_only,
                "users": users,
            },
        )
        insert_users(conn, check_id, center, users)
        insert_session_log(conn, session_id, "abiesweb_check", center, check_status, f"Gestores encontrados: {len(users)}")
        conn.commit()

        output = {
            "center": {
                "codigo": code,
                "nombre": name,
                "localidad": center.get("Localidad", ""),
                "provincia": center.get("Provincia", ""),
                "denominacion": center.get("Denominación Centro", ""),
                "titularidad": center.get("Titularidad", ""),
                "codigo_postal": center.get("Código Postal", ""),
                "direccion_postal": center.get("Dirección Postal", ""),
                "correo_electronico": center.get("Correo Electrónico", ""),
                "telefono": center.get("Teléfono", ""),
            },
            "search_text": search_text,
            "db_path": str(db_path),
            "export_reuse_max_age_days": export_reuse_max_age_days,
            "local_timezone": local_timezone,
            "check_id": check_id,
            "role_filter": args.role,
            "rows_found": len(users),
            "rows": users,
        }

        if not users and not args.list_only:
            print(json.dumps(output, ensure_ascii=False, indent=2))
            if args.debug:
                save_debug(page, SCRIPT_DIR / "debug_gestores", "no_users_found")
            logout_and_log(conn, page, session_id, center, args.timeout_ms)
            insert_session_log(conn, session_id, "finish", center, "no_users_found", "Proceso finalizado sin gestores impersonables")
            conn.commit()
            return 3

        if not args.list_only:
            download_started_at = utc_now()
            click_view_as_for_user(page, selected_user, args.timeout_ms)
            switch_to_admin_abiesweb_profile(page, args.timeout_ms)
            open_exportacion(page, cfg, args.timeout_ms)
            download_info = export_and_download(
                page,
                cfg,
                center,
                args.timeout_ms,
                args.export_timeout_seconds,
                export_reuse_max_age_days,
                local_timezone,
            )
            downloaded_file = str(download_info["file_path"])
            completed_at = utc_now()
            download_id = insert_download(
                conn,
                check_id,
                center,
                "downloaded",
                download_started_at,
                completed_at,
                downloaded_file,
                selected_user,
                int(download_info.get("export_progress_initial", -1)),
                int(download_info.get("export_progress_final", -1)),
                "",
                {
                    "session_id": session_id,
                    "source_url": download_info.get("source_url", ""),
                    "suggested_filename": download_info.get("suggested_filename", ""),
                    "download_mode": download_info.get("download_mode", ""),
                    "export_date_detected": download_info.get("export_date_detected"),
                    "initial_export_date_detected": download_info.get("initial_export_date_detected"),
                    "final_export_date_detected": download_info.get("final_export_date_detected"),
                    "export_age_days": download_info.get("export_age_days"),
                    "export_reuse_max_age_days": download_info.get("export_reuse_max_age_days"),
                    "local_timezone": download_info.get("local_timezone"),
                    "policy_today": download_info.get("policy_today"),
                    "export_date_matches_policy": download_info.get("export_date_matches_policy"),
                    "selected_unit": selected_unit,
                    "selected_user": selected_user,
                },
            )
            insert_session_log(
                conn,
                session_id,
                "download",
                center,
                "downloaded",
                downloaded_file,
                {
                    "download_id": download_id,
                    "download_mode": download_info.get("download_mode", ""),
                    "export_date_detected": download_info.get("export_date_detected"),
                    "initial_export_date_detected": download_info.get("initial_export_date_detected"),
                    "final_export_date_detected": download_info.get("final_export_date_detected"),
                    "export_age_days": download_info.get("export_age_days"),
                    "export_reuse_max_age_days": export_reuse_max_age_days,
                    "local_timezone": local_timezone,
                    "policy_today": download_info.get("policy_today"),
                },
            )
            conn.commit()
            output["selected_user"] = selected_user
            output["downloaded_file"] = downloaded_file
            output["download_id"] = download_id
            output["download_mode"] = download_info.get("download_mode", "")
            output["export_date_detected"] = download_info.get("export_date_detected")
            output["export_age_days"] = download_info.get("export_age_days")
            output["policy_today"] = download_info.get("policy_today")

        print(json.dumps(output, ensure_ascii=False, indent=2))
        if args.debug:
            save_debug(page, SCRIPT_DIR / "debug_gestores", "ultimo_resultado")
        logout_and_log(conn, page, session_id, center, args.timeout_ms)
        insert_session_log(conn, session_id, "finish", center, "ok", "Proceso completado")
        conn.commit()
        return 0
    except CenterNotFoundInAbiesWeb as exc:
        logging.exception("Centro no encontrado en AbiesWeb")
        check_id = insert_check(
            conn,
            center,
            "center_not_found",
            0,
            0,
            search_text,
            selected_unit,
            selected_user,
            str(exc),
            {"session_id": session_id, "role_filter": args.role},
        )
        insert_session_log(conn, session_id, "abiesweb_check", center, "center_not_found", str(exc), {"check_id": check_id})
        conn.commit()
        try:
            save_debug(page, SCRIPT_DIR / "debug_gestores", "center_not_found")
        except Exception:
            logging.debug("No se pudo guardar diagnostico", exc_info=True)
        logout_and_log(conn, page, session_id, center, args.timeout_ms)
        return 2
    except Exception:
        logging.exception("Error buscando gestores")
        if not check_id:
            check_id = insert_check(
                conn,
                center,
                "error",
                None,
                len(users),
                search_text,
                selected_unit,
                selected_user,
                "Error general durante el procesamiento",
                {"session_id": session_id, "role_filter": args.role},
            )
            if users:
                insert_users(conn, check_id, center, users)
        if not args.list_only:
            insert_download(
                conn,
                check_id,
                center,
                "error",
                utc_now(),
                None,
                None,
                selected_user,
                None,
                None,
                "Error antes de completar la descarga",
                {"session_id": session_id},
            )
        insert_session_log(conn, session_id, "error", center, "error", "Error buscando gestores")
        conn.commit()
        try:
            save_debug(page, SCRIPT_DIR / "debug_gestores", "error")
        except Exception:
            logging.debug("No se pudo guardar diagnostico", exc_info=True)
        logout_and_log(conn, page, session_id, center, args.timeout_ms)
        return 1
    finally:
        context.close()
        browser.close()
        playwright.stop()
        conn.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Busca gestores Admin. Biblioteca en AbiesWeb para centros del CSV")
    parser.add_argument("--csv", default=None, help="CSV de centros para sincronizar antes de procesar (opcional)")
    parser.add_argument("--db", default=os.getenv("V2_DB_PATH", str(DEFAULT_DB_PATH)), help="SQLite de seguimiento AbiesZipZap")
    parser.add_argument("--index", type=int, default=0, help="Indice 0-based del centro a probar")
    parser.add_argument("--center-code", default="", help="Codigo de centro concreto")
    parser.add_argument("--center-name", default="", help="Texto incluido en Nombre de centro")
    parser.add_argument("--role", default=DEFAULT_ROLE_VALUE, help="Valor del perfil a filtrar")
    parser.add_argument("--timeout-ms", type=int, default=30000, help="Timeout Playwright en milisegundos")
    parser.add_argument("--export-timeout-seconds", type=int, default=2400, help="Timeout esperando la exportacion")
    parser.add_argument("--list-only", action="store_true", help="Solo lista gestores; no impersona ni descarga")
    parser.add_argument("--headful", action="store_true", help="Muestra el navegador aunque HEADLESS=true en .env")
    parser.add_argument("--debug", action="store_true", help="Guarda HTML y captura del resultado en v2/debug_gestores")
    parser.add_argument("--verbose", action="store_true", help="Activa log detallado")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    setup_logging(args.verbose)
    raise SystemExit(run(args))


if __name__ == "__main__":
    main()
