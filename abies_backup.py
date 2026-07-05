import argparse
import csv
import json
import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from dotenv import load_dotenv
from playwright.sync_api import Browser, BrowserContext, Download, Page, Playwright, sync_playwright


@dataclass
class Center:
    center_id: str
    center_name: str
    role: str
    profile_href: str = ""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return cleaned.strip("_") or "center"


def center_folder_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    compact = re.sub(r"[^A-Za-z0-9]+", "", ascii_only).lower()
    return compact or "centro"


def parse_bool(value: str, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def setup_logging(log_file: str) -> None:
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler()],
    )


class Config:
    def __init__(self) -> None:
        load_dotenv()
        self.base_url = os.environ["ABIES_BASE_URL"].rstrip("/")
        self.username = os.environ["ABIES_USERNAME"]
        self.password = os.environ["ABIES_PASSWORD"]

        self.login_path = os.getenv("ABIES_LOGIN_PATH", "/")
        self.protected_path = os.getenv("ABIES_PROTECTED_PATH", "/datosCentro/edit/action")
        self.tools_path = os.getenv("ABIES_TOOLS_PATH", "/importacion?eMenuActivo=1&eSubmenuActivo=0")
        self.export_path = os.getenv("ABIES_EXPORT_PATH", "/")

        self.prelogin_center_text = os.getenv("PRELOGIN_CENTER_TEXT", "")
        self.selector_prelogin_center_input = os.getenv("SELECTOR_PRELOGIN_CENTER_INPUT", "")
        self.selector_prelogin_submit = os.getenv("SELECTOR_PRELOGIN_SUBMIT", "")

        self.headless = parse_bool(os.getenv("HEADLESS"), True)
        self.browser_name = os.getenv("BROWSER", "chromium")
        self.browser_extra_args = (os.getenv("BROWSER_EXTRA_ARGS") or "").split()

        self.max_tabs = int(os.getenv("MAX_TABS", "3"))
        self.max_centers_per_batch = int(os.getenv("MAX_CENTERS_PER_BATCH", "20"))
        self.poll_interval_seconds = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))
        self.max_retries_per_center = int(os.getenv("MAX_RETRIES_PER_CENTER", "2"))

        self.selector_user = os.environ["SELECTOR_USER"]
        self.selector_password = os.environ["SELECTOR_PASSWORD"]
        self.selector_login_button = os.environ["SELECTOR_LOGIN_BUTTON"]
        self.selector_post_login_ready = os.getenv("SELECTOR_POST_LOGIN_READY", "body")

        self.selector_center_input = os.environ["SELECTOR_CENTER_INPUT"]
        self.selector_role_select = os.environ["SELECTOR_ROLE_SELECT"]
        self.selector_switch_button = os.environ["SELECTOR_SWITCH_BUTTON"]
        self.selector_export_trigger = os.environ["SELECTOR_EXPORT_TRIGGER"]
        self.selector_status_text = os.environ["SELECTOR_STATUS_TEXT"]
        self.selector_download_link = os.environ["SELECTOR_DOWNLOAD_LINK"]

        self.use_profile_switch = parse_bool(os.getenv("USE_PROFILE_SWITCH"), False)
        self.selector_change_profile_link = os.getenv("SELECTOR_CHANGE_PROFILE_LINK", "text=cambiar perfil")
        self.selector_profile_rows = os.getenv("SELECTOR_PROFILE_ROWS", "tr")
        self.profile_admin_label = os.getenv("PROFILE_ADMIN_LABEL", "Administrador AbiesWeb")

        self.status_pending = os.getenv("STATUS_PENDING", "pending").lower()
        self.status_in_progress = os.getenv("STATUS_IN_PROGRESS", "in_progress").lower()
        self.status_finished = os.getenv("STATUS_FINISHED", "finished").lower()
        self.status_error = os.getenv("STATUS_ERROR", "error").lower()

        self.state_file = os.getenv("STATE_FILE", "./state/backups_state.json")
        self.log_file = os.getenv("LOG_FILE", "./logs/backup_abies.log")
        self.output_dir = os.getenv("OUTPUT_DIR", "./backups")
        self.mapping_csv = os.getenv("MAPPING_CSV", "./backups/mapeo_descargas.csv")
        self.auth_state_file = os.getenv("AUTH_STATE_FILE", "./state/auth_state.json")
        self.reuse_auth_state = parse_bool(os.getenv("REUSE_AUTH_STATE"), True)


