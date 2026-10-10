#!/usr/bin/env python3
"""Generate a searchable Markdown index for Python and TypeScript source symbols."""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable


SOURCE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx"}
EXCLUDED_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
}
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "websocket"}
INTERFACE_BASES = {"ABC", "Protocol", "TypedDict"}
MAX_DETAIL_LENGTH = 180


@dataclass
class Symbol:
    file: str
    line: int
    kind: str
    name: str
    scope: str = ""
    signature: str = ""
    doc: str = ""
    language: str = "Python"


@dataclass
class Interface:
    file: str
    line: int
    kind: str
    name: str
    scope: str = ""
    signature: str = ""
    doc: str = ""
    language: str = "Python"


@dataclass
class ApiEndpoint:
    file: str
    line: int
    method: str
    path: str
    handler: str
    doc: str = ""


@dataclass
class ParseError:
    file: str
    line: int
    message: str


def compact(value: str, limit: int = MAX_DETAIL_LENGTH) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1].rstrip()}…"


def first_doc_line(value: str) -> str:
    for line in (value or "").splitlines():
        line = line.strip()
        if line:
            return compact(line)
    return ""


def safe_unparse(node: ast.AST | None) -> str:
    if node is None:
        return ""
    try:
        return compact(ast.unparse(node))
    except Exception:
        return ""


def literal_string(node: ast.AST | None) -> str:
    if node is None:
        return ""
    try:
        value = ast.literal_eval(node)
    except Exception:
        return safe_unparse(node)
    return str(value) if isinstance(value, (str, int, float)) else safe_unparse(node)


def expression_tail(node: ast.AST | None) -> str:
    if node is None:
        return ""
    value = safe_unparse(node)
    return value.rsplit(".", 1)[-1]


def target_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, (ast.Tuple, ast.List)):
        result: list[str] = []
        for item in node.elts:
            result.extend(target_names(item))
        return result
    if isinstance(node, ast.Starred):
        return target_names(node.value)
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in {"self", "cls"}:
        return [f"{node.value.id}.{node.attr}"]
    return []


def collect_router_prefixes(tree: ast.AST) -> dict[str, str]:
    prefixes: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if not isinstance(value, ast.Call) or expression_tail(value.func) != "APIRouter":
            continue
        prefix = ""
        for keyword in value.keywords:
            if keyword.arg == "prefix":
                prefix = literal_string(keyword.value)
                break
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            for name in target_names(target):
                prefixes[name] = prefix
    return prefixes


