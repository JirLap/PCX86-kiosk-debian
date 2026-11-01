"""Check voice service scope and network rules without touching the host."""

from pathlib import PurePosixPath
import unittest
from unittest.mock import patch
import install_voice as voice


class VoiceSetupTests(unittest.TestCase):
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
