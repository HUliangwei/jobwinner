import io
import unittest
from unittest.mock import Mock, patch

from rich.console import Console


class BrowserDiagnosticsTests(unittest.TestCase):
    @patch("jobwinner.browser.diagnostics.httpx.get")
    def test_browser_identity_detects_edge_from_older_runtime_health(self, http_get):
        from jobwinner.browser.diagnostics import _browser_identity

        response = Mock()
        response.json.return_value = {"Browser": "Edg/138.0"}
        http_get.return_value = response

        product, name = _browser_identity({"chromePort": 9222})

        self.assertEqual(product, "Edg/138.0")
        self.assertEqual(name, "Microsoft Edge")

    @patch("jobwinner.browser.diagnostics.find_tab")
    @patch("jobwinner.browser.diagnostics.find_boss_tab")
    @patch("jobwinner.browser.diagnostics.runtime_targets")
    @patch("jobwinner.browser.diagnostics.ensure_runtime")
    @patch("jobwinner.browser.diagnostics.check_node_available")
    def test_run_browser_diagnostics_reports_ready_runtime(self, check_node, ensure_runtime, runtime_targets, find_boss_tab, find_tab):
        from jobwinner.browser.diagnostics import run_browser_diagnostics

        check_node.return_value = {"available": True, "version": "v22.1.0"}
        ensure_runtime.return_value = True
        runtime_targets.return_value = [{"targetId": "1", "url": "https://www.zhipin.com"}]
        find_boss_tab.return_value = {"targetId": "1", "title": "BOSS直聘"}
        # 新逻辑按渠道 domain 找 tab：两个渠道都返回同一个模拟 tab。
        find_tab.return_value = {"targetId": "1", "title": "BOSS直聘"}

        result = run_browser_diagnostics({"browser": {"proxy_port": 3456}})

        self.assertTrue(result["node"]["available"])
        self.assertTrue(result["runtime"])
        self.assertTrue(result["chrome"])
        self.assertEqual(result["boss_tab"]["title"], "BOSS直聘")
        self.assertEqual(result["channel_tabs"]["bosszp"]["title"], "BOSS直聘")
        self.assertEqual(result["runtime_url"], "http://127.0.0.1:3456")

    @patch("jobwinner.browser.diagnostics.find_boss_tab")
    @patch("jobwinner.browser.diagnostics.runtime_targets")
    @patch("jobwinner.browser.diagnostics.runtime_health")
    @patch("jobwinner.browser.diagnostics.ensure_runtime")
    @patch("jobwinner.browser.diagnostics.check_node_available")
    def test_run_browser_diagnostics_reports_updated_url_after_runtime_port_fallback(
        self,
        check_node,
        ensure_runtime,
        runtime_health,
        runtime_targets,
        find_boss_tab,
    ):
        from jobwinner.browser.diagnostics import run_browser_diagnostics

        config = {"browser": {"proxy_port": 3456}}
        check_node.return_value = {"available": True, "version": "v22.1.0"}
        ensure_runtime.side_effect = lambda cfg: cfg["browser"].update({"proxy_port": 3457}) or True
        runtime_health.return_value = {"status": "ok", "runtime": "jobwinner"}
        runtime_targets.return_value = [{"targetId": "1", "url": "https://www.zhipin.com"}]
        find_boss_tab.return_value = {"targetId": "1", "title": "BOSS直聘"}

        result = run_browser_diagnostics(config)

        self.assertTrue(result["runtime"])
        self.assertEqual(result["runtime_url"], "http://127.0.0.1:3457")
        self.assertFalse(any("Runtime port is occupied" in error for error in result["errors"]))

    @patch("jobwinner.browser.diagnostics.ensure_runtime")
    @patch("jobwinner.browser.diagnostics.check_node_available")
    def test_run_browser_diagnostics_reports_missing_node(self, check_node, ensure_runtime):
        from jobwinner.browser.diagnostics import run_browser_diagnostics

        check_node.return_value = {"available": False, "version": None, "error": "node missing"}
        ensure_runtime.return_value = False

        result = run_browser_diagnostics({})

        self.assertFalse(result["node"]["available"])
        self.assertFalse(result["runtime"])
        self.assertIn("Node.js", result["errors"][0])

    @patch("jobwinner.browser.diagnostics.find_boss_tab")
    @patch("jobwinner.browser.diagnostics.runtime_targets")
    @patch("jobwinner.browser.diagnostics.ensure_runtime")
    @patch("jobwinner.browser.diagnostics.check_node_available")
    def test_running_runtime_does_not_require_node_on_path(
        self,
        check_node,
        ensure_runtime,
        runtime_targets,
        find_boss_tab,
    ):
        from jobwinner.browser.diagnostics import run_browser_diagnostics

        check_node.return_value = {"available": False, "version": None, "error": "node missing"}
        ensure_runtime.return_value = True
        runtime_targets.return_value = [{"targetId": "1", "url": "https://www.zhipin.com"}]
        find_boss_tab.return_value = {"targetId": "1", "title": "BOSS直聘"}

        result = run_browser_diagnostics({})

        self.assertTrue(result["runtime"])
        self.assertFalse(any("Node.js" in error for error in result["errors"]))

    @patch("jobwinner.browser.diagnostics.runtime_health")
    @patch("jobwinner.browser.diagnostics.ensure_runtime")
    @patch("jobwinner.browser.diagnostics.check_node_available")
    def test_run_browser_diagnostics_reports_non_jobwinner_service_on_runtime_port(self, check_node, ensure_runtime, runtime_health):
        from jobwinner.browser.diagnostics import run_browser_diagnostics

        check_node.return_value = {"available": True, "version": "v22.1.0"}
        ensure_runtime.return_value = False
        runtime_health.return_value = {"status": "ok", "connected": True}

        result = run_browser_diagnostics({})

        self.assertFalse(result["runtime"])
        self.assertTrue(any("non-JobWinner" in error for error in result["errors"]))

    @patch("jobwinner.browser.diagnostics.run_browser_diagnostics")
    def test_print_browser_diagnostics_shows_non_jobwinner_service_message(self, run_browser_diagnostics):
        from jobwinner.browser.diagnostics import print_browser_diagnostics

        run_browser_diagnostics.return_value = {
            "node": {"available": True, "version": "v22.1.0"},
            "runtime": False,
            "chrome": False,
            "targets": [],
            "boss_tab": None,
            "errors": ["Runtime port is occupied by a non-JobWinner service."],
            "runtime_url": "http://127.0.0.1:3456",
            "health": {"status": "ok", "connected": True},
        }
        buffer = io.StringIO()
        console = Console(file=buffer, force_terminal=False, color_system=None, width=120)

        result = print_browser_diagnostics({}, console)

        self.assertFalse(result)
        self.assertIn("端口已被非 JobWinner 服务占用", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
