"""Git transfer: isolated checkouts, explicit revisions and new remote refs."""
from __future__ import annotations

import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from .channels import http_url, request_json
from .packages import ExportResult, SkillPackage, TransferError, discover, read_directory, safe_path
from .process import workspace


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
        if (parsed.scheme == "ssh" and parsed.hostname and not parsed.password
                and not parsed.query and not parsed.fragment and not any(ord(c) < 32 for c in url)):
            return url
        if re.fullmatch(r"[\w.-]+@[\w.-]+:[\w./-]+", url):
            return url
        raise TransferError("Git 地址必须是 HTTP(S)、SSH 或 git@host:path 格式")

    def _workspace(self, options: Mapping[str, Any]):
        return workspace(options, allow_local=self.allow_local)

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
            options.get("_checkpoint", lambda _: None)({"effect": "create_repository"})
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
            options.get("_checkpoint", lambda _: None)({"effect": "create_repository"})
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
            metadata = {"channel": "git", "exported": [p.name for p in packages],
                        "url": http_url(web_url) if web_url else url, "branch": new_branch,
                        "commit": run("rev-parse", "HEAD").stdout.strip()}
            checkpoint = options.get("_checkpoint", lambda _: None)
            checkpoint({"effect": "push", "url": url, "expected": metadata})
            # Empty lease requires the ref to remain absent, including racing writers.
            run("push", f"--force-with-lease=refs/heads/{new_branch}:", "origin", f"HEAD:refs/heads/{new_branch}")
            checkpoint({"receipt": metadata})
            return ExportResult(metadata)

    def reconcile(self, checkpoint, options):
        """Read-only confirmation after push response loss; never create/push again."""
        if checkpoint.get("effect") != "push":
            return None
        expected = checkpoint["expected"]
        with self._workspace(options) as (_repo, run):
            value = run("ls-remote", "--heads", self._url(checkpoint["url"]),
                        "refs/heads/" + expected["branch"]).stdout.strip()
            if value and value.split()[0] == expected["commit"]:
                return expected
        return None
