# Copyright 2026 The android_world Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Unit tests for AppiumAdbBridge."""

from unittest import mock

from absl.testing import absltest
from android_env.proto import adb_pb2
from android_world.env.agentruntime import appium_adb_bridge
from android_world.env.agentruntime import appium_env as appium_env_lib


def _make_bridge():
  """Return an AppiumAdbBridge instance with a mocked AppiumEnv."""
  bridge = appium_adb_bridge.AppiumAdbBridge()
  mock_env = mock.create_autospec(appium_env_lib.AppiumEnv, instance=True)
  mock_env.shell.return_value = ''
  mock_env.driver = mock.MagicMock()
  bridge._appium_env = mock_env
  return bridge, mock_env


def _make_request(**kwargs) -> adb_pb2.AdbRequest:
  return adb_pb2.AdbRequest(**kwargs)


class AppiumAdbBridgeGenericTest(absltest.TestCase):

  def test_generic_shell_strips_shell_prefix(self):
    bridge, env = _make_bridge()
    env.shell.return_value = 'package:android'
    req = adb_pb2.AdbRequest(
        generic=adb_pb2.AdbRequest.GenericRequest(
            args=['shell', 'pm', 'list', 'packages', 'android']
        )
    )
    resp = bridge.execute_adb_call(req)
    self.assertEqual(resp.status, adb_pb2.AdbResponse.Status.OK)
    env.shell.assert_called_once_with('pm list packages android')

  def test_generic_empty_args_returns_error(self):
    bridge, _ = _make_bridge()
    req = adb_pb2.AdbRequest(
        generic=adb_pb2.AdbRequest.GenericRequest(args=[])
    )
    resp = bridge.execute_adb_call(req)
    self.assertEqual(resp.status, adb_pb2.AdbResponse.Status.INTERNAL_ERROR)

  def test_unknown_command_returns_unknown(self):
    bridge, _ = _make_bridge()
    # Empty AdbRequest has no oneof set.
    req = adb_pb2.AdbRequest()
    resp = bridge.execute_adb_call(req)
    self.assertEqual(resp.status, adb_pb2.AdbResponse.Status.UNKNOWN_COMMAND)


class AppiumAdbBridgeTapTest(absltest.TestCase):

  def test_tap_calls_driver_tap(self):
    bridge, env = _make_bridge()
    req = adb_pb2.AdbRequest(
        tap=adb_pb2.AdbRequest.Tap(x=100, y=200)
    )
    resp = bridge.execute_adb_call(req)
    self.assertEqual(resp.status, adb_pb2.AdbResponse.Status.OK)
    env.driver.tap.assert_called_once_with([(100, 200)])


class AppiumAdbBridgePressButtonTest(absltest.TestCase):

  def test_press_home(self):
    bridge, env = _make_bridge()
    req = adb_pb2.AdbRequest(
        press_button=adb_pb2.AdbRequest.PressButton(
            button=adb_pb2.AdbRequest.PressButton.HOME
        )
    )
    resp = bridge.execute_adb_call(req)
    self.assertEqual(resp.status, adb_pb2.AdbResponse.Status.OK)
    env.driver.press_keycode.assert_called_once_with(3)  # KEYCODE_HOME

  def test_press_back(self):
    bridge, env = _make_bridge()
    req = adb_pb2.AdbRequest(
        press_button=adb_pb2.AdbRequest.PressButton(
            button=adb_pb2.AdbRequest.PressButton.BACK
        )
    )
    resp = bridge.execute_adb_call(req)
    self.assertEqual(resp.status, adb_pb2.AdbResponse.Status.OK)
    env.driver.press_keycode.assert_called_once_with(4)  # KEYCODE_BACK


class AppiumAdbBridgeInputTextTest(absltest.TestCase):

  def test_input_text_shell_call(self):
    bridge, env = _make_bridge()
    req = adb_pb2.AdbRequest(
        input_text=adb_pb2.AdbRequest.InputText(text='hello world')
    )
    resp = bridge.execute_adb_call(req)
    self.assertEqual(resp.status, adb_pb2.AdbResponse.Status.OK)
    env.shell.assert_called_once()
    call_cmd = env.shell.call_args[0][0]
    self.assertIn('input text', call_cmd)
    self.assertIn('hello world', call_cmd)


