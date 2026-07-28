from __future__ import annotations

import logging
import re

LINE_PREFIX_RE = re.compile(r"^(.*?\|\s+(INFO|WARNING|ERROR|DEBUG)\s+\|\s+([^|]+)\|\s+)(.*)$")

ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_BLUE = "\033[34m"
ANSI_CYAN = "\033[36m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_RED = "\033[31m"
ANSI_MAGENTA = "\033[35m"
ANSI_ORANGE = "\033[38;5;208m"  # orange (256-color)


def _info_color_for_logger(logger_name: str) -> str:
    name = logger_name.lower()
    if "teamEvolver.proxy" in name:
        return ANSI_GREEN
    if "teamEvolver.launcher" in name:
        return ANSI_CYAN
    if "teamEvolver.skills" in name:
        return ANSI_MAGENTA
    if "httpx" in name:
        return ANSI_CYAN
    return ANSI_BLUE


def _colorize_message(message: str, *, level: str, logger_name: str) -> str:
    text = message
    if "[SkillManager]" in text:
        return f"{ANSI_BOLD}{ANSI_MAGENTA}{text}{ANSI_RESET}"
    if "teamEvolver service ready" in text:
        return f"{ANSI_BOLD}{ANSI_CYAN}{text}{ANSI_RESET}"
    if text.startswith("======================================================================"):
        return f"{ANSI_CYAN}{text}{ANSI_RESET}"
    if '"GET /docs HTTP/1.1" 200 OK' in text:
        return f"{ANSI_GREEN}{text}{ANSI_RESET}"

    if level == "INFO":
        level_color = _info_color_for_logger(logger_name)
    elif level == "WARNING":
        level_color = ANSI_YELLOW
    elif level == "ERROR":
        level_color = ANSI_RED
    elif level == "DEBUG":
        level_color = ANSI_MAGENTA
    else:
        return text
    return f"{ANSI_BOLD}{level_color}{text}{ANSI_RESET}"


class ColorFormatter(logging.Formatter):
    def __init__(self, fmt: str, *, use_color: bool):
        super().__init__(fmt=fmt)
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        if not self.use_color:
            return rendered
        match = LINE_PREFIX_RE.match(rendered)
        if not match:
            return rendered
        prefix = match.group(1)
        level = match.group(2)
        logger_name = match.group(3).strip()
        message = match.group(4)
        return f"{prefix}{_colorize_message(message, level=level, logger_name=logger_name)}"


def setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    handler = logging.StreamHandler()
    fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    handler.setFormatter(ColorFormatter(fmt, use_color=handler.stream.isatty()))
    root.addHandler(handler)
