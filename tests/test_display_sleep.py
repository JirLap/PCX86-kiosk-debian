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
        agent._last_idle_ms = None
        agent.brightness = Mock(return_value=brightness)
        agent.chromium_running = Mock(return_value=browser)
        for method in ('checked','set_brightness','stop_kiosk','start_kiosk','write_config','start_power_action','configure_desktop_display'):
            setattr(agent, method, Mock())
        return agent

    def test_repeat_sleep_keeps_original_brightness_and_browser_state(self):
        agent = self.agent()
        agent.sleep_display()
        agent.brightness.return_value = 0
        agent.chromium_running.return_value = False
        agent.sleep_display()
        self.assertEqual(agent.config['display_sleep'], {'brightness':65, 'restore_kiosk':True, 'reason':'manual'})
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

    def test_native_timers_are_disabled_so_the_agent_owns_idle_blanking(self):
        agent = self.agent()
        agent.config.update(screensaver_enabled=True,screensaver_timeout=7)
        agent.apply_screensaver()
        agent.checked.assert_any_call(['/usr/bin/xset','s','off'])
        agent.checked.assert_any_call(['/usr/bin/xset','dpms','0','0','0'])
        agent.config['screensaver_enabled'] = False
        agent.apply_screensaver()
        agent.checked.assert_called_with(['/usr/bin/xset','dpms','0','0','0'])

    def test_manual_sleep_overrides_idle_timeout_and_wake_restores_it(self):
        agent = self.agent()
        agent.config.update(screensaver_enabled=True,screensaver_timeout=3)
        agent.sleep_display()
        agent.checked.assert_any_call(['/usr/bin/xset','dpms','0','0','0'])
        agent.wake_display()
        self.assertTrue(agent.config['screensaver_enabled'])
        self.assertEqual(agent.config['screensaver_timeout'],3)
        agent.checked.assert_any_call(['/usr/bin/xset','s','reset'])

    def test_disabling_screensaver_wakes_idle_screen_but_not_manual_sleep(self):
        agent = self.agent()
        agent._dispatch('screensaver_off')
        agent.checked.assert_any_call(['/usr/bin/xset','dpms','force','on'])
        agent.checked.reset_mock()
        agent._display_sleep = {'brightness':65}
        agent._dispatch('screensaver_off')
        self.assertNotIn((['/usr/bin/xset','dpms','force','on'],),[c.args for c in agent.checked.call_args_list])

    def test_timeout_validation_and_persistence(self):
        agent = self.agent()
        for invalid in ('0','241','x'):
            with self.assertRaises(ValueError):
                agent._dispatch('screensaver_timeout_set:'+invalid)
        agent.write_config.assert_not_called()
        agent._dispatch('screensaver_timeout_set:12')
        self.assertEqual(agent.config['screensaver_timeout'],12)
        agent.write_config.assert_called_once()

    def test_idle_timeout_starts_automatic_sleep_and_local_input_wakes_it(self):
        from unittest.mock import patch
        agent = self.agent()
        agent.config.update(screensaver_enabled=True,screensaver_timeout=1)
        with patch.object(namespace['shutil'],'which',return_value='/usr/bin/xprintidle'), patch.dict(PanelAgent.check_screensaver.__globals__,run=Mock(return_value=SimpleNamespace(returncode=0,stdout='61000'))):
            agent.check_screensaver()
        self.assertEqual(agent._display_sleep['reason'],'screensaver')
        agent.set_brightness.assert_called_with(0)
        with patch.object(namespace['shutil'],'which',return_value='/usr/bin/xprintidle'), patch.dict(PanelAgent.check_screensaver.__globals__,run=Mock(return_value=SimpleNamespace(returncode=0,stdout='200'))):
            agent.check_screensaver()
        self.assertIsNone(agent._display_sleep)
        agent.start_kiosk.assert_called_once()

    def test_manual_sleep_is_not_canceled_by_local_input(self):
        agent = self.agent()
        agent.sleep_display()
        agent.check_screensaver()
        self.assertIsNotNone(agent._display_sleep)

    def test_monitor_reports_standby_and_suspend_as_screen_off(self):
        from unittest.mock import patch
        for mode,expected in [('On','on'),('Off','off'),('Standby','off'),('Suspend','off')]:
            with patch.dict(namespace['os'].environ,{'DISPLAY':':0'}), patch.dict(PanelAgent.monitor_state.__globals__,run=Mock(return_value=SimpleNamespace(stdout='Monitor is '+mode))):
                self.assertEqual(PanelAgent.monitor_state(),expected)
        with patch.dict(namespace['os'].environ,{'DISPLAY':':0'}), patch.dict(PanelAgent.monitor_state.__globals__,run=Mock(return_value=SimpleNamespace(stdout='DPMS is Disabled'))):
            self.assertEqual(PanelAgent.monitor_state(),'on')

    def test_browser_password_store_is_explicit_and_legacy_choice_is_preserved(self):
        from unittest.mock import patch
        for store in ('auto','basic',None):
            agent = self.agent(browser=False)
            if store is not None:
                agent.config['browser_password_store']=store
            agent.config['kiosk_url']='https://ha.example'
            with patch.object(namespace['subprocess'],'Popen') as launch, patch.object(namespace['Path'],'read_text',return_value='Exec=chromium --password-store=basic --kiosk'):
                PanelAgent.start_kiosk(agent)
            args = launch.call_args.args[0]
            self.assertEqual('--password-store=basic' in args,store != 'auto')


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
