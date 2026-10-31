"""Verify display-only sleep and MQTT recovery without touching hardware."""

import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

SOURCE = Path(__file__).resolve().parents[1] / 'src/panel_agent.py'
tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
tree.body = [node for node in tree.body if not (
    isinstance(node, ast.Import) and any(name.name == 'psutil' or name.name.startswith('paho.') for name in node.names)
)]
namespace = {'__name__':'agent_test', 'psutil':Mock(), 'mqtt':Mock()}
exec(compile(tree, str(SOURCE), 'exec'), namespace)
PanelAgent = namespace['PanelAgent']


class DisplaySleepTests(unittest.TestCase):
    def agent(self, browser=True, brightness=65):
        agent = PanelAgent.__new__(PanelAgent)
        agent.config = {'mqtt_host':'broker', 'mqtt_port':1883}
        agent._display_sleep = None
        agent.brightness = Mock(return_value=brightness)
        agent.chromium_running = Mock(return_value=browser)
        for method in ('checked','set_brightness','stop_kiosk','start_kiosk','write_config','start_power_action'):
            setattr(agent, method, Mock())
        return agent

    def test_repeat_sleep_keeps_original_brightness_and_browser_state(self):
        agent = self.agent()
        agent.sleep_display()
        agent.brightness.return_value = 0
        agent.chromium_running.return_value = False
        agent.sleep_display()
        self.assertEqual(agent.config['display_sleep'], {'brightness':65, 'restore_kiosk':True})
        agent.write_config.assert_called_once()
        self.assertEqual(agent.set_brightness.call_count, 2)
        self.assertTrue(all(c.args == (0,) for c in agent.set_brightness.call_args_list))
        agent.wake_display()
        agent.set_brightness.assert_called_with(65)
        agent.start_kiosk.assert_called_once()
        self.assertNotIn('display_sleep', agent.config)
        agent.start_power_action.assert_not_called()

    def test_stopped_browser_is_not_started_by_wake(self):
        agent = self.agent(browser=False)
        agent.sleep_display()
        agent.wake_display()
        agent.start_kiosk.assert_not_called()

    def test_no_backlight_device_still_uses_dpms(self):
        agent = self.agent(brightness=None)
        agent.sleep_display()
        agent.wake_display()
        agent.set_brightness.assert_not_called()
        self.assertIn((['/usr/bin/xset','dpms','force','off'],), [c.args for c in agent.checked.call_args_list])
        self.assertIn((['/usr/bin/xset','dpms','force','on'],), [c.args for c in agent.checked.call_args_list])

    def test_legacy_suspend_is_display_sleep_and_never_a_power_action(self):
        for command in ('sleep','screen_off','suspend'):
            with self.subTest(command=command):
                agent = self.agent()
                agent.sleep_display = Mock()
                agent._dispatch(command)
                agent.sleep_display.assert_called_once()
                agent.start_power_action.assert_not_called()

    def test_saved_sleep_state_survives_agent_restart(self):
        agent = self.agent()
        saved = {'brightness':42, 'restore_kiosk':True}
        agent._display_sleep = saved
        agent.config['display_sleep'] = saved
        agent.sleep_display()
        agent.brightness.assert_not_called()
        agent.wake_display()
        agent.set_brightness.assert_called_with(42)


class MqttRecoveryTests(unittest.TestCase):
    def test_command_failure_does_not_escape_mqtt_callback(self):
        agent = PanelAgent.__new__(PanelAgent)
        agent._dispatch = Mock(side_effect=RuntimeError('hardware error'))
        agent.publish_status = Mock()
        with self.assertLogs('pcx86_kiosk', level='ERROR'):
            agent.on_message(None, None, SimpleNamespace(retain=False, payload=b'sleep'))
        agent.publish_status.assert_called_once()
        self.assertIn('hardware error', agent._result)

    def test_sensor_failure_cannot_kill_mqtt_callback(self):
        agent = PanelAgent.__new__(PanelAgent)
        agent._dispatch = Mock()
        agent.publish_status = Mock(side_effect=OSError('sensor error'))
        with self.assertLogs('pcx86_kiosk', level='ERROR'):
            agent.on_message(None, None, SimpleNamespace(retain=False, payload=b'status'))

    def test_network_worker_restarts_after_unexpected_exception(self):
        agent = PanelAgent.__new__(PanelAgent)
        agent._stop = Mock()
        agent._stop.is_set.return_value = False
        agent._stop.wait.side_effect = [False, True]
        agent.client = Mock()
        agent.client.loop_forever.side_effect = [RuntimeError('worker error'), None]
        agent.config = {'mqtt_host':'broker'}
        with self.assertLogs('pcx86_kiosk', level='ERROR'):
            agent.network_loop()
        self.assertEqual(agent.client.loop_forever.call_count, 2)
        agent.client.connect_async.assert_called_once_with('broker',1883,keepalive=45)

    def test_successful_power_helper_exit_is_not_an_error(self):
        from unittest.mock import patch
        process = Mock()
        process.poll.return_value = 0
        process.returncode = 0
        with patch.object(namespace['subprocess'], 'Popen', return_value=process), patch.object(namespace['time'],'sleep'):
            PanelAgent.start_power_action('reboot')
        process.stderr.read.assert_not_called()


if __name__ == '__main__':
    unittest.main()
