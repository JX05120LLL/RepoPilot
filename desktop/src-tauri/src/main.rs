//! RepoPilot Desktop 的本机后端生命周期管理。
//!
//! 前端只能调用回环地址上的 Python API。开发环境由 `uv` 启动项目后端；
//! 打包时由安装器提供的 sidecar 或 `REPOPILOT_BACKEND_EXECUTABLE` 接管。

use std::{
    env, fs,
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
    TcpStream::connect_timeout(&address, Duration::from_millis(250)).is_ok()
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
        if env::var_os("REPOPILOT_STATE_DB_PATH").is_none() {
            command.env("REPOPILOT_STATE_DB_PATH", runtime_dir.join("state.sqlite"));
        }
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
            let runtime_dir = app.path().app_data_dir().ok().and_then(|path| {
                fs::create_dir_all(&path)
                    .map(|_| path)
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
