"""Bounded Git child processes with no inherited credentials or configuration."""
import os
import signal
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from .packages import TransferError

MAX_GIT_BYTES = 256 * 1024 * 1024
MAX_GIT_FILES = 16384
MAX_OUTPUT_BYTES = 1024 * 1024
GIT_TIMEOUT = 300


def check_workspace(root):
    size = count = 0
    for directory, _dirs, files in os.walk(root, followlinks=False):
        for name in files:
            count += 1
            size += os.lstat(Path(directory) / name).st_size
            if count > MAX_GIT_FILES or size > MAX_GIT_BYTES:
                raise TransferError("Git 工作目录大小或文件数量超出限制", 413)


@contextmanager
def workspace(options, *, allow_local=False):
    with tempfile.TemporaryDirectory(prefix="te-skill-git-") as tmp:
        root = Path(tmp)
        repo = root / "repo"
        repo.mkdir()
        askpass = root / "askpass"
        askpass.write_text('#!/bin/sh\ncase "$1" in\n*Username*) printf "%s\\n" "$TE_SKILL_GIT_USER" ;;\n'
                           '*) printf "%s\\n" "$TE_SKILL_GIT_TOKEN" ;;\nesac\n')
        askpass.chmod(0o700)
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": tmp, "XDG_CONFIG_HOME": tmp,
            "LANG": "C", "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": str(askpass),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_SSH_COMMAND": (
                "ssh -F /dev/null -oBatchMode=yes -oStrictHostKeyChecking=yes -oIdentitiesOnly=yes "
                "-oIdentityAgent=none -oIdentityFile=/run/secrets/team-evolver/git_identity "
                "-oUserKnownHostsFile=/run/secrets/team-evolver/git_known_hosts -oGlobalKnownHostsFile=/dev/null"
            ),
            "GIT_TEMPLATE_DIR": tmp + "/empty-template",
            "TE_SKILL_GIT_USER": str(options.get("username") or "oauth2"),
            "TE_SKILL_GIT_TOKEN": str(options.get("token") or ""),
        }
        (root / "empty-template").mkdir()
        deadline = time.monotonic() + GIT_TIMEOUT

        def run(*args, check=True):
            guard = options.get("_check", lambda: None)
            guard()
            command = ["git", "-c", "core.hooksPath=/dev/null", "-c", "credential.helper=",
                       "-c", "http.followRedirects=false", "-c", "protocol.allow=never",
                       "-c", "protocol.https.allow=always", "-c", "protocol.http.allow=always",
                       "-c", "protocol.ssh.allow=always",
                       "-c", "protocol.file.allow=" + ("always" if allow_local else "never"),
                       "-c", "user.name=teamEvolver", "-c", "user.email=skills@teamevolver.local", *args]
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                try:
                    process = subprocess.Popen(command, cwd=repo, env=env, stdout=stdout, stderr=stderr,
                                               start_new_session=True)
                except OSError:
                    raise TransferError("无法运行 Git，请检查服务端 Git 安装", 503) from None
                try:
                    last_guard = time.monotonic()
                    while True:
                        if time.monotonic() >= deadline:
                            raise TransferError("Git 操作超时", 504)
                        if max(os.fstat(stdout.fileno()).st_size, os.fstat(stderr.fileno()).st_size) > MAX_OUTPUT_BYTES:
                            raise TransferError("Git 输出大小超出限制", 413)
                        check_workspace(repo)
                        if time.monotonic() - last_guard > 2:
                            guard()
                            last_guard = time.monotonic()
                        if process.poll() is not None:
                            break
                        try:
                            process.wait(timeout=0.1)
                        except subprocess.TimeoutExpired:
                            pass
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                stdout.seek(0)
                result = subprocess.CompletedProcess(command, process.returncode,
                                                     stdout.read(MAX_OUTPUT_BYTES).decode(errors="replace"), "")
                if check and result.returncode:
                    raise TransferError("Git 操作失败，请检查仓库地址、分支和访问权限", 502)
                return result
        yield repo, run
