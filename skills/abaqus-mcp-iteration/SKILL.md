---
name: abaqus-mcp-iteration
description: 积累 Abaqus MCP（abaqus-mcp，工具包括 ping、run_python、execute_script、get_model_info、submit_job、monitor_job_status、inspect_odb、capture_viewport 等）的使用经验，供后续持续改进这个 MCP。只要正在调用 abaqus MCP 的工具、操作 Abaqus/CAE、编写或调试 Abaqus Python、检查有限元模型、提交作业或读取 ODB，就要使用本 skill：一旦遇到缺少需要的工具、发现某个工具有更好的实现方式、MCP 报错或行为与说明不符、遇到问题并找到解决方法、用户讲述自己的 Abaqus 或 MCP 使用经验，就当场写入一条记录。用户要求根据积累的反馈改进、迭代、升级 abaqus-mcp，或者要查看、整理、上报这些记录时，同样使用本 skill。
---

# Abaqus MCP 迭代记录

每次实际使用 Abaqus MCP 都会暴露它的不足：缺少的工具、不够好的返回值、只有试过才知道的 Abaqus 行为。这些信息只存在于当次对话里，对话结束就丢失。本 skill 把它们写成本地记录文件，以后迭代 MCP 时逐条处理。

记录工作附带在用户的任务里完成：当场写入，写完继续原来的任务，在回复中用一句话说明记录文件的路径。

## 记录目录

目录由环境变量 `ABAQUS_MCP_FEEDBACK_DIR` 指定，未设置时为用户主目录下的 `.abaqus-mcp/feedback`（Windows 上是 `%USERPROFILE%\.abaqus-mcp\feedback`）。目录不存在就新建。

查询环境变量：Bash 用 `printenv ABAQUS_MCP_FEEDBACK_DIR`，PowerShell 用 `$env:ABAQUS_MCP_FEEDBACK_DIR`。输出为空就使用默认目录。

记录只保存在本机，可以写入模型名、路径等具体信息，便于以后复现。不要写入密码、令牌、账号等凭据。

## 需要记录的情况

| 类别 | 写入时机 | 例子 |
|---|---|---|
| `missing-tool` | 想完成某件事，MCP 没有对应工具，只能用 `run_python` 拼代码，或者根本做不到 | 想检查哪些区域没有分配截面，只能自己遍历 `part.cells` 和 `sectionAssignments` |
| `tool-improvement` | 现有工具能用，但参数、返回内容、默认值或实现方式有更好的做法 | `get_model_info` 不返回网格单元数量，检查网格时还要再调用一次 `run_python` |
| `bug` | MCP 报错、卡住、返回错误结果，或者行为与工具说明不符，当时没有解决 | 读取中文路径下的 `.msg` 文件出现 `UnicodeDecodeError` |
| `solved-problem` | 遇到问题，经过排查找到了解决方法；这个方法下次还用得上 | 作业状态一直停在 `SUBMITTED`，原因是 CAE 只在内核空闲时更新状态，改为多次短调用查询后解决 |
| `user-tip` | 用户讲述自己的经验、习惯做法或对 MCP 的意见 | 用户说 `job.kill()` 在自己的环境里无效，要用 `abaqus terminate job=名称` |

调试模型和检查模型时得到的经验同样按上表归类：可以做成工具的检查方法归入 `missing-tool`，排查后找到原因的问题归入 `solved-problem`。

不需要记录的情况：

*   自己代码里的笔误、一次就改好的普通语法错误。
*   正常完成、没有任何波折的操作。
*   只与当前模型的数值有关、换一个模型就没有参考价值的内容，例如某个零件的材料参数取多少。

判断标准是：这条信息能否改变 MCP 的代码、工具说明，或者改变下一个智能体使用 Abaqus 的做法。能改变就记录。

## 写入步骤

1.  **确定目录**：按上面的方法得到记录目录。

