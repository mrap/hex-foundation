use chrono::Utc;
use rusqlite::{params, Connection};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use crate::server::{Request, Response};

// Migration tracking table: _ext_migrations
const MIGRATIONS_TABLE: &str = "_ext_migrations";

pub struct ExtensionDb {
    db: Arc<Mutex<Connection>>,
}

impl ExtensionDb {
    pub fn open(hex_dir: &Path) -> Result<Arc<Self>, String> {
        let ext_dir = hex_dir.join(".hex/extensions");
        let _ = std::fs::create_dir_all(&ext_dir);
        let db_path = ext_dir.join("ext.db");
        let conn = Connection::open(&db_path)
            .map_err(|e| format!("ext db open failed: {e}"))?;
        init_schema(&conn)?;
        Ok(Arc::new(Self {
            db: Arc::new(Mutex::new(conn)),
        }))
    }

    /// Scan extension directories and run pending migrations.
    /// Checks both hex_dir/extensions/ and hex_dir/.hex/extensions/.
    pub fn scan_and_migrate(&self, hex_dir: &Path) {
        let search_dirs = [
            hex_dir.join("extensions"),
            hex_dir.join(".hex/extensions"),
        ];
        for base in &search_dirs {
            let entries = match std::fs::read_dir(base) {
                Ok(e) => e,
                Err(_) => continue,
            };
            for entry in entries.flatten() {
                let path = entry.path();
                if !path.is_dir() {
                    continue;
                }
                let ext_name = path
                    .file_name()
                    .and_then(|n| n.to_str())
                    .unwrap_or("")
                    .to_string();
                if ext_name.is_empty() || ext_name.starts_with('.') {
                    continue;
                }
                if !path.join("extension.yaml").exists() {
                    continue;
                }
                let migrations_dir = path.join("migrations");
                if migrations_dir.is_dir() {
                    if let Err(e) = self.run_migrations_for_ext(&ext_name, &migrations_dir) {
                        eprintln!("extensions: migration failed for {}: {}", ext_name, e);
                    }
                }
            }
        }
    }

    fn run_migrations_for_ext(&self, ext_name: &str, migrations_dir: &Path) -> Result<(), String> {
        let mut files: Vec<PathBuf> = std::fs::read_dir(migrations_dir)
            .map_err(|e| format!("read migrations dir failed: {e}"))?
            .flatten()
            .map(|e| e.path())
            .filter(|p| p.extension().and_then(|e| e.to_str()) == Some("sql"))
            .collect();
        files.sort();

        let db = self.db.lock().unwrap();
        for file in &files {
            let migration_name = file
                .file_name()
                .and_then(|n| n.to_str())
                .unwrap_or("")
                .to_string();

            let already: bool = db
                .query_row(
                    &format!(
                        "SELECT COUNT(*) FROM {} WHERE extension = ?1 AND migration = ?2",
                        MIGRATIONS_TABLE
                    ),
                    params![ext_name, migration_name],
                    |row| row.get::<_, i64>(0),
                )
                .map(|c| c > 0)
                .unwrap_or(false);

            if already {
                continue;
            }

            let sql = std::fs::read_to_string(file)
                .map_err(|e| format!("read {}: {e}", migration_name))?;

            db.execute_batch(&sql)
                .map_err(|e| format!("migration {} failed: {e}", migration_name))?;

            let now = Utc::now().to_rfc3339();
            db.execute(
                &format!(
                    "INSERT INTO {} (extension, migration, applied_at) VALUES (?1, ?2, ?3)",
                    MIGRATIONS_TABLE
                ),
                params![ext_name, migration_name, now],
            )
            .map_err(|e| format!("record migration failed: {e}"))?;

            eprintln!("extensions: applied {}/{}", ext_name, migration_name);
        }
        Ok(())
    }

    /// Handle GET /ext/<name>/api/query?sql=SELECT...
    /// Only allows SELECT on tables prefixed with ext_{name}_.
    pub fn handle_query(&self, req: &Request, ext_name: &str) -> Response {
        let sql = match req.query.get("sql") {
            Some(s) => s.clone(),
            None => return json_error(400, "missing ?sql= parameter"),
        };

        let sql_upper = sql.trim().to_uppercase();
        if !sql_upper.starts_with("SELECT") {
            return json_error(403, "only SELECT queries are allowed");
        }

        let required_prefix = format!(
            "EXT_{}_",
            ext_name.replace('-', "_").to_uppercase()
        );
        if !sql_upper.contains(&required_prefix) {
            return json_error(
                403,
                &format!(
                    "query must only access tables prefixed with ext_{}_",
                    ext_name.replace('-', "_").to_lowercase()
                ),
            );
        }

        let db = self.db.lock().unwrap();
        let mut stmt = match db.prepare(&sql) {
            Ok(s) => s,
            Err(e) => return json_error(400, &format!("invalid SQL: {e}")),
        };

        let col_count = stmt.column_count();
        let col_names: Vec<String> = (0..col_count)
            .map(|i| stmt.column_name(i).unwrap_or("col").to_string())
            .collect();

        let rows: Vec<serde_json::Value> = match stmt.query_map([], |row| {
            let mut obj = serde_json::Map::new();
            for (i, name) in col_names.iter().enumerate() {
                let val: rusqlite::types::Value = row.get(i)?;
                obj.insert(name.clone(), sqlite_to_json(val));
            }
            Ok(serde_json::Value::Object(obj))
        }) {
            Ok(mapped) => mapped.flatten().collect(),
            Err(e) => return json_error(500, &format!("query failed: {e}")),
        };

        let body =
            serde_json::to_vec(&serde_json::json!({ "rows": rows })).unwrap_or_default();
        Response {
            status: 200,
            content_type: "application/json".to_string(),
            headers: vec![("Access-Control-Allow-Origin".to_string(), "*".to_string())],
            body,
        }
    }
}

fn init_schema(conn: &Connection) -> Result<(), String> {
    conn.execute_batch(&format!(
        "PRAGMA journal_mode=WAL;
         PRAGMA busy_timeout=5000;
         CREATE TABLE IF NOT EXISTS {} (
             extension TEXT,
             migration TEXT,
             applied_at TEXT,
             PRIMARY KEY (extension, migration)
         );",
        MIGRATIONS_TABLE
    ))
    .map_err(|e| format!("ext schema init failed: {e}"))
}

fn sqlite_to_json(val: rusqlite::types::Value) -> serde_json::Value {
    match val {
        rusqlite::types::Value::Null => serde_json::Value::Null,
        rusqlite::types::Value::Integer(i) => serde_json::json!(i),
        rusqlite::types::Value::Real(f) => serde_json::json!(f),
        rusqlite::types::Value::Text(s) => serde_json::Value::String(s),
        rusqlite::types::Value::Blob(_) => serde_json::Value::String("<binary>".to_string()),
    }
}

fn json_error(status: u16, msg: &str) -> Response {
    Response {
        status,
        content_type: "application/json".to_string(),
        headers: vec![("Access-Control-Allow-Origin".to_string(), "*".to_string())],
        body: serde_json::to_vec(&serde_json::json!({ "error": msg })).unwrap_or_default(),
    }
}
