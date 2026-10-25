# 根据记录改进 abaqus-mcp

## 1. 找到仓库

当前目录或其上级目录中存在 `abaqus_mcp/server.py` 时，那就是仓库根目录。找不到时询问用户仓库位置；用户没有本地仓库时，询问是否克隆 <https://github.com/LuckYang1/abaqus-mcp>。

开始前阅读仓库的 `README.md` 和 `CLAUDE.md`（如果存在），了解当前的工具、测试方法和发布方法。

## 2. 读取并整理记录

1.  在记录目录（见 SKILL.md 的“记录目录”）中找出所有 `status: open` 的记录，逐个完整阅读。
2.  把根本原因相同的记录归为一组。例如“读取 `.msg` 出现乱码”和“读取 `.dat` 出现乱码”都来自同一处文件编码处理。
3.  按下面的顺序排列各组：
    1.  `bug`，尤其是会导致结果错误或 CAE 卡住的问题。
    2.  `occurrences` 较大、在多个客户端或多次对话中出现的问题。
    3.  `missing-tool` 和 `tool-improvement`。
    4.  只需要修改说明文字的经验。
4.  对每一组，用当前代码重新确认问题是否仍然存在：阅读相关代码；桥接可用时，用 MCP 实际复现一次。已经不存在的问题直接标为 `resolved`，在“处理”一节写明在哪个版本已经解决。
5.  把整理结果告诉用户：每组的问题、涉及的记录文件、准备怎样修改。用户指定了处理范围时，只处理指定的部分。

## 3. 决定修改方式

| 记录内容 | 修改位置 |
|---|---|
| 新增工具 | `abaqus_mcp/server.py` 中用 `@mcp.tool` 新增工具；在内核中执行的代码放进 `abaqus_mcp/kernel_code.py`，用 `render` 填入参数 |
| 改进现有工具 | `server.py` 中对应的工具函数，以及 `kernel_code.py` 中对应的代码模板 |
| 插件、桥接、线程、取消、编码问题 | `abaqus_mcp/gui_plugin.py`、`client.py`、`protocol.py` |
| 智能体使用 MCP 或 Abaqus 的规则 | `server.py` 中的 `INSTRUCTIONS`（英文，会在初始化时下发给所有客户端，并通过 `abaqus://agent-instructions` 提供） |
| 安装、配置、诊断方法 | `README.md`、`docs/windows-release.md` |

新增或修改工具时沿用现有写法：

*   每个工具都接受可选的 `timeout`，用 `_invalid_timeout` 检查。
*   返回值用 `_result` 构造，包含 `ok` 和 `executionId`；出错时 `error=True`。
*   只读的工具加上 `annotations=READ_ONLY`。
*   工具说明（`description`）写清参数含义、默认值和返回内容。
*   README 的“工具”一节同步更新。

一条记录不值得修改时，把 `status` 改为 `rejected`，在“处理”一节写明原因。

## 4. 测试

1.  修改了 `gui_plugin.py` 时，同时把三处版本号保持一致（见第 6 步），运行 `uv run abaqus-mcp-setup` 把新插件写入插件目录，再请用户重启 Abaqus/CAE 并选择 **Plug-ins → Abaqus MCP → Start MCP Bridge**。
2.  新增工具时，更新 `tests/test_live_bridge.py` 中的 `EXPECTED_TOOLS`，并为新工具或新行为编写连接真实 CAE 的测试。测试不使用 mock。
3.  运行 `uv run pytest`，全部通过后再继续。失败时修改代码并重新测试。
4.  用 MCP 按记录中的“场景”实际操作一遍，确认原来的问题已经解决。

## 5. 更新记录

处理完成的记录：

*   `status` 改为 `resolved`，更新 `updated`。
*   frontmatter 中加上 `resolved_in: <版本号>`，提交后再加上 `commit: <提交哈希>`。
*   在文件末尾加上“处理”一节，写明修改了什么、怎样验证。

```markdown
## 处理

1.0.1 新增工具 `check_sections`，返回未分配截面的区域；新增连接真实 CAE 的测试 `test_check_sections`，并按原场景复现确认。
```

## 6. 发布新版本

只在用户要求发布时执行：

1.  修改三处版本号：`pyproject.toml`、`abaqus_mcp/__init__.py`、`abaqus_mcp/gui_plugin.py`。
2.  运行 `uv lock`，然后在本机开着 CAE 运行 `uv run pytest`。
3.  提交改动并推送 `main`。
4.  创建标签 `vX.Y.Z` 并推送这个标签。GitHub Actions 会自动构建 exe 并创建 Release。
5.  把相关记录的 `resolved_in` 填为这个版本号。