2.  **查找重复**：在记录目录中按工具名、错误类型和关键词搜索已有记录。
    *   已有同一问题的 `open` 记录：在它的“出现记录”一节追加一行，`occurrences` 加一，更新 `updated`，补充新的信息。不再新建文件。
    *   已有同一问题的 `resolved` 记录但问题再次出现：把 `status` 改回 `open`，在“出现记录”中写明在哪个版本再次出现。
    *   没有重复：新建文件。

3.  **核对现有工具**：写“建议改进”之前，把当前可用的 abaqus 工具的说明和参数逐个看一遍，确认建议的功能是否已经存在。例如停止作业已经由 `monitor_job_status(job_name, terminate=true)` 提供。功能已经存在时，按实际情况改写记录：智能体没有找到它，就写成 `tool-improvement`，建议改进工具说明或智能体工作规则；它的行为不对，就写成 `bug`。

4.  **新建文件**：文件名为 `YYYYMMDD-HHMMSS-<类别>-<英文短名>.md`，例如 `20261026-140312-missing-tool-unassigned-sections.md`。英文短名用小写字母和连字符，概括问题内容。一个文件只写一个问题。

5.  **填写版本**：`mcp_version` 取 `ping` 结果中的 `session.bridge.version`，`abaqus_version` 取 `session.abaqus_version`。本次对话还没有调用过 `ping` 且桥接可用时，调用一次；桥接不可用时填 `未知`。

6.  **告知用户**：在回复中写一句“已记录到 `<文件路径>`”，然后继续原来的任务。

时间使用本地时间并带时区，可以用 Bash 的 `date -Iseconds` 或 PowerShell 的 `Get-Date -Format o` 获取。

## 记录格式

````markdown
---
category: missing-tool
status: open
created: 2026-10-26T14:03:12+08:00
updated: 2026-10-26T14:03:12+08:00
occurrences: 1
source: agent
verified: true
tools: [run_python, get_model_info]
mcp_version: 1.0.0
abaqus_version: 2024
client: Claude Code
---

# 用一句话写明问题或经验

## 场景

当时在做什么任务，进行到哪一步。

## 现象

实际调用的工具和关键参数、返回的 `executionId`、错误类型、错误原文中的关键几行。

## 当时的做法

采用的替代办法或解决办法，附上可以直接复用的完整代码。没有解决就写“未解决”，并写明已经排除了哪些原因。

## 建议改进

对 MCP 的具体建议：新增什么工具、需要哪些参数、返回哪些字段；现有工具改成什么做法；工具说明或智能体工作规则里加哪一条。

## 出现记录

- 2026-10-26 Claude Code：首次记录。
````

字段说明：

*   `status`：`open`（待处理）、`resolved`（已处理）、`rejected`（决定不处理）。
*   `source`：`agent` 表示智能体自己遇到，`user` 表示用户提供。
*   `verified`：这条结论是否经过实际调用验证。用户提供的经验在没有验证之前填 `false`。
*   `tools`：涉及的 MCP 工具名；只涉及 Abaqus 本身时写 `[]`。
*   `client`：当前使用的 AI 客户端，例如 Claude Code、Codex。

内容要求：

*   写可以复现的事实。代码片段要完整，复制过去就能运行。
*   错误信息保留原文中的关键行，不要改写。
*   用户提供的经验要如实转述用户的说法，自己的推测写在“建议改进”里并注明是推测。
*   能用一两次只读的 MCP 调用验证用户经验时，先验证，再把 `verified` 改为 `true`，把验证过程写进“现象”。会修改模型或提交作业的验证，先征得用户同意。

## 改进 MCP

用户要求根据记录改进、迭代或升级 abaqus-mcp 时，阅读 [references/iteration.md](references/iteration.md)，按其中的流程处理记录。

## 上报到 GitHub

用户想把记录提交给上游仓库 <https://github.com/LuckYang1/abaqus-mcp> 时：

1.  根据记录起草 issue 的标题和正文，删去用户的私有路径、模型名、公司或项目信息。
2.  把草稿完整展示给用户，得到明确同意后再用 `gh issue create --repo LuckYang1/abaqus-mcp` 提交。
3.  在记录的 frontmatter 中加上 `issue: <issue 链接>`。
