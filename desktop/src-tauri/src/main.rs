//! RepoPilot Desktop 的本机后端生命周期管理。
//!
//! 前端只能调用回环地址上的 Python API。开发环境由 `uv` 启动项目后端；
//! 打包时由安装器提供的 sidecar 或 `REPOPILOT_BACKEND_EXECUTABLE` 接管。

use std::{
    env, fs,
    io::{Read, Write},
    net::{SocketAddr, TcpStream},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::Mutex,
    time::Duration,
};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

use tauri::{Manager, RunEvent, WindowEvent};

const API_HOST: &str = "127.0.0.1";
const API_PORT: u16 = 8765;
const API_IO_TIMEOUT: Duration = Duration::from_millis(500);
const API_HEALTH_RESPONSE_LIMIT: usize = 16 * 1024;
const DESKTOP_SETTINGS_TEMPLATE: &str = r#"# RepoPilot Desktop 本地运行配置。
# 本文件只保存在当前用户应用数据目录，不会打进安装包或上传。

# Chat：OpenAI-compatible
REPOPILOT_CHAT_BASE_URL=
REPOPILOT_CHAT_API_KEY=
REPOPILOT_CHAT_MODEL=

# Embedding：可与 Chat 使用不同供应商
REPOPILOT_EMBEDDING_BASE_URL=
REPOPILOT_EMBEDDING_API_KEY=
REPOPILOT_EMBEDDING_MODEL=
REPOPILOT_EMBEDDING_DIMENSIONS=

# 本地基础设施
REPOPILOT_QDRANT_URL=http://127.0.0.1:6333
"#;
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

struct BackendProcess {
    child: Mutex<Option<Child>>,
}

impl BackendProcess {
    fn launch_if_needed(resource_dir: Option<&Path>, runtime_dir: Option<&Path>) -> Self {
        if api_is_reachable() {
            return Self {
                child: Mutex::new(None),
            };
        }

        match backend_command(resource_dir, runtime_dir).spawn() {
            Ok(child) => Self {
                child: Mutex::new(Some(child)),
            },
            Err(error) => {
                // API 不可用时，React 会明确显示“本机服务未连接”。不要让壳进程崩溃。
                eprintln!("无法启动 RepoPilot 本机后端: {error}");
                Self {
                    child: Mutex::new(None),
                }
            }
        }
    }

    fn stop(&self) {
        let Ok(mut child) = self.child.lock() else {
            return;
        };
        if let Some(mut process) = child.take() {
            terminate_backend_process(&mut process);
        }
    }
}

#[cfg(windows)]
fn terminate_backend_process(process: &mut Child) {
    // PyInstaller 的 one-file sidecar 会派生真正的 Python 进程；只结束 bootloader
    // 会留下监听端口的子进程，因此仅针对本应用启动的根 PID 结束整棵进程树。
    let process_id = process.id().to_string();
    let taskkill = env::var_os("SystemRoot")
        .map(|root| PathBuf::from(root).join("System32").join("taskkill.exe"))
        .filter(|path| path.is_file())
        .unwrap_or_else(|| PathBuf::from("taskkill.exe"));
    let mut command = Command::new(taskkill);
    command
        .args(["/PID", &process_id, "/T", "/F"])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .creation_flags(CREATE_NO_WINDOW);

    let terminated = command
        .status()
        .map(|status| status.success())
        .unwrap_or(false);
    if !terminated {
        let _ = process.kill();
    }
    let _ = process.wait();
}

#[cfg(not(windows))]
fn terminate_backend_process(process: &mut Child) {
    let _ = process.kill();
    let _ = process.wait();
}

fn api_is_reachable() -> bool {
    let address = SocketAddr::from(([127, 0, 0, 1], API_PORT));
    let Ok(mut stream) = TcpStream::connect_timeout(&address, API_IO_TIMEOUT) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(API_IO_TIMEOUT));
    let _ = stream.set_write_timeout(Some(API_IO_TIMEOUT));
    if stream
        .write_all(b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        .is_err()
    {
        return false;
    }

    let mut response = Vec::with_capacity(1024);
    let mut buffer = [0_u8; 1024];
    while response.len() < API_HEALTH_RESPONSE_LIMIT {
        match stream.read(&mut buffer) {
            Ok(0) => break,
            Ok(read) => {
                let remaining = API_HEALTH_RESPONSE_LIMIT - response.len();
                response.extend_from_slice(&buffer[..read.min(remaining)]);
            }
            Err(_) => return false,
        }
    }
    is_repopilot_health_response(&response)
}

fn is_repopilot_health_response(response: &[u8]) -> bool {
    let Ok(response) = std::str::from_utf8(response) else {
        return false;
    };
    (response.starts_with("HTTP/1.1 200 ") || response.starts_with("HTTP/1.0 200 "))
        && response.contains("\"status\":\"READY\"")
        && response.contains("\"scope\":\"127.0.0.1-only\"")
}

