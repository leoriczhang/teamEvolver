"""Git transfer: isolated checkouts, explicit revisions and new remote refs."""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from .channels import http_url, request_json
from .packages import ExportResult, SkillPackage, TransferError, discover, read_directory, safe_path


class GitAdapter:
    def __init__(self, *, allow_local: bool = False):
        # Only tests/embedded callers can opt into local repositories, never HTTP clients.
        self.allow_local = allow_local

    def _url(self, value: Any) -> str:
        url = str(value or "").strip()
        parsed = urlsplit(url)
        if self.allow_local and Path(url).is_absolute():
            return url
        if parsed.scheme in {"http", "https"}:
            return http_url(url)
        if parsed.scheme == "ssh" and parsed.hostname and not parsed.password:
            return url
        if re.fullmatch(r"[\w.-]+@[\w.-]+:[\w./-]+", url):
            return url
        raise TransferError("Git 地址必须是 HTTP(S)、SSH 或 git@host:path 格式")

    @contextmanager
    def _workspace(self, options: Mapping[str, Any]):
        with tempfile.TemporaryDirectory(prefix="te-skill-git-") as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            askpass = root / "askpass"
            askpass.write_text('#!/bin/sh\ncase "$1" in\n*Username*) printf "%s\\n" "$TE_SKILL_GIT_USER" ;;\n'
                               '*) printf "%s\\n" "$TE_SKILL_GIT_TOKEN" ;;\nesac\n')
            askpass.chmod(0o700)
            env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
            env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": str(askpass),
                        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                        "GIT_SSH_COMMAND": "ssh -oBatchMode=yes -oStrictHostKeyChecking=yes",
                        "TE_SKILL_GIT_USER": str(options.get("username") or "oauth2"),
                        "TE_SKILL_GIT_TOKEN": str(options.get("token") or "")})

            def run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
                command = ["git", "-c", "core.hooksPath=/dev/null", "-c", "credential.helper=",
                           "-c", "protocol.file.allow=" + ("always" if self.allow_local else "never"),
                           "-c", "user.name=teamEvolver", "-c", "user.email=skills@teamevolver.local", *args]
                try:
                    process = subprocess.Popen(command, cwd=repo, env=env, stdout=subprocess.PIPE,
                                               stderr=subprocess.PIPE, text=True, start_new_session=True)
                    try:
                        stdout, stderr = process.communicate(timeout=300)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.communicate()
                        raise TransferError("Git 操作超时，请检查网络或仓库大小", 504) from None
                except OSError as exc:
                    raise TransferError("无法运行 Git，请检查服务端 Git 安装", 503) from exc
                result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
                if check and result.returncode:
                    # Git errors can contain credentials/URLs; never echo raw stderr.
                    raise TransferError("Git 操作失败，请检查仓库地址、分支和访问权限", 502)
                return result

            yield repo, run

    @staticmethod
    def _ref(value: Any, run, *, default: str = "") -> str:
        ref = str(value or default).strip()
        if ref and (ref.startswith("-") or run("check-ref-format", "--branch", ref, check=False).returncode):
            raise TransferError("Git 分支名称不合法")
        return ref

    @staticmethod
    def _subdir(repo: Path, value: Any) -> Path:
        relative = str(value if value is not None else "skills").strip()
        target = repo if relative in {"", "."} else repo / safe_path(relative)
        for part in (target, *target.parents):
            if part == repo:
                break
            if part.is_symlink():
                raise TransferError("Git Skill 路径不能包含符号链接")
        if ".git" in target.relative_to(repo).parts:
            raise TransferError("Git Skill 路径不能指向 .git")
        return target

    def _clone(self, repo: Path, run, url: str, branch: str) -> None:
        args = ["clone", "--depth", "1", "--single-branch", "--no-tags"]
        if branch:
            args += ["--branch", branch]
        run(*args, "--", url, str(repo))

    def read(self, options: Mapping[str, Any]) -> list[SkillPackage]:
        url = self._url(options.get("url"))
        with self._workspace(options) as (repo, run):
            branch = self._ref(options.get("branch"), run)
            self._clone(repo, run, url, branch)
            commit = str(options.get("commit") or "").strip()
            if commit:
                if not re.fullmatch(r"[0-9a-fA-F]{7,40}", commit):
                    raise TransferError("commit 必须是 7–40 位十六进制提交 ID")
                if run("cat-file", "-e", f"{commit}^{{commit}}", check=False).returncode:
                    run("fetch", "--depth", "1", "origin", commit)
                run("checkout", "--detach", commit)
            revision = run("rev-parse", "HEAD").stdout.strip()
            files = read_directory(self._subdir(repo, options.get("path")))
            return discover(files, name=str(options.get("name") or ""),
                            origin={"branch": branch, "commit": revision,
                                    "path": str(options.get("path") or "skills")})

    def _create_repository(self, options: Mapping[str, Any]) -> tuple[str, str]:
        provider = str(options.get("provider") or "github")
        token = str(options.get("token") or "")
        name = str(options.get("repo_name") or "").strip()
        namespace = str(options.get("namespace") or "").strip()
        if not token or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", name):
            raise TransferError("新建仓库需要 Token 和合法的 repo_name")
        private = options.get("private", True)
        if not isinstance(private, bool):
            raise TransferError("private 必须是布尔值")
        if provider == "github":
            base = http_url(options.get("api_url") or "https://api.github.com")
            if namespace and not re.fullmatch(r"[A-Za-z0-9_-]+", namespace):
                raise TransferError("GitHub 组织名不合法")
            route = f"/orgs/{namespace}/repos" if namespace else "/user/repos"
            result = request_json("POST", base + route, headers={"Authorization": f"Bearer {token}"},
                                  json={"name": name, "private": private, "auto_init": False})
            return self._url(result.get("clone_url")), str(result.get("html_url") or "")
        if provider == "gitlab":
            base = http_url(options.get("api_url") or "https://gitlab.com/api/v4")
            payload: dict[str, Any] = {"name": name, "path": name, "visibility": "private" if private else "public"}
            if namespace:
                if not namespace.isdigit():
                    raise TransferError("GitLab namespace 必须填写数字 namespace_id")
                payload["namespace_id"] = int(namespace)
            result = request_json("POST", base + "/projects", headers={"PRIVATE-TOKEN": token}, json=payload)
            return self._url(result.get("http_url_to_repo")), str(result.get("web_url") or "")
        raise TransferError("新建仓库 provider 必须是 github 或 gitlab")

    def write(self, packages: Sequence[SkillPackage], options: Mapping[str, Any]) -> ExportResult:
        mode = str(options.get("mode") or "new_branch")
        if mode not in {"new_branch", "new_repository"}:
            raise TransferError("Git 导出 mode 必须是 new_branch 或 new_repository")
        with self._workspace(options) as (repo, run):
            branch = self._ref(options.get("branch"), run)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            generated = f"skills-export-{stamp}-{uuid.uuid4().hex[:6]}"
            new_branch = self._ref(options.get("new_branch"), run,
                                   default="main" if mode == "new_repository" else generated)
            url = ""
            web_url = ""
            if mode == "new_branch":
                url = self._url(options.get("url"))
                self._clone(repo, run, url, branch)
                if run("ls-remote", "--heads", "origin", f"refs/heads/{new_branch}").stdout.strip():
                    raise TransferError("目标分支已存在，请填写新的分支名", 409)
                run("checkout", "-b", new_branch)
            else:
                run("init", "--initial-branch", new_branch)
            target = self._subdir(repo, options.get("path"))
            for item in packages:
                dest = target / item.name
                if dest.is_symlink():
                    raise TransferError("目标 Skill 不能是符号链接")
                if dest.exists():
                    shutil.rmtree(dest)
                for rel, data in item.files.items():
                    path = dest / safe_path(rel)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
            run("add", "--all")
            run("commit", "--allow-empty", "-m", str(options.get("message") or "Export Skills from teamEvolver"))
            if mode == "new_repository":
                # Validate and stage all local content before the external creation.
                url, web_url = self._create_repository(options)
                run("remote", "add", "origin", url)
            try:
                # Empty lease requires the ref to remain absent, including racing writers.
                run("push", f"--force-with-lease=refs/heads/{new_branch}:", "origin", f"HEAD:refs/heads/{new_branch}")
            except TransferError as exc:
                if mode == "new_repository":
                    message = f"仓库已创建（{web_url or url}），上传失败；可向该仓库新建分支重试"
                    raise TransferError(message, 502) from exc
                raise
            return ExportResult({"channel": "git", "exported": [p.name for p in packages],
                                 "url": web_url or url, "branch": new_branch,
                                 "commit": run("rev-parse", "HEAD").stdout.strip()})
