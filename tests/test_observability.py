"""阶段 1 可观测性埋点的单元测试：默认 no-op、启用后指标、探针端点。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from repopilot_guard import observability
from repopilot_guard.api import create_app
from repopilot_guard.project_registry import ProjectRegistry


class ObservabilityDisabledTests(unittest.TestCase):
    def setUp(self) -> None:
        observability._reset_for_tests()

    def tearDown(self) -> None:
        observability._reset_for_tests()

    def test_default_disabled_is_noop_and_does_not_crash(self) -> None:
        observability.init_observability()
        self.assertFalse(observability.is_enabled())

        with observability.span("test.span") as active:
            active.set_attribute("key", "value")
            active.set_attributes({"nested": "ignored"})
            active.record_exception(RuntimeError("boom"))
            active.set_status_error()
        observability.record_model_usage(1, 2, 3, 0.0, "CNY")
        observability.record_task_started()
        observability.record_task_terminal("REPORT")

        self.assertIn("disabled", observability.metrics_text())

    def test_trace_id_contextvar_roundtrip(self) -> None:
        self.assertIsNone(observability.current_trace_id())
        observability.set_trace_id("trace-abc-123")
        self.assertEqual("trace-abc-123", observability.current_trace_id())


class ObservabilityEnabledTests(unittest.TestCase):
    def setUp(self) -> None:
        observability._reset_for_tests()
        self._environment = {
            "REPOPILOT_OTEL_ENABLED": "true",
            "REPOPILOT_OTEL_SERVICE_NAME": "repopilot-test",
            "REPOPILOT_OTEL_EXPORTER_ENDPOINT": "http://127.0.0.1:4318/v1/traces",
        }

    def tearDown(self) -> None:
        observability._reset_for_tests()

    def test_enabled_registers_and_increments_metrics(self) -> None:
        with patch.dict(os.environ, self._environment, clear=False):
            observability.init_observability()
            self.assertTrue(observability.is_enabled())

            observability.record_task_started()
            observability.record_task_terminal("REPORT")
            observability.record_model_usage(10, 20, 30, 0.001, "CNY")

            registry = observability._metrics_registry
            text = observability.metrics_text()

        self.assertIsNotNone(registry)
        self.assertEqual(1.0, registry.get_sample_value("repopilot_tasks_started_total"))
        self.assertEqual(1.0, registry.get_sample_value("repopilot_tasks_terminal_total", {"status": "REPORT"}))
        self.assertEqual(30.0, registry.get_sample_value("repopilot_model_tokens_total", {"type": "total"}))
        self.assertAlmostEqual(0.001, registry.get_sample_value("repopilot_model_cost_total", {"currency": "CNY"}), places=9)
        self.assertIn("repopilot_tasks_started_total", text)


class ApiProbeEndpointsTests(unittest.TestCase):
    def test_healthz_readyz_and_metrics_endpoints(self) -> None:
        observability._reset_for_tests()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            registry = ProjectRegistry(root / "state.sqlite")
            try:
                with TestClient(create_app(SimpleNamespace(), registry, root / "runs")) as client:
                    healthz = client.get("/healthz")
                    readyz = client.get("/readyz")
                    metrics = client.get("/metrics")

                self.assertEqual(200, healthz.status_code)
                self.assertEqual("ALIVE", healthz.json()["status"])
                self.assertEqual(200, readyz.status_code)
                self.assertEqual("READY", readyz.json()["status"])
                self.assertEqual(200, metrics.status_code)
                self.assertIn("text/plain", metrics.headers["content-type"])
            finally:
                registry.close()


if __name__ == "__main__":
    unittest.main()
