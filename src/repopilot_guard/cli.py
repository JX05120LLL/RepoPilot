"""安全优先的 RepoPilot Guard 骨架 CLI 入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from threading import Event, Thread
from pathlib import Path
from typing import Callable, Sequence
from uuid import uuid4

from pydantic import ValidationError

from repopilot_guard import __version__
from repopilot_guard.capabilities import CapabilityPolicy, CapabilityScope
from repopilot_guard.config import AppSettings, ComponentCheck, LocalStateSettings, sanitized_settings_error
from repopilot_guard.coordinator import TaskCoordinator
from repopilot_guard.context import ContextChunkStore, ContextIndexer, ContextLoader, ContextRetriever, ManagedDocumentStore
from repopilot_guard.document_indexing import index_uploaded_document
from repopilot_guard.evaluation import BaselineValidator, EvaluationCatalog, EvaluationRunner, FixtureBuilder
from repopilot_guard.graph import GraphRunner, SqliteCheckpointStore, create_live_graph
from repopilot_guard.models import TaskMode, TaskOperation, TaskRequest, WorkspaceMode, WorkspaceSelection, default_output_root
from repopilot_guard.mcp import McpCapabilityRegistry, McpConfigError, McpConfigLoader
from repopilot_guard.mcp_runtime import MAX_MCP_INPUT_CHARS, McpRuntime, McpRuntimeError
from repopilot_guard.permissions import FULL_ACCESS_CONFIRMATION, PermissionGrant, PermissionMode
from repopilot_guard.plugins import PluginError, PluginRegistry
from repopilot_guard.preflight import PreflightInspector
from repopilot_guard.project_diagnostics import diagnose_project
from repopilot_guard.project_registry import ProjectRegistry
from repopilot_guard.providers import OpenAICompatibleProvider
from repopilot_guard.qdrant_bootstrap import QdrantBootstrapper, check_qdrant_health
from repopilot_guard.skills import SkillError, SkillRegistry
from repopilot_guard.task_store import StoredTask, TaskStore
from repopilot_guard.workspace import GitClient, GitCommandError, WorkspaceManager


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="repopilot-guard", description="RepoPilot：安全、可审计的本地 Coding Agent")
    parser.add_argument("--version", action="version", version=f"RepoPilot {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    welcome_parser = subparsers.add_parser("welcome", help="查看本机项目状态和推荐下一步，不调用模型或修改仓库")
    welcome_parser.add_argument("--state-db", type=Path, help="SQLite 项目状态库路径")
    subparsers.add_parser("doctor", help="检查配置、Qdrant 与本地状态库")
    subparsers.add_parser("bootstrap-qdrant", help="幂等创建 Qdrant Collection 和 Payload 索引")
    evaluate_parser = subparsers.add_parser("evaluate", help="准备并校验可重放 Java/Maven 评测 fixture")
    evaluate_subparsers = evaluate_parser.add_subparsers(dest="evaluate_command", required=True)
    evaluate_prepare = evaluate_subparsers.add_parser("prepare", help="为 15 条评测任务创建独立 Git 基线")
    evaluate_prepare.add_argument("--output", required=True, type=Path, help="必须是不存在或为空的新评测目录")
    evaluate_prepare.add_argument("--catalog", type=Path, default=Path("evaluation/tasks.json"))
    evaluate_baseline = evaluate_subparsers.add_parser("validate-baseline", help="不调用模型，验证修复前 Maven 基线")
    evaluate_baseline.add_argument("--fixtures", required=True, type=Path, help="evaluate prepare 的输出目录")
    evaluate_baseline.add_argument("--output", required=True, type=Path, help="必须是不存在或为空的新证据目录")
    evaluate_baseline.add_argument("--catalog", type=Path, default=Path("evaluation/tasks.json"))
    baseline_selection = evaluate_baseline.add_mutually_exclusive_group(required=True)
    baseline_selection.add_argument("--all", action="store_true", help="验证所有已声明 baseline_status 的任务")
    baseline_selection.add_argument("--task-id", action="append", help="验证一个或多个任务 ID，可重复指定")
    evaluate_run = evaluate_subparsers.add_parser("run", help="以真实模型运行 fixture 并生成实际结果报告")
    evaluate_run.add_argument("--fixtures", required=True, type=Path, help="evaluate prepare 的输出目录")
    evaluate_run.add_argument("--output", required=True, type=Path, help="必须是不存在或为空的新结果目录")
    evaluate_run.add_argument("--catalog", type=Path, default=Path("evaluation/tasks.json"))
    task_selection = evaluate_run.add_mutually_exclusive_group(required=True)
    task_selection.add_argument("--all", action="store_true", help="显式运行全部 15 项，可能消耗模型额度")
    task_selection.add_argument("--task-id", action="append", help="运行一个或多个任务 ID，可重复指定")
    evaluate_run.add_argument("--approval", choices=["manual", "auto"], default="manual", help="auto 会在 fixture 内自动通过两级审批")
    evaluate_run.add_argument("--state-db", type=Path)
    api_parser = subparsers.add_parser("api", help="启动仅本机访问的桌面端后端")
    api_subparsers = api_parser.add_subparsers(dest="api_command", required=True)
    api_serve = api_subparsers.add_parser("serve", help="监听 127.0.0.1 的 FastAPI/SSE 服务")
    api_serve.add_argument("--host", default="127.0.0.1")
    api_serve.add_argument("--port", type=int, default=8765)
    api_serve.add_argument("--state-db", type=Path)

    desktop_parser = subparsers.add_parser("desktop", help="诊断 RepoPilot Desktop 预览和原生打包环境")
    desktop_subparsers = desktop_parser.add_subparsers(dest="desktop_command", required=True)
    desktop_subparsers.add_parser("doctor", help="只读检查 Vite/Tauri、Rust 和 Windows 链接器依赖")
    desktop_subparsers.add_parser("paths", help="只读显示桌面端配置和状态目录")

    project_parser = subparsers.add_parser("project", help="管理已授权的本地项目目录")
    project_subparsers = project_parser.add_subparsers(dest="project_command", required=True)
    project_add = project_subparsers.add_parser("add", help="添加本地项目")
    project_add.add_argument("--path", required=True, type=Path)
    project_add.add_argument("--name")
    project_add.add_argument("--state-db", type=Path)
    project_list = project_subparsers.add_parser("list", help="列出本地项目")
    project_list.add_argument("--state-db", type=Path)
    project_doctor = project_subparsers.add_parser("doctor", help="诊断项目是否适合安全隔离修复或完全本机控制")
    project_doctor.add_argument("--project-id", required=True)
    project_doctor.add_argument("--state-db", type=Path)
    project_remove = project_subparsers.add_parser("remove", help="移除项目记录，不删除目录")
    project_remove.add_argument("--project-id", required=True)
    project_remove.add_argument("--state-db", type=Path)

    plugin_parser = subparsers.add_parser("plugin", help="管理经审计的本地 Skill/MCP 插件包")
    plugin_subparsers = plugin_parser.add_subparsers(dest="plugin_command", required=True)
    plugin_install = plugin_subparsers.add_parser("install", help="登记本地插件目录并计算完整包哈希")
    plugin_install.add_argument("--source", required=True, type=Path)
    plugin_install.add_argument("--state-db", type=Path)
    plugin_list = plugin_subparsers.add_parser("list", help="列出插件、启用状态和当前完整性结论")
    plugin_list.add_argument("--state-db", type=Path)
    plugin_enable = plugin_subparsers.add_parser("enable", help="启用已通过完整性校验的插件")
    plugin_enable.add_argument("--plugin-id", required=True)
    plugin_enable.add_argument("--state-db", type=Path)
    plugin_disable = plugin_subparsers.add_parser("disable", help="禁用插件，不删除插件目录")
    plugin_disable.add_argument("--plugin-id", required=True)
    plugin_disable.add_argument("--state-db", type=Path)
    plugin_remove = plugin_subparsers.add_parser("remove", help="移除插件登记，不删除插件目录")
    plugin_remove.add_argument("--plugin-id", required=True)
    plugin_remove.add_argument("--state-db", type=Path)
    plugin_audit = plugin_subparsers.add_parser("audit", help="查看插件安装、启停和完整性审计")
    plugin_audit.add_argument("--plugin-id")
    plugin_audit.add_argument("--limit", type=int, default=100)
    plugin_audit.add_argument("--state-db", type=Path)

    workspace_parser = subparsers.add_parser("workspace", help="管理任务隔离 worktree")
    workspace_subparsers = workspace_parser.add_subparsers(dest="workspace_command", required=True)
    prepare_parser = workspace_subparsers.add_parser("prepare", help="创建并保留 detached worktree")
    workspace_source = prepare_parser.add_mutually_exclusive_group(required=True)
    workspace_source.add_argument("--repo", type=Path, help="目标 Git 仓库路径")
    workspace_source.add_argument("--project-id", help="已注册项目 ID")
    prepare_parser.add_argument("--task", required=True, help="任务描述，仅用于审计")
    prepare_parser.add_argument("--output", type=Path, default=default_output_root(), help="任务产物目录")
    prepare_parser.add_argument(
        "--permission",
        choices=[mode.value for mode in PermissionMode],
        default=PermissionMode.SAFE.value,
        help="任务权限模式，默认 safe",
    )
    prepare_parser.add_argument(
        "--confirm-full-access",
        help=f"完全权限模式必须填写：{FULL_ACCESS_CONFIRMATION}",
    )
    prepare_parser.add_argument("--state-db", type=Path, help="项目注册表 SQLite 路径")
    prepare_parser.add_argument("--mode", choices=[mode.value for mode in WorkspaceMode], default=WorkspaceMode.WORKTREE.value)
    prepare_parser.add_argument("--task-mode", choices=[mode.value for mode in TaskMode], help="产品模式：安全隔离修复或完全本机控制")
    prepare_parser.add_argument("--start-ref", default="HEAD", help="起始分支或提交")
    prepare_parser.add_argument("--include-uncommitted-changes", action="store_true", help="显式迁移未提交改动")
    workspace_status = workspace_subparsers.add_parser("status", help="查看工作区状态")
    workspace_status.add_argument("--path", required=True, type=Path)
    workspace_branch = workspace_subparsers.add_parser("create-branch", help="在 detached worktree 创建分支")
    workspace_branch.add_argument("--path", required=True, type=Path)
    workspace_branch.add_argument("--name", required=True)

    index_parser = subparsers.add_parser("index", help="索引项目代码或研发文档")
    index_subparsers = index_parser.add_subparsers(dest="index_command", required=True)
    index_project = index_subparsers.add_parser("project", help="索引 Java/XML/Markdown/TXT")
    index_project.add_argument("--project-id", required=True)
    index_project.add_argument("--task-id", help="已登记的任务工作区；省略时索引项目 Local 根目录")
    index_project.add_argument("--repo-commit")
    index_project.add_argument("--state-db", type=Path)
    index_document = index_subparsers.add_parser("document", help="显式索引 MD/TXT 研发文档")
    index_document.add_argument("--project-id", required=True)
    index_document.add_argument("--file", required=True, type=Path)
    index_document.add_argument("--repo-commit")
    index_document.add_argument("--permission", choices=[mode.value for mode in PermissionMode], default=PermissionMode.SAFE.value)
    index_document.add_argument("--confirm-full-access")
    index_document.add_argument("--state-db", type=Path)

    document_parser = subparsers.add_parser("document", help="导入和查看受控研发文档")
    document_subparsers = document_parser.add_subparsers(dest="document_command", required=True)
    document_add = document_subparsers.add_parser("add", help="复制、索引显式选择的 MD/TXT 文档")
    document_add.add_argument("--project-id", required=True)
    document_add.add_argument("--file", required=True, type=Path)
    document_add.add_argument("--state-db", type=Path)
    document_list = document_subparsers.add_parser("list", help="查看项目已导入文档，不暴露磁盘路径")
    document_list.add_argument("--project-id", required=True)
    document_list.add_argument("--state-db", type=Path)

    search_parser = subparsers.add_parser("search", help="检索已索引代码与文档上下文")
    search_subparsers = search_parser.add_subparsers(dest="search_command", required=True)
    search_context = search_subparsers.add_parser("context", help="按项目和提交检索上下文")
    search_context.add_argument("--project-id", required=True)
    search_context.add_argument("--repo-commit", required=True)
    search_context.add_argument("--query", required=True)
    search_context.add_argument("--limit", type=int, default=8)

    skill_parser = subparsers.add_parser("skill", help="发现和检查项目/用户 Skills")
    skill_subparsers = skill_parser.add_subparsers(dest="skill_command", required=True)
    skill_list = skill_subparsers.add_parser("list", help="渐进披露 Skill 名称、描述和来源")
    skill_list.add_argument("--repo", type=Path)
    skill_list.add_argument("--user-root", action="append", type=Path, default=[])
    skill_list.add_argument("--max-chars", type=int, default=8_000)
    skill_inspect = skill_subparsers.add_parser("inspect", help="选择后加载一个 SKILL.md")
    skill_inspect.add_argument("--name", required=True)
    skill_inspect.add_argument("--repo", type=Path)
    skill_inspect.add_argument("--user-root", action="append", type=Path, default=[])

    mcp_parser = subparsers.add_parser("mcp", help="校验 MCP 配置和任务级信任策略")
    mcp_subparsers = mcp_parser.add_subparsers(dest="mcp_command", required=True)
    mcp_validate = mcp_subparsers.add_parser("validate", help="只读校验配置，不启动进程或连接网络")
    mcp_validate.add_argument("--config", required=True, type=Path)
    mcp_validate.add_argument("--scope", choices=[CapabilityScope.PROJECT.value, CapabilityScope.USER.value], default=CapabilityScope.PROJECT.value)
    mcp_validate.add_argument("--permission", choices=[mode.value for mode in PermissionMode], default=PermissionMode.SAFE.value)
    mcp_validate.add_argument("--confirm-full-access")
    mcp_validate.add_argument("--approve-risk", action="store_true", help="安全模式下批准当前配置的网络/写入风险")
    mcp_probe = mcp_subparsers.add_parser("probe", help="真实连接、握手、发现工具并执行 Ping")
    _add_mcp_runtime_arguments(mcp_probe)
    mcp_call = mcp_subparsers.add_parser("call", help="显式调用一个已发现的 MCP 工具")
    _add_mcp_runtime_arguments(mcp_call)
    mcp_call.add_argument("--tool", required=True, help="命名空间工具名，例如 mcp__docs__search")
    mcp_arguments = mcp_call.add_mutually_exclusive_group()
    mcp_arguments.add_argument("--arguments", help="内联 JSON 对象；Windows PowerShell 推荐使用 --arguments-file")
    mcp_arguments.add_argument("--arguments-file", type=Path, help="UTF-8 JSON 文件，最大 64 KiB")

    task_parser = subparsers.add_parser("task", help="日常 Coding Agent 任务：启动、追踪、审批和归档")
    task_subparsers = task_parser.add_subparsers(dest="task_command", required=True)
    task_list = task_subparsers.add_parser("list", help="列出持久化任务摘要，不读取模型上下文或仓库路径")
    task_list.add_argument("--limit", type=int, default=50, help="返回最近 1-200 个任务，默认 50")
    task_list.add_argument("--include-archived", action="store_true", help="同时显示已归档任务")
    task_list.add_argument("--state-db", type=Path)
    task_start = task_subparsers.add_parser("start", help="以安全隔离或完全本机控制模式启动任务")
    task_source = task_start.add_mutually_exclusive_group(required=True)
    task_source.add_argument("--repo", type=Path, help="已授权的 Git 仓库路径")
    task_source.add_argument("--project-id", help="已注册项目 ID")
    task_start.add_argument("--task", required=True, help="问题描述、修复目标或代码评审请求")
    task_start.add_argument("--operation", choices=[operation.value for operation in TaskOperation], default=TaskOperation.CHANGE.value, help="任务类型：change 会申请执行审批，research 只输出计划和报告")
    task_start.add_argument("--task-mode", choices=[mode.value for mode in TaskMode], default=TaskMode.SAFE_ISOLATED.value)
    task_start.add_argument("--confirm-full-access", help=f"完全本机控制必须填写：{FULL_ACCESS_CONFIRMATION}")
    task_start.add_argument("--start-ref", default="HEAD", help="安全隔离修复使用的 Git 起始分支或提交")
    task_start.add_argument("--include-uncommitted-changes", action="store_true", help="显式将未提交改动带入隔离工作区")
    task_start.add_argument("--approve-mcp-tool", action="append", default=[], help="显式授权本任务使用的只读 MCP capability ID，可重复提供")
    task_start.add_argument("--thread-id", help="用于恢复和审计的稳定任务线程 ID")
    task_start.add_argument("--output", type=Path, default=default_output_root(), help="任务证据与报告产物目录")
    task_start.add_argument("--state-db", type=Path, help="SQLite checkpoint 路径")
    task_status = task_subparsers.add_parser("status", help="读取任务状态、计划摘要和下一步操作，不调用模型")
    task_status.add_argument("--thread-id", required=True)
    task_status.add_argument("--state-db", type=Path)
    task_events = task_subparsers.add_parser("events", help="按游标读取已脱敏的持久化证据事件")
    task_events.add_argument("--thread-id", required=True)
    task_events.add_argument("--after-sequence", type=int, default=0, help="只返回该序号之后的事件")
    task_events.add_argument("--limit", type=int, default=100, help="返回 1-500 个事件，默认 100")
    task_events.add_argument("--state-db", type=Path)
    task_decide = task_subparsers.add_parser("decide", help="批准、要求重写或拒绝当前审批关卡")
    task_decide.add_argument("--thread-id", required=True)
    task_decide.add_argument("--decision", choices=["approve", "revise", "reject"], required=True)
    task_decide.add_argument("--comment", help="要求重写时给出具体反馈，最多 2000 字符")
    task_decide.add_argument("--state-db", type=Path)
    task_artifacts = task_subparsers.add_parser("artifacts", help="列出任务的可审计产物，不读取正文")
    task_artifacts.add_argument("--thread-id", required=True)
    task_artifacts.add_argument("--state-db", type=Path)
    task_artifact = task_subparsers.add_parser("artifact", help="读取经哈希校验的计划、Diff、验证或报告产物")
    task_artifact.add_argument("--thread-id", required=True)
    task_artifact.add_argument("--kind", required=True, help="例如 plan_markdown、git_diff、verification 或 report")
    task_artifact.add_argument("--version", type=int, help="读取指定不可变历史版本，必须为正整数")
    task_artifact.add_argument("--state-db", type=Path)
    task_archive = task_subparsers.add_parser("archive", help="归档终态任务，保留 checkpoint、证据和产物")
    task_archive.add_argument("--thread-id", required=True)
    task_archive.add_argument("--state-db", type=Path)

    agent_parser = subparsers.add_parser("agent", help="运行可恢复的只读 Coding Agent")
    agent_subparsers = agent_parser.add_subparsers(dest="agent_command", required=True)
    agent_plan = agent_subparsers.add_parser("plan", help="研究代码并生成待确认的修改计划")
    agent_source = agent_plan.add_mutually_exclusive_group(required=True)
    agent_source.add_argument("--repo", type=Path)
    agent_source.add_argument("--project-id")
    agent_plan.add_argument("--task", required=True)
    agent_plan.add_argument("--operation", choices=[operation.value for operation in TaskOperation], default=TaskOperation.CHANGE.value, help="任务类型：change 会申请执行审批，research 只输出计划和报告")
    agent_plan.add_argument("--output", type=Path, default=default_output_root())
    agent_plan.add_argument("--thread-id")
    agent_plan.add_argument("--state-db", type=Path)
    agent_plan.add_argument("--permission", choices=[mode.value for mode in PermissionMode], default=PermissionMode.SAFE.value)
    agent_plan.add_argument(
        "--approve-mcp-tool",
        action="append",
        default=[],
        help="显式授权本任务使用的只读 MCP capability ID，可重复提供；安全模式默认不连接 MCP。",
    )
    agent_plan.add_argument("--confirm-full-access")
    agent_plan.add_argument("--mode", choices=[mode.value for mode in WorkspaceMode], default=WorkspaceMode.WORKTREE.value)
    agent_plan.add_argument("--task-mode", choices=[mode.value for mode in TaskMode], help="产品模式：安全隔离修复或完全本机控制")
    agent_plan.add_argument("--start-ref", default="HEAD")
    agent_plan.add_argument("--include-uncommitted-changes", action="store_true")
    agent_resume = agent_subparsers.add_parser("resume", help="确认或拒绝已暂停的计划")
    agent_resume.add_argument("--thread-id", required=True)
    agent_resume.add_argument("--approved", choices=["true", "false"], required=True)
    agent_resume.add_argument("--state-db", type=Path)

    inspect_parser = subparsers.add_parser("inspect", help="执行只读 Java/Maven 预检")
    inspect_parser.add_argument("--repo", required=True, type=Path, help="目标 Git 仓库路径")

    run_parser = subparsers.add_parser("run", help="运行兼容的只读干跑生命周期")
    run_parser.add_argument("--repo", required=True, type=Path, help="目标 Git 仓库路径")
    run_parser.add_argument("--task", required=True, help="Bug 描述或小型改动请求")
    run_parser.add_argument("--output", type=Path, default=default_output_root(), help="证据产物目录")
    run_parser.add_argument("--max-steps", type=int, default=12, help="未来 Agent 的最大步骤数")
    return parser


def _add_mcp_runtime_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--server", required=True)
    parser.add_argument("--scope", choices=[CapabilityScope.PROJECT.value, CapabilityScope.USER.value], default=CapabilityScope.PROJECT.value)
    parser.add_argument("--workspace-root", type=Path, help="STDIO MCP 的受控工作目录")
    parser.add_argument("--permission", choices=[mode.value for mode in PermissionMode], default=PermissionMode.SAFE.value)
    parser.add_argument("--confirm-full-access")
    parser.add_argument("--approve-risk", action="store_true", help="批准当前任务的网络/写入风险")
    parser.add_argument("--force", action="store_true", help="手动跳过本进程内的连接熔断冷却")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "welcome":
        return _run_welcome(args)
    if args.command == "doctor":
        return _run_doctor()
    if args.command == "bootstrap-qdrant":
        return _run_bootstrap_qdrant()
    if args.command == "evaluate":
        return _run_evaluation(args)
    if args.command == "api":
        return _run_api(args)
    if args.command == "desktop":
        return _run_desktop(args)
    if args.command == "project":
        return _run_project(args)
    if args.command == "plugin":
        return _run_plugin(args)
    if args.command == "workspace":
        return _run_workspace(args)
    if args.command == "index":
        return _run_index(args)
    if args.command == "document":
        return _run_document(args)
    if args.command == "search":
        return _run_search(args)
    if args.command == "skill":
        return _run_skill(args)
    if args.command == "mcp":
        return _run_mcp(args)
    if args.command == "task":
        return _run_task(args)
    if args.command == "agent":
        return _run_agent(args)
    if args.command == "inspect":
        result = PreflightInspector().inspect(args.repo)
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0 if result.ready else 2

    request = TaskRequest(
        repository=args.repo,
        description=args.task,
        output_root=args.output,
        max_steps=args.max_steps,
    )
    result = TaskCoordinator().run_dry_run(request)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.verdict.value == "UNVERIFIED" else 2


def _run_workspace(args: argparse.Namespace) -> int:
    if args.workspace_command == "status":
        try:
            return _print_json_result(WorkspaceManager().status(args.path))
        except GitCommandError:
            return _print_json_result({"status": "BLOCKED", "code": "WORKSPACE_STATUS_FAILED", "message": "无法读取工作区状态。"}, 2)
    if args.workspace_command == "create-branch":
        try:
            return _print_json_result(WorkspaceManager().create_branch(args.path, args.name))
        except GitCommandError:
            return _print_json_result({"status": "BLOCKED", "code": "BRANCH_CREATION_FAILED", "message": "无法创建分支。"}, 2)
    return _run_workspace_prepare(args)


def _run_evaluation(args: argparse.Namespace) -> int:
    if args.evaluate_command == "prepare":
        try:
            output = args.output.expanduser().resolve()
            if output.exists() and any(output.iterdir()):
                return _print_json_result({"status": "BLOCKED", "code": "EVALUATION_OUTPUT_NOT_EMPTY", "message": "评测目录必须为空，避免覆盖已有证据。"}, 2)
            results = FixtureBuilder(EvaluationCatalog(args.catalog)).prepare_all(output)
        except (OSError, ValueError):
            return _print_json_result({"status": "BLOCKED", "code": "EVALUATION_FIXTURE_PREPARE_FAILED", "message": "无法创建可重放评测 fixture。"}, 2)
        return _print_json_result({"status": "READY", "fixture_count": len(results), "report": str(output / "fixtures.json"), "results": [item.to_dict() for item in results]})
    if args.evaluate_command == "validate-baseline":
        try:
            selected = None if args.all else set(args.task_id or [])
            results = BaselineValidator(EvaluationCatalog(args.catalog)).validate(
                args.fixtures,
                args.output,
                task_ids=selected,
            )
        except (OSError, ValueError):
            return _print_json_result(
                {
                    "status": "BLOCKED",
                    "code": "EVALUATION_BASELINE_VALIDATION_FAILED",
                    "message": "基线验证未完成；请检查 fixture、Git、Maven、JDK 和空输出目录。",
                },
                2,
            )
        matched = sum(item.matched_expectation for item in results)
        all_matched = matched == len(results)
        return _print_json_result(
            {
                "status": "READY" if all_matched else "FAILED",
                "validation_count": len(results),
                "matched_expectations": matched,
                "report": str(args.output.expanduser().resolve() / "baseline-report.json"),
                "results": [item.to_dict() for item in results],
            },
            0 if all_matched else 2,
        )
    if args.evaluate_command != "run":
        return 2
    try:
        settings = AppSettings()
        checkpoint = SqliteCheckpointStore(_state_db_path(args.state_db))
        try:
            runner = GraphRunner(create_live_graph(settings, checkpoint.checkpointer), default_budget=settings.task_budget())
            selected = None if args.all else set(args.task_id or [])
            results = EvaluationRunner(EvaluationCatalog(args.catalog), runner).run(
                args.fixtures,
                args.output,
                task_ids=selected,
                approval=args.approval,
            )
        finally:
            checkpoint.close()
    except (OSError, ValueError):
        return _print_json_result({"status": "BLOCKED", "code": "EVALUATION_RUN_FAILED", "message": "评测未完成；请检查模型、Embedding、Qdrant、Git 基线和结果目录。"}, 2)
    matched = sum(item.matched_expectation for item in results)
    return _print_json_result({"status": "READY", "run_count": len(results), "matched_expectations": matched, "report": str(args.output.expanduser().resolve() / "evaluation-report.json"), "results": [item.to_dict() for item in results]})


def _run_api(args: argparse.Namespace) -> int:
    if args.host != "127.0.0.1":
        return _print_json_result({"status": "BLOCKED", "code": "LOCALHOST_ONLY", "message": "桌面后端只允许监听 127.0.0.1。"}, 2)
    import uvicorn
    from repopilot_guard.api import create_app

    settings = AppSettings()
    state_path = _state_db_path(args.state_db)
    store = SqliteCheckpointStore(state_path)
    registry = ProjectRegistry(state_path)
    try:
        qdrant_configuration = settings.qdrant_bootstrap_check()
        if qdrant_configuration.ready:
            qdrant_health = QdrantBootstrapper.from_settings(settings).health_check
        else:
            qdrant_health = lambda: qdrant_configuration
        app = create_app(
            GraphRunner(create_live_graph(settings, store.checkpointer), default_budget=settings.task_budget()),
            registry,
            default_output_root(),
            runtime_health_checks=lambda: (settings.chat_check(), settings.embedding_check(), qdrant_health()),
        )
        uvicorn.run(app, host=args.host, port=args.port)
    finally:
        registry.close()
        store.close()
    return 0


def _run_welcome(args: argparse.Namespace) -> int:
    """提供产品级首次使用摘要，只读取本地项目登记和项目预检信息。"""

    try:
        registry = ProjectRegistry(_state_db_path(args.state_db))
        try:
            projects = registry.list()
            summaries = [_welcome_project_summary(project) for project in projects]
        finally:
            registry.close()
    except (OSError, ValueError):
        return _print_json_result(
            {
                "status": "BLOCKED",
                "code": "WELCOME_STATE_UNAVAILABLE",
                "message": "无法读取本地项目状态库；未调用模型、未扫描或修改项目代码。",
                "next_action": {
                    "type": "CHECK_RUNTIME",
                    "command": "repopilot-guard doctor",
                },
            },
            2,
        )

    if not summaries:
        return _print_json_result(
            {
                "status": "READY",
                "code": "WELCOME_PROJECT_REQUIRED",
                "message": "尚未登记本地项目。先授权一个项目目录，再开始代码研究或修复。",
                "project_count": 0,
                "projects": [],
                "next_action": {
                    "type": "REGISTER_PROJECT",
                    "command": 'repopilot-guard project add --path <项目目录> --name "服务名称"',
                },
                "runtime_check": "repopilot-guard doctor",
            }
        )

    selected = summaries[0]
    return _print_json_result(
        {
            "status": "READY",
            "code": "WELCOME_READY",
            "message": "已根据最近使用的项目生成推荐操作；开始任务前仍会重新检查权限、Git 基线和运行依赖。",
            "project_count": len(summaries),
            "selected_project": selected,
            "projects": summaries,
            "next_action": _welcome_next_action(selected),
            "runtime_check": "repopilot-guard doctor",
        }
    )


def _welcome_project_summary(project: object) -> dict[str, object]:
    """将完整项目诊断压缩为首次使用所需的公开摘要，避免输出仓库绝对路径。"""

    # ProjectRecord 的来源固定为 ProjectRegistry；保持 object 形参可避免 CLI 公开内部类型。
    diagnosis = diagnose_project(project)  # type: ignore[arg-type]
    project_payload = diagnosis["project"]
    task_modes = diagnosis["task_modes"]
    java_profile = diagnosis["profiles"]["java_maven"]
    assert isinstance(project_payload, dict)
    assert isinstance(task_modes, dict)
    assert isinstance(java_profile, dict)
    safe_mode = task_modes["safe_isolated"]
    full_mode = task_modes["full_local"]
    assert isinstance(safe_mode, dict)
    assert isinstance(full_mode, dict)
    return {
        "project_id": project_payload["project_id"],
        "display_name": project_payload["display_name"],
        "is_git_repository": project_payload["is_git_repository"],
        "recommended_task_mode": diagnosis["recommended_task_mode"],
        "safe_isolated": {"status": safe_mode["status"], "code": safe_mode["code"]},
        "full_local": {"status": full_mode["status"], "code": full_mode["code"]},
        "java_maven": {"status": java_profile["status"], "code": java_profile["code"]},
    }


def _welcome_next_action(project: dict[str, object]) -> dict[str, str]:
    project_id = str(project["project_id"])
    if project["recommended_task_mode"] == TaskMode.SAFE_ISOLATED.value:
        return {
            "type": "START_SAFE_ISOLATED_TASK",
            "command": f'repopilot-guard task start --project-id {project_id} --task "<描述代码任务>"',
        }
    return {
        "type": "REVIEW_FULL_LOCAL_RISK",
        "command": f"repopilot-guard project doctor --project-id {project_id}",
    }


def _run_desktop(args: argparse.Namespace) -> int:
    """检查桌面开发和打包前置条件，不启动子进程或修改本机环境。"""

    if args.desktop_command == "paths":
        runtime_dir = _desktop_runtime_dir()
        return _print_json_result(
            {
                "status": "READY",
                "code": "DESKTOP_RUNTIME_PATHS_READY",
                "runtime_dir": str(runtime_dir),
                "config_file": str(runtime_dir / ".env"),
                "state_db": str(runtime_dir / "state.sqlite"),
                "message": "本命令只计算路径，不创建目录、读取配置或输出密钥。",
            }
        )
    if args.desktop_command != "doctor":
        return 2
    repository_root = Path(__file__).resolve().parents[2]
    desktop_root = repository_root / "desktop"
    tauri_config = desktop_root / "src-tauri" / "tauri.conf.json"
    checks = [
        _desktop_file_check("desktop_frontend", desktop_root / "package.json", "已找到桌面前端配置。"),
        _desktop_file_check("tauri_config", tauri_config, "已找到 Tauri 配置。"),
        _desktop_backend_delivery_check(desktop_root, tauri_config),
        _desktop_command_check("node", ("node.exe", "node"), "Node.js 可用。", "未找到 Node.js，无法运行 Vite。"),
        _desktop_command_check("npm", ("npm.cmd", "npm"), "npm 可用。", "未找到 npm，无法安装或构建桌面前端。"),
        _desktop_command_check("uv", ("uv.exe", "uv"), "uv 可用。", "未找到 uv，开发模式无法自动启动 Python 后端。"),
        _desktop_command_check("rustc", ("rustc.exe", "rustc"), "Rust 编译器可用。", "未找到 rustc，无法构建 Tauri。"),
        _desktop_command_check("cargo", ("cargo.exe", "cargo"), "Cargo 可用。", "未找到 cargo，无法构建 Tauri。"),
        _desktop_command_check("linker", ("link.exe",), "MSVC 链接器可用。", "未找到 link.exe；请安装 Visual Studio Build Tools 的 C++ 工作负载。"),
        _desktop_command_check("resource_compiler", ("rc.exe",), "Windows 资源编译器可用。", "未找到 rc.exe；请安装 Windows SDK。"),
        _desktop_command_check("manifest_tool", ("mt.exe",), "Windows Manifest 工具可用。", "未找到 mt.exe；请安装 Windows SDK。"),
    ]
    check_by_component = {check.component: check for check in checks}
    preview_components = ("desktop_frontend", "tauri_config", "node", "npm", "uv")
    package_components = preview_components + ("backend_sidecar", "rustc", "cargo", "linker", "resource_compiler", "manifest_tool")
    preview_ready = all(check_by_component[name].ready for name in preview_components)
    package_ready = all(check_by_component[name].ready for name in package_components)
    payload = {
        "status": "READY" if package_ready else "BLOCKED",
        "code": "DESKTOP_PACKAGE_READY" if package_ready else "DESKTOP_PACKAGE_PREREQUISITES_MISSING",
        "preview": {
            "status": "READY" if preview_ready else "BLOCKED",
            "message": "浏览器预览可启动。" if preview_ready else "浏览器预览依赖不完整。",
            "command": ".\\scripts\\start-desktop-preview.ps1" if preview_ready else None,
        },
        "package": {
            "status": "READY" if package_ready else "BLOCKED",
            "message": "Tauri 原生包可尝试构建。" if package_ready else "Tauri 原生包依赖不完整；未执行 cargo 或安装操作。",
            "command": "cd desktop; npm.cmd run tauri:build" if package_ready else None,
        },
        "checks": [check.to_dict() for check in checks],
    }
    return _print_json_result(payload, 0 if package_ready else 2)


def _desktop_runtime_dir() -> Path:
    override = os.environ.get("REPOPILOT_DESKTOP_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    identifier = "com.repopilot.desktop"
    if os.name == "nt" and os.environ.get("APPDATA"):
        return (Path(os.environ["APPDATA"]) / identifier).resolve()
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support" / identifier).resolve()
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return (data_home / identifier).expanduser().resolve()


def _desktop_file_check(component: str, path: Path, ready_message: str) -> ComponentCheck:
    return ComponentCheck(
        component=component,
        ready=path.is_file(),
        code=f"{component.upper()}_{'READY' if path.is_file() else 'MISSING'}",
        message=ready_message if path.is_file() else "桌面项目文件缺失。",
    )


def _desktop_backend_delivery_check(desktop_root: Path, tauri_config: Path) -> ComponentCheck:
    """确认 release 安装包会带上 Agent 后端，而不是只打出 WebView 外壳。"""

    expected_relative_path = "binaries/repopilot-guard.exe"
    try:
        configuration = json.loads(tauri_config.read_text(encoding="utf-8"))
        resources = configuration.get("bundle", {}).get("resources", [])
    except (OSError, json.JSONDecodeError, AttributeError):
        resources = []
    configured = isinstance(resources, list) and expected_relative_path in resources
    expected_binary = desktop_root / "src-tauri" / expected_relative_path
    ready = configured and expected_binary.is_file()
    if ready:
        return ComponentCheck(
            component="backend_sidecar",
            ready=True,
            code="BACKEND_SIDECAR_READY",
            message="安装包将从资源目录启动 RepoPilot Agent 后端 sidecar。",
        )
    return ComponentCheck(
        component="backend_sidecar",
        ready=False,
        code="BACKEND_SIDECAR_MISSING",
        message="未找到已配置的 Python Agent sidecar；先运行 scripts/build-desktop-backend-sidecar.ps1。",
    )


def _desktop_command_check(component: str, candidates: tuple[str, ...], ready_message: str, missing_message: str) -> ComponentCheck:
    version = _desktop_command_version(*candidates)
    return ComponentCheck(
        component=component,
        ready=version is not None,
        code=f"{component.upper()}_{'READY' if version is not None else 'MISSING'}",
        message=ready_message if version is not None else missing_message,
    )


def _desktop_command_version(*candidates: str) -> str | None:
    """使用固定 `--version` 参数探测可执行文件，不经 shell 运行。"""

    for candidate in candidates:
        for executable in _desktop_executable_candidates(candidate):
            if Path(executable).name.lower() in {"link.exe", "rc.exe", "mt.exe"}:
                # MSVC/SDK 工具不提供统一的 --version 契约；标准目录中的文件存在即可交给 Cargo 最终验证。
                return executable
            try:
                completed = subprocess.run(
                    (executable, "--version"),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if completed.returncode == 0:
                return (completed.stdout or completed.stderr).strip()[:200] or candidate
    return None


def _desktop_executable_candidates(candidate: str) -> tuple[str, ...]:
    """从 PATH 和 Windows 标准安装目录发现桌面构建工具。"""

    discovered: list[Path] = []
    path_match = shutil.which(candidate)
    if path_match:
        discovered.append(Path(path_match))
    if os.name == "nt":
        discovered.extend(_windows_build_tool_candidates(candidate))

    unique: list[str] = []
    seen: set[str] = set()
    for path in discovered:
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            continue
        key = os.path.normcase(str(resolved))
        if not resolved.is_file() or key in seen:
            continue
        seen.add(key)
        unique.append(str(resolved))
    return tuple(unique)


def _windows_build_tool_candidates(candidate: str) -> tuple[Path, ...]:
    """只扫描 Visual Studio 与 Windows SDK 的固定安装层级，不执行安装器。"""

    name = candidate.lower()
    matches: list[Path] = []
    program_roots = {
        Path(value)
        for variable in ("ProgramFiles", "ProgramFiles(x86)")
        if (value := os.environ.get(variable))
    }
    if name == "link.exe":
        for root in program_roots:
            visual_studio = root / "Microsoft Visual Studio"
            matches.extend(visual_studio.glob("*/*/VC/Tools/MSVC/*/bin/Hostx64/x64/link.exe"))
    elif name in {"rc.exe", "mt.exe"}:
        for root in program_roots:
            windows_sdk = root / "Windows Kits" / "10" / "bin"
            matches.extend(windows_sdk.glob(f"*/x64/{name}"))
    return tuple(sorted((path for path in matches if path.is_file()), reverse=True))


def _run_workspace_prepare(args: argparse.Namespace) -> int:
    try:
        permission, workspace_mode = _mode_context(args)
    except ValueError:
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "code": "FULL_ACCESS_CONFIRMATION_REQUIRED",
                    "message": f"完全权限模式必须明确确认：{FULL_ACCESS_CONFIRMATION}",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2

    registry: ProjectRegistry | None = None
    try:
        registry = ProjectRegistry(_state_db_path(args.state_db))
        repository = registry.get(args.project_id).root_path if args.project_id else args.repo
        request = TaskRequest(
            repository=repository,
            description=args.task,
            output_root=args.output,
            project_id=args.project_id,
            workspace_selection=WorkspaceSelection(
                mode=workspace_mode,
                start_ref=args.start_ref,
                    include_uncommitted_changes=args.include_uncommitted_changes,
                ),
                )
        result = TaskCoordinator(project_registry=registry).prepare_workspace(request, permission)
    except (GitCommandError, OSError, ValueError):
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "code": "WORKSPACE_PREPARATION_FAILED",
                    "message": "无法创建任务 worktree；源仓库未被自动修改或清理。",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    finally:
        if registry and 'result' not in locals():
            registry.close()
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    if registry:
        registry.close()
    return 0 if result.status == "READY" else 2


def _run_project(args: argparse.Namespace) -> int:
    registry = ProjectRegistry(_state_db_path(args.state_db))
    try:
        if args.project_command == "add":
            return _print_json_result({"status": "READY", "project": registry.add(args.path, args.name).to_dict()})
        if args.project_command == "list":
            return _print_json_result({"status": "READY", "projects": [item.to_dict() for item in registry.list()]})
        if args.project_command == "doctor":
            return _print_json_result(diagnose_project(registry.get(args.project_id)))
        removed = registry.remove(args.project_id)
        return _print_json_result(
            {"status": "READY" if removed else "BLOCKED", "code": "PROJECT_REMOVED" if removed else "PROJECT_NOT_FOUND"},
            0 if removed else 2,
        )
    except ValueError as error:
        return _print_json_result({"status": "BLOCKED", "code": str(error), "message": "项目目录不可用。"}, 2)
    finally:
        registry.close()


def _run_plugin(args: argparse.Namespace) -> int:
    """插件管理只操作本地登记和审计，不执行插件脚本、不连接 MCP。"""

    registry = PluginRegistry(_state_db_path(args.state_db))
    try:
        if args.plugin_command == "install":
            return _print_json_result({"status": "READY", "plugin": registry.install(args.source).to_dict()})
        if args.plugin_command == "list":
            return _print_json_result({"status": "READY", "plugins": [item.to_dict() for item in registry.list()]})
        if args.plugin_command == "enable":
            return _print_json_result({"status": "READY", "plugin": registry.enable(args.plugin_id).to_dict()})
        if args.plugin_command == "disable":
            return _print_json_result({"status": "READY", "plugin": registry.disable(args.plugin_id).to_dict()})
        if args.plugin_command == "remove":
            registry.remove(args.plugin_id)
            return _print_json_result({"status": "READY", "code": "PLUGIN_REMOVED", "directory_deleted": False})
        return _print_json_result({"status": "READY", "events": list(registry.audit(args.plugin_id, args.limit))})
    except PluginError as error:
        return _print_json_result({"status": "BLOCKED", "code": error.code, "message": error.message}, 2)
    finally:
        registry.close()


def _run_index(args: argparse.Namespace) -> int:
    try:
        settings = AppSettings()
        provider = OpenAICompatibleProvider(settings)
        check = provider.embedding_check()
        if not check.ready:
            return _print_checks((check,))
        registry = ProjectRegistry(_state_db_path(args.state_db))
        project = registry.get(args.project_id)
        workspace = registry.get_workspace(args.task_id, project.project_id) if args.index_command == "project" and args.task_id else project.root_path
        registry.close()
        commit = args.repo_commit or GitClient().head_commit(project.root_path)
        loader = ContextLoader()
        permission = PermissionGrant.safe()
        if args.index_command == "document":
            permission = PermissionGrant(PermissionMode(args.permission), args.confirm_full_access)
            chunks = loader.load_document(
                args.file,
                project_root=project.root_path,
                project_id=project.project_id,
                repo_commit=commit,
                permission=permission,
            )
            skipped_files = 0
        else:
            chunks, skipped_files = loader.load_project(
                workspace,
                project_id=project.project_id,
                repo_commit=commit,
                permission=permission,
            )
        bootstrapper = QdrantBootstrapper.from_settings(settings)
        health = bootstrapper.health_check()
        if not health.ready:
            return _print_checks((health,))
        store = ContextChunkStore(settings.state_db_path)
        try:
            result = ContextIndexer(bootstrapper.client, provider.create_embeddings(), store).index(chunks, skipped_files)
        finally:
            store.close()
        return _print_json_result(result.to_dict(), 0 if result.status == "READY" else 2)
    except (ValueError, GitCommandError):
        return _print_json_result({"status": "BLOCKED", "code": "CONTEXT_INDEX_INPUT_INVALID", "message": "索引参数、项目或文档不可用。"}, 2)


def _run_document(args: argparse.Namespace) -> int:
    """产品级文档入口：只使用 RepoPilot 管理副本，结果不泄露原始文件路径。"""

    registry = ProjectRegistry(_state_db_path(args.state_db))
    try:
        # 先验证项目存在，避免在无效 project_id 下创建无主文档副本。
        registry.get(args.project_id)
        if args.document_command == "list":
            documents = ManagedDocumentStore(registry.database_path).list_documents(project_id=args.project_id)
            return _print_json_result(
                {"status": "READY", "documents": [document.to_dict() for document in documents]}
            )
        result = index_uploaded_document(registry, args.project_id, args.file)
        return _print_json_result(result, 0 if result.get("status") == "READY" else 2)
    except ValueError as error:
        code = str(error)
        if not code.startswith("DOCUMENT_"):
            code = "DOCUMENT_PROJECT_NOT_FOUND"
        return _print_json_result({"status": "BLOCKED", "code": code, "message": "项目或研发文档不可用。"}, 2)
    finally:
        registry.close()


def _run_search(args: argparse.Namespace) -> int:
    try:
        settings = AppSettings()
        provider = OpenAICompatibleProvider(settings)
        check = provider.embedding_check()
        if not check.ready:
            return _print_checks((check,))
        bootstrapper = QdrantBootstrapper.from_settings(settings)
        health = bootstrapper.health_check()
        if not health.ready:
            return _print_checks((health,))
        result = ContextRetriever(bootstrapper.client, provider.create_embeddings()).search(
            args.query,
            project_id=args.project_id,
            repo_commit=args.repo_commit,
            limit=args.limit,
        )
        return _print_json_result(result.to_dict(), 0 if result.status == "READY" else 2)
    except ValueError:
        return _print_json_result({"status": "BLOCKED", "code": "CONTEXT_SEARCH_INPUT_INVALID", "message": "检索参数无效。"}, 2)


def _run_skill(args: argparse.Namespace) -> int:
    """Skill 命令只发现或读取 SKILL.md，不执行其中脚本。"""

    try:
        registry = SkillRegistry.discover(project_root=args.repo, user_roots=args.user_root)
        if args.skill_command == "list":
            return _print_json_result(registry.catalog(args.max_chars).to_dict())
        loaded = registry.load(args.name)
        return _print_json_result({"status": "READY", "skill": loaded.to_dict()})
    except (OSError, SkillError, ValueError) as error:
        code = error.code if isinstance(error, SkillError) else "SKILL_INPUT_INVALID"
        return _print_json_result(
            {"status": "BLOCKED", "code": code, "message": "Skill 不可用；未执行任何 Skill 脚本或工具。"},
            2,
        )


def _run_mcp(args: argparse.Namespace) -> int:
    """配置校验保持纯只读；probe/call 才显式建立真实连接。"""

    if args.mcp_command != "validate":
        try:
            payload, exit_code = asyncio.run(_run_mcp_runtime(args))
            return _print_json_result(payload, exit_code)
        except (McpConfigError, McpRuntimeError, ValueError) as error:
            return _print_json_result(
                {
                    "status": "BLOCKED",
                    "code": _mcp_error_code(error),
                    "message": "MCP 连接或调用失败；未报告为成功。",
                },
                2,
            )

    try:
        configuration = McpConfigLoader.load(args.config, expected_scope=CapabilityScope(args.scope))
        registry = McpCapabilityRegistry(configuration)
        permission = PermissionGrant(PermissionMode(args.permission), args.confirm_full_access)
        policy = CapabilityPolicy()
        decisions: list[dict[str, object]] = []
        blocked = False
        missing_env: set[str] = set()
        for server in configuration.servers:
            descriptor = registry.capabilities.get(f"mcp_server__{server.name}")
            if descriptor is None:
                raise ValueError("MCP_SERVER_CAPABILITY_MISSING")
            decision = policy.decide(descriptor, permission, approved=args.approve_risk)
            required_env = set(server.env)
            if server.bearer_token_env:
                required_env.add(server.bearer_token_env)
            unavailable = sorted(name for name in required_env if not os.getenv(name))
            missing_env.update(unavailable)
            server_allowed = decision.allowed and not unavailable
            blocked = blocked or (server.enabled and not server_allowed)
            decisions.append(
                {
                    "server": server.name,
                    "enabled": server.enabled,
                    "connection_status": "CONFIGURED_NOT_CONNECTED",
                    "decision": decision.to_dict(),
                    "missing_env": unavailable,
                    "effective_ready": server_allowed if server.enabled else False,
                }
            )
        return _print_json_result(
            {
                "status": "BLOCKED" if blocked else "READY",
                "code": "MCP_POLICY_BLOCKED" if blocked else "MCP_CONFIGURATION_VALID",
                "message": "配置校验完成；本命令不会启动 MCP 进程或发起网络连接。",
                "configuration": configuration.to_dict(),
                "decisions": decisions,
                "missing_env": sorted(missing_env),
            },
            2 if blocked else 0,
        )
    except (McpConfigError, ValueError) as error:
        code = error.code if isinstance(error, McpConfigError) else str(error)
        if not code or len(code) > 80:
            code = "MCP_CONFIGURATION_INVALID"
        return _print_json_result(
            {"status": "BLOCKED", "code": code, "message": "MCP 配置或权限无效；未建立任何外部连接。"},
            2,
        )


async def _run_mcp_runtime(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    configuration = McpConfigLoader.load(args.config, expected_scope=CapabilityScope(args.scope))
    permission = PermissionGrant(PermissionMode(args.permission), args.confirm_full_access)
    arguments: dict[str, object] | None = None
    if args.mcp_command == "call":
        encoded_arguments = args.arguments or "{}"
        if args.arguments_file:
            argument_path = args.arguments_file.expanduser().resolve()
            if not argument_path.is_file() or argument_path.stat().st_size > MAX_MCP_INPUT_CHARS:
                raise ValueError("MCP_INVALID_TOOL_ARGUMENTS")
            try:
                encoded_arguments = argument_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as error:
                raise ValueError("MCP_INVALID_TOOL_ARGUMENTS") from error
        try:
            decoded = json.loads(encoded_arguments)
        except json.JSONDecodeError as error:
            raise ValueError("MCP_INVALID_TOOL_ARGUMENTS") from error
        if not isinstance(decoded, dict):
            raise ValueError("MCP_INVALID_TOOL_ARGUMENTS")
        arguments = decoded

    runtime = McpRuntime(configuration, workspace_root=args.workspace_root)
    connected = await runtime.connect(
        args.server,
        permission,
        approved=args.approve_risk,
        force=args.force,
    )
    if connected.status != "READY":
        await runtime.close()
        return (
            {
                "status": "BLOCKED",
                "code": connected.code,
                "connection": connected.to_dict(),
                "events": [event.to_dict() for event in runtime.events],
            },
            2,
        )

    if args.mcp_command == "probe":
        operation: dict[str, object] = await runtime.ping(args.server)
    else:
        assert arguments is not None
        operation = (
            await runtime.call_tool(
                args.tool,
                arguments,
                permission,
                approved=args.approve_risk,
            )
        ).to_dict()
    closed = await runtime.disconnect(args.server)
    status = str(operation.get("status", "BLOCKED"))
    return (
        {
            "status": status,
            "code": operation.get("code", "MCP_OPERATION_FAILED"),
            "connection": connected.to_dict(),
            "operation": operation,
            "closed": closed.to_dict(),
            "events": [event.to_dict() for event in runtime.events],
        },
        0 if status == "READY" else 2,
    )


def _mcp_error_code(error: BaseException) -> str:
    code = getattr(error, "code", str(error))
    if not isinstance(code, str) or not code or len(code) > 80:
        return "MCP_RUNTIME_FAILED"
    if not all(character.isupper() or character.isdigit() or character == "_" for character in code):
        return "MCP_RUNTIME_FAILED"
    return code


def _run_task(args: argparse.Namespace) -> int:
    """面向终端用户的任务入口；只输出可审阅摘要，不倾倒完整图状态或模型上下文。"""

    registry: ProjectRegistry | None = None
    try:
        if args.task_command in {"list", "events", "archive"}:
            return _run_task_store_command(args)
        if args.task_command in {"artifacts", "artifact"}:
            return _run_task_artifact_command(args)
        if args.task_command == "status":
            return _run_task_status(args)
        # 在构造模型、Qdrant 与图依赖前先验证高风险模式确认，避免无效请求产生外部调用。
        permission: PermissionGrant | None = None
        workspace_mode: WorkspaceMode | None = None
        if args.task_command == "start":
            permission, workspace_mode = _mode_context(args)
        settings = AppSettings()
        state_path = _state_db_path(args.state_db)
        checkpoint_store = SqliteCheckpointStore(state_path)
        task_store = TaskStore(state_path)
        try:
            runner = GraphRunner(create_live_graph(settings, checkpoint_store.checkpointer), default_budget=settings.task_budget())
            request: TaskRequest | None = None
            if args.task_command == "decide":
                if args.comment and len(args.comment) > 2000:
                    raise ValueError("TASK_DECISION_COMMENT_TOO_LARGE")
                try:
                    result, runtime_error = _run_cli_execution(
                        task_store,
                        args.thread_id,
                        lambda: runner.resume(args.thread_id, decision=args.decision, comment=args.comment),
                    )
                except ValueError as error:
                    # 兼容第一版未写入任务索引、但仍存在 LangGraph checkpoint 的旧任务。
                    if str(error) != "TASK_NOT_FOUND":
                        raise
                    try:
                        result = runner.resume(args.thread_id, decision=args.decision, comment=args.comment)
                    except Exception as runtime_error:
                        return _print_json_result(
                            _cli_runtime_failure_summary(args.thread_id, f"TASK_RUNTIME_FAILED: {type(runtime_error).__name__}"),
                            2,
                        )
                    runtime_error = None
                if runtime_error:
                    return _print_json_result(
                        _cli_runtime_failure_summary(args.thread_id, runtime_error),
                        2,
                    )
                assert result is not None
            else:
                assert permission is not None and workspace_mode is not None
                if args.project_id:
                    registry = ProjectRegistry(state_path)
                    repository = registry.get(args.project_id).root_path
                else:
                    repository = args.repo
                request = TaskRequest(
                    repository=repository,
                    description=args.task,
                    output_root=args.output,
                    project_id=args.project_id,
                    workspace_selection=WorkspaceSelection(
                        mode=workspace_mode,
                        start_ref=args.start_ref,
                        include_uncommitted_changes=args.include_uncommitted_changes,
                    ),
                    approved_mcp_tools=tuple(args.approve_mcp_tool),
                    operation=TaskOperation(args.operation),
                )
                thread_id = args.thread_id or str(uuid4())
                task_store.create(
                    thread_id=thread_id,
                    task_id=request.task_id,
                    project_id=request.project_id,
                    repository=request.repository,
                    output_root=request.output_root,
                    task_mode=TaskMode(args.task_mode).value,
                    permission_mode=permission.mode.value,
                    workspace_mode=request.workspace_selection.mode.value,
                    display_title=request.description,
                )
                result, runtime_error = _run_cli_execution(
                    task_store,
                    thread_id,
                    lambda: runner.run(request, thread_id, permission),
                )
                if runtime_error:
                    return _print_json_result(
                        _cli_runtime_failure_summary(thread_id, runtime_error),
                        2,
                    )
                assert result is not None
            _project_cli_task_result(task_store, result, request)
        finally:
            task_store.close()
            checkpoint_store.close()
        return _print_json_result(_task_summary(result), 0 if result.status in {"WAITING_APPROVAL", "REPORT"} else 2)
    except (ValueError, GitCommandError, ValidationError) as error:
        code = str(error)
        return _print_json_result(
            {
                "status": "BLOCKED",
                "code": code if code == "THREAD_ID_ALREADY_EXISTS" else "TASK_CONFIGURATION_OR_INPUT_INVALID",
                "message": "任务配置、权限确认、项目或工作区参数无效；未开始模型或写入操作。",
            },
            2,
        )
    finally:
        if registry:
            registry.close()


def _start_cli_lease_heartbeat(
    store: TaskStore,
    thread_id: str,
    stop: Event,
    *,
    interval_seconds: float = 30.0,
) -> Thread:
    """为同步 CLI 进程维持任务租约，允许另一个进程实时读取 RUNNING。"""

    def renew() -> None:
        while not stop.wait(interval_seconds):
            try:
                store.renew_lease(thread_id)
            except ValueError:
                return

    worker = Thread(target=renew, name=f"repopilot-cli-lease-{thread_id}", daemon=True)
    worker.start()
    return worker


def _run_cli_execution(
    store: TaskStore,
    thread_id: str,
    operation: Callable[[], object],
) -> tuple[object | None, str | None]:
    """执行 CLI Graph，并把异常收敛为可审计的 BLOCKED，而不是让进程崩溃。"""

    store.begin_execution(thread_id)
    heartbeat_stop = Event()
    heartbeat = _start_cli_lease_heartbeat(store, thread_id, heartbeat_stop)
    try:
        return operation(), None
    except Exception as error:
        error_code = f"TASK_RUNTIME_FAILED: {type(error).__name__}"
        store.mark_runtime_failure(thread_id, error_code)
        return None, error_code
    finally:
        heartbeat_stop.set()
        heartbeat.join(timeout=1)


def _cli_runtime_failure_summary(thread_id: str, error_code: str) -> dict[str, object]:
    """运行时异常只返回类型和下一步，不暴露异常正文、路径或环境变量。"""

    return {
        "status": "BLOCKED",
        "code": "TASK_RUNTIME_FAILED",
        "thread_id": thread_id,
        "error_summary": error_code,
        "message": "任务运行时失败；已写入 SQLite 任务索引和脱敏证据，请查看 task events。",
        "next_action": {
            "type": "INSPECT_TASK_EVENTS",
            "command": f"repopilot-guard task events --thread-id {thread_id}",
        },
    }


def _project_cli_task_result(store: TaskStore, result: object, request: TaskRequest | None) -> None:
    """把 CLI Graph 快照投影到任务索引，使状态、API 和产物审阅共用同一证据模型。"""

    raw = result.to_dict()  # type: ignore[union-attr]
    if not isinstance(raw, dict):
        raise ValueError("TASK_RESULT_UNAVAILABLE")
    state = raw.get("state")
    if not isinstance(state, dict):
        raise ValueError("TASK_RESULT_UNAVAILABLE")
    projected_state = dict(state)
    if request is not None:
        # Graph 正常快照已包含这些字段；在同步测试或旧图实现中缺失时以请求为准补齐。
        projected_state.setdefault("repository", str(request.repository))
        projected_state.setdefault("output_root", str(request.output_root))
        projected_state.setdefault("project_id", request.project_id)
        projected_state.setdefault("workspace_mode", request.workspace_selection.mode.value)
    elif not projected_state.get("repository") or not projected_state.get("output_root"):
        # 旧 checkpoint 若没有路径元数据，不能猜测并写入当前目录。
        return
    projected = dict(raw)
    projected["state"] = projected_state
    store.sync_graph_result(projected)


def _run_task_store_command(args: argparse.Namespace) -> int:
    """管理 SQLite 任务索引；不构造模型、Qdrant 或 LangGraph。"""

    store = TaskStore(_state_db_path(args.state_db))
    try:
        if args.task_command == "list":
            if not 1 <= args.limit <= 200:
                raise ValueError("TASK_LIST_LIMIT_INVALID")
            store.reap_expired_leases()
            tasks = [
                _stored_task_summary(task)
                for task in store.list(args.limit, include_archived=args.include_archived)
            ]
            return _print_json_result(
                {
                    "status": "READY",
                    "code": "TASK_LIST_READY",
                    "count": len(tasks),
                    "include_archived": args.include_archived,
                    "tasks": tasks,
                }
            )
        if args.task_command == "events":
            if args.after_sequence < 0:
                raise ValueError("TASK_EVENT_CURSOR_INVALID")
            if not 1 <= args.limit <= 500:
                raise ValueError("TASK_EVENT_LIMIT_INVALID")
            events = store.events_after(args.thread_id, args.after_sequence, limit=args.limit)
            return _print_json_result(
                {
                    "status": "READY",
                    "code": "TASK_EVENTS_READY",
                    "thread_id": args.thread_id,
                    "after_sequence": args.after_sequence,
                    "next_sequence": events[-1].sequence if events else args.after_sequence,
                    "count": len(events),
                    "events": [event.to_dict() for event in events],
                }
            )
        task = store.archive(args.thread_id)
        return _print_json_result(
            {"status": "READY", "code": "TASK_ARCHIVED", "task": _stored_task_summary(task)}
        )
    except ValueError as error:
        code = str(error)
        allowed_codes = {
            "TASK_NOT_FOUND",
            "TASK_NOT_ARCHIVABLE",
            "TASK_LIST_LIMIT_INVALID",
            "TASK_EVENT_CURSOR_INVALID",
            "TASK_EVENT_LIMIT_INVALID",
        }
        return _print_json_result(
            {
                "status": "BLOCKED",
                "code": code if code in allowed_codes else "TASK_STORE_OPERATION_FAILED",
                "message": "任务列表、证据事件或归档操作无效；未删除 checkpoint、证据或产物。",
            },
            2,
        )
    finally:
        store.close()


def _run_task_status(args: argparse.Namespace) -> int:
    """优先返回 Graph 详情；依赖配置异常时退回可信 SQLite 任务索引。"""

    try:
        state_path = _state_db_path(args.state_db)
        task_store = TaskStore(state_path)
    except (OSError, ValueError, ValidationError, sqlite3.Error):
        return _print_json_result(
            {
                "status": "BLOCKED",
                "code": "TASK_STATUS_UNAVAILABLE",
                "message": "本机任务状态库不可读取；未调用模型或执行工具。",
            },
            2,
        )

    checkpoint_store: SqliteCheckpointStore | None = None
    try:
        indexed = task_store.get(args.thread_id)
        try:
            settings = AppSettings()
            checkpoint_store = SqliteCheckpointStore(state_path)
            runner = GraphRunner(
                create_live_graph(settings, checkpoint_store.checkpointer),
                default_budget=settings.task_budget(),
            )
            result = runner.get(args.thread_id)
            _project_cli_task_result(task_store, result, None)
            payload = _task_summary(result)
            payload["code"] = "TASK_STATUS_READY"
            payload["source"] = "graph_checkpoint"
            return _print_json_result(
                payload,
                0 if result.status in {"WAITING_APPROVAL", "REPORT"} else 2,
            )
        except (OSError, ValueError, KeyError, ValidationError, sqlite3.Error):
            return _print_json_result(
                _indexed_task_status(indexed),
                0 if indexed.status in {"WAITING_APPROVAL", "REPORT"} else 2,
            )
    except ValueError:
        return _print_json_result(
            {
                "status": "BLOCKED",
                "code": "TASK_NOT_FOUND",
                "message": "未找到对应任务；未调用模型或执行工具。",
            },
            2,
        )
    finally:
        if checkpoint_store is not None:
            checkpoint_store.close()
        task_store.close()


def _indexed_task_status(task: StoredTask) -> dict[str, object]:
    """SQLite 回退只报告已持久化事实，不推测计划、验证或 interrupt 类型。"""

    next_action = {
        "type": "OPEN_TASK_EVENTS",
        "command": f"repopilot-guard task events --thread-id {task.thread_id} --after-sequence 0",
    }
    return {
        "status": task.status,
        "code": "TASK_STATUS_INDEX_ONLY",
        "source": "task_index",
        "detail_available": False,
        "thread_id": task.thread_id,
        "trace_id": task.trace_id,
        "task_id": task.task_id,
        "display_title": task.display_title,
        "project_id": task.project_id,
        "task_mode": task.task_mode,
        "pending_approval": task.pending_approval,
        "verdict": task.verdict,
        "error_summary": task.error_summary,
        "workspace": {
            "mode": task.workspace_mode,
            "path": None,
            "base_commit": None,
            "permission": task.permission_mode,
        },
        "plan": None,
        "verification": None,
        "evidence": None,
        "next_action": next_action,
        "message": "已从本机任务索引读取状态；Graph 详情因配置或 checkpoint 不可用而省略。",
    }


def _stored_task_summary(task: StoredTask) -> dict[str, object]:
    """任务列表只暴露管理字段，不输出仓库和产物绝对路径。"""

    return {
        "thread_id": task.thread_id,
        "trace_id": task.trace_id,
        "task_id": task.task_id,
        "display_title": task.display_title,
        "project_id": task.project_id,
        "task_mode": task.task_mode,
        "status": task.status,
        "pending_approval": task.pending_approval,
        "verdict": task.verdict,
        "error_summary": task.error_summary,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "archived_at": task.archived_at,
    }


def _run_task_artifact_command(args: argparse.Namespace) -> int:
    """任务产物只允许按 SQLite 登记的 kind 读取，并在输出前复验不可变哈希。"""

    store = TaskStore(_state_db_path(args.state_db))
    try:
        if args.task_command == "artifacts":
            artifacts = [artifact.to_dict() for artifact in store.artifacts(args.thread_id)]
            return _print_json_result({"status": "READY", "thread_id": args.thread_id, "artifacts": artifacts})
        if args.version is not None and args.version <= 0:
            raise ValueError("TASK_ARTIFACT_VERSION_INVALID")
        if args.version is None:
            artifact, content = store.read_artifact(args.thread_id, args.kind)
        else:
            artifact, content = store.read_artifact_version(args.thread_id, args.kind, args.version)
        return _print_json_result(
            {
                "status": "READY",
                "thread_id": args.thread_id,
                "artifact": artifact.to_dict(),
                "content": content,
            }
        )
    except ValueError as error:
        code = str(error)
        if not code.startswith("TASK_ARTIFACT_") and code != "TASK_NOT_FOUND":
            code = "TASK_ARTIFACT_UNAVAILABLE"
        return _print_json_result(
            {"status": "BLOCKED", "code": code, "message": "任务产物不可读取、超出限制或完整性校验失败。"},
            2,
        )
    finally:
        store.close()


def _task_summary(result: object) -> dict[str, object]:
    """将 LangGraph 原始快照收敛为 CLI 审阅卡片，避免输出完整消息和工具正文。"""

    raw = result.to_dict()  # type: ignore[union-attr]
    state = raw.get("state") if isinstance(raw, dict) else None
    if not isinstance(state, dict):
        return {"status": "BLOCKED", "code": "TASK_STATE_UNAVAILABLE", "message": "任务状态不可读取。"}

    events = state.get("tool_events")
    event_types = [str(item.get("type", "UNKNOWN")) for item in events if isinstance(item, dict)] if isinstance(events, list) else []
    plan = state.get("plan")
    verification = state.get("verification_result")
    return {
        "status": raw.get("status"),
        "thread_id": raw.get("thread_id"),
        "task_id": raw.get("task_id"),
        "verdict": raw.get("verdict"),
        "pending_approval": raw.get("pending_approval", False),
        "interrupts": raw.get("interrupts", []),
        "workspace": {
            "mode": state.get("workspace_mode"),
            "path": state.get("workspace_path"),
            "base_commit": state.get("base_commit"),
            "permission": state.get("permission_mode"),
        },
        "plan": _task_plan_summary(plan),
        "verification": verification if isinstance(verification, dict) else None,
        "evidence": {"event_count": len(event_types), "recent_event_types": event_types[-12:]},
        "next_action": _task_next_action(raw, state),
    }


def _task_plan_summary(plan: object) -> dict[str, object] | None:
    if not isinstance(plan, dict):
        return None
    allowed = ("summary", "candidate_files", "steps", "verification", "assumptions", "risks", "verification_recipe", "target_test_class")
    return {key: plan[key] for key in allowed if key in plan}


def _task_next_action(raw: dict[str, object], state: dict[str, object]) -> dict[str, str]:
    if raw.get("pending_approval"):
        interrupt = raw.get("interrupts")
        interrupt_type = interrupt[0].get("type") if isinstance(interrupt, list) and interrupt and isinstance(interrupt[0], dict) else "APPROVAL_REQUIRED"
        return {
            "type": str(interrupt_type),
            "command": f"repopilot-guard task decide --thread-id {raw.get('thread_id')} --decision approve",
        }
    if raw.get("status") == "REPORT":
        return {"type": "TASK_FINISHED", "command": f"repopilot-guard task status --thread-id {raw.get('thread_id')}"}
    if raw.get("status") == "BLOCKED":
        return {"type": "CHECK_BLOCK_REASON", "command": f"repopilot-guard task status --thread-id {raw.get('thread_id')}"}
    return {"type": "TASK_STATE_UPDATED", "command": f"repopilot-guard task status --thread-id {raw.get('thread_id')}"}


def _run_agent(args: argparse.Namespace) -> int:
    try:
        settings = AppSettings()
        state_path = _state_db_path(args.state_db)
        store = SqliteCheckpointStore(state_path)
        try:
            runner = GraphRunner(create_live_graph(settings, store.checkpointer), default_budget=settings.task_budget())
            if args.agent_command == "resume":
                result = runner.resume(args.thread_id, args.approved == "true")
            else:
                permission, workspace_mode = _mode_context(args)
                registry: ProjectRegistry | None = None
                if args.project_id:
                    registry = ProjectRegistry(state_path)
                    repository = registry.get(args.project_id).root_path
                else:
                    repository = args.repo
                request = TaskRequest(
                    repository=repository,
                    description=args.task,
                    output_root=args.output,
                    project_id=args.project_id,
                    workspace_selection=WorkspaceSelection(
                        mode=workspace_mode,
                        start_ref=args.start_ref,
                        include_uncommitted_changes=args.include_uncommitted_changes,
                    ),
                    approved_mcp_tools=tuple(args.approve_mcp_tool),
                    operation=TaskOperation(args.operation),
                )
                result = runner.run(request, args.thread_id, permission)
                if registry:
                    registry.close()
        finally:
            store.close()
        return _print_json_result(result.to_dict(), 0 if result.status in {"WAITING_APPROVAL", "REPORT"} else 2)
    except (ValueError, GitCommandError, ValidationError):
        return _print_json_result(
            {"status": "BLOCKED", "code": "AGENT_CONFIGURATION_OR_INPUT_INVALID", "message": "Agent 配置、权限、项目或工作区参数无效。"},
            2,
        )


def _state_db_path(value: Path | None) -> Path:
    return value.expanduser().resolve() if value else LocalStateSettings().state_db_path.expanduser().resolve()


def _mode_context(args: argparse.Namespace) -> tuple[PermissionGrant, WorkspaceMode]:
    """高层产品模式优先；低层参数只为调试和兼容保留。"""

    task_mode = getattr(args, "task_mode", None)
    if task_mode:
        selected = TaskMode(task_mode)
        permission = PermissionGrant(PermissionMode(selected.permission_mode), getattr(args, "confirm_full_access", None))
        return permission, selected.workspace_mode
    permission = PermissionGrant(PermissionMode(args.permission), getattr(args, "confirm_full_access", None))
    return permission, WorkspaceMode(args.mode)


def _print_json_result(payload: dict[str, object], exit_code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code


def _run_doctor() -> int:
    try:
        settings = AppSettings()
    except ValidationError:
        return _print_checks((sanitized_settings_error(),))

    checks: list[ComponentCheck] = [
        settings.chat_check(),
        settings.embedding_check(),
        settings.qdrant_settings_check(),
        _state_database_check(settings.state_db_path),
    ]
    if settings.qdrant_settings_check().ready:
        # 连通性检查不发起模型调用，也不会写入 Qdrant。
        checks.append(check_qdrant_health(settings.qdrant_url))

    return _print_checks(tuple(checks))


def _run_bootstrap_qdrant() -> int:
    try:
        settings = AppSettings()
    except ValidationError:
        return _print_checks((sanitized_settings_error(),))

    bootstrap_check = settings.qdrant_bootstrap_check()
    if not bootstrap_check.ready:
        return _print_checks((bootstrap_check,))

    try:
        bootstrapper = QdrantBootstrapper.from_settings(settings)
        health = bootstrapper.health_check()
        if not health.ready:
            return _print_checks((health,))
        result = bootstrapper.bootstrap()
    except Exception:
        return _print_checks(
            (
                ComponentCheck(
                    component="qdrant_bootstrap",
                    ready=False,
                    code="QDRANT_BOOTSTRAP_FAILED",
                    message="Qdrant 初始化失败；没有删除已有 Collection 或向量。",
                ),
            )
        )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0


def _state_database_check(database_path: Path) -> ComponentCheck:
    """只检查未来状态库目录是否可写，不在 doctor 中创建文件。"""

    resolved = database_path.expanduser().resolve()
    candidate = resolved.parent
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    if candidate.exists() and os.access(candidate, os.W_OK):
        return ComponentCheck(
            component="state_database",
            ready=True,
            code="STATE_DATABASE_READY",
            message="状态库路径可用；首次运行 Graph 时会创建 SQLite 文件。",
        )
    return ComponentCheck(
        component="state_database",
        ready=False,
        code="STATE_DATABASE_UNAVAILABLE",
        message="状态库父目录不可写。",
    )


def _print_checks(checks: tuple[ComponentCheck, ...]) -> int:
    ready = all(check.ready for check in checks)
    print(
        json.dumps(
            {
                "status": "READY" if ready else "BLOCKED",
                "checks": [check.to_dict() for check in checks],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if ready else 2