class PythonExtractor(ast.NodeVisitor):
    def __init__(self, root: Path, file: Path, tree: ast.AST) -> None:
        self.root = root
        self.file = file
        self.relative_file = file.relative_to(root).as_posix()
        self.tree = tree
        self.scope: list[tuple[str, str]] = []
        self.symbols: list[Symbol] = []
        self.interfaces: list[Interface] = []
        self.endpoints: list[ApiEndpoint] = []
        self.router_prefixes = collect_router_prefixes(tree)
        self.variable_keys: set[tuple[str, str]] = set()
        self.endpoint_keys: set[tuple[int, str, str]] = set()

    def scope_text(self, *, class_only: bool = False) -> str:
        if class_only:
            names = [name for kind, name in self.scope if kind == "class"]
        else:
            names = [name for _, name in self.scope]
        return ".".join(names)

    def in_function(self) -> bool:
        return any(kind == "function" for kind, _ in self.scope)

    def immediate_scope_kind(self) -> str:
        return self.scope[-1][0] if self.scope else "module"

    def add_symbol(
        self,
        kind: str,
        name: str,
        node: ast.AST,
        *,
        signature: str = "",
        doc: str = "",
        scope: str | None = None,
    ) -> None:
        self.symbols.append(
            Symbol(
                file=self.relative_file,
                line=getattr(node, "lineno", 1),
                kind=kind,
                name=name,
                scope=self.scope_text() if scope is None else scope,
                signature=compact(signature),
                doc=first_doc_line(doc),
            )
        )

    def add_interface(
        self,
        kind: str,
        name: str,
        node: ast.AST,
        *,
        signature: str = "",
        doc: str = "",
        scope: str | None = None,
    ) -> None:
        self.interfaces.append(
            Interface(
                file=self.relative_file,
                line=getattr(node, "lineno", 1),
                kind=kind,
                name=name,
                scope=self.scope_text() if scope is None else scope,
                signature=compact(signature),
                doc=first_doc_line(doc),
            )
        )

    def add_variable(
        self,
        name: str,
        node: ast.AST,
        *,
        annotation: ast.AST | None = None,
        value: ast.AST | None = None,
        force_kind: str | None = None,
    ) -> None:
        if name.startswith("self.") or name.startswith("cls."):
            display_name = name.split(".", 1)[1]
            scope = self.scope_text(class_only=True)
            kind = "实例变量" if name.startswith("self.") else "类变量"
        else:
            display_name = name
            scope = self.scope_text()
            if force_kind:
                kind = force_kind
            elif self.in_function():
                kind = "局部变量"
            elif self.immediate_scope_kind() == "class":
                kind = "类字段"
            elif display_name.isupper():
                kind = "常量"
            else:
                kind = "模块变量"
        key = (scope, display_name)
        if key in self.variable_keys:
            return
        self.variable_keys.add(key)
        detail = safe_unparse(annotation)
        if not detail and value is not None:
            detail = safe_unparse(value)
        self.add_symbol(kind, display_name, node, signature=detail, scope=scope)

    def add_target(
        self,
        target: ast.AST,
        *,
        annotation: ast.AST | None = None,
        value: ast.AST | None = None,
        force_kind: str | None = None,
    ) -> None:
        for name in target_names(target):
            self.add_variable(name, target, annotation=annotation, value=value, force_kind=force_kind)

    def function_signature(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
        prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
        result = f"{prefix}{node.name}({safe_unparse(node.args)})"
        if node.returns:
            result += f" -> {safe_unparse(node.returns)}"
        return result

    def add_parameters(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        arguments: list[ast.arg] = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        if node.args.vararg:
            arguments.append(node.args.vararg)
        if node.args.kwarg:
            arguments.append(node.args.kwarg)
        for argument in arguments:
            self.add_variable(
                argument.arg,
                argument,
                annotation=argument.annotation,
                force_kind="参数",
            )

    def class_interface_kind(self, node: ast.ClassDef) -> str:
        bases = {expression_tail(base) for base in node.bases}
        if "Protocol" in bases:
            return "Python Protocol"
        if "TypedDict" in bases:
            return "Python TypedDict"
        if "ABC" in bases or "ABCMeta" in bases:
            return "Python 抽象类"
        return ""

    def route_from_decorator(
        self,
        decorator: ast.AST,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        doc: str,
    ) -> None:
        call = decorator if isinstance(decorator, ast.Call) else None
        func = call.func if call else decorator
        if not isinstance(func, ast.Attribute):
            return
        method = func.attr.lower()
        owner = expression_tail(func.value)
        if method in HTTP_METHODS:
            path_node = call.args[0] if call and call.args else None
            route_path = literal_string(path_node) or "/"
            prefix = self.router_prefixes.get(owner, "")
            if route_path.startswith("/") and prefix:
                route_path = f"{prefix.rstrip('/')}{route_path}"
            self.add_endpoint(method.upper(), route_path, node.name, node, doc)
        elif method in {"command", "group"}:
            command_name = node.name.replace("_", "-")
            if call and call.args:
                command_name = literal_string(call.args[0]) or command_name
            kind = "CLI 命令组" if method == "group" else "CLI 命令"
            self.add_interface(kind, command_name, node, signature=self.function_signature(node), doc=doc)

    def add_endpoint(
        self,
        method: str,
        path: str,
        handler: str,
        node: ast.AST,
        doc: str = "",
    ) -> None:
        key = (getattr(node, "lineno", 1), method, path)
        if key in self.endpoint_keys:
            return
        self.endpoint_keys.add(key)
        self.endpoints.append(
            ApiEndpoint(
                file=self.relative_file,
                line=getattr(node, "lineno", 1),
                method=method,
                path=compact(path),
                handler=handler,
                doc=first_doc_line(doc),
            )
        )

    def visit_ClassDef(self, node: ast.ClassDef) -> Any:
        bases = ", ".join(safe_unparse(base) for base in node.bases)
        doc = ast.get_docstring(node, clean=True) or ""
        self.add_symbol("类", node.name, node, signature=bases, doc=doc)
        interface_kind = self.class_interface_kind(node)
        if interface_kind:
            self.add_interface(interface_kind, node.name, node, signature=bases, doc=doc)
        self.scope.append(("class", node.name))
        for statement in node.body:
            self.visit(statement)
        self.scope.pop()
        for decorator in node.decorator_list:
            self.visit(decorator)
        return None

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        return self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        return self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        decorators = {expression_tail(item.func if isinstance(item, ast.Call) else item) for item in node.decorator_list}
        if "property" in decorators:
            kind = "属性"
        elif self.immediate_scope_kind() == "class":
            kind = "异步方法" if isinstance(node, ast.AsyncFunctionDef) else "方法"
        else:
            kind = "异步函数" if isinstance(node, ast.AsyncFunctionDef) else "函数"
        doc = ast.get_docstring(node, clean=True) or ""
        signature = self.function_signature(node)
        self.add_symbol(kind, node.name, node, signature=signature, doc=doc)
        if "abstractmethod" in decorators:
            self.add_interface("Python 抽象方法", node.name, node, signature=signature, doc=doc)
        for decorator in node.decorator_list:
            self.route_from_decorator(decorator, node, doc)
        self.scope.append(("function", node.name))
        self.add_parameters(node)
        for default in [*node.args.defaults, *node.args.kw_defaults]:
            if default is not None:
                self.visit(default)
        for statement in node.body:
            self.visit(statement)
        self.scope.pop()
        for decorator in node.decorator_list:
            self.visit(decorator)
        return None

    def visit_Assign(self, node: ast.Assign) -> Any:
        for target in node.targets:
            self.add_target(target, value=node.value)
        if isinstance(node.value, ast.Lambda):
            names = [name for target in node.targets for name in target_names(target)]
            for name in names:
                self.add_symbol("函数", name, node, signature=f"{name}({safe_unparse(node.value.args)})")
        self.visit(node.value)
        return None

    def visit_AnnAssign(self, node: ast.AnnAssign) -> Any:
        self.add_target(node.target, annotation=node.annotation, value=node.value)
        self.visit(node.annotation)
        if node.value is not None:
            if isinstance(node.value, ast.Lambda):
                for name in target_names(node.target):
                    self.add_symbol("函数", name, node, signature=f"{name}({safe_unparse(node.value.args)})")
            self.visit(node.value)
        return None

    def visit_AugAssign(self, node: ast.AugAssign) -> Any:
        self.add_target(node.target, value=node.value)
        self.visit(node.value)
        return None

    def visit_NamedExpr(self, node: ast.NamedExpr) -> Any:
        self.add_target(node.target, value=node.value)
        self.visit(node.value)
        return None

    def visit_For(self, node: ast.For) -> Any:
        self.add_target(node.target)
        self.visit(node.iter)
        for statement in [*node.body, *node.orelse]:
            self.visit(statement)
        return None

    def visit_AsyncFor(self, node: ast.AsyncFor) -> Any:
        return self.visit_For(node)

    def visit_comprehension(self, node: ast.comprehension) -> Any:
        self.add_target(node.target)
        self.visit(node.iter)
        for condition in node.ifs:
            self.visit(condition)
        return None

    def visit_With(self, node: ast.With) -> Any:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars:
                self.add_target(item.optional_vars)
        for statement in node.body:
            self.visit(statement)
        return None

    def visit_AsyncWith(self, node: ast.AsyncWith) -> Any:
        return self.visit_With(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> Any:
        if node.name:
            self.add_variable(node.name, node)
        if node.type:
            self.visit(node.type)
        for statement in node.body:
            self.visit(statement)
        return None

    def visit_MatchAs(self, node: ast.MatchAs) -> Any:
        if node.name:
            self.add_variable(node.name, node)
        if node.pattern:
            self.visit(node.pattern)
        return None

    def visit_Call(self, node: ast.Call) -> Any:
        if isinstance(node.func, ast.Attribute) and node.func.attr in {"add_api_route", "add_route"}:
            route_path = literal_string(node.args[0] if node.args else None) or "/"
            handler = safe_unparse(node.args[1] if len(node.args) > 1 else None) or "<dynamic>"
            methods: list[str] = []
            for keyword in node.keywords:
                if keyword.arg == "methods":
                    try:
                        raw_methods = ast.literal_eval(keyword.value)
                    except Exception:
                        raw_methods = []
                    if isinstance(raw_methods, (list, tuple, set)):
                        methods.extend(str(item).upper() for item in raw_methods)
            if not methods:
                methods = ["ROUTE"]
            for method in methods:
                self.add_endpoint(method, route_path, handler, node)
        self.generic_visit(node)
        return None


TOKEN_TRANSLATIONS = {
    "account": "账户",
    "active": "活跃",
    "admin": "管理员",
    "agent": "Agent",
    "api": "API",
    "async": "异步",
    "audit": "审计",
    "auth": "鉴权",
    "baseline": "Baseline",
    "batch": "批次",
    "body": "请求体",
    "cache": "缓存",
    "candidate": "候选",
    "change": "变更",
    "client": "客户端",
    "config": "配置",
    "connection": "连接",
    "content": "内容",
    "context": "上下文",
    "count": "数量",
    "current": "当前",
    "data": "数据",
    "dataset": "Test Dataset",
    "default": "默认",
    "dir": "目录",
    "document": "文档",
    "enabled": "启用状态",
    "endpoint": "端点",
    "error": "错误",
    "evidence": "Evidence",
    "experience": "经验",
    "file": "文件",
    "filter": "过滤条件",
    "handler": "处理器",
    "id": "ID",
    "index": "索引",
    "item": "条目",
    "items": "条目列表",
    "job": "任务",
    "key": "键",
    "limit": "上限",
    "list": "列表",
    "lock": "锁",
    "logger": "日志器",
    "manifest": "清单",
    "member": "成员",
    "memory": "Memory",
    "message": "消息",
    "metadata": "元数据",
    "method": "方法",
    "model": "模型",
    "name": "名称",
    "offset": "偏移量",
    "output": "输出",
    "page": "分页",
    "path": "路径",
    "pattern": "模式",
    "payload": "载荷",
    "pool": "连接池",
    "prefix": "前缀",
    "prompt": "提示词",
    "record": "记录",
    "registry": "注册表",
    "request": "请求",
    "response": "响应",
    "result": "结果",
    "role": "角色",
    "root": "根目录",
    "route": "路由",
    "schema": "Schema",
    "service": "服务",
    "session": "Session",
    "skill": "Skill",
    "source": "来源",
    "state": "状态",
    "status": "状态",
    "store": "存储",
    "target": "目标",
    "task": "任务",
    "tenant": "租户",
    "timeout": "超时",
    "token": "令牌",
    "trace": "追踪",
    "type": "类型",
    "uri": "资源 URI",
    "url": "URL",
    "user": "用户",
    "value": "值",
    "version": "版本",
    "workspace": "Workspace",
}


def identifier_tokens(name: str) -> list[str]:
    clean_name = name.strip("_")
    clean_name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", clean_name)
    clean_name = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff]+", "_", clean_name)
    return [token.lower() for token in clean_name.split("_") if token]


def identifier_subject(name: str) -> str:
    tokens = identifier_tokens(name)
    if not tokens:
        return f"`{name}`"
    translated = [TOKEN_TRANSLATIONS.get(token, token) for token in tokens]
    return " ".join(translated)


def function_description(symbol: Symbol) -> str:
    if symbol.doc:
        return symbol.doc
    name = symbol.name.split(".")[-1]
    tokens = identifier_tokens(name)
    if name.startswith("__") and name.endswith("__"):
        return f"实现 Python `{name}` 协议行为"
    if tokens and tokens[0] == "test":
        subject = identifier_subject("_".join(tokens[1:]))
        return f"验证{subject}相关行为"
    operations = {
        "add": "添加",
        "apply": "应用",
        "build": "构建",
        "check": "检查",
        "clear": "清理",
        "close": "关闭",
        "collect": "收集",
        "compile": "编译",
        "convert": "转换",
        "create": "创建",
        "decode": "解码",
        "delete": "删除",
        "dispatch": "分发",
        "dump": "序列化",
        "emit": "发出",
        "encode": "编码",
        "ensure": "确保",
        "execute": "执行",
        "export": "导出",
        "extract": "提取",
        "fetch": "获取",
        "find": "查找",
        "format": "格式化",
        "get": "获取",
        "handle": "处理",
        "has": "判断是否存在",
        "import": "导入",
        "is": "判断",
        "list": "列出",
        "load": "加载",
        "merge": "合并",
        "normalize": "规范化",
        "parse": "解析",
        "process": "处理",
        "publish": "发布",
        "read": "读取",
        "remove": "移除",
        "render": "渲染",
        "resolve": "解析",
        "run": "执行",
        "save": "保存",
        "search": "搜索",
        "serialize": "序列化",
        "set": "设置",
        "start": "启动",
        "stop": "停止",
        "sync": "同步",
        "to": "转换为",
        "update": "更新",
        "validate": "校验",
        "write": "写入",
    }
    prefix = tokens[0] if tokens else ""
    subject_tokens = tokens[1:] if prefix in operations else tokens
    subject = identifier_subject("_".join(subject_tokens)) if subject_tokens else f"`{name}`"
    operation = operations.get(prefix, "处理")
    if symbol.language in {"TSX", "TypeScript"} and name[:1].isupper():
        return f"渲染或封装{subject}前端组件"
    return f"{operation}{subject}相关逻辑"


def class_description(symbol: Symbol) -> str:
    if symbol.doc:
        return symbol.doc
    subject = identifier_subject(symbol.name)
    if symbol.language in {"TSX", "TypeScript"} and symbol.name[:1].isupper():
        return f"封装{subject}前端状态与行为"
    return f"封装{subject}相关状态与行为"


def variable_description(symbol: Symbol) -> str:
    if symbol.doc:
        return symbol.doc
    subject = identifier_subject(symbol.name)
    if symbol.kind == "参数":
        return f"函数或方法的{subject}参数"
    if symbol.kind == "常量":
        return f"定义{subject}常量"
    if symbol.kind in {"类字段", "实例变量", "类变量", "接口字段", "枚举成员"}:
        return f"保存{subject}字段值"
    if symbol.kind == "模块变量":
        return f"保存模块级{subject}值"
    return f"保存{subject}局部中间值"


def symbol_description(symbol: Symbol) -> str:
    if symbol.kind in {"类", "接口", "类型别名", "枚举"}:
        return class_description(symbol)
    if symbol.kind in {"函数", "异步函数", "方法", "异步方法", "属性", "访问器", "构造函数"}:
        return function_description(symbol)
    return variable_description(symbol)


def interface_description(interface: Interface) -> str:
    if interface.doc:
        return interface.doc
    subject = identifier_subject(interface.name)
    if "字段" in interface.kind:
        return f"定义{subject}接口字段"
    if "方法" in interface.kind:
        return f"定义{subject}接口方法"
    if interface.kind.startswith("CLI"):
        return f"提供 `{interface.name}` 命令行入口"
    return f"定义{subject}的结构或行为契约"


def endpoint_description(endpoint: ApiEndpoint) -> str:
    if endpoint.doc:
        return endpoint.doc
    return f"由 `{endpoint.handler}` 处理 `{endpoint.method} {endpoint.path}` 请求"


def markdown_cell(value: str) -> str:
    text = compact(value)
    text = text.replace("\\", "\\\\").replace("|", "\\|").replace("`", "\\`")
    return text or "-"


def code_cell(value: str) -> str:
    return f"`{markdown_cell(value)}`" if value else "-"


def discover_source_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for file in root.rglob("*"):
        if not file.is_file() or file.suffix.lower() not in SOURCE_SUFFIXES:
            continue
        relative = file.relative_to(root)
        if any(part in EXCLUDED_PARTS for part in relative.parts):
            continue
        if relative.parts[:3] == ("teamEvolver", "web", "dist"):
            continue
        files.append(file)
    return sorted(files)


def extract_python(root: Path, files: Iterable[Path]) -> tuple[list[Symbol], list[Interface], list[ApiEndpoint], list[ParseError]]:
    symbols: list[Symbol] = []
    interfaces: list[Interface] = []
    endpoints: list[ApiEndpoint] = []
    errors: list[ParseError] = []
    for file in files:
        try:
            source = file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(file), type_comments=True)
        except (OSError, UnicodeError, SyntaxError) as exc:
            errors.append(
                ParseError(
                    file=file.relative_to(root).as_posix(),
                    line=getattr(exc, "lineno", 1) or 1,
                    message=compact(str(exc)),
                )
            )
            continue
        extractor = PythonExtractor(root, file, tree)
        extractor.visit(tree)
        symbols.extend(extractor.symbols)
        interfaces.extend(extractor.interfaces)
        endpoints.extend(extractor.endpoints)
    return symbols, interfaces, endpoints, errors


def extract_typescript(root: Path, files: list[Path]) -> tuple[list[Symbol], list[Interface], list[ParseError]]:
    if not files:
        return [], [], []
    helper = root / "scripts" / "extract_ts_symbols.mjs"
    process = subprocess.run(
        ["node", str(helper)],
        cwd=root,
        input=json.dumps([str(file) for file in files], ensure_ascii=False),
        text=True,
        capture_output=True,
        check=False,
    )
    if process.returncode != 0:
        return [], [], [ParseError(file=str(helper.relative_to(root)), line=1, message=compact(process.stderr))]
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        return [], [], [ParseError(file=str(helper.relative_to(root)), line=1, message=f"JSON 解析失败：{exc}")]
    symbols = [Symbol(**item) for item in payload.get("records", [])]
    interfaces = [Interface(**item) for item in payload.get("interfaces", [])]
    errors = [ParseError(**item) for item in payload.get("errors", [])]
    return symbols, interfaces, errors


def repository_state(root: Path) -> tuple[str, str]:
    revision = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    ).stdout
    return revision or "unknown", "包含未提交改动" if status.strip() else "干净工作树"