class AppiumAdbBridgeSettingsTest(absltest.TestCase):

  def test_settings_get(self):
    bridge, env = _make_bridge()
    env.shell.return_value = '1'
    req = adb_pb2.AdbRequest(
        settings=adb_pb2.AdbRequest.SettingsRequest(
            name_space=adb_pb2.AdbRequest.SettingsRequest.GLOBAL,
            get=adb_pb2.AdbRequest.SettingsRequest.Get(key='airplane_mode_on'),
        )
    )
    resp = bridge.execute_adb_call(req)
    self.assertEqual(resp.status, adb_pb2.AdbResponse.Status.OK)
    env.shell.assert_called_once_with('settings get global airplane_mode_on')

  def test_settings_put(self):
    bridge, env = _make_bridge()
    req = adb_pb2.AdbRequest(
        settings=adb_pb2.AdbRequest.SettingsRequest(
            name_space=adb_pb2.AdbRequest.SettingsRequest.SYSTEM,
            put=adb_pb2.AdbRequest.SettingsRequest.Put(
                key='screen_brightness', value='128'
            ),
        )
    )
    resp = bridge.execute_adb_call(req)
    self.assertEqual(resp.status, adb_pb2.AdbResponse.Status.OK)
    env.shell.assert_called_once_with('settings put system screen_brightness 128')


class AppiumAdbBridgeStartActivityTest(absltest.TestCase):

  def test_start_activity(self):
    bridge, env = _make_bridge()
    env.shell.return_value = 'Starting: Intent ...'
    req = adb_pb2.AdbRequest(
        start_activity=adb_pb2.AdbRequest.StartActivity(
            full_activity='com.example/.MainActivity',
            force_stop=False,
        )
    )
    resp = bridge.execute_adb_call(req)
    self.assertEqual(resp.status, adb_pb2.AdbResponse.Status.OK)
    env.shell.assert_called_once()
    self.assertIn('am start', env.shell.call_args[0][0])

  def test_start_activity_with_force_stop(self):
    bridge, env = _make_bridge()
    env.shell.return_value = ''
    req = adb_pb2.AdbRequest(
        start_activity=adb_pb2.AdbRequest.StartActivity(
            full_activity='com.example/.MainActivity',
            force_stop=True,
        )
    )
    bridge.execute_adb_call(req)
    # Should have called shell twice: once for force-stop, once for start.
    self.assertEqual(env.shell.call_count, 2)
    first_cmd = env.shell.call_args_list[0][0][0]
    self.assertIn('force-stop', first_cmd)


class AppiumAdbBridgeErrorRecoveryTest(absltest.TestCase):

  def test_handler_exception_returns_error_response(self):
    bridge, env = _make_bridge()
    env.shell.side_effect = RuntimeError('driver died')
    req = adb_pb2.AdbRequest(
        generic=adb_pb2.AdbRequest.GenericRequest(args=['shell', 'ls'])
    )
    resp = bridge.execute_adb_call(req)
    self.assertEqual(resp.status, adb_pb2.AdbResponse.Status.INTERNAL_ERROR)
    self.assertIn('driver died', resp.error_message)


def _emu_sms_request(phone, *words):
  return adb_pb2.AdbRequest(
      generic=adb_pb2.AdbRequest.GenericRequest(
          args=['emu', 'sms', 'send', phone, *words]
      )
  )


class AppiumAdbBridgeSmsTest(absltest.TestCase):

  def test_emu_sms_send_uses_device_tool_with_base64_args(self):
    bridge, env = _make_bridge()
    env.shell.side_effect = ['yes\n', 'rc=0\n']
    resp = bridge.execute_adb_call(
        _emu_sms_request('+1555', "it's", 'a "test"')
    )
    self.assertEqual(resp.status, adb_pb2.AdbResponse.Status.OK)
    cmd = env.shell.call_args_list[1][0][0]
    self.assertEqual(
        cmd,
        '/system/bin/smartrun-sms-inject --b64 KzE1NTU= '
        'aXQncyBhICJ0ZXN0Ig==; echo rc=$?',
    )

  def test_emu_sms_send_reports_tool_failure(self):
    bridge, env = _make_bridge()
    env.shell.side_effect = ['yes\n', 'mmssms.db missing\nrc=1\n']
    resp = bridge.execute_adb_call(_emu_sms_request('+1555', 'hi'))
    self.assertEqual(resp.status, adb_pb2.AdbResponse.Status.INTERNAL_ERROR)
    self.assertIn('mmssms.db missing', resp.error_message)

  def test_emu_sms_send_falls_back_to_sqlite_without_tool(self):
    bridge, env = _make_bridge()
    env.shell.side_effect = ['', 'Row: 0 _id=7\n', '']
    resp = bridge.execute_adb_call(_emu_sms_request('+1555', 'hi'))
    self.assertEqual(resp.status, adb_pb2.AdbResponse.Status.OK)
    self.assertIn('INSERT INTO sms', env.shell.call_args_list[2][0][0])


if __name__ == '__main__':
  absltest.main()