fn backend_command(resource_dir: Option<&Path>, runtime_dir: Option<&Path>) -> Command {
    let custom_executable = env::var_os("REPOPILOT_BACKEND_EXECUTABLE");
    let use_uv = cfg!(debug_assertions) && custom_executable.is_none();
    let executable = custom_executable.unwrap_or_else(|| {
        if cfg!(debug_assertions) {
            "uv".into()
        } else {
            // 发布包会将同名 Python sidecar 放入可执行文件搜索路径。
            bundled_backend_path(resource_dir)
                .map(PathBuf::into_os_string)
                .unwrap_or_else(|| "repopilot-guard".into())
        }
    });
    let mut command = Command::new(executable);

    if use_uv {
        command.args(["run", "repopilot-guard"]);
        command.current_dir(repository_root());
    } else if let Some(runtime_dir) = runtime_dir {
        command.current_dir(runtime_dir);
    }
    if let Some(runtime_dir) = runtime_dir {
        if env::var_os("REPOPILOT_STATE_DB_PATH").is_none() {
            command.env("REPOPILOT_STATE_DB_PATH", runtime_dir.join("state.sqlite"));
        }
        if env::var_os("REPOPILOT_CONFIG_FILE").is_none() {
            // 安装包不能依赖开发仓库 `.env`；密钥只由用户应用数据目录中的配置文件提供。
            command.env("REPOPILOT_CONFIG_FILE", runtime_dir.join("settings.env"));
        }
        // 仅由 Tauri sidecar 启动的 API 可保存桌面运行配置；浏览器预览和普通 CLI 永远无此能力。
        command.env("REPOPILOT_DESKTOP_CONFIG_WRITE_ENABLED", "1");
    }
    command
        .args(["api", "serve", "--host", API_HOST, "--port"])
        .arg(API_PORT.to_string())
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(windows)]
    command.creation_flags(CREATE_NO_WINDOW);
    command
}

fn ensure_desktop_config(runtime_dir: &Path) -> PathBuf {
    let config_file = runtime_dir.join("settings.env");
    if config_file.exists() {
        return config_file;
    }
    match fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&config_file)
    {
        Ok(mut file) => {
            if let Err(error) = file.write_all(DESKTOP_SETTINGS_TEMPLATE.as_bytes()) {
                eprintln!("无法写入 RepoPilot 桌面配置模板: {error}");
            }
        }
        Err(error) => {
            // 另一个实例可能刚完成创建；运行时会在健康检查中清楚报告缺失配置。
            eprintln!("无法创建 RepoPilot 桌面配置模板: {error}");
        }
    }
    config_file
}

fn bundled_backend_path(resource_dir: Option<&Path>) -> Option<PathBuf> {
    let path = resource_dir?.join("binaries").join("repopilot-guard.exe");
    path.is_file().then_some(path)
}

fn repository_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(|desktop| desktop.parent())
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

fn main() {
    let app = tauri::Builder::default()
        .setup(|app| {
            let resource_dir = app.path().resource_dir().ok();
            let runtime_override = env::var_os("REPOPILOT_DESKTOP_DATA_DIR").and_then(|value| {
                let path = PathBuf::from(value);
                if path.is_absolute() {
                    Some(path)
                } else {
                    eprintln!("REPOPILOT_DESKTOP_DATA_DIR 必须是绝对路径，已回退到默认数据目录。");
                    None
                }
            });
            let runtime_dir = runtime_override
                .or_else(|| app.path().app_data_dir().ok())
                .and_then(|path| {
                    fs::create_dir_all(&path)
                        .map(|_| {
                            ensure_desktop_config(&path);
                            path
                        })
                        .map_err(|error| eprintln!("无法创建 RepoPilot 运行目录: {error}"))
                        .ok()
                });
            app.manage(BackendProcess::launch_if_needed(
                resource_dir.as_deref(),
                runtime_dir.as_deref(),
            ));
            Ok(())
        })
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_single_instance::init(
            |app, _arguments, _working_directory| {
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.unminimize();
                    let _ = window.show();
                    let _ = window.set_focus();
                }
            },
        ))
        .build(tauri::generate_context!())
        .expect("无法启动 RepoPilot Desktop");

    app.run(|app_handle, event| match event {
        RunEvent::WindowEvent {
            label,
            event: WindowEvent::CloseRequested { .. },
            ..
        } if label == "main" => {
            app_handle.state::<BackendProcess>().stop();
            app_handle.exit(0);
        }
        RunEvent::Exit => app_handle.state::<BackendProcess>().stop(),
        _ => {}
    });
}

#[cfg(test)]
mod tests {
    use std::fs;

    use super::{ensure_desktop_config, is_repopilot_health_response};

    #[test]
    fn accepts_repopilot_local_health_response() {
        let response = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{\"status\":\"READY\",\"scope\":\"127.0.0.1-only\"}";
        assert!(is_repopilot_health_response(response));
    }

    #[test]
    fn rejects_unrelated_or_unsuccessful_services() {
        let unrelated = b"HTTP/1.1 200 OK\r\n\r\n{\"status\":\"READY\"}";
        let unavailable = b"HTTP/1.1 503 Service Unavailable\r\n\r\n{\"status\":\"READY\",\"scope\":\"127.0.0.1-only\"}";
        assert!(!is_repopilot_health_response(unrelated));
        assert!(!is_repopilot_health_response(unavailable));
    }

    #[test]
    fn desktop_config_template_is_created_once_without_overwrite() {
        let directory =
            std::env::temp_dir().join(format!("repopilot-config-test-{}", std::process::id()));
        fs::create_dir_all(&directory).expect("创建测试目录");
        let config_file = ensure_desktop_config(&directory);
        let initial = fs::read_to_string(&config_file).expect("读取配置模板");
        assert!(initial.contains("REPOPILOT_CHAT_API_KEY="));

        fs::write(&config_file, "REPOPILOT_CHAT_API_KEY=user-value\n").expect("写入用户配置");
        let repeated = ensure_desktop_config(&directory);
        assert_eq!(config_file, repeated);
        assert_eq!(
            "REPOPILOT_CHAT_API_KEY=user-value\n",
            fs::read_to_string(&config_file).expect("读取用户配置")
        );
        fs::remove_dir_all(directory).expect("清理测试目录");
    }
}
