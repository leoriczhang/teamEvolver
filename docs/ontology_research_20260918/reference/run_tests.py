"""Run the reference suite and write a factual JSON receipt; no external services."""
from __future__ import annotations
from datetime import datetime, timezone
import importlib.metadata
import json
from pathlib import Path
import platform
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
class RecordedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs); self.records = []
    def addSuccess(self, test):
        super().addSuccess(test); self.records.append({"test": test.id(), "status": "passed"})
    def addFailure(self, test, err):
        super().addFailure(test, err); self.records.append({"test": test.id(), "status": "failed"})
    def addError(self, test, err):
        super().addError(test, err); self.records.append({"test": test.id(), "status": "error"})

suite = unittest.defaultTestLoader.discover(str(Path(__file__).parent), pattern="test_contracts.py")
result = unittest.TextTestRunner(verbosity=2, resultclass=RecordedResult).run(suite)
report = {"executed_at": datetime.now(timezone.utc).isoformat(), "scope": "offline synthetic contract and pure-function invariants only",
          "python": platform.python_version(), "jsonschema": importlib.metadata.version("jsonschema"),
          "tests_run": result.testsRun, "passed": sum(x["status"] == "passed" for x in result.records),
          "failed": len(result.failures), "errors": len(result.errors), "success": result.wasSuccessful(),
          "not_tested": ["deployed TE/OpenViking integration", "real IAM and credentials", "database transactions", "LLM extraction", "network isolation", "production source authenticity", "latency or throughput"],
          "tests": result.records}
(ROOT / "results" / "contract_test_results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
sys.exit(0 if result.wasSuccessful() else 1)