def dedupe_interfaces(interfaces: list[Interface]) -> list[Interface]:
    seen: set[tuple[str, int, str, str]] = set()
    result: list[Interface] = []
    for interface in interfaces:
        key = (interface.file, interface.line, interface.kind, interface.name)
        if key in seen:
            continue
        seen.add(key)
        result.append(interface)
    return result


def render_markdown(
    root: Path,
    source_files: list[Path],
    symbols: list[Symbol],
    interfaces: list[Interface],
    endpoints: list[ApiEndpoint],
    errors: list[ParseError],
) -> str:
    revision, worktree_state = repository_state(root)
    source_counts = Counter(file.suffix.lower() for file in source_files)
    symbol_counts = Counter(symbol.kind for symbol in symbols)
    group_counts = Counter(symbol.file.split("/", 1)[0] for symbol in symbols)
    interfaces = dedupe_interfaces(interfaces)
    symbols = sorted(symbols, key=lambda item: (item.file, item.line, item.kind, item.name))
    interfaces = sorted(interfaces, key=lambda item: (item.file, item.line, item.kind, item.name))
    endpoints = sorted(endpoints, key=lambda item: (item.path, item.method, item.file, item.line))
    errors = sorted(errors, key=lambda item: (item.file, item.line))

    lines = [
        "# teamEvolver 代码符号索引",
        "",
        f"> 生成日期：{date.today().isoformat()}  ",
        f"> 代码基线：`{revision}`（{worktree_state}）  ",
        "> 生成命令：`python scripts/generate_code_symbol_index.py`",
        "",
        "## 说明",
        "",
        "本文档按当前工作区源码生成，记录变量、参数、函数、方法、类、类型契约、HTTP/WebSocket 路由与 CLI 入口的名字、代码位置和功能。",
        "功能优先采用源码 docstring/JSDoc；缺少注释时依据标识符、类型、签名和声明上下文归纳。",
        "",
        "扫描范围包括仓库内的 Python、TypeScript、TSX、JavaScript 源码及测试；排除 `.git`、虚拟环境、缓存、",
        "`node_modules`、`teamEvolver/web/dist` 构建产物和 source map。变量按声明统计，重复赋值不重复列出；",
        "Python 导入别名不作为变量，匿名回调仅在能从赋值目标确定名称时作为函数列出。",
        "",
        "## 统计",
        "",
        "| 指标 | 数量 |",
        "| --- | ---: |",
        f"| 扫描源码文件 | {len(source_files)} |",
        f"| 代码符号 | {len(symbols)} |",
        f"| 接口与类型契约 | {len(interfaces)} |",
        f"| HTTP/WebSocket 路由 | {len(endpoints)} |",
        f"| 解析错误 | {len(errors)} |",
        "",
        "### 源码文件",
        "",
        "| 扩展名 | 文件数 |",
        "| --- | ---: |",
    ]
    for suffix, count in sorted(source_counts.items()):
        lines.append(f"| `{suffix}` | {count} |")

    lines.extend(
        [
            "",
            "### 符号类别",
            "",
            "| 类别 | 数量 |",
            "| --- | ---: |",
        ]
    )
    for kind, count in sorted(symbol_counts.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| {markdown_cell(kind)} | {count} |")

    lines.extend(
        [
            "",
            "### 顶层模块",
            "",
            "| 模块 | 符号数 |",
            "| --- | ---: |",
        ]
    )
    for group, count in sorted(group_counts.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| `{markdown_cell(group)}` | {count} |")

    lines.extend(
        [
            "",
            "## HTTP 与 WebSocket 接口",
            "",
            "| 方法 | 路径 | 处理器 | 所在代码 | 功能 |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for endpoint in endpoints:
        lines.append(
            "| "
            + " | ".join(
                [
                    code_cell(endpoint.method),
                    code_cell(endpoint.path),
                    code_cell(endpoint.handler),
                    code_cell(f"{endpoint.file}:L{endpoint.line}"),
                    markdown_cell(endpoint_description(endpoint)),
                ]
            )
            + " |"
        )
    if not endpoints:
        lines.append("| - | - | - | - | 未发现静态路由声明 |")

    lines.extend(
        [
            "",
            "## 接口与类型契约",
            "",
            "| 类型 | 名字 | 所在代码 | 作用域 | 功能 |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for interface in interfaces:
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_cell(interface.kind),
                    code_cell(interface.name),
                    code_cell(f"{interface.file}:L{interface.line}"),
                    code_cell(interface.scope),
                    markdown_cell(interface_description(interface)),
                ]
            )
            + " |"
        )
    if not interfaces:
        lines.append("| - | - | - | - | 未发现显式接口或类型契约 |")

    lines.extend(["", "## 详细符号", ""])
    by_group: dict[str, dict[str, list[Symbol]]] = defaultdict(lambda: defaultdict(list))
    for symbol in symbols:
        group = symbol.file.split("/", 1)[0]
        by_group[group][symbol.file].append(symbol)
    for group in sorted(by_group):
        lines.extend([f"### `{markdown_cell(group)}`", ""])
        for file, file_symbols in sorted(by_group[group].items()):
            lines.extend(
                [
                    f"#### `{markdown_cell(file)}`",
                    "",
                    "| 类别 | 名字 | 所在代码 | 作用域 | 签名/类型 | 功能 |",
                    "| --- | --- | --- | --- | --- | --- |",
                ]
            )
            for symbol in file_symbols:
                lines.append(
                    "| "
                    + " | ".join(
                        [
                            markdown_cell(symbol.kind),
                            code_cell(symbol.name),
                            code_cell(f"{symbol.file}:L{symbol.line}"),
                            code_cell(symbol.scope),
                            code_cell(symbol.signature),
                            markdown_cell(symbol_description(symbol)),
                        ]
                    )
                    + " |"
                )
            lines.append("")

    lines.extend(["## 解析异常", ""])
    if errors:
        lines.extend(
            [
                "| 所在代码 | 错误 |",
                "| --- | --- |",
            ]
        )
        for error in errors:
            lines.append(
                f"| {code_cell(f'{error.file}:L{error.line}')} | {markdown_cell(error.message)} |"
            )
    else:
        lines.append("全部纳入范围的源文件均已完成语法解析。")
    lines.append("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default="docs/zh/reference/code-symbol-index.md",
        help="Markdown output path, relative to the repository root",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(__file__).resolve().parents[1]
    source_files = discover_source_files(root)
    python_files = [file for file in source_files if file.suffix.lower() == ".py"]
    typescript_files = [file for file in source_files if file.suffix.lower() in {".ts", ".tsx", ".js", ".jsx"}]

    py_symbols, py_interfaces, endpoints, py_errors = extract_python(root, python_files)
    ts_symbols, ts_interfaces, ts_errors = extract_typescript(root, typescript_files)
    output = render_markdown(
        root,
        source_files,
        [*py_symbols, *ts_symbols],
        [*py_interfaces, *ts_interfaces],
        endpoints,
        [*py_errors, *ts_errors],
    )

    output_path = root / args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(output, encoding="utf-8")
    print(f"Wrote {output_path.relative_to(root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
