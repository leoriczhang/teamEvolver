"""Report generation and persistence tool."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from .base import Tool, ToolResult

logger = logging.getLogger(__name__)


class SaveReportTool(Tool):
    """Save a maintenance report to disk."""

    def __init__(self, report_dir: Path):
        self._report_dir = report_dir
        self._report_dir.mkdir(parents=True, exist_ok=True)

    @property
    def name(self) -> str:
        return "save_report"

    @property
    def description(self) -> str:
        return "Save today's maintenance report summary to disk for auditing."

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Report title."},
                "content": {"type": "string", "description": "Full report content (markdown)."},
            },
            "required": ["title", "content"],
        }

    def execute(self, **kwargs) -> ToolResult:
        title = kwargs.get("title", "Untitled Report")
        content = kwargs.get("content", "")

        now = datetime.now(timezone.utc)
        filename = f"{now.strftime('%Y-%m-%d_%H%M%S')}_{title.replace(' ', '_')[:40]}.md"
        filepath = self._report_dir / filename

        try:
            report = (
                f"# {title}\n\n"
                f"**Generated**: {now.isoformat()}\n"
                f"**Maintainer**: DreamCycle (memory maintenance process)\n\n"
                f"---\n\n"
                f"{content}\n"
            )
            filepath.write_text(report, encoding="utf-8")
            logger.info("Report saved: %s", filepath)
            return ToolResult(success=True, output=f"Report saved to {filepath}")
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))
