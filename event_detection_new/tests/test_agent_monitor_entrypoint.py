import unittest
from unittest.mock import Mock, patch


class MonitorEntrypointTest(unittest.TestCase):
    def test_main_runs_idle_server_on_configured_host_and_port(self) -> None:
        from agent_monitor_server.__main__ import main

        config = Mock(server_host="0.0.0.0", server_port=8781)
        app = object()
        with (
            patch("agent_monitor_server.__main__.load_dotenv"),
            patch("agent_monitor_server.__main__.logging.basicConfig") as configure_logging,
            patch("agent_monitor_server.__main__.MonitorConfig.from_env", return_value=config),
            patch("agent_monitor_server.__main__.create_app", return_value=app),
            patch("agent_monitor_server.__main__.web.run_app") as run_app,
        ):
            main()

        run_app.assert_called_once_with(app, host="0.0.0.0", port=8781)
        configure_logging.assert_called_once()
        self.assertEqual(configure_logging.call_args.kwargs["level"], 20)


if __name__ == "__main__":
    unittest.main()
