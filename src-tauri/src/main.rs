// AgentShell Tauri 入口
// 这个壳子本身不做业务逻辑：它只负责打开一个原生窗口，
// 里面加载的是本地的 server.py（devUrl = http://localhost:8787）。
// 真正的网关、Token 管理、Agent 循环都在 Python 端。

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    tauri::Builder::default()
        .run(tauri::generate_context!())
        .expect("error while running AgentShell");
}
