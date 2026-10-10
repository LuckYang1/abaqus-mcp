# Abaqus MCP

Abaqus MCP 是一套本地 MCP 服务端加一个 Abaqus/CAE 插件，让 Claude Code、Codex 等 AI 客户端实时操作正在运行的 Abaqus/CAE：执行 Abaqus Python、查看模型、提交和监控作业、读取 ODB 数值、获取视口图片。

本项目合并了 [Abaqus-Control-MCP 2.0.0](https://github.com/Whfkl/Abaqus-Control-MCP) 与 [CAE-Agent-Hub](https://github.com/Cai-aa/CAE-Agent-Hub) 的 Abaqus MCP 5.0.0，两者原有的工具名都可以继续使用。各部分的来源见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 运行要求

*   Windows，已安装 Abaqus/CAE 2024 或更新版本（Python 3）。Python 2 版 Abaqus（例如 2021）加载插件时会给出报错 “Abaqus MCP requires Abaqus/CAE with Python 3”。
*   一个支持 MCP 的 AI 客户端，例如 Claude Code 或 Codex。
*   发行包中的服务端 exe 自带 Python 运行环境，不需要另装 Python。

## 安装

最省事的方法是把[下方的提示词](#让-ai-代理自动安装)复制给 Claude Code、Codex 等 AI 代理，由它完成全部步骤。手动安装按下面四步操作。

1.  **下载并解压**。打开[最新版本页面](https://github.com/LuckYang1/abaqus-mcp/releases/latest)，下载 `abaqus-mcp-vX.Y.Z-windows-x64.zip`。在 ZIP 文件上点右键，选择“全部解压缩”，目标文件夹填 `C:\Abaqus-MCP`。解压后应能看到 `C:\Abaqus-MCP\abaqus-mcp-server.exe` 和 `C:\Abaqus-MCP\abaqus_mcp_plugin.py`。exe 由 AI 客户端自动启动，不需要双击。

    （可选）核对下载是否完整：在 PowerShell 中运行 `Get-FileHash <ZIP 文件路径>`，得到的值应与同一页面下载的 `.sha256` 文件中的值一致，字母大小写不同不影响。

2.  **安装插件**。打开文件资源管理器，在地址栏输入 `%USERPROFILE%` 并回车；如果没有 `abaqus_plugins` 文件夹，就新建一个；把 `C:\Abaqus-MCP\abaqus_mcp_plugin.py` 复制进去。

    这个文件夹里如果已有 `abaqus_mcp_gui_plugin.py` 或 `mcp_control` 文件夹（Abaqus-Control-MCP、CAE-Agent-Hub 的插件），请把它们移到别处：它们同样使用 48152 端口，Plug-ins 菜单里也会出现重复的项目。

3.  **配置 AI 客户端**。

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

    *   **其他客户端**：以 stdio 方式登记 `C:\Abaqus-MCP\abaqus-mcp-server.exe`，工具超时设为 3700 秒以上。

4.  **启动并测试**。打开 Abaqus/CAE，选择 **Plug-ins → Abaqus MCP → Start MCP Bridge**，消息区出现 “bridge listening on 127.0.0.1:48152” 表示桥接已启动。然后在 AI 客户端里新开一个对话，对它说：“调用 abaqus 的 ping，再用 run_python 执行 result = list(mdb.models.keys())”。能返回模型列表，就说明安装成功。

每次打开 Abaqus/CAE 后都要选择一次 Start MCP Bridge。插件菜单还有 **Bridge Status**（在消息区打印版本、端口、已处理请求数、排队数和运行时长）和 **Stop MCP Bridge**。

### 让 AI 代理自动安装

把下面的提示词复制给有本地文件和终端权限的 Claude Code、Codex 等 AI 代理。它会完成上面的四步并做连接测试，中间会提醒你在 Abaqus/CAE 中启动桥接：

```text
请在这台 Windows 电脑上安装并配置 https://github.com/LuckYang1/abaqus-mcp 。先阅读仓库 README，再检查我当前使用的 MCP 客户端。从 GitHub Releases 下载最新版本的 abaqus-mcp-v<版本>-windows-x64.zip 和同名的 .sha256 文件，核对 SHA-256 后解压到 C:\Abaqus-MCP。把其中的 abaqus_mcp_plugin.py 复制到 %USERPROFILE%\abaqus_plugins（文件夹不存在就新建）。如果插件目录里已有其他 Abaqus MCP 插件（例如 abaqus_mcp_gui_plugin.py 或 mcp_control 文件夹），或者 MCP 客户端里已有指向其他 Abaqus MCP 的配置，先列出来告诉我，等我同意后再移走或替换。接着把 C:\Abaqus-MCP\abaqus-mcp-server.exe 按 stdio 方式配置到我当前的 MCP 客户端，服务名为 abaqus，客户端的工具超时设为 3700 秒（Claude Code 用 claude mcp add-json 写入 "timeout": 3700000，Codex 在 config.toml 中写 tool_timeout_sec = 3700），不改动无关的 MCP 配置。配置完成后提醒我打开 Abaqus/CAE 2024 或更新版本，并选择 Plug-ins → Abaqus MCP → Start MCP Bridge。然后开启新的客户端会话，实际调用 ping，再调用 run_python，代码为 result = list(mdb.models.keys())。最后报告安装路径和实际测试结果；如果当前无法启动 Abaqus/CAE，说明已完成哪些步骤、还差哪项实测。
```

## 工具

所有工具都可以传入 `timeout`（秒），用于单独调整这一次调用的等待时间。

*   `ping`：检查连接，返回 Python 与 Abaqus 版本、进程号、工作目录、模型、视口和桥接运行信息。默认等待 10 秒。
*   `check_abaqus_connection`：用一段文字给出连接状态、Abaqus 版本、模型、视口和工作目录。默认等待 10 秒。
*   `run_python(code)`：在 Abaqus 内核执行 Python。单个表达式返回其值，多条语句返回赋给 `result` 的值；同时返回标准输出、错误位置、调用链和修改建议。
*   `execute_script(script)`：`run_python` 的文本形式，返回标准输出和 JSON 格式的返回值。
*   `set_workdir(path)`：把 CAE 工作目录切换到已存在的绝对路径，返回切换前后的路径。
*   `get_model_info`：列出所有模型的部件、材料、截面、分析步、载荷、边界条件、相互作用、约束、幅值、实例、集合、表面和部件拓扑数量，以及作业和视口。每个名称列表最多 200 项，`counts` 给出完整数量，`truncated` 标出被截断的列表。
*   `list_jobs`：列出当前 CAE 会话中的作业及其状态、模型、CPU 数等设置。
*   `submit_job(job_name)`：提交已有作业并等待求解结束，返回最终状态与证据。默认最长等待 3600 秒；超时后作业继续运行，结果中的 `submittedAt` 可作为 `monitor_job_status` 的 `since`。
*   `monitor_job_status(job_name, terminate, since)`：不带作业名时列出作业；带作业名时综合 CAE 状态与 `.log`、`.sta`、`.dat`、`.msg`、`.lck` 文件，给出 `COMPLETED`、`FAILED`、`TERMINATED`、`RUNNING`、`SUBMITTED` 或 `UNKNOWN`，以及当前分析步、增量步、错误和警告。证据冲突或无法确认属于本次运行时返回 `UNKNOWN`；`since` 用于排除旧文件。`terminate=true` 用当前 Abaqus 版本的启动程序（内核环境变量 `ABA_COMMAND`）执行 `terminate`，Abaqus 收尾约需一分钟，之后再调用一次确认状态为 `TERMINATED` 且锁文件已消失。
*   `inspect_odb(odb_path, step, frame, variable, set_name, component, history_region, history_variable, max_points)`：只读打开 ODB，列出分析步（起始、结束和持续时间）、抽样帧、场变量（分量、不变量和输出位置）、历史区域和集合；可统计指定分析步、帧、集合、分量的场变量最小值和最大值，或返回抽样后的历史曲线。向量不指定分量时统计模长，张量必须指定分量。
*   `get_odb_info(odb_path)`：只返回 ODB 概况，等同于不带统计参数的 `inspect_odb`。
*   `capture_viewport(viewport_name, image_format, save_path)`：PNG、TIFF 以 MCP 图片返回，可同时保存；SVG、EPS、PS 必须提供保存路径。结构化结果中不含图片的 base64。
*   `get_viewport_image(viewport_name, image_format)`：以 data URI 文本返回 PNG 或 TIFF 图片。

## 资源

*   `abaqus://session-telemetry`：实时会话与桥接信息（JSON），未连接时给出原因。
*   `abaqus://status`：与上一项相同。
*   `abaqus://agent-instructions`：给智能体的工作规则，与服务端初始化时下发的说明相同。

## 超时与结果未知

*   默认等待时间由 `ABAQUS_MCP_TIMEOUT` 决定（120 秒）。
*   服务端发出请求时写入期限：发出时刻加 `timeout` 再加 5 秒。到期时仍未开始执行的请求被取消，状态为 `CANCELLED`，代码不会执行，可以重发。CAE 执行内核命令期间可能暂停插件的后台线程，插件会在前一个调用结束后才读到新请求；按发出时刻计算的期限保证超期的请求不会执行，这种情况下取消的回复要等前一个调用结束后才发出。
*   服务端最多等待 `timeout + 10` 秒，收不到回复就返回 `UNKNOWN`；插件发现已经开始执行的调用超过 `timeout + 5` 秒时也返回 `UNKNOWN`。这时代码可能仍在运行，也可能已经运行完，先检查模型状态再决定是否重做。
*   客户端自身的工具超时必须比上面的等待时间更长。Claude Code 用服务端配置中的 `"timeout": 3700000`（毫秒），Codex 用 `tool_timeout_sec = 3700`，都覆盖 `submit_job` 默认的 3600 秒等待。

## 环境变量

*   `ABAQUS_MCP_HOST`：桥接地址，默认 `127.0.0.1`。
*   `ABAQUS_MCP_PORT`：桥接端口，默认 `48152`。
*   `ABAQUS_MCP_TIMEOUT`：默认等待秒数，默认 `120`。
*   `ABAQUS_MCP_MAX_MESSAGE_BYTES`：单条消息上限，默认 16 MiB。
*   `ABAQUS_MCP_PLUGIN_DIR`：从源码安装时 `abaqus-mcp-setup` 写入插件的目录，默认 `~/abaqus_plugins`。
*   `ABAQUS_MCP_DEBUG`：设为 `1` 后插件写入有大小上限的调试日志；未开启时，启动桥接会清除以前留下的调试日志。
*   `ABAQUS_MCP_LOG`：调试日志路径，默认在系统临时目录下的 `abaqus_mcp_plugin.log`。

服务端读取 MCP 客户端配置中设置的环境变量，没有设置时使用上面的默认值。插件读取的是 Abaqus/CAE 进程的环境变量，修改后需要重启 CAE。

## 诊断

*   在 CAE 中选择 **Plug-ins → Abaqus MCP → Bridge Status**，查看桥接是否在运行。
*   AI 调用 `ping` 返回 `NOT_STARTED`（连接被拒绝）时，说明 Abaqus/CAE 没有打开，或者没有选择 Start MCP Bridge。
*   更新插件后要重启 CAE。
*   从源码安装时还可以使用命令行诊断工具，见[从源码安装](#从源码安装)。

## 工作原理

```mermaid
flowchart LR
    A[Claude Code / Codex / 其他 MCP 客户端] -->|stdio| S[abaqus-mcp-server]
    S -->|127.0.0.1:48152 · executionId| P[Abaqus/CAE 插件]
    P -->|图形界面主线程 · sendCommand| K[Abaqus 内核]
    K --> M[(模型数据库 · 作业 · ODB)]
```

*   服务端运行在 Abaqus 之外，可以用独立 exe，也可以用 Python 环境。
*   插件运行在 Abaqus/CAE 图形界面进程内，在主线程上把代码交给内核执行。用户代码在一个持久命名空间中运行，变量在多次调用之间保留；每次调用前后都会清除 `result`。
*   每次调用都有独立的 `executionId`，返回值、输出和错误诊断只属于这一次调用。
*   CAE 只在两次调用之间的空闲时刻更新作业状态，所以 `submit_job` 等待求解时由服务端分多次短调用查询，等待期间 CAE 界面保持可用。

## 从源码安装

适合开发者，或需要在非 Windows 系统上运行服务端的情况。需要 Python 3.10 以上版本和 [uv](https://docs.astral.sh/uv/)。

1.  安装服务端和命令行工具：

    ```bash
    uv tool install git+https://github.com/LuckYang1/abaqus-mcp.git
    ```

2.  安装插件，默认写入 `~/abaqus_plugins/abaqus_mcp_plugin.py`，可用 `ABAQUS_MCP_PLUGIN_DIR` 指定其他目录：

    ```bash
    abaqus-mcp-setup
    ```

3.  在 AI 客户端中以 stdio 方式登记命令 `abaqus-mcp-server`。Claude Code：

    ```bash
    claude mcp add-json --scope user abaqus '{"type":"stdio","command":"abaqus-mcp-server","timeout":3700000}'
    ```

    Codex 的 `config.toml` 中写 `command = "abaqus-mcp-server"` 和 `tool_timeout_sec = 3700`。

4.  启动桥接和测试的方法与[安装](#安装)第 4 步相同。

源码安装附带的命令行工具：

*   `abaqus-mcp-doctor --verify-connection`：打印包版本、运行环境、命令位置、插件是否已安装且与包内版本一致，并检查连接。
*   `abaqus-mcp-check`：调用 `ping` 并在内核执行一段短代码。
*   `abaqus-mcp-stop`：请求正在运行的桥接停止；之后要在 CAE 菜单中重新选择 Start MCP Bridge。

在 Windows 版 Claude 桌面应用中执行安装命令时要注意：从它启动的程序在 `AppData` 下新建的文件会被重定向到应用的私有目录，Codex 和 Abaqus 都看不到；`uv tool install` 的工具环境默认位于 `AppData` 下，会受这种重定向影响。

## 开发与测试

```bash
uv sync
```

在项目目录中开发时，上面的命令行工具前加 `uv run` 即可运行，例如 `uv run abaqus-mcp-doctor --verify-connection`。

测试连接真实的 Abaqus/CAE，运行前先在 CAE 中启动桥接。测试会建立名为 `McpTestShort`、`McpTestLong` 的临时模型和作业，作业文件写在 `.work\abaqus-runs\tests`，结束后删除这两个模型和作业并恢复原工作目录。`test_python2_abaqus_gets_a_clear_error` 需要 Abaqus 2021 安装在 `C:\SIMULIA\Commands2021`，用它检查 Python 2 版 Abaqus 的报错。

```bash
uv run pytest
```

构建 Windows 发行包，生成 `dist\abaqus-mcp-server.exe` 和 `dist\abaqus-mcp-vX.Y.Z-windows-x64.zip`（附 `.sha256`）：

```bash
powershell -ExecutionPolicy Bypass -File scripts/build_windows_release.ps1
```

用打包好的 exe 运行同一套测试，把路径换成 exe 的实际位置：

```bash
ABAQUS_MCP_TEST_SERVER="C:/path/to/abaqus-mcp/dist/abaqus-mcp-server.exe" uv run pytest
```

检查远程停止（会停止桥接）：

```bash
uv run pytest -m stops_bridge
```
