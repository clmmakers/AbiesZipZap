import argparse
import json
import logging
import os
import re
import shlex
import sqlite3
import sys
import tempfile
import unicodedata
import uuid
import zipfile
from difflib import SequenceMatcher
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from xml.etree import ElementTree as ET

from dotenv import load_dotenv
from playwright.sync_api import Browser, Page, Playwright, sync_playwright

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DEFAULT_DB_PATH = SCRIPT_DIR / "data" / "v2.sqlite3"
INVALID_XML_CONTROL_BYTES = set(range(0x00, 0x09)) | {0x0B, 0x0C} | set(range(0x0E, 0x20))
DEFAULT_PROVINCE_ALIAS_TO_CODE = {
    "ceuta": "51",
    "melilla": "52",
    "exteriores": "53",
    "extranjero": "53",
    "exterior": "53",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_bool(value: str, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on", "si", "s"}


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    compact = re.sub(r"\s+", " ", ascii_only).strip().lower()
    return compact


def digits_only(value: str) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


def ensure_parent(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def setup_logging(log_file: str) -> None:
    ensure_parent(log_file)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler()],
    )


def build_province_alias_map(raw_mapping: str) -> Dict[str, str]:
    mapping = dict(DEFAULT_PROVINCE_ALIAS_TO_CODE)
    if not raw_mapping:
        return mapping

    for chunk in re.split(r"[;,]", raw_mapping):
        entry = chunk.strip()
        if not entry or "=" not in entry:
            continue

        left, right = [part.strip() for part in entry.split("=", 1)]
        if not left or not right:
            continue

        if left.isdigit() and not right.isdigit():
            code = left
            aliases_raw = right
        elif right.isdigit() and not left.isdigit():
            code = right
            aliases_raw = left
        else:
            continue

        for alias in re.split(r"[|/]", aliases_raw):
            normalized = normalize_text(alias)
            if normalized:
                mapping[normalized] = code

    return mapping


@dataclass
class LibraryRow:
    center_code: str
    center_name: str
    tipo: str
    titularidad: str
    provincia: str
    localidad: str
    cpostal: str
    direccion: str
    nombrebiblioteca: str
    uri: str
    observaciones: str
    zip_file: str
    download_id: Optional[int] = None


@dataclass
class BatchOptions:
    usar_registro: bool
    syncusers: bool
    importusers: bool


class Config:
    def __init__(self) -> None:
        load_dotenv()
        self.base_url = os.getenv("ABIESPLUS_BASE_URL", "https://sed.abiesplus.es").rstrip("/")
        self.username = os.getenv("ABIESPLUS_USERNAME", "")
        self.password = os.getenv("ABIESPLUS_PASSWORD", "")

        self.login_path = os.getenv("ABIESPLUS_LOGIN_PATH", "/")
        self.bibliotecas_path = os.getenv("ABIESPLUS_BIBLIOTECAS_PATH", "/bibliotecas/index")

        self.headless = parse_bool(os.getenv("HEADLESS"), True)
        self.browser = os.getenv("BROWSER", "chromium")
        self.browser_extra_args = (os.getenv("BROWSER_EXTRA_ARGS") or "").split()

        self.log_file = os.getenv("ABIESPLUS_LOG_FILE", "./logs/abiesplus_import.log")
        self.state_file = os.getenv("ABIESPLUS_STATE_FILE", "./state/abiesplus_state.json")

        self.selector_user = os.getenv("SELECTOR_PLUS_USER", "#usr")
        self.selector_password = os.getenv("SELECTOR_PLUS_PASSWORD", "#pwd")
        self.selector_login_submit = os.getenv("SELECTOR_PLUS_LOGIN_SUBMIT", "#login")
        self.selector_login_anchor = os.getenv("SELECTOR_PLUS_LOGIN_ANCHOR", "a.btn-login")
        self.selector_new_button = os.getenv("SELECTOR_PLUS_NEW_BUTTON", "span.badd:has-text('Nueva')")

        self.ask_batch_options = parse_bool(os.getenv("ABIESPLUS_ASK_BATCH_OPTIONS"), True)
        self.default_usar_registro = parse_bool(os.getenv("ABIESPLUS_DEFAULT_USAR_REGISTRO"), False)
        self.default_syncusers = parse_bool(os.getenv("ABIESPLUS_DEFAULT_SYNCUSERS"), False)
        self.default_importusers = parse_bool(os.getenv("ABIESPLUS_DEFAULT_IMPORTUSERS"), False)
        self.province_alias_to_code = build_province_alias_map(os.getenv("ABIESPLUS_PROVINCE_EQUIVALENCES", ""))


def connect_sqlite(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_bool_setting(conn: sqlite3.Connection, key: str, default: bool) -> bool:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    if not row:
        return default
    return str(row["value"] or "").strip().lower() in {"1", "true", "yes", "y", "on", "si", "s"}


def load_libraries_from_sqlite(
    db_path: str,
    only_codes: str,
    selected_only: bool,
) -> List[LibraryRow]:
    conn = connect_sqlite(db_path)
    try:
        params: list = []
        where = ["d.status = 'downloaded'", "d.file_path IS NOT NULL", "d.file_path != ''"]
        if selected_only:
            where.append("c.selected = 1")
        if only_codes:
            codes = [x.strip() for x in only_codes.split(",") if x.strip()]
            placeholders = ",".join("?" for _ in codes)
            where.append(f"c.center_code IN ({placeholders})")
            params.extend(codes)

        sql = f"""
            SELECT c.center_code, c.center_name, c.center_type, c.ownership,
                   c.province, c.city, c.postal_code, c.postal_address,
                   d.id AS download_id, d.file_path
            FROM centers c
            JOIN downloads d ON d.id = (
                SELECT id FROM downloads
                WHERE center_code = c.center_code AND status = 'downloaded'
                ORDER BY id DESC LIMIT 1
            )
            WHERE {' AND '.join(where)}
            ORDER BY c.priority ASC, c.center_code ASC
        """
        rows = conn.execute(sql, params).fetchall()

        result: List[LibraryRow] = []
        for r in rows:
            result.append(LibraryRow(
                center_code=r["center_code"],
                center_name=r["center_name"] or "",
                tipo=r["center_type"] or "",
                titularidad=(r["ownership"] or "Público").strip() or "Centro público",
                provincia=r["province"] or "",
                localidad=r["city"] or "",
                cpostal=r["postal_code"] or "",
                direccion=r["postal_address"] or "",
                nombrebiblioteca=r["center_name"] or "",
                uri="",
                observaciones="",
                zip_file=r["file_path"] or "",
                download_id=int(r["download_id"]) if r["download_id"] else None,
            ))
        return result
    finally:
        conn.close()


def insert_import_record(
    db_path: str,
    session_id: str,
    center_code: str,
    status: str,
    download_id: int | None,
    zip_file: str,
    xml_entry: str,
    dry_run: bool,
    started_at: str,
    completed_at: str,
    reason: str,
    details: dict,
) -> None:
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """
            INSERT INTO abiesplus_imports (
                center_code, status, download_id, zip_file, xml_entry, dry_run,
                started_at, completed_at, imported_at, reason, details_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                center_code, status, download_id, zip_file, xml_entry, 1 if dry_run else 0,
                started_at, completed_at, completed_at if status == "created" else None,
                reason, json.dumps(details, ensure_ascii=False, sort_keys=True), utc_now(),
            ),
        )
        conn.execute(
            "INSERT INTO session_logs (session_id, action, center_code, status, message, details_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, "abiesplus_import", center_code, status, reason, json.dumps(details, ensure_ascii=False, sort_keys=True), utc_now()),
        )
        conn.commit()
    finally:
        conn.close()


def load_state(path: str) -> Dict:
    p = Path(path)
    if not p.exists():
        ensure_parent(path)
        initial = {"libraries": {}}
        p.write_text(json.dumps(initial, indent=2), encoding="utf-8")
        return initial
    return json.loads(p.read_text(encoding="utf-8"))


def save_state(path: str, state: Dict) -> None:
    ensure_parent(path)
    Path(path).write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def ask_yes_no(question: str, default: bool) -> bool:
    if not os.isatty(0):
        return default
    suffix = "[Y/n]" if default else "[y/N]"
    raw = input(f"{question} {suffix}: ").strip().lower()
    if not raw:
        return default
    return raw in {"y", "yes", "s", "si"}


def read_batch_options(cfg: Config, non_interactive: bool) -> BatchOptions:
    if non_interactive or not cfg.ask_batch_options:
        return BatchOptions(
            usar_registro=cfg.default_usar_registro,
            syncusers=cfg.default_syncusers,
            importusers=cfg.default_importusers,
        )
    return BatchOptions(
        usar_registro=ask_yes_no("Activar 'utilizar campos de registro en ejemplares' para todo el lote?", cfg.default_usar_registro),
        syncusers=ask_yes_no("Activar 'visualizar menus de sincronizacion de usuarios' para todo el lote?", cfg.default_syncusers),
        importusers=ask_yes_no("Activar 'bibliotecarios pueden importar usuarios masivamente' para todo el lote?", cfg.default_importusers),
    )


def province_to_code(value: str, province_alias_to_code: Dict[str, str]) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if raw.isdigit():
        return raw
    return province_alias_to_code.get(normalize_text(raw), raw)


def compact_alnum(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", normalize_text(value))


def addresses_are_compatible(db_address: str, xml_address: str) -> bool:
    db_norm = normalize_text(db_address)
    xml_norm = normalize_text(xml_address)
    if not db_norm or not xml_norm:
        return True

    if db_norm in xml_norm or xml_norm in db_norm:
        return True

    db_compact = compact_alnum(db_address)
    xml_compact = compact_alnum(xml_address)
    if not db_compact or not xml_compact:
        return True

    if db_compact in xml_compact or xml_compact in db_compact:
        return True

    stop_tokens = {
        "calle",
        "c",
        "cl",
        "numero",
        "num",
        "n",
        "no",
        "av",
        "avenida",
        "rue",
        "carretera",
    }
    db_tokens = {t for t in re.findall(r"[a-z0-9]+", db_norm) if t not in stop_tokens and len(t) > 1}
    xml_tokens = {t for t in re.findall(r"[a-z0-9]+", xml_norm) if t not in stop_tokens and len(t) > 1}
    if db_tokens and xml_tokens:
        overlap = len(db_tokens & xml_tokens)
        if overlap >= 2:
            return True

    similarity = SequenceMatcher(None, db_compact, xml_compact).ratio()
    return similarity >= 0.72


def resolve_zip_for_row(row: LibraryRow) -> str:
    if not row.zip_file:
        raise FileNotFoundError(f"Falta zip_file para '{row.center_name}'")
    if not Path(row.zip_file).exists():
        raise FileNotFoundError(
            f"No existe el ZIP indicado para '{row.center_name}': {row.zip_file}"
        )
    return row.zip_file


def sanitize_xml_bytes(xml_bytes: bytes, source: str = "") -> bytes:
    cleaned = bytearray()
    removed = 0
    for b in xml_bytes:
        if b in INVALID_XML_CONTROL_BYTES:
            removed += 1
            continue
        cleaned.append(b)

    if removed > 0:
        origin = f" ({source})" if source else ""
        logging.warning(
            "XML con caracteres de control invalidos%s: se eliminaron %d bytes antes de parsear",
            origin,
            removed,
        )
    return bytes(cleaned)


def extract_center_from_xml_bytes(xml_bytes: bytes) -> Dict[str, str]:
    root = ET.fromstring(xml_bytes)
    center = root.find(".//Centro")
    if center is None:
        raise ValueError("No existe nodo <Centro> en el XML")
    return {
        "Nombre": center.attrib.get("Nombre", ""),
        "Direccion": center.attrib.get("Direccion", ""),
        "Localidad": center.attrib.get("Localidad", ""),
        "CodPostal": center.attrib.get("CodPostal", ""),
        "Provincia": center.attrib.get("Provincia", ""),
    }


def validate_row_vs_xml(row: LibraryRow, xml_center: Dict[str, str], province_alias_to_code: Dict[str, str]) -> None:
    db_norm = normalize_text(row.center_name)
    xml_norm = normalize_text(xml_center["Nombre"])
    if db_norm != xml_norm and xml_norm not in db_norm and db_norm not in xml_norm:
        raise ValueError(
            f"Nombre centro no coincide DB/XML: '{row.center_name}' vs '{xml_center['Nombre']}'"
        )

    db_cp = digits_only(row.cpostal)
    xml_cp = digits_only(xml_center["CodPostal"])
    if db_cp and xml_cp and db_cp != xml_cp:
        logging.warning(
            "[%s] Codigo postal no coincide DB/XML: '%s' vs '%s'. Se continua porque nombre/provincia/direccion validan el centro.",
            row.center_code,
            row.cpostal,
            xml_center["CodPostal"],
        )
    if db_cp and not xml_cp:
        logging.warning(
            "[%s] Codigo postal vacio en XML; se mantiene valor DB (%s)",
            row.center_code,
            row.cpostal,
        )

    db_prov = province_to_code(row.provincia, province_alias_to_code)
    xml_prov = province_to_code(xml_center["Provincia"], province_alias_to_code)
    if db_prov and xml_prov and db_prov != xml_prov:
        raise ValueError(f"Provincia no coincide DB/XML: '{row.provincia}' vs '{xml_center['Provincia']}'")

    db_addr = normalize_text(row.direccion)
    xml_addr = normalize_text(xml_center["Direccion"])
    if db_addr and xml_addr and not addresses_are_compatible(row.direccion, xml_center["Direccion"]):
        logging.warning(
            "[%s] Direccion no coincide DB/XML: '%s' vs '%s'. Se continua porque nombre/provincia validan el centro.",
            row.center_code,
            row.direccion,
            xml_center["Direccion"],
        )


def materialize_xml_from_zip(zip_path: str, temp_dir: str, center_code: str) -> tuple[str, str, Dict[str, str]]:
    with zipfile.ZipFile(zip_path, "r") as zf:
        entries = [name for name in zf.namelist() if name.lower().endswith(".xml")]
        if not entries:
            raise ValueError(f"ZIP sin XML: {zip_path}")
        xml_entry = entries[0]
        xml_bytes = zf.read(xml_entry)

    xml_bytes = sanitize_xml_bytes(xml_bytes, source=f"{zip_path}:{xml_entry}")

    xml_center = extract_center_from_xml_bytes(xml_bytes)
    xml_file = Path(temp_dir) / f"{center_code}.xml"
    xml_file.write_bytes(xml_bytes)
    return str(xml_file), xml_entry, xml_center


def init_browser(cfg: Config) -> tuple[Playwright, Browser]:
    p = sync_playwright().start()
    args = cfg.browser_extra_args
    if cfg.browser == "firefox":
        browser = p.firefox.launch(headless=cfg.headless)
    elif cfg.browser == "webkit":
        browser = p.webkit.launch(headless=cfg.headless)
    else:
        browser = p.chromium.launch(headless=cfg.headless, args=args)
    return p, browser


def close_browser(playwright: Playwright, browser: Browser) -> None:
    browser.close()
    playwright.stop()


def is_login_screen(page: Page, cfg: Config) -> bool:
    return page.locator(cfg.selector_user).count() > 0 and page.locator(cfg.selector_password).count() > 0


def do_login(page: Page, cfg: Config) -> None:
    logging.info("[Abies+] Intentando login con usuario configurado")
    page.fill(cfg.selector_user, cfg.username)
    page.fill(cfg.selector_password, cfg.password)
    login_anchor = page.locator(cfg.selector_login_anchor).first
    try:
        with page.expect_navigation(wait_until="domcontentloaded", timeout=10000):
            if login_anchor.count() > 0 and login_anchor.is_visible():
                login_anchor.click()
            else:
                submit = page.locator(cfg.selector_login_submit).first
                try:
                    submit.click(force=True, timeout=5000)
                except Exception:
                    logging.info("[Abies+] Submit login no visible, se fuerza click por evento DOM")
                    submit.dispatch_event("click")
    except Exception:
        page.wait_for_timeout(1500)
    logging.info("[Abies+] URL tras intento de login: %s", page.url)


def assert_not_unauthorized(page: Page) -> None:
    body = page.locator("body").inner_text(timeout=30000)
    if "No autorizado" in body or "Error: No autorizado" in body:
        raise PermissionError("Abies+ responde 'No autorizado' al acceder a Bibliotecas")


def has_bibliotecas_view(page: Page, cfg: Config) -> bool:
    return page.locator(cfg.selector_new_button).count() > 0


def open_bibliotecas_from_menu(page: Page, cfg: Config) -> None:
    logging.info("[Abies+] Intentando navegar por menu: Administracion -> Bibliotecas")
    page.goto(f"{cfg.base_url}/", wait_until="domcontentloaded")
    admin_item = page.locator("#panelnav li", has_text="Administración").first
    admin_toggle = admin_item.locator("> a").first
    admin_toggle.click()
    page.wait_for_timeout(700)

    expected_href = f"{cfg.base_url.rstrip('/')}/{cfg.bibliotecas_path.lstrip('/')}"
    bib_link = page.locator(f"#panelnav a[href='{expected_href}']")
    if bib_link.count() == 0:
        bib_link = page.locator("#panelnav a[href*='bibliotecas/index']")

    if bib_link.count() == 0:
        raise RuntimeError(
            "No se encuentra enlace de Bibliotecas dentro de Administración "
            f"(href esperado: {expected_href})"
        )

    if bib_link.first.is_visible():
        bib_link.first.click()
    else:
        logging.info("[Abies+] Enlace Bibliotecas oculto, se fuerza click por JS")
        bib_link.first.evaluate("el => el.click()")

    page.wait_for_timeout(2200)
    logging.info(
        "[Abies+] Tras click Bibliotecas: URL=%s nueva=%s nuevo=%s",
        page.url,
        page.locator("span.badd:has-text('Nueva')").count(),
        page.locator("span.badd:has-text('Nuevo')").count(),
    )


def goto_bibliotecas(page: Page, cfg: Config) -> None:
    url = f"{cfg.base_url}{cfg.bibliotecas_path}"
    logging.info("[Abies+] Navegando a Bibliotecas: %s", url)
    page.goto(url, wait_until="domcontentloaded")

    try:
        assert_not_unauthorized(page)
    except PermissionError:
        login_url = f"{cfg.base_url}{cfg.login_path}"
        logging.warning("[Abies+] Respuesta 'No autorizado'. Se intentara login explicito en %s", login_url)
        page.goto(login_url, wait_until="domcontentloaded")
        if not is_login_screen(page, cfg):
            raise
        do_login(page, cfg)
        page.goto(url, wait_until="domcontentloaded")

    if is_login_screen(page, cfg):
        logging.info("No hay sesion en Abies+. Se realiza login")
        do_login(page, cfg)
        page.goto(url, wait_until="domcontentloaded")

    assert_not_unauthorized(page)

    logging.info("[Abies+] URL tras acceso: %s", page.url)

    if not has_bibliotecas_view(page, cfg):
        open_bibliotecas_from_menu(page, cfg)

    if not has_bibliotecas_view(page, cfg):
        raise PermissionError(
            "No se pudo abrir Bibliotecas (ni por URL directa ni por menu). "
            "Revisa permisos de Administracion/Bibliotecas para este usuario. "
            f"URL actual: {page.url}"
        )

    page.wait_for_selector(cfg.selector_new_button, timeout=30000)
    logging.info("[Abies+] Vista de Bibliotecas cargada, boton 'Nueva' detectado")


def select_option_flexible(page: Page, selector: str, desired: str) -> None:
    if not desired:
        return
    page.wait_for_selector(selector, timeout=30000)
    options = page.locator(f"{selector} option")
    total = options.count()
    available = []

    desired_norm = normalize_text(desired)
    for i in range(total):
        value = options.nth(i).get_attribute("value") or ""
        label = options.nth(i).inner_text().strip()
        available.append(label or value)
        if label == desired or value == desired or normalize_text(label) == desired_norm:
            page.select_option(selector, value=value)
            return

    raise ValueError(
        f"No existe la opcion '{desired}' en {selector}. Opciones disponibles: {', '.join(available)}"
    )


def tipo_candidates(tipo: str, center_name: str) -> List[str]:
    candidates: List[str] = []

    def add(value: str) -> None:
        if value and value not in candidates:
            candidates.append(value)

    add(tipo)
    tipo_norm = normalize_text(tipo)
    tipo_key = re.sub(r"[^a-z0-9]", "", tipo_norm)
    tipo_aliases = {
        "ceip": "C.E.I.P.",
        "ies": "I.E.S.",
        "ieso": "I.E.S.O.",
        "cee": "C.E.E.",
        "cepa": "C.E.P.A.",
        "cifp": "C.I.F.P.",
        "cra": "C.R.A.",
        "crie": "C.R.I.E.",
        "cpr": "C.P.R.",
        "eoi": "E.O.I.",
        "eoep": "E.O.E.P.",
        "seccies": "SECC.I.E.S.",
    }
    add(tipo_aliases.get(tipo_key, ""))
    if tipo_norm == "ctex":
        name_norm = normalize_text(center_name)
        if "instituto" in name_norm or "liceo" in name_norm:
            add("I.E.S.")
        elif "colegio" in name_norm:
            add("COL.")
        add("Biblioteca")
    elif tipo_norm in {"cee", "c.e.e."}:
        add("Biblioteca")
    add("Biblioteca")
    return candidates


def titularidad_candidates(titularidad: str) -> List[str]:
    candidates: List[str] = []

    def add(value: str) -> None:
        if value and value not in candidates:
            candidates.append(value)

    add(titularidad)
    titularidad_norm = normalize_text(titularidad)
    if titularidad_norm == "publico":
        add("Centro público")
    elif titularidad_norm == "privado":
        add("Centro privado")
    return candidates


def select_first_option(page: Page, selector: str, candidates: List[str]) -> str:
    last_error = None
    for candidate in candidates:
        try:
            select_option_flexible(page, selector, candidate)
            return candidate
        except Exception as exc:
            last_error = exc
    if last_error:
        raise last_error
    return ""


def set_checkbox(page: Page, selector: str, checked: bool) -> None:
    loc = page.locator(selector)
    if loc.count() == 0:
        return
    current = loc.first.is_checked()
    if current != checked:
        loc.first.click()


def is_library_form_visible(page: Page) -> bool:
    form_field = page.locator("#nombrecentro").first
    return form_field.count() > 0 and form_field.is_visible()


def open_new_dialog(page: Page, cfg: Config) -> None:
    if is_library_form_visible(page):
        return
    overlay = page.locator(".ui-widget-overlay.ui-front").first
    if overlay.count() > 0:
        try:
            overlay.wait_for(state="hidden", timeout=10000)
        except Exception:
            logging.info("[Abies+] Capa modal activa al abrir 'Nueva'; se intenta cerrar con Escape")
            page.keyboard.press("Escape")
            page.wait_for_timeout(700)
    new_button = page.locator(cfg.selector_new_button).first
    try:
        new_button.click(timeout=10000)
    except Exception:
        logging.info("[Abies+] Click normal en 'Nueva' bloqueado; se fuerza click")
        new_button.click(force=True, timeout=5000)
    page.wait_for_selector("#nombrecentro", timeout=30000)


def fill_library_form(
    page: Page,
    row: LibraryRow,
    xml_file: str,
    opts: BatchOptions,
    province_alias_to_code: Dict[str, str],
) -> None:
    selected_tipo = select_first_option(page, "#tipo", tipo_candidates(row.tipo, row.center_name))
    if selected_tipo != row.tipo:
        logging.info("[%s] Tipo '%s' no disponible; se usa '%s'", row.center_code, row.tipo, selected_tipo)

    page.fill("#nombrecentro", row.center_name)
    selected_titularidad = select_first_option(
        page,
        "select[name='titularidad']",
        titularidad_candidates(row.titularidad),
    )
    if selected_titularidad != row.titularidad:
        logging.info(
            "[%s] Titularidad '%s' no disponible; se usa '%s'",
            row.center_code,
            row.titularidad,
            selected_titularidad,
        )
    page.fill("input[name='codigocentro']", row.center_code)

    select_option_flexible(
        page,
        "#provincia",
        province_to_code(row.provincia, province_alias_to_code) or row.provincia,
    )
    page.wait_for_timeout(700)
    select_option_flexible(page, "#municipios", row.localidad)

    page.fill("input[name='cpostal']", row.cpostal)
    page.fill("input[name='direccion']", row.direccion)

    # La seccion "Datos de la biblioteca" puede estar plegada.
    title_biblioteca = page.locator("h3", has_text="Datos de la biblioteca").first
    if title_biblioteca.count() > 0:
        title_biblioteca.click()
        page.wait_for_selector("#nombrebiblioteca", state="visible", timeout=30000)

    page.fill("#nombrebiblioteca", row.nombrebiblioteca)
    page.fill("#uri", row.uri)
    page.fill("textarea[name='observaciones']", row.observaciones)

    set_checkbox(page, "input[name='usar_registro']", opts.usar_registro)
    set_checkbox(page, "input[name='syncusers']", opts.syncusers)
    set_checkbox(page, "input[name='importusers']", opts.importusers)

    select_option_flexible(page, "#origen", "2")
    page.wait_for_selector("div#fxml input[name='xml']", timeout=30000)
    page.set_input_files("div#fxml input[name='xml']", xml_file)


def save_dialog(page: Page, cfg: Config, is_last: bool) -> None:
    if is_last:
        page.click("button:has-text('Guardar')")
        page.wait_for_timeout(1200)
        return
    page.click("button:has-text('Guardar y nuevo')")
    page.wait_for_timeout(1200)
    if is_library_form_visible(page):
        return
    logging.info("[Abies+] 'Guardar y nuevo' volvio al listado; se abre nuevo formulario manualmente")
    try:
        open_new_dialog(page, cfg)
    except Exception as exc:
        logging.warning(
            "[Abies+] Biblioteca guardada, pero no se pudo preparar el siguiente formulario: %s",
            exc,
        )


def process_rows(
    cfg: Config,
    rows: List[LibraryRow],
    opts: BatchOptions,
    dry_run: bool,
    db_path: str,
    session_id: str,
    job_id: int,
) -> None:
    state = load_state(cfg.state_file)
    playwright, browser = init_browser(cfg)
    context = browser.new_context(accept_downloads=False)
    page = context.new_page()

    with tempfile.TemporaryDirectory(prefix="abiesplus_xml_") as tmp_dir:
        try:
            goto_bibliotecas(page, cfg)
            open_new_dialog(page, cfg)

            for i, row in enumerate(rows):
                logging.info("[%s] Preparando biblioteca '%s'", row.center_code, row.center_name)
                record = state.setdefault("libraries", {}).setdefault(row.center_code, {})
                record["center_name"] = row.center_name
                record["last_update"] = utc_now()
                record["status"] = "processing"
                save_state(cfg.state_file, state)

                started_at = utc_now()
                download_id = row.download_id
                xml_entry_val = ""
                zip_path_val = ""
                update_abiesplus_job_center(db_path, job_id, row.center_code, "running", download_id, "Procesando", started_at, started_at)
                try:
                    if not is_library_form_visible(page):
                        open_new_dialog(page, cfg)
                    zip_path = resolve_zip_for_row(row)
                    zip_path_val = zip_path
                    xml_file, xml_entry, xml_center = materialize_xml_from_zip(zip_path, tmp_dir, row.center_code)
                    xml_entry_val = xml_entry
                    validate_row_vs_xml(row, xml_center, cfg.province_alias_to_code)

                    if dry_run:
                        fill_library_form(page, row, xml_file, opts, cfg.province_alias_to_code)
                        logging.info("[%s] DRY-RUN OK. UI+DB/XML validado. ZIP=%s", row.center_code, zip_path)
                        if i < len(rows) - 1:
                            page.click("button:has-text('Cancelar')")
                            page.wait_for_timeout(600)
                            open_new_dialog(page, cfg)
                    else:
                        fill_library_form(page, row, xml_file, opts, cfg.province_alias_to_code)
                        save_dialog(page, cfg, is_last=(i == len(rows) - 1))
                        logging.info("[%s] Biblioteca creada", row.center_code)

                    status = "dry_run_ok" if dry_run else "created"
                    record["status"] = status
                    record["zip_file"] = zip_path
                    record["xml_entry"] = xml_entry
                    record["message"] = "ok"
                    record["last_update"] = utc_now()
                    finished_at = utc_now()
                    insert_import_record(
                        db_path, session_id, row.center_code, status, download_id, zip_path_val,
                        xml_entry_val, dry_run, started_at, finished_at, "ok",
                        {"center_name": row.center_name, "zip_file": zip_path_val},
                    )
                    update_abiesplus_job_center(db_path, job_id, row.center_code, status, download_id, "ok", started_at, finished_at)
                except Exception as exc:
                    record["status"] = "error"
                    record["message"] = str(exc)
                    record["last_update"] = utc_now()
                    logging.exception("[%s] Error procesando biblioteca", row.center_code)
                    finished_at = utc_now()
                    insert_import_record(
                        db_path, session_id, row.center_code, "error", download_id, zip_path_val or row.zip_file,
                        xml_entry_val, dry_run, started_at, finished_at, str(exc),
                        {"center_name": row.center_name, "zip_file": row.zip_file, "error": str(exc)},
                    )
                    update_abiesplus_job_center(db_path, job_id, row.center_code, "error", download_id, str(exc), started_at, finished_at)
                    if not dry_run:
                        try:
                            page.click("button:has-text('Cancelar')")
                            page.wait_for_timeout(800)
                            open_new_dialog(page, cfg)
                        except Exception:
                            goto_bibliotecas(page, cfg)
                            open_new_dialog(page, cfg)
                finally:
                    save_state(cfg.state_file, state)
        finally:
            context.close()
            close_browser(playwright, browser)


def _json_dumps(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def create_abiesplus_job(db_path: str, rows: List[LibraryRow], dry_run: bool, command: str) -> tuple[int, str]:
    session_id = f"abiesplus-{uuid.uuid4()}"
    conn = connect_sqlite(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        active = conn.execute(
            "SELECT id, status FROM jobs WHERE status IN ('queued', 'running') ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if active:
            raise RuntimeError(f"Ya existe un job activo: #{active['id']} ({active['status']})")
        cursor = conn.execute(
            """
            INSERT INTO jobs (
                status, source, mode, total_centers, retries, force_remote_check, dry_run,
                command, started_at, details_json, updated_at
            ) VALUES (?, 'cli', 'abiesplus', ?, 0, 0, ?, ?, ?, ?, ?)
            """,
            (
                "running", len(rows), 1 if dry_run else 0, command, utc_now(),
                _json_dumps({"center_codes": [r.center_code for r in rows], "dry_run": dry_run}),
                utc_now(),
            ),
        )
        job_id = int(cursor.lastrowid)
        conn.executemany(
            """
            INSERT INTO job_centers (job_id, center_code, status, details_json, updated_at)
            VALUES (?, ?, 'queued', ?, ?)
            """,
            [(job_id, r.center_code, _json_dumps({"center_name": r.center_name}), utc_now()) for r in rows],
        )
        conn.execute(
            "INSERT INTO session_logs (session_id, action, center_code, status, message, details_json, created_at) VALUES (?, ?, NULL, 'running', ?, ?, ?)",
            (session_id, "abiesplus_start", f"Job #{job_id} Abies+ iniciado", _json_dumps({"job_id": job_id}), utc_now()),
        )
        conn.commit()
        return job_id, session_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_abiesplus_job_center(
    db_path: str,
    job_id: int,
    center_code: str,
    status: str,
    download_id: int | None,
    reason: str,
    started_at: str,
    finished_at: str,
) -> None:
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """
            UPDATE job_centers
            SET status = ?, download_id = ?, started_at = COALESCE(started_at, ?),
                finished_at = ?, reason = ?, updated_at = ?
            WHERE job_id = ? AND center_code = ?
            """,
            (status, download_id, started_at, finished_at, reason, utc_now(), job_id, center_code),
        )
        conn.commit()
    finally:
        conn.close()


def finish_abiesplus_job(db_path: str, session_id: str, job_id: int) -> str:
    conn = connect_sqlite(db_path)
    try:
        counts = {r["status"]: int(r["count"]) for r in conn.execute(
            "SELECT status, COUNT(*) AS count FROM job_centers WHERE job_id = ? GROUP BY status", (job_id,)
        ).fetchall()}
        processed = sum(c for s, c in counts.items() if s not in {"queued", "running"})
        errors = counts.get("error", 0)
        unfinished = counts.get("queued", 0) + counts.get("running", 0)
        if unfinished:
            final_status = "failed"
        elif errors:
            final_status = "completed_with_errors"
        else:
            final_status = "completed"
        conn.execute(
            """
            UPDATE jobs
            SET status = ?, processed_centers = ?, error_count = ?,
                finished_at = ?, reason = ?, updated_at = ?
            WHERE id = ?
            """,
            (final_status, processed, errors, utc_now(), f"Job #{job_id} finalizado", utc_now(), job_id),
        )
        conn.execute(
            "INSERT INTO session_logs (session_id, action, center_code, status, message, details_json, created_at) VALUES (?, ?, NULL, ?, ?, ?, ?)",
            (session_id, "abiesplus_finish", final_status, f"Job #{job_id} finalizado", _json_dumps({"job_id": job_id}), utc_now()),
        )
        conn.commit()
        return final_status
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Alta de bibliotecas en Abies+ desde SQLite AbiesZipZap")
    parser.add_argument("--db", default=os.getenv("V2_DB_PATH", str(DEFAULT_DB_PATH)), help="SQLite de seguimiento AbiesZipZap")
    parser.add_argument("--selected-only", action="store_true", help="Procesa centros seleccionados con descarga downloaded")
    parser.add_argument("--only-codes", default="", help="Codigos concretos separados por coma")
    parser.add_argument("--dry-run", action="store_true", help="Valida ZIP/XML sin crear bibliotecas")
    parser.add_argument("--non-interactive", action="store_true", help="No preguntar opciones de checkboxes")
    args = parser.parse_args()

    cfg = Config()
    setup_logging(cfg.log_file)
    db_path = str(Path(args.db).resolve())
    logging.info("DB AbiesZipZap: %s", db_path)
    logging.info(
        "Mapeo provincias activo (ABIESPLUS_PROVINCE_EQUIVALENCES): %s",
        cfg.province_alias_to_code,
    )

    conn = connect_sqlite(db_path)
    try:
        cfg.default_usar_registro = get_bool_setting(conn, "abiesplus_default_usar_registro", cfg.default_usar_registro)
        cfg.default_syncusers = get_bool_setting(conn, "abiesplus_default_syncusers", cfg.default_syncusers)
        cfg.default_importusers = get_bool_setting(conn, "abiesplus_default_importusers", cfg.default_importusers)
    finally:
        conn.close()

    rows = load_libraries_from_sqlite(db_path, args.only_codes, args.selected_only)
    if not rows:
        logging.error("No hay centros con descarga downloaded para procesar")
        return

    if not cfg.username or not cfg.password:
        logging.error("Faltan ABIESPLUS_USERNAME/ABIESPLUS_PASSWORD en .env para ejecutar (incluye dry-run)")
        return

    opts = read_batch_options(cfg, non_interactive=args.non_interactive)
    logging.info(
        "Opciones lote: utilizar campos de registro en ejemplares=%s visualizar menus de sincronizacion de usuarios=%s bibliotecarios pueden importar usuarios masivamente=%s",
        opts.usar_registro,
        opts.syncusers,
        opts.importusers,
    )

    command = " ".join(shlex.quote(p) for p in sys.argv)
    job_id, session_id = create_abiesplus_job(db_path, rows, dry_run=args.dry_run, command=command)
    logging.info("Job Abies+ #%d iniciado (session=%s) con %d centros.", job_id, session_id, len(rows))
    try:
        process_rows(cfg, rows, opts, dry_run=args.dry_run, db_path=db_path, session_id=session_id, job_id=job_id)
        final_status = finish_abiesplus_job(db_path, session_id, job_id)
        logging.info("Job Abies+ #%d finalizado: %s", job_id, final_status)
    except Exception:
        finish_abiesplus_job(db_path, session_id, job_id)
        raise


if __name__ == "__main__":
    main()
