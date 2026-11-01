"""Check voice service scope and network rules without touching the host."""

from pathlib import PurePosixPath
import contextlib
import io
import subprocess
import unittest
from unittest.mock import patch
import install
import install_voice as voice


class VoiceSetupTests(unittest.TestCase):
    def select(self, *, default='alsa_input.mic', sources=None, answers=('',), preferred='', interactive=True):
        if sources is None:
            sources = '1\talsa_input.mic\tdriver\n2\talsa_input.usb\tdriver\n3\toutput.monitor\tdriver\n'
        def output(command, **kwargs):
            self.assertEqual(kwargs['timeout'], 10)
            if command == ['pactl', 'get-default-source']:
                if isinstance(default, Exception):
                    raise default
                return default + '\n'
            self.assertEqual(command, ['pactl', 'list', 'short', 'sources'])
            return sources
        with patch.object(voice.shutil, 'which', return_value='/usr/bin/pactl'), patch.object(
            voice.subprocess, 'check_output', side_effect=output
        ), patch('builtins.input', side_effect=answers) as prompt, contextlib.redirect_stdout(io.StringIO()):
            selected = voice.select_microphone(preferred, interactive=interactive)
        return selected, prompt

    def test_enter_accepts_detected_real_default(self):
        selected, prompt = self.select()
        self.assertEqual(selected, 'alsa_input.mic')
        self.assertIn('[alsa_input.mic]', prompt.call_args.args[0])

    def test_manual_override_and_saved_preference(self):
        self.assertEqual(self.select(answers=('alsa_input.usb',))[0], 'alsa_input.usb')
        selected, prompt = self.select(preferred='alsa_input.usb')
        self.assertEqual(selected, 'alsa_input.usb')
        self.assertIn('[alsa_input.usb]', prompt.call_args.args[0])

    def test_monitor_or_missing_default_is_never_offered(self):
        for default in ('output.monitor', 'missing', ''):
            with self.subTest(default=default):
                selected, prompt = self.select(default=default, answers=('alsa_input.usb',))
                self.assertEqual(selected, 'alsa_input.usb')
                self.assertNotIn('[', prompt.call_args.args[0])

    def test_stale_or_monitor_preference_offers_real_system_default(self):
        for preferred in ('missing', 'output.monitor'):
            with self.subTest(preferred=preferred):
                selected, prompt = self.select(preferred=preferred)
                self.assertEqual(selected, 'alsa_input.mic')
                self.assertIn('[alsa_input.mic]', prompt.call_args.args[0])

    def test_failed_default_lookup_allows_manual_selection(self):
        error = subprocess.CalledProcessError(1, ['pactl'])
        self.assertEqual(self.select(default=error, answers=('alsa_input.mic',))[0], 'alsa_input.mic')

    def test_no_microphone_can_be_skipped(self):
        for sources in ('', '1\toutput.monitor\tdriver\n'):
            with self.subTest(sources=sources):
                self.assertIsNone(self.select(sources=sources)[0])

    def test_invalid_selection_can_be_skipped_or_corrected(self):
        for selected in ('output.monitor', 'missing', '!clear'):
            with self.subTest(selected=selected):
                self.assertIsNone(self.select(answers=(selected, 'skip'))[0])
        self.assertEqual(self.select(answers=('missing', 'retry', 'alsa_input.usb'))[0], 'alsa_input.usb')

    def test_retry_rediscovers_connected_microphone(self):
        with patch.object(voice.shutil, 'which', return_value='/usr/bin/pactl'), patch.object(
            voice, 'audio_names', side_effect=[set(), {'alsa_input.usb'}]
        ), patch.object(voice.subprocess, 'check_output', return_value='alsa_input.usb\n'), patch(
            'builtins.input', side_effect=['retry', '']
        ), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(voice.select_microphone(), 'alsa_input.usb')

    def test_missing_pactl_or_unreachable_audio_can_be_skipped(self):
        for error in (OSError('offline'), subprocess.CalledProcessError(1, ['pactl']), subprocess.TimeoutExpired(['pactl'], 10)):
            with self.subTest(error=error), patch.object(voice.shutil, 'which', return_value='/usr/bin/pactl'), patch.object(
                voice, 'audio_names', side_effect=error
            ), patch('builtins.input', return_value=''), contextlib.redirect_stdout(io.StringIO()):
                self.assertIsNone(voice.select_microphone())
        with patch.object(voice.shutil, 'which', return_value=None), patch('builtins.input', return_value=''), contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(voice.select_microphone())

    def test_noninteractive_selection_does_not_prompt(self):
        selected, prompt = self.select(interactive=False)
        self.assertEqual(selected, 'alsa_input.mic')
        prompt.assert_not_called()
        for kwargs in ({'sources': ''}, {'preferred': 'missing'}, {'preferred': 'output.monitor'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(SystemExit):
                self.select(interactive=False, **kwargs)

    def test_skip_returns_before_any_installation_or_config_write(self):
        with patch.object(voice.os, 'geteuid', return_value=1000, create=True), patch.object(
            voice.Path, 'exists', side_effect=lambda: True
        ), patch.object(voice.Path, 'read_text', return_value='{}'), patch.object(
            voice.Path, 'write_text'
        ) as write, patch.object(voice, 'select_microphone', return_value=None), patch.object(
            voice.subprocess, 'run'
        ) as run, patch('sys.argv', ['install_voice.py']):
            voice.main()
        write.assert_not_called()
        run.assert_not_called()

    def test_optional_voice_failure_does_not_abort_kiosk(self):
        for error in (OSError('unavailable'), subprocess.CalledProcessError(1, ['voice'])):
            with self.subTest(error=error), patch.object(install.subprocess, 'run', side_effect=error), contextlib.redirect_stdout(io.StringIO()) as output:
                install.install_optional_voice()
                self.assertIn('Kiosk installation is prepared', output.getvalue())

    def test_firewall_only_exposes_voice_to_the_ha_hosts(self):
        rules = voice.render_firewall(['192.0.2.10','2001:db8::10'],6053)
        self.assertIn('table inet pcx86_voice',rules)
        self.assertIn('tcp dport 6053 ip saddr { 192.0.2.10 } accept',rules)
        self.assertIn('tcp dport 6053 ip6 saddr { 2001:db8::10 } accept',rules)
        self.assertIn('tcp dport 6053 drop',rules)
        self.assertNotIn('flush ruleset',rules+voice.NETWORK_HELPER)
        self.assertNotIn('tcp dport 22',rules)

    def test_invalid_or_broad_network_addresses_are_rejected(self):
        for address in ('0.0.0.0','::','224.0.0.251','192.0.2.0/24','bad\naddress'):
            with self.subTest(address=address), self.assertRaises(ValueError):
                voice.resolve_ha_addresses(address)

    def test_hostname_is_resolved_to_explicit_addresses(self):
        with patch.object(voice.socket,'getaddrinfo',return_value=[(2,1,6,'',('192.0.2.20',0))]):
            self.assertEqual(voice.resolve_ha_addresses('ha.example'),['192.0.2.20'])

    def test_service_uses_existing_desktop_audio_and_persistent_preferences(self):
        home = PurePosixPath('/home/kiosk')
        config = {'name':'Hall voice','port':6053,'wake_word':'hey_jarvis','input_device':'alsa_input.test','output_device':'pulse/alsa_output.test'}
        unit = voice.render_service(config,home,'kiosk',1000,home/'lva')
        self.assertIn('User=kiosk',unit)
        self.assertIn('WorkingDirectory=/home/kiosk/lva\n',unit)
        self.assertIn('PULSE_SERVER=unix:/run/user/1000/pulse/native',unit)
        self.assertIn('--disable-peripheral-api',unit)
        self.assertIn('--audio-input-device',unit)
        self.assertIn('alsa_input.test',unit)
        self.assertIn('preferences.json',unit)
        self.assertIn('Requires=pcx86-voice-network.service',unit)
        self.assertNotIn('pipewire-pulse',voice.PACKAGES)
        self.assertNotIn('User=root',unit)

    def test_service_strings_cannot_inject_new_unit_lines(self):
        with self.assertRaises(ValueError):
            voice.systemd_quote('bad\nExecStart=bad')
        self.assertEqual(voice.systemd_quote('room%1'), '"room%%1"')

    def test_source_is_pinned_to_a_full_commit(self):
        self.assertRegex(voice.LVA_COMMIT,r'^[0-9a-f]{40}$')
        self.assertEqual(voice.LVA_VERSION,'v1.1.18')

    def test_audio_preflight_rejects_output_loopback_and_missing_devices(self):
        with patch.object(voice.shutil,'which',return_value='/usr/bin/pactl'), patch.object(
            voice.subprocess,'check_output',side_effect=lambda args, **kwargs:
                '1\talsa_input.mic\tdriver\n2\talsa_output.speaker.monitor\tdriver\n'
                if args[-1] == 'sources' else '1\talsa_output.speaker\tdriver\n'
        ):
            voice.validate_audio({'input_device':'alsa_input.mic','output_device':'pulse/alsa_output.speaker'})
            for source, output in [('alsa_output.speaker.monitor','pulse'),('missing','pulse'),('alsa_input.mic','pulse/missing')]:
                with self.subTest(source=source,output=output), self.assertRaises(ValueError):
                    voice.validate_audio({'input_device':source,'output_device':output})


if __name__ == '__main__':
    unittest.main()
