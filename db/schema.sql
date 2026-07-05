PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS centers (
  center_code TEXT PRIMARY KEY,
  province TEXT NOT NULL,
  city TEXT NOT NULL,
  center_type TEXT NOT NULL,
  center_name TEXT NOT NULL,
  ownership TEXT,
  postal_code TEXT,
  postal_address TEXT,
  email TEXT,
  phone TEXT,
  csv_source TEXT,
  selected INTEGER NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 1,
  priority INTEGER NOT NULL DEFAULT 100,
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  description TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS abiesweb_checks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  center_code TEXT NOT NULL,
  checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  status TEXT NOT NULL,
  exists_in_abiesweb INTEGER,
  users_found INTEGER NOT NULL DEFAULT 0,
  search_text TEXT,
  abiesweb_uo_value TEXT,
  abiesweb_uo_label TEXT,
  selected_username TEXT,
  selected_full_name TEXT,
  reason TEXT,
  details_json TEXT,
  FOREIGN KEY (center_code) REFERENCES centers(center_code) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS abiesweb_users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  check_id INTEGER,
  center_code TEXT NOT NULL,
  full_name TEXT NOT NULL,
  username TEXT NOT NULL,
  document TEXT,
  profiles TEXT,
  profile_count INTEGER,
  raw_json TEXT,
  detected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (check_id) REFERENCES abiesweb_checks(id) ON DELETE SET NULL,
  FOREIGN KEY (center_code) REFERENCES centers(center_code) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS downloads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  check_id INTEGER,
  center_code TEXT NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT,
  requested_at TEXT,
  completed_at TEXT,
  downloaded_at TEXT,
  file_dir TEXT,
  file_name TEXT,
  file_path TEXT,
  relative_file_path TEXT,
  suggested_filename TEXT,
  source_url TEXT,
  file_size_bytes INTEGER,
  sha256 TEXT,
  impersonated_username TEXT,
  impersonated_full_name TEXT,
  export_date_detected TEXT,
  export_age_days INTEGER,
  export_reuse_max_age_days INTEGER,
  export_date_matches_policy INTEGER,
  download_mode TEXT,
  export_progress_initial INTEGER,
  export_progress_final INTEGER,
  reason TEXT,
  details_json TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (check_id) REFERENCES abiesweb_checks(id) ON DELETE SET NULL,
  FOREIGN KEY (center_code) REFERENCES centers(center_code) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS abiesplus_imports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  center_code TEXT NOT NULL,
  status TEXT NOT NULL,
  download_id INTEGER,
  zip_file TEXT,
  xml_entry TEXT,
  dry_run INTEGER NOT NULL DEFAULT 0,
  started_at TEXT,
  completed_at TEXT,
  imported_at TEXT,
  reason TEXT,
  details_json TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (center_code) REFERENCES centers(center_code) ON DELETE CASCADE,
  FOREIGN KEY (download_id) REFERENCES downloads(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  status TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'cli',
  mode TEXT NOT NULL,
  requested_by TEXT,
  total_centers INTEGER NOT NULL DEFAULT 0,
  processed_centers INTEGER NOT NULL DEFAULT 0,
  downloaded_count INTEGER NOT NULL DEFAULT 0,
  skipped_count INTEGER NOT NULL DEFAULT 0,
  error_count INTEGER NOT NULL DEFAULT 0,
  center_not_found_count INTEGER NOT NULL DEFAULT 0,
  no_users_found_count INTEGER NOT NULL DEFAULT 0,
  retries INTEGER NOT NULL DEFAULT 0,
  force_remote_check INTEGER NOT NULL DEFAULT 0,
  dry_run INTEGER NOT NULL DEFAULT 0,
  command TEXT,
  started_at TEXT,
  finished_at TEXT,
  reason TEXT,
  details_json TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS job_centers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL,
  center_code TEXT NOT NULL,
  status TEXT NOT NULL,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  last_exit_code INTEGER,
  check_id INTEGER,
  download_id INTEGER,
  started_at TEXT,
  finished_at TEXT,
  reason TEXT,
  details_json TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE,
  FOREIGN KEY (center_code) REFERENCES centers(center_code) ON DELETE CASCADE,
  FOREIGN KEY (check_id) REFERENCES abiesweb_checks(id) ON DELETE SET NULL,
  FOREIGN KEY (download_id) REFERENCES downloads(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS session_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT,
  action TEXT NOT NULL,
  center_code TEXT,
  status TEXT NOT NULL,
  message TEXT,
  details_json TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (center_code) REFERENCES centers(center_code) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_abiesweb_checks_center_code ON abiesweb_checks(center_code);
CREATE INDEX IF NOT EXISTS idx_abiesweb_checks_checked_at ON abiesweb_checks(checked_at);
CREATE INDEX IF NOT EXISTS idx_abiesweb_users_center_code ON abiesweb_users(center_code);
CREATE INDEX IF NOT EXISTS idx_downloads_center_code ON downloads(center_code);
CREATE INDEX IF NOT EXISTS idx_downloads_downloaded_at ON downloads(downloaded_at);
CREATE INDEX IF NOT EXISTS idx_abiesplus_imports_center_code ON abiesplus_imports(center_code);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_job_centers_job_id ON job_centers(job_id);
CREATE INDEX IF NOT EXISTS idx_job_centers_center_code ON job_centers(center_code);
CREATE UNIQUE INDEX IF NOT EXISTS idx_job_centers_job_center ON job_centers(job_id, center_code);
CREATE INDEX IF NOT EXISTS idx_session_logs_session_id ON session_logs(session_id);
CREATE INDEX IF NOT EXISTS idx_session_logs_center_code ON session_logs(center_code);

INSERT OR IGNORE INTO app_settings (key, value, description)
VALUES (
  'export_reuse_max_age_days',
  '0',
  'Maximo de dias de antiguedad permitidos para reutilizar un ZIP de exportacion existente; 0 significa solo hoy.'
);

INSERT OR IGNORE INTO app_settings (key, value, description)
VALUES (
  'local_timezone',
  'Europe/Madrid',
  'Zona horaria usada para comparar fechas DD/MM/YYYY mostradas por AbiesWeb.'
);

INSERT OR IGNORE INTO app_settings (key, value, description)
VALUES (
  'orchestrator_default_retries',
  '0',
  'Numero de reintentos por defecto para errores tecnicos en el orquestador.'
);

INSERT OR IGNORE INTO app_settings (key, value, description)
VALUES (
  'orchestrator_export_timeout_seconds',
  '2400',
  'Timeout por defecto en segundos para esperar exportaciones AbiesWeb desde el orquestador.'
);

INSERT OR IGNORE INTO app_settings (key, value, description)
VALUES (
  'orchestrator_skip_downloaded_today',
  'true',
  'Si es true, el orquestador salta centros con descarga registrada hoy salvo --force-remote-check.'
);

INSERT OR IGNORE INTO schema_migrations (version, description)
VALUES (1, 'initial v2 tracking schema');

INSERT OR IGNORE INTO schema_migrations (version, description)
VALUES (2, 'download and abiesweb check metadata columns');

INSERT OR IGNORE INTO schema_migrations (version, description)
VALUES (3, 'export reuse policy settings and download decision metadata');

INSERT OR IGNORE INTO schema_migrations (version, description)
VALUES (4, 'center selection fields for v2 front');

INSERT OR IGNORE INTO schema_migrations (version, description)
VALUES (5, 'orchestrator jobs and settings');

INSERT OR IGNORE INTO app_settings (key, value, description)
VALUES (
  'abiesplus_default_usar_registro',
  'false',
  'Valor por defecto del checkbox usar registro en ejemplares para creacion de bibliotecas en Abies+.'
);

INSERT OR IGNORE INTO app_settings (key, value, description)
VALUES (
  'abiesplus_default_syncusers',
  'false',
  'Valor por defecto del checkbox visualizar menus de sincronizacion de usuarios en Abies+.'
);

INSERT OR IGNORE INTO app_settings (key, value, description)
VALUES (
  'abiesplus_default_importusers',
  'false',
  'Valor por defecto del checkbox bibliotecarios pueden importar usuarios masivamente en Abies+.'
);

INSERT OR IGNORE INTO app_settings (key, value, description)
VALUES (
  'abiesplus_default_dry_run',
  'true',
  'Si es true, el lanzamiento desde el front de Abies+ usa dry-run por defecto.'
);

INSERT OR IGNORE INTO schema_migrations (version, description)
VALUES (6, 'abiesplus imports metadata and abiesplus settings');