def load_centers(csv_path: str) -> List[Center]:
    centers: List[Center] = []
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            center_id = (row.get("center_id") or "").strip()
            center_name = (row.get("center_name") or center_id).strip()
            role = (row.get("role") or "").strip()
            if not center_id:
                continue
            centers.append(Center(center_id=center_id, center_name=center_name, role=role))
    return centers


def ensure_file_path(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def load_state(path: str) -> Dict:
    file_path = Path(path)
    if not file_path.exists():
        ensure_file_path(path)
        initial = {"centers": {}}
        file_path.write_text(json.dumps(initial, indent=2), encoding="utf-8")
        return initial
    return json.loads(file_path.read_text(encoding="utf-8"))


def save_state(path: str, state: Dict) -> None:
    ensure_file_path(path)
    Path(path).write_text(json.dumps(state, indent=2, ensure_ascii=True), encoding="utf-8")


def init_browser(cfg: Config) -> tuple[Playwright, Browser]:
    p = sync_playwright().start()
    args = cfg.browser_extra_args
    if cfg.browser_name == "firefox":
        return p, p.firefox.launch(headless=cfg.headless)
    if cfg.browser_name == "webkit":
        return p, p.webkit.launch(headless=cfg.headless)
    return p, p.chromium.launch(headless=cfg.headless, args=args)


def new_context(browser: Browser, cfg: Config, accept_downloads: bool) -> BrowserContext:
    kwargs = {"accept_downloads": accept_downloads}
    if cfg.reuse_auth_state and Path(cfg.auth_state_file).exists():
        kwargs["storage_state"] = cfg.auth_state_file
        logging.info("Reutilizando storage_state: %s", cfg.auth_state_file)
    return browser.new_context(**kwargs)


def save_auth_state(context: BrowserContext, cfg: Config) -> None:
    if not cfg.reuse_auth_state:
        return
    ensure_file_path(cfg.auth_state_file)
    context.storage_state(path=cfg.auth_state_file)
    logging.info("storage_state actualizado: %s", cfg.auth_state_file)


def login(page: Page, cfg: Config) -> None:
    protected_url = f"{cfg.base_url}{cfg.protected_path}"
    login_url = f"{cfg.base_url}{cfg.login_path}"

    page.goto(protected_url, wait_until="domcontentloaded")
    if page.locator(cfg.selector_post_login_ready).first.count() > 0 and "/signin" not in page.url:
        logging.info("Sesion ya autenticada. Se reutiliza sesion y no se abre /signin")
        return

    logging.info("Abriendo login: %s", login_url)
    page.goto(login_url, wait_until="domcontentloaded")

    if "/signin" not in page.url and not page.url.startswith(login_url):
        logging.info("Sesion ya autenticada (redirigido desde login). URL: %s", page.url)
        return

    has_prelogin = bool(cfg.prelogin_center_text and cfg.selector_prelogin_center_input and cfg.selector_prelogin_submit)
    if has_prelogin:
        try:
            page.locator(cfg.selector_prelogin_center_input).wait_for(state="visible", timeout=8000)
            page.fill(cfg.selector_prelogin_center_input, cfg.prelogin_center_text)
            page.click(cfg.selector_prelogin_submit)
            page.wait_for_selector(cfg.selector_user, timeout=30000)
        except Exception:
            logging.info("Prelogin no disponible, intentando login directo")

    try:
        page.wait_for_selector(cfg.selector_user, timeout=10000)
    except Exception:
        if page.locator(cfg.selector_post_login_ready).first.count() > 0 and "/signin" not in page.url:
            logging.info("Sin formulario de login, sesion ya activa. URL: %s", page.url)
            return
        raise

    page.fill(cfg.selector_user, cfg.username)
    page.fill(cfg.selector_password, cfg.password)
    page.click(cfg.selector_login_button)
    page.wait_for_selector(cfg.selector_post_login_ready, timeout=30000)
    logging.info("Login completado. URL actual: %s", page.url)


def open_export_zone(page: Page, cfg: Config) -> None:
    if cfg.use_profile_switch:
        page.goto(f"{cfg.base_url}{cfg.tools_path}", wait_until="domcontentloaded")
        export_link = page.locator("a", has_text="Exportación")
        if export_link.count() > 0:
            export_link.first.click()
            page.wait_for_load_state("domcontentloaded")
        else:
            page.goto(f"{cfg.base_url}{cfg.export_path}", wait_until="domcontentloaded")
        page.wait_for_selector("#salidaImport", timeout=30000)
        return

    page.goto(f"{cfg.base_url}{cfg.export_path}", wait_until="domcontentloaded")
    if not cfg.use_profile_switch:
        page.wait_for_selector(cfg.selector_center_input, timeout=30000)


def select_center_and_role(page: Page, cfg: Config, center: Center) -> None:
    if cfg.use_profile_switch:
        return
    page.fill(cfg.selector_center_input, center.center_id)
    if center.role:
        page.select_option(cfg.selector_role_select, label=center.role)
    page.click(cfg.selector_switch_button)
    page.wait_for_timeout(1200)


def trigger_export(page: Page, cfg: Config) -> None:
    page.click(cfg.selector_export_trigger)
    page.wait_for_timeout(800)


def normalize_status(raw: str, cfg: Config) -> str:
    value = (raw or "").strip().lower()
    if cfg.status_error in value:
        return cfg.status_error
    if cfg.status_finished in value:
        return cfg.status_finished
    if cfg.status_in_progress in value:
        return cfg.status_in_progress
    if cfg.status_pending in value:
        return cfg.status_pending
    return value or "unknown"


def read_status(page: Page, cfg: Config) -> str:
    if cfg.selector_status_text == "AUTO":
        body_text = page.locator("body").inner_text(timeout=30000)
        return normalize_status(body_text, cfg)
    text = page.locator(cfg.selector_status_text).first.inner_text(timeout=30000)
    return normalize_status(text, cfg)


def read_export_date(page: Page) -> str:
    text = page.locator("#FormExportacion").inner_text(timeout=30000)
    match = re.search(r"generado\s+el\s+d[ií]a:\s*(\d{2}/\d{2}/\d{4})", text, re.IGNORECASE)
    return match.group(1) if match else ""


def parse_progress_percent(text: str) -> int:
    match = re.search(r"(\d{1,3})\s*%", text or "")
    if not match:
        return -1
    value = int(match.group(1))
    if value < 0:
        return -1
    if value > 100:
        return 100
    return value


def read_progress_percent(page: Page) -> int:
    if page.locator("#resultadoEvolucion").count() == 0:
        return -1
    pct_text = page.locator("#resultadoEvolucion").inner_text(timeout=30000).strip()
    return parse_progress_percent(pct_text)


def wait_for_download_ready_in_active_tab(page: Page, center_id: str, timeout_seconds: int = 900) -> bool:
    start = time.time()
    last_pct_value = -1
    last_has_link = None
    last_heartbeat_slot = -1
    while True:
        has_link = page.locator("#enlaceZip").count() > 0
        pct = ""
        if page.locator("#resultadoEvolucion").count() > 0:
            pct = page.locator("#resultadoEvolucion").inner_text(timeout=30000).strip()

        pct_value = parse_progress_percent(pct)
        elapsed = int(time.time() - start)
        changed = pct_value != last_pct_value or has_link != last_has_link
        if changed:
            logging.info("[%s] Progreso exportacion: %s (link=%s, t=%ss)", center_id, pct or "?", has_link, elapsed)
            last_pct_value = pct_value
            last_has_link = has_link

        heartbeat_slot = elapsed // 30
        if heartbeat_slot != last_heartbeat_slot:
            logging.info("[%s] HEARTBEAT 30s: progreso=%s (link=%s, t=%ss)", center_id, pct or "?", has_link, elapsed)
            last_heartbeat_slot = heartbeat_slot

        if has_link and pct_value >= 100:
            logging.info("[%s] Exportacion lista para descargar (100%% + enlace ZIP)", center_id)
            return True

        if time.time() - start > timeout_seconds:
            logging.error("[%s] Timeout esperando exportacion lista (%ss)", center_id, timeout_seconds)
            return False

        page.wait_for_timeout(10_000)


def launch_and_wait_same_tab(page: Page, cfg: Config, center: Center, state: Dict, timeout_seconds: int) -> None:
    record = center_state(state, center)
    target_date = datetime.now().strftime("%d/%m/%Y")

    if cfg.use_profile_switch:
        switch_profile(page, cfg, center)
    open_export_zone(page, cfg)

    logging.info("[%s] Comprobacion inicial de fecha de exportacion (una sola vez)", center.center_id)
    current_date = read_export_date(page)
    has_link_now = page.locator("#enlaceZip").count() > 0
    current_pct = read_progress_percent(page)
    if current_date:
        logging.info(
            "[%s] Fecha export actual=%s (hoy=%s), coincide=%s, progreso=%s%%, enlace=%s",
            center.center_id,
            current_date,
            target_date,
            current_date == target_date,
            current_pct if current_pct >= 0 else "?",
            has_link_now,
        )
    else:
        logging.warning(
            "[%s] No se pudo leer la fecha de exportacion. Estado inicial progreso=%s%% enlace=%s",
            center.center_id,
            current_pct if current_pct >= 0 else "?",
            has_link_now,
        )

    should_download_direct = current_date == target_date and has_link_now
    already_running = current_pct >= 0 and current_pct < 100 and not has_link_now

    if should_download_direct:
        logging.info("[%s] Exportacion de hoy ya disponible, se descarga sin relanzar", center.center_id)
    elif already_running:
        logging.info("[%s] Exportacion ya en curso. No se relanza, se espera a 100%%", center.center_id)
        logging.info("[%s] Entrando en espera activa del progreso", center.center_id)
        ok = wait_for_download_ready_in_active_tab(page, center_id=center.center_id, timeout_seconds=timeout_seconds)
        if not ok:
            raise RuntimeError("Timeout esperando a que la exportacion quede lista")
    else:
        logging.info("[%s] Lanzando 'Exportar otra vez' y esperando en pestana activa", center.center_id)
        trigger_export(page, cfg)
        logging.info("[%s] Entrando en espera activa del progreso", center.center_id)
        ok = wait_for_download_ready_in_active_tab(page, center_id=center.center_id, timeout_seconds=timeout_seconds)
        if not ok:
            raise RuntimeError("Timeout esperando a que la exportacion quede lista")

    with page.expect_download(timeout=120000) as dl_info:
        page.click(cfg.selector_download_link)
    download = dl_info.value
    downloaded_file = save_download(download, cfg.output_dir, center)
    record["launch_status"] = "triggered"
    record["download_status"] = "downloaded"
    record["downloaded_file"] = downloaded_file
    record["last_status_text"] = "downloaded_same_tab"
    record["last_update"] = utc_now()
    append_mapping(
        cfg.mapping_csv,
        {
            "center_id": center.center_id,
            "center_name": center.center_name,
            "role": center.role,
            "zip_file": downloaded_file,
            "downloaded_at": utc_now(),
            "status": "downloaded_same_tab",
        },
    )
    logging.info("[%s] Descarga completada en mismo flujo: %s", center.center_id, downloaded_file)


def discover_centers_from_profiles(page: Page, cfg: Config) -> List[Center]:
    page.goto(f"{cfg.base_url}/mostrarperfiles", wait_until="domcontentloaded")
    page.wait_for_selector(cfg.selector_profile_rows, timeout=30000)

    discovered: List[Center] = []
    rows = page.locator(cfg.selector_profile_rows)
    total = rows.count()
    for i in range(total):
        row = rows.nth(i)
        link = row.locator("a", has_text=cfg.profile_admin_label)
        if link.count() == 0:
            continue
        href = link.first.get_attribute("href") or ""
        if not href.startswith("/cambioDePerfil/"):
            continue

        cells = row.locator("td")
        center_name = ""
        if cells.count() > 0:
            center_name = cells.first.inner_text().strip()
        if not center_name:
            center_name = row.inner_text().split("\n", 1)[0].strip()

        match = re.search(r"/cambioDePerfil/\d+/(\d+)", href)
        center_id = match.group(1) if match else slugify(center_name)
        discovered.append(Center(center_id=center_id, center_name=center_name, role=cfg.profile_admin_label, profile_href=href))

    return discovered


def switch_profile(page: Page, cfg: Config, center: Center) -> None:
    if not center.profile_href:
        raise RuntimeError(f"Centro sin profile_href: {center.center_id}")
    logging.info("[%s] Cambiando perfil a: %s", center.center_id, center.center_name)
    page.goto(f"{cfg.base_url}/mostrarperfiles", wait_until="domcontentloaded")
    page.goto(f"{cfg.base_url}{center.profile_href}", wait_until="domcontentloaded")
    page.wait_for_timeout(800)


def append_mapping(path: str, row: Dict[str, str]) -> None:
    ensure_file_path(path)
    file_exists = Path(path).exists()
    fields = ["center_id", "center_name", "role", "zip_file", "downloaded_at", "status"]
    with open(path, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def downloaded_ids_from_mapping(path: str) -> set[str]:
    csv_path = Path(path)
    if not csv_path.exists():
        return set()

    downloaded = set()
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            center_id = (row.get("center_id") or "").strip()
            zip_file = (row.get("zip_file") or "").strip()
            status = (row.get("status") or "").strip().lower()
            if center_id and (zip_file or status.startswith("downloaded")):
                downloaded.add(center_id)
    return downloaded


def is_already_downloaded(state: Dict, center: Center, downloaded_ids: set[str]) -> bool:
    record = state.get("centers", {}).get(center.center_id, {})
    return record.get("download_status") == "downloaded" or center.center_id in downloaded_ids


def save_download(download: Download, output_dir: str, center: Center) -> str:
    center_dir = Path(output_dir) / center_folder_slug(center.center_name)
    center_dir.mkdir(parents=True, exist_ok=True)
    suggested_name = download.suggested_filename
    destination = center_dir / suggested_name
    download.save_as(str(destination))
    return str(destination)


def center_state(state: Dict, center: Center) -> Dict:
    record = state["centers"].get(center.center_id)
    if record is None:
        record = {
            "center_name": center.center_name,
            "role": center.role,
            "launch_status": "not_started",
            "download_status": "not_downloaded",
            "tries": 0,
            "last_status_text": "",
            "last_update": utc_now(),
            "initial_date_checked": False,
            "initial_export_date": "",
        }
        state["centers"][center.center_id] = record
    return record


def check_initial_date_once(page: Page, center: Center, record: Dict) -> None:
    if record.get("initial_date_checked"):
        return
    try:
        export_date = read_export_date(page)
    except Exception:
        export_date = ""
    record["initial_date_checked"] = True
    record["initial_export_date"] = export_date
    logging.info("[%s] Comprobacion inicial de fecha (una vez): %s", center.center_id, export_date or "no detectable")


def launch_for_center(page: Page, cfg: Config, state: Dict, center: Center) -> None:
    record = center_state(state, center)
    if record["launch_status"] == "triggered":
        logging.info("[%s] ya lanzado, se omite", center.center_id)
        return
    if record["tries"] >= cfg.max_retries_per_center:
        logging.warning("[%s] maximo de reintentos alcanzado", center.center_id)
        return

    try:
        if cfg.use_profile_switch:
            switch_profile(page, cfg, center)
        open_export_zone(page, cfg)
        check_initial_date_once(page, center, record)
        select_center_and_role(page, cfg, center)
        trigger_export(page, cfg)
        status = read_status(page, cfg)
        record["launch_status"] = "triggered"
        record["last_status_text"] = status
        record["last_update"] = utc_now()
        logging.info("[%s] backup lanzado, estado inicial=%s", center.center_id, status)
    except Exception as exc:
        record["tries"] += 1
        record["launch_status"] = "launch_error"
        record["last_status_text"] = str(exc)
        record["last_update"] = utc_now()
        logging.exception("[%s] error al lanzar backup", center.center_id)


def poll_and_download_for_center(page: Page, cfg: Config, state: Dict, center: Center) -> None:
    record = center_state(state, center)
    if record["download_status"] == "downloaded":
        logging.info("[%s] ya descargado, se omite", center.center_id)
        return

    try:
        if cfg.use_profile_switch:
            switch_profile(page, cfg, center)
        open_export_zone(page, cfg)
        check_initial_date_once(page, center, record)
        select_center_and_role(page, cfg, center)
        status = read_status(page, cfg)
        record["last_status_text"] = status
        record["last_update"] = utc_now()

        has_download_link = page.locator(cfg.selector_download_link).count() > 0
        if has_download_link:
            status = cfg.status_finished
            logging.info("[%s] descarga disponible -> iniciando descarga", center.center_id)

        if status in {cfg.status_pending, cfg.status_in_progress}:
            logging.info("[%s] aun no disponible, estado=%s", center.center_id, status)
            return

        if status == cfg.status_error:
            record["download_status"] = "error"
            record["tries"] += 1
            logging.error("[%s] estado de error en exportacion", center.center_id)
            return

        if status == cfg.status_finished:
            with page.expect_download(timeout=60000) as dl_info:
                page.click(cfg.selector_download_link)
            download = dl_info.value
            downloaded_file = save_download(download, cfg.output_dir, center)
            record["download_status"] = "downloaded"
            record["downloaded_file"] = downloaded_file
            record["last_update"] = utc_now()
            append_mapping(
                cfg.mapping_csv,
                {
                    "center_id": center.center_id,
                    "center_name": center.center_name,
                    "role": center.role,
                    "zip_file": downloaded_file,
                    "downloaded_at": utc_now(),
                    "status": status,
                },
            )
            logging.info("[%s] ZIP descargado: %s", center.center_id, downloaded_file)
            return

        logging.warning("[%s] estado no reconocido: %s", center.center_id, status)
    except Exception:
        record["tries"] += 1
        record["download_status"] = "download_error"
        record["last_update"] = utc_now()
        logging.exception("[%s] error comprobando/descargando backup", center.center_id)


def chunked(items: List[Center], chunk_size: int) -> List[List[Center]]:
    return [items[i : i + chunk_size] for i in range(0, len(items), chunk_size)]


def build_subset(centers: List[Center], only_csv_ids: str, only_names: str) -> List[Center]:
    filtered = centers

    if only_csv_ids:
        selected_ids = {x.strip() for x in only_csv_ids.split(",") if x.strip()}
        filtered = [c for c in filtered if c.center_id in selected_ids]

    if only_names:
        tokens = [x.strip().lower() for x in only_names.split(",") if x.strip()]
        filtered = [
            c
            for c in filtered
            if any(token in c.center_name.lower() for token in tokens)
        ]

    return filtered


def load_filter_csv(csv_path: str) -> Dict[str, set]:
    selected_ids = set()
    selected_names = set()
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            enabled = (row.get("enabled") or "1").strip().lower()
            if enabled not in {"1", "true", "yes", "si", "sí", "y"}:
                continue
            center_id = (row.get("center_id") or "").strip()
            center_name = (row.get("center_name") or "").strip()
            if center_id:
                selected_ids.add(center_id)
            if center_name:
                selected_names.add(center_name.lower())
    return {"ids": selected_ids, "names": selected_names}


def apply_filter_csv(centers: List[Center], csv_path: str) -> List[Center]:
    selected = load_filter_csv(csv_path)
    if not selected["ids"] and not selected["names"]:
        return []
    return [
        c
        for c in centers
        if c.center_id in selected["ids"] or c.center_name.lower() in selected["names"]
    ]


def dedupe_centers(centers: List[Center]) -> List[Center]:
    seen = set()
    unique: List[Center] = []
    for center in centers:
        key = center.center_id
        if key in seen:
            continue
        seen.add(key)
        unique.append(center)
    return unique


def run_download_loop(cfg: Config, centers: List[Center], state: Dict, max_polls: int) -> None:
    polls = 0
    logging.info("Iniciando bucle de descarga continuo (misma sesion de navegador)")
    playwright, browser = init_browser(cfg)
    context = new_context(browser, cfg, accept_downloads=True)
    pages = [context.new_page() for _ in range(cfg.max_tabs)]

    try:
        login(pages[0], cfg)
        save_auth_state(context, cfg)

        while True:
            polls += 1
            logging.info("Ciclo de descarga #%s", polls)
            batches = chunked(centers, cfg.max_centers_per_batch)
            for batch_index, batch in enumerate(batches, start=1):
                logging.info("Procesando tanda %s/%s (%s centros)", batch_index, len(batches), len(batch))
                for i, center in enumerate(batch):
                    page = pages[i % len(pages)]
                    poll_and_download_for_center(page, cfg, state, center)
                    save_state(cfg.state_file, state)

            if all_done(state):
                logging.info("Todos los centros descargados")
                break
            if max_polls and polls >= max_polls:
                logging.warning("Se alcanzo max-polls=%s", max_polls)
                break

            logging.info("Esperando %s segundos para siguiente comprobacion", cfg.poll_interval_seconds)
            time.sleep(cfg.poll_interval_seconds)
    finally:
        context.close()
        browser.close()
        playwright.stop()
        logging.info("Navegador cerrado tras bucle continuo")


def run_direct_queue_same_tab(cfg: Config, centers: List[Center], state: Dict, timeout_seconds: int, force: bool = False) -> None:
    logging.info("Iniciando cola directa por centro (misma pestana por centro)")
    downloaded_ids = downloaded_ids_from_mapping(cfg.mapping_csv)
    playwright, browser = init_browser(cfg)
    context = new_context(browser, cfg, accept_downloads=True)
    try:
        bootstrap_page = context.new_page()
        try:
            login(bootstrap_page, cfg)
            save_auth_state(context, cfg)
        finally:
            bootstrap_page.close()

        total = len(centers)
        for idx, center in enumerate(centers, start=1):
            logging.info("Centro %s/%s -> [%s] %s", idx, total, center.center_id, center.center_name)
            if not force and is_already_downloaded(state, center, downloaded_ids):
                logging.info("[%s] ya descargado, se omite. Usa --force para redescargar", center.center_id)
                continue
            page = context.new_page()
            try:
                login(page, cfg)
                launch_and_wait_same_tab(page, cfg, center, state, timeout_seconds=timeout_seconds)
                save_state(cfg.state_file, state)
            finally:
                page.close()

        logging.info("Cola directa completada")
    finally:
        context.close()
        browser.close()
        playwright.stop()
        logging.info("Navegador cerrado tras cola directa")


def process_parallel(cfg: Config, centers: List[Center], state: Dict, mode: str) -> None:
    logging.info("Iniciando modo=%s con %s centros, max_tabs=%s, batch=%s", mode, len(centers), cfg.max_tabs, cfg.max_centers_per_batch)
    playwright, browser = init_browser(cfg)
    context = new_context(browser, cfg, accept_downloads=True)

    pages = [context.new_page() for _ in range(cfg.max_tabs)]
    login(pages[0], cfg)
    save_auth_state(context, cfg)

    try:
        batches = chunked(centers, cfg.max_centers_per_batch)
        for batch_index, batch in enumerate(batches, start=1):
            logging.info("Procesando tanda %s/%s (%s centros)", batch_index, len(batches), len(batch))
            for i, center in enumerate(batch):
                page = pages[i % len(pages)]
                if mode == "launch":
                    launch_for_center(page, cfg, state, center)
                else:
                    poll_and_download_for_center(page, cfg, state, center)
                save_state(cfg.state_file, state)
    finally:
        context.close()
        browser.close()
        playwright.stop()
        logging.info("Navegador cerrado para modo=%s", mode)


def all_done(state: Dict) -> bool:
    for data in state["centers"].values():
        if data.get("download_status") != "downloaded":
            return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Backups AbiesWeb con Playwright")
    parser.add_argument("mode", choices=["launch", "download", "run", "focus"], help="Modo de ejecucion")
    parser.add_argument("--only-centers", default="", help="Lista CSV de center_id")
    parser.add_argument("--only-names", default="", help="Lista CSV de nombres (match parcial)")
    parser.add_argument(
        "--filter-csv",
        default="",
        help="CSV con columnas: center_id,center_name,enabled",
    )
    parser.add_argument(
        "--max-polls",
        type=int,
        default=0,
        help="Solo en mode=run. 0 = sin limite.",
    )
    parser.add_argument(
        "--focus-timeout-seconds",
        type=int,
        default=1800,
        help="Solo mode=focus. Timeout total de espera exportacion.",
    )
    parser.add_argument(
        "--direct-timeout-seconds",
        type=int,
        default=2400,
        help="Timeout por centro en modo run directo.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Redescarga aunque el centro figure ya como descargado en state o mapeo_descargas.csv.",
    )
    args = parser.parse_args()

    cfg = Config()
    setup_logging(cfg.log_file)
    state = load_state(cfg.state_file)

    centers_csv = args.filter_csv or None
    centers = load_centers(centers_csv) if centers_csv else []
    centers = build_subset(centers, args.only_centers, args.only_names)
    if cfg.use_profile_switch:
        playwright, browser = init_browser(cfg)
        context = new_context(browser, cfg, accept_downloads=False)
        page = context.new_page()
        try:
            login(page, cfg)
            save_auth_state(context, cfg)
            discovered = discover_centers_from_profiles(page, cfg)
            if discovered:
                centers = build_subset(discovered, args.only_centers, args.only_names)
                logging.info("Centros detectados con perfil '%s': %s", cfg.profile_admin_label, len(centers))
        finally:
            context.close()
            browser.close()
            playwright.stop()

    if args.filter_csv:
        centers = apply_filter_csv(centers, args.filter_csv)
        logging.info("Centros tras --filter-csv (%s): %s", args.filter_csv, len(centers))

    before_dedupe = len(centers)
    centers = dedupe_centers(centers)
    if len(centers) != before_dedupe:
        logging.info("Centros unicos tras deduplicar: %s (antes %s)", len(centers), before_dedupe)

    if not centers:
        logging.error("No hay centros para procesar")
        return

    if args.mode == "launch":
        process_parallel(cfg, centers, state, mode="launch")
        return

    if args.mode == "download":
        process_parallel(cfg, centers, state, mode="download")
        return

    if args.mode == "focus":
        if len(centers) != 1:
            logging.error("mode=focus requiere exactamente 1 centro filtrado. Actual: %s", len(centers))
            return
        if not args.force and is_already_downloaded(state, centers[0], downloaded_ids_from_mapping(cfg.mapping_csv)):
            logging.info("[%s] ya descargado, se omite. Usa --force para redescargar", centers[0].center_id)
            return
        playwright, browser = init_browser(cfg)
        context = new_context(browser, cfg, accept_downloads=True)
        page = context.new_page()
        try:
            login(page, cfg)
            save_auth_state(context, cfg)
            launch_and_wait_same_tab(page, cfg, centers[0], state, timeout_seconds=args.focus_timeout_seconds)
            save_state(cfg.state_file, state)
        finally:
            context.close()
            browser.close()
            playwright.stop()
        return

    if cfg.use_profile_switch:
        run_direct_queue_same_tab(cfg, centers, state, timeout_seconds=args.direct_timeout_seconds, force=args.force)
        return

    process_parallel(cfg, centers, state, mode="launch")
    save_state(cfg.state_file, state)
    run_download_loop(cfg, centers, state, args.max_polls)


if __name__ == "__main__":
    main()
