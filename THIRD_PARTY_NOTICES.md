# 来源与许可

本项目由以下两个 MIT 许可的项目合并而成，两者的版权声明均保留在 [LICENSE](LICENSE) 中。

## Abaqus-Control-MCP 2.0.0

*   仓库：<https://github.com/Whfkl/Abaqus-Control-MCP>
*   版本：v2.0.0，提交 `53d32434481abca1d42d32653f32acf0d02559f5`
*   许可：MIT，Copyright (c) 2026 Abaqus Control MCP Contributors
*   采用的部分：本地 socket 桥接与图形界面主线程调度、执行 ID、持久执行命名空间与 `result` 清理、基于 AST 的错误诊断、排队超时取消与结果未知语义、作业状态证据合并、ODB 数值查询、原生图片返回、调试日志、命令行诊断工具、Windows 独立 exe 构建流程。

## CAE-Agent-Hub Abaqus MCP 5.0.0

*   仓库：<https://github.com/Cai-aa/CAE-Agent-Hub>（`MCP/Abaqus` 目录）
*   版本：5.0.0，提交 `5bd51ff41cf39f5300451684fd90039ce9167c10`
*   许可：MIT，Copyright (c) 2026
*   采用的部分：工具名 `check_abaqus_connection`、`execute_script`、`get_model_info`、`list_jobs`、`submit_job`、`get_odb_info`、`get_viewport_image`，各工具的单次 `timeout` 参数，MCP 资源 `abaqus://status`、`abaqus://session-telemetry`、`abaqus://agent-instructions`，建模工作建议，ping 中的工作目录与桥接运行信息，Bridge Status 菜单，远程停止桥接，可配置的调试日志路径与消息大小上限。
