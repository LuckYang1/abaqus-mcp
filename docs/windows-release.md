# Abaqus MCP Windows 发行包

发行包包含：

*   `abaqus-mcp-server.exe`：MCP 服务端，自带 Python 运行环境，由 AI 客户端自动启动，不需要双击。
*   `abaqus_mcp_plugin.py`：Abaqus/CAE 插件。
*   `skills\abaqus-mcp-iteration`：可选安装的 skill，让 AI 在使用过程中记录 MCP 的不足和使用经验。
*   `SHA256SUMS.txt`：exe 的 SHA-256 校验值。
*   `LICENSE`、`THIRD_PARTY_NOTICES.md`：许可与来源说明。

## 安装

1.  把发行包解压到 `C:\Abaqus-MCP`，解压后应能看到 `C:\Abaqus-MCP\abaqus-mcp-server.exe`。

2.  打开文件资源管理器，在地址栏输入 `%USERPROFILE%` 并回车；如果没有 `abaqus_plugins` 文件夹，就新建一个；把 `C:\Abaqus-MCP\abaqus_mcp_plugin.py` 复制进去。这个文件夹里如果已有 `abaqus_mcp_gui_plugin.py` 或 `mcp_control` 文件夹（其他 Abaqus MCP 插件），请移到别处。

3.  配置 AI 客户端。

    *   **Claude Code**：从开始菜单打开 Git Bash，粘贴下面这行命令并回车：

        ```bash
        claude mcp add-json --scope user abaqus '{"type":"stdio","command":"C:/Abaqus-MCP/abaqus-mcp-server.exe","timeout":3700000}'
        ```

    *   **Codex**：按 Win+R，输入 `notepad %USERPROFILE%\.codex\config.toml` 并回车，在文件末尾加上下面三行后保存。文件里已有 `[mcp_servers.abaqus]` 这一节时，用这三行替换原来的整节：

        ```toml
        [mcp_servers.abaqus]
        command = 'C:\Abaqus-MCP\abaqus-mcp-server.exe'
        tool_timeout_sec = 3700
        ```

4.  打开 Abaqus/CAE 2024 或更新版本，选择 **Plug-ins → Abaqus MCP → Start MCP Bridge**。在 AI 客户端里新开一个对话，对它说：“调用 abaqus 的 ping，再用 run_python 执行 result = list(mdb.models.keys())”。能返回模型列表，就说明安装成功。

每次打开 Abaqus/CAE 后都要选择一次 Start MCP Bridge。

（可选）安装经验记录 skill：把 `C:\Abaqus-MCP\skills\abaqus-mcp-iteration` 整个文件夹复制到 `%USERPROFILE%\.claude\skills`（Claude Code）或 `%USERPROFILE%\.agents\skills`（Codex），文件夹不存在就新建。

更多说明见 <https://github.com/LuckYang1/abaqus-mcp>。
