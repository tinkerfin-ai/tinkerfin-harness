# 术语表

[Documentation](index.md) · [English](../en/glossary.md)


| 名称 | 简单理解 |
| --- | --- |
| Agent | 调用模型和工具完成任务的程序 |
| Graph | Agent 实际执行的工作流 |
| Runtime | 按已保存的智能体配置执行调用与流式运行的可复用对象 |
| Plan Mode | 执行前进行需求澄清与计划审批的工作流 |
| stream | 运行过程中连续产生的数据 |
| namespace | 由应用选择的业务隔离范围 |
| RunIdentity | 包含 `namespace`、`thread_id` 和 `run_id` 的框架身份 |
| thread | 一个 namespace 内可继续的对话 |
| run | thread 内的一次语义执行 |
| graph namespace | 执行 Graph 内的位置，与业务 namespace 分开 |
| Trace | 对话、模型调用、工具和子智能体的执行记录 |
| AG-UI | 前端和 Agent 交换运行事件的协议 |
| SSE | 服务端持续向浏览器发送事件的一种 HTTP 格式 |
| Messaging | 保存并投递事件流的组件 |
| Sandbox | Agent 可执行命令、操作文件的隔离环境 |
| Notifications | 面向独立监听者的资源变化提示广播 |
| Gateway | 组合已授权的运行命令、持久输出与浏览器通知 |
