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

"""ADB-to-Appium protocol bridge.

``AppiumAdbBridge`` is a mixin that implements ``execute_adb_call()`` —  the
single method that ``android_world/env/adb_utils.py`` calls for every device
operation — in terms of Appium and ``mobile: shell`` calls.

Architecture
------------
``AdbRequest`` is a Protobuf message with a ``oneof command`` field.  Each
variant maps to a specific device operation.  This bridge dispatches on
``WhichOneof('command')`` and translates each variant into the most
appropriate Appium equivalent:

  1. **Appium native API** (tap, swipe, key events, app lifecycle …)
  2. ``mobile: shell <cmd>`` for everything else (pm, am, settings, dumpsys …)
  3. Wraps results back into ``AdbResponse`` protos.

``Tap`` coordinate convention
------------------------------
``adb_utils.tap_screen(x, y, env)`` passes *absolute pixel* coordinates
(integer) in the ``Tap.x`` / ``Tap.y`` fields.  Do NOT multiply by screen
size; use ``driver.tap([(x, y)])`` directly.

``GenericRequest`` parsing
--------------------------
``GenericRequest.args`` is a *repeated string* field: the first element is
expected to be ``'shell'``, followed by the actual shell tokens.  Some callers
pass a pre-split list, others rely on the whole thing being re-joined.  The
bridge joins everything after (and including) the first ``shell`` token with
spaces and executes via ``mobile: shell``.

``Pull`` directory semantics
-----------------------------
``AndroidWorldController.pull_file(remote_path)`` passes
``os.path.dirname(remote_path)`` to ``file_utils.tmp_directory_from_device``
which in turn issues a ``Pull(path=dir_path)`` request.  The bridge must pull
an entire directory, not a single file.

``SettingsRequest``
-------------------
``SettingsRequest`` uses ``name_space`` + one of ``{get, put, delete, list}``
sub-messages.  We map all of them to ``mobile: shell settings …``.
"""

from __future__ import annotations

import base64
import contextlib
import os
import re
import tempfile
import time
from typing import TYPE_CHECKING

from absl import logging
from android_env.proto import adb_pb2


# Path to the SMS database on Android devices (device-encrypted storage).
_SMS_DB_PATH = (
    '/data/user_de/0/com.android.providers.telephony/databases/mmssms.db'
)

# Device-side `emu sms send` replacement baked into SmartRun redroid images.
_SMS_INJECT_TOOL = '/system/bin/smartrun-sms-inject'


def _b64(text: str) -> str:
  return base64.b64encode(text.encode('utf-8')).decode('ascii')


if TYPE_CHECKING:
  from android_world.env.agentruntime.appium_env import AppiumEnv


# Mapping from PressButton.Button enum value to the corresponding Android
# keycode passed to Appium's press_keycode().
_BUTTON_TO_KEYCODE = {
    adb_pb2.AdbRequest.PressButton.HOME: 3,    # KEYCODE_HOME
    adb_pb2.AdbRequest.PressButton.BACK: 4,    # KEYCODE_BACK
    adb_pb2.AdbRequest.PressButton.ENTER: 66,  # KEYCODE_ENTER
}

# SettingsRequest.Namespace enum → settings namespace string
_NAMESPACE_TO_STR = {
    adb_pb2.AdbRequest.SettingsRequest.SYSTEM: 'system',
    adb_pb2.AdbRequest.SettingsRequest.SECURE: 'secure',
    adb_pb2.AdbRequest.SettingsRequest.GLOBAL: 'global',
}


def _ok_response(**kwargs) -> adb_pb2.AdbResponse:
  """Return an AdbResponse with Status.OK and optional payload fields."""
  return adb_pb2.AdbResponse(
      status=adb_pb2.AdbResponse.Status.OK,
      **kwargs,
  )


def _error_response(msg: str) -> adb_pb2.AdbResponse:
  """Return an AdbResponse with INTERNAL_ERROR status."""
  logging.error('ADB bridge error: %s', msg)
  return adb_pb2.AdbResponse(
      status=adb_pb2.AdbResponse.Status.INTERNAL_ERROR,
      error_message=msg,
  )


class AppiumAdbBridge:
  """Mixin that implements ``execute_adb_call`` on top of an ``AppiumEnv``.

  Subclasses must set ``self._appium_env`` before calling any methods.
  ``AppiumController`` does this in its ``__init__``.
  """

  # Subclass must set this.
  _appium_env: 'AppiumEnv'

  # -------------------------------------------------------------------------
  # Public entry point
  # -------------------------------------------------------------------------

  def execute_adb_call(
      self, call: adb_pb2.AdbRequest
  ) -> adb_pb2.AdbResponse:
    """Translate an ``AdbRequest`` proto into Appium / shell operations.

    This is the single method that the entire ``adb_utils.py`` layer calls.
    Every upper-layer ``adb_utils.*`` function ends up here.

    Args:
      call: The ``AdbRequest`` message describing the desired operation.

    Returns:
      An ``AdbResponse`` whose ``status`` field is ``OK`` on success.
    """
    variant = call.WhichOneof('command')
    handler = self._DISPATCH.get(variant)
    if handler is None:
      msg = f'Unsupported AdbRequest variant: {variant!r}'
      logging.warning('%s — returning UNKNOWN_COMMAND', msg)
      return adb_pb2.AdbResponse(
          status=adb_pb2.AdbResponse.Status.UNKNOWN_COMMAND,
          error_message=msg,
      )
    try:
      return handler(self, call)
    except Exception as exc:  # pylint: disable=broad-except
      msg = f'ADB bridge handler for {variant!r} raised: {exc}'
      # 'pm clear' on system apps always exits with code 1 ("Failed") because
      # the package lives on the read-only system partition.  This is expected
      # behaviour; downgrade to a warning so it doesn't pollute ERROR logs.
      exc_str = str(exc)
      if 'pm clear' in exc_str and (
          'exited with code 1' in exc_str or 'Failed' in exc_str
      ):
        logging.warning('ADB bridge: %s (expected for system apps)', msg)
      else:
        logging.error('ADB bridge error: %s', msg)
      return adb_pb2.AdbResponse(
          status=adb_pb2.AdbResponse.Status.INTERNAL_ERROR,
          error_message=msg,
      )

  # -------------------------------------------------------------------------
  # Per-variant handlers
  # -------------------------------------------------------------------------

  def _handle_generic(self, call: adb_pb2.AdbRequest) -> adb_pb2.AdbResponse:
    """Execute a GenericRequest via ``mobile: shell``."""
    args = list(call.generic.args)
    if not args:
      return _error_response('GenericRequest.args is empty')

    # args[0] is typically 'shell'; strip it and join the rest.
    if args[0].lower() == 'shell':
      args = args[1:]

    # Intercept 'emu sms send <phone> <message>' which is an emulator console
    # command that cannot be executed via mobile: shell.  Translate it into
    # sqlite3 + content-provider operations that achieve the same result.
    if (len(args) >= 4
        and args[0].lower() == 'emu'
        and args[1].lower() == 'sms'
        and args[2].lower() == 'send'):
      phone = args[3]
      message = ' '.join(args[4:]) if len(args) > 4 else ''
      return self._inject_sms_via_sqlite(phone, message)

    command = ' '.join(args)
    output = self._appium_env.shell(command)
    return _ok_response(
        generic=adb_pb2.AdbResponse.GenericResponse(
            output=output.encode('utf-8', errors='replace')
        )
    )

  def _inject_sms_via_sqlite(
      self, phone: str, message: str
  ) -> adb_pb2.AdbResponse:
    """Inject an incoming SMS by writing directly to the telephony database.

    This is a fallback for environments where ``emu sms send`` is unavailable
    (e.g. sandbox/redroid without emulator console access).  If the image
    ships ``/system/bin/smartrun-sms-inject`` it is used.  Otherwise:

      1. Query ``content://mms-sms/threadID?recipient=<phone>`` to obtain or
         create a conversation thread.
      2. Insert a row into the ``sms`` table via ``sqlite3`` with the correct
         ``thread_id``, so the SMS app can display it immediately.

    Args:
      phone: The sender phone number (e.g. '+15551234567').
      message: The SMS body text.

    Returns:
      An ``AdbResponse`` with OK status on success.
    """
    logging.info(
        'Injecting SMS (emu sms send replacement): '
        'from=%s body=%r', phone, message[:60]
    )
    shell = self._appium_env.shell

    # Preferred path: the device-side tool shipped in SmartRun redroid images
    # (>= v33.3).  It handles thread creation and the FTS/trusted_schema quirks
    # itself.  Arguments go base64-encoded so no quoting survives to the
    # device shell, whatever the message contains.
    if 'yes' in shell(f'test -x {_SMS_INJECT_TOOL} && echo yes'):
      output = shell(
          f'{_SMS_INJECT_TOOL} --b64 {_b64(phone)} {_b64(message)}; '
          'echo rc=$?'
      )
      if 'rc=0' not in output:
        return _error_response(f'smartrun-sms-inject failed: {output!r}')
      logging.info('SMS injected via %s: from=%s', _SMS_INJECT_TOOL, phone)
      return _ok_response(
          generic=adb_pb2.AdbResponse.GenericResponse(output=b'OK')
      )

    # Fallback for images without the tool.
    # Step 1: Get or create a thread for this phone number.
    thread_query = (
        f'content query --uri '
        f'"content://mms-sms/threadID?recipient={phone}"'
    )
    thread_output = shell(thread_query)
    # Expected output: "Row: 0 _id=<N>"
    match = re.search(r'_id=(\d+)', thread_output)
    if not match:
      return _error_response(
          f'Failed to obtain thread_id for {phone}: {thread_output!r}'
      )
    thread_id = match.group(1)

    # Step 2: Insert SMS via sqlite3.  Use single-quote escaping for the
    # message body to prevent SQL injection.
    escaped_body = message.replace("'", "''")
    escaped_phone = phone.replace("'", "''")
    now_ms = int(time.time() * 1000)
    sql = (
        f"INSERT INTO sms "
        f"(thread_id, address, body, date, type, read, seen) "
        f"VALUES ({thread_id}, '{escaped_phone}', '{escaped_body}', "
        f"{now_ms}, 1, 0, 0);"
    )
    insert_cmd = f'sqlite3 {_SMS_DB_PATH} "{sql}"'
    insert_output = shell(insert_cmd)

    if insert_output.strip():
      # sqlite3 prints nothing on success; any output likely indicates error.
      logging.warning('sqlite3 SMS insert output: %s', insert_output)

    logging.info(
        'SMS injected: thread_id=%s from=%s', thread_id, phone
    )
    return _ok_response(
        generic=adb_pb2.AdbResponse.GenericResponse(
            output=b'OK'
        )
    )

  def _handle_tap(self, call: adb_pb2.AdbRequest) -> adb_pb2.AdbResponse:
    """Execute a Tap via Appium tap().  Coordinates are absolute pixels."""
    x = int(call.tap.x)
    y = int(call.tap.y)
    driver = self._appium_env.driver
    driver.tap([(x, y)])
    return _ok_response()

  def _handle_press_button(
      self, call: adb_pb2.AdbRequest
  ) -> adb_pb2.AdbResponse:
    """Press HOME, BACK, or ENTER via Appium press_keycode()."""
    button = call.press_button.button
    keycode = _BUTTON_TO_KEYCODE.get(button)
    if keycode is None:
      return _error_response(f'Unknown button value: {button}')
    self._appium_env.driver.press_keycode(keycode)
    return _ok_response(
        press_button=adb_pb2.AdbResponse.PressButtonResponse()
    )

  def _handle_input_text(
      self, call: adb_pb2.AdbRequest
  ) -> adb_pb2.AdbResponse:
    """Type text via Appium set_value on the active element."""
    text = call.input_text.text
    # Use mobile: shell input text for reliable Unicode support.
    # Escape single quotes in the text.
    escaped = text.replace("'", "'\\''")
    self._appium_env.shell(f"input text '{escaped}'")
    return _ok_response(
        input_text=adb_pb2.AdbResponse.InputTextResponse()
    )

  def _handle_start_activity(
      self, call: adb_pb2.AdbRequest
  ) -> adb_pb2.AdbResponse:
    """Start an Android activity via ``am start``."""
    sa = call.start_activity
    activity = sa.full_activity
    extra_args = ' '.join(sa.extra_args) if sa.extra_args else ''

    if sa.force_stop:
      # Extract package and force-stop before launching.
      pkg = activity.split('/')[0] if '/' in activity else activity
      self._appium_env.shell(f'am force-stop {pkg}')

    cmd = f'am start -n {activity}'
    if extra_args:
      cmd = f'{cmd} {extra_args}'
    output = self._appium_env.shell(cmd)
    return _ok_response(
        start_activity=adb_pb2.AdbResponse.StartActivityResponse(
            full_activity=activity,
            output=output.encode('utf-8', errors='replace'),
        )
    )

  def _handle_force_stop(
      self, call: adb_pb2.AdbRequest
  ) -> adb_pb2.AdbResponse:
    """Force-stop an app package via ``am force-stop``."""
    pkg = call.force_stop.package_name
    self._appium_env.shell(f'am force-stop {pkg}')
    return _ok_response()

  def _handle_get_current_activity(
      self, call: adb_pb2.AdbRequest
  ) -> adb_pb2.AdbResponse:
    """Return the foreground activity as ``package/activity``."""
    pkg, act = self._appium_env.current_activity()
    if pkg and act:
      full = f'{pkg}/{act}'
    elif pkg:
      full = pkg
    else:
      full = ''
    return _ok_response(
        get_current_activity=adb_pb2.AdbResponse.GetCurrentActivityResponse(
            full_activity=full
        )
    )

  def _handle_get_orientation(
      self, call: adb_pb2.AdbRequest
  ) -> adb_pb2.AdbResponse:
    """Return the current screen orientation as an integer (0=portrait)."""
    orientation_str = self._appium_env.get_orientation()
    # Map Appium's string to AndroidWorld's integer convention.
    _ORI_MAP = {
        'PORTRAIT': 0,
        'LANDSCAPE': 1,
        'PORTRAIT_REVERSED': 2,
        'LANDSCAPE_REVERSED': 3,
    }
    ori_int = _ORI_MAP.get(orientation_str.upper(), 0)
    return _ok_response(
        get_orientation=adb_pb2.AdbResponse.GetOrientationResponse(
            orientation=ori_int
        )
    )

  def _handle_push(self, call: adb_pb2.AdbRequest) -> adb_pb2.AdbResponse:
    """Push bytes to a remote path on the device."""
    remote_path = call.push.path
    content = call.push.content

    # Write content to a local temp file, then push.
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
      tmp.write(content)
      tmp_path = tmp.name
    try:
      self._appium_env.push_file(tmp_path, remote_path)
    finally:
      os.unlink(tmp_path)
    return _ok_response(push=adb_pb2.AdbResponse.PushResponse())

  def _handle_pull(self, call: adb_pb2.AdbRequest) -> adb_pb2.AdbResponse:
    """Pull a file or directory from the device.

    ``adb_utils`` always passes a *directory* path (it calls
    ``os.path.dirname(remote_file_path)`` before issuing this request via
    ``file_utils.tmp_directory_from_device``).  We pull all files in the
    directory into a local temp directory and return the content of the first
    successfully pulled file as the response (the caller uses the local dir,
    not the response payload, for actual data access).
    """
    remote_path = call.pull.path

    # Try to determine whether it's a file or a directory.
    stat_out = self._appium_env.shell(f'stat -c "%F" "{remote_path}"')
    is_dir = 'directory' in stat_out.lower()

    if is_dir:
      tmp_dir = tempfile.mkdtemp(prefix='aw_pull_')
      self._appium_env.pull_directory(remote_path, tmp_dir)
      # Return empty content; callers use the local filesystem.
      return _ok_response(
          pull=adb_pb2.AdbResponse.PullResponse(content=b'')
      )
    else:
      try:
        content = self._appium_env.pull_file(remote_path)
        return _ok_response(
            pull=adb_pb2.AdbResponse.PullResponse(content=content)
        )
      except Exception as exc:  # pylint: disable=broad-except
        return _error_response(f'pull_file failed for {remote_path!r}: {exc}')

  def _handle_settings(
      self, call: adb_pb2.AdbRequest
  ) -> adb_pb2.AdbResponse:
    """Execute a SettingsRequest via ``mobile: shell settings …``."""
    sr = call.settings
    ns = _NAMESPACE_TO_STR.get(sr.name_space, 'global')
    # The oneof field in SettingsRequest is named 'verb', not 'command'.
    sub = sr.WhichOneof('verb')

    if sub == 'get':
      key = sr.get.key
      output = self._appium_env.shell(f'settings get {ns} {key}')
    elif sub == 'put':
      key = sr.put.key
      value = sr.put.value
      output = self._appium_env.shell(f'settings put {ns} {key} {value}')
    elif sub == 'delete_key':
      key = sr.delete_key.key
      output = self._appium_env.shell(f'settings delete {ns} {key}')
    elif sub == 'reset':
      pkg = sr.reset.package_name
      output = self._appium_env.shell(f'settings reset {ns} {pkg}')
    elif sub == 'list':
      output = self._appium_env.shell(f'settings list {ns}')
    else:
      return _error_response(f'Unknown SettingsRequest sub-command: {sub!r}')

    return _ok_response(
        settings=adb_pb2.AdbResponse.SettingsResponse(
            output=output.encode('utf-8', errors='replace')
        )
    )

  def _handle_package_manager(
      self, call: adb_pb2.AdbRequest
  ) -> adb_pb2.AdbResponse:
    """Execute a PackageManagerRequest via ``mobile: shell pm …``."""
    pm = call.package_manager
    # The oneof field in PackageManagerRequest is named 'verb', not 'command'.
    sub = pm.WhichOneof('verb')

    if sub == 'list':
      output = self._appium_env.shell('pm list packages')
    elif sub == 'clear':
      pkg = pm.clear.package_name if hasattr(pm.clear, 'package_name') else ''
      output = self._appium_env.shell(f'pm clear {pkg}') if pkg else ''
    elif sub == 'grant':
      pkg = pm.grant.package_name if hasattr(pm.grant, 'package_name') else ''
      # 'permissions' is a repeated field; join all entries.
      perms = list(pm.grant.permissions) if hasattr(pm.grant, 'permissions') else []
      output = ''
      for perm in perms:
        output += self._appium_env.shell(f'pm grant {pkg} {perm}') if pkg and perm else ''
    else:
      return _error_response(
          f'Unknown PackageManagerRequest sub-command: {sub!r}'
      )

    return _ok_response(
        package_manager=adb_pb2.AdbResponse.PackageManagerResponse(
            output=output.encode('utf-8', errors='replace')
        )
    )

  def _handle_dumpsys(
      self, call: adb_pb2.AdbRequest
  ) -> adb_pb2.AdbResponse:
    """Execute a DumpsysRequest via ``mobile: shell dumpsys …``."""
    ds = call.dumpsys
    cmd = f'dumpsys {ds.service}'
    if ds.args:
      cmd = f'{cmd} {" ".join(ds.args)}'
    output = self._appium_env.shell(cmd, timeout=int(ds.timeout_sec) or None)
    return _ok_response(
        dumpsys=adb_pb2.AdbResponse.DumpsysResponse(
            output=output.encode('utf-8', errors='replace')
        )
    )

  def _handle_send_broadcast(
      self, call: adb_pb2.AdbRequest
  ) -> adb_pb2.AdbResponse:
    """Send an Android broadcast intent via ``am broadcast``."""
    sb = call.send_broadcast
    cmd = f'am broadcast -a {sb.action}'
    if sb.component:
      cmd = f'{cmd} -n {sb.component}'
    self._appium_env.shell(cmd)
    return _ok_response()

  def _handle_install_apk(
      self, call: adb_pb2.AdbRequest
  ) -> adb_pb2.AdbResponse:
    """Install an APK — only blob-based install is supported via push+pm."""
    # We can only handle blob-based installs: push to /data/local/tmp then pm.
    blob = call.install_apk.blob
    if not blob.contents:
      return _error_response('install_apk: only blob-based install supported, and blob.contents is empty')
    tmp_remote = '/data/local/tmp/aw_install.apk'
    # Write to temp, push, install.
    with tempfile.NamedTemporaryFile(suffix='.apk', delete=False) as f:
      f.write(blob.contents)
      local_tmp = f.name
    try:
      self._appium_env.push_file(local_tmp, tmp_remote)
      output = self._appium_env.shell(f'pm install -r {tmp_remote}')
      self._appium_env.shell(f'rm -f {tmp_remote}')
    finally:
      os.unlink(local_tmp)
    return _ok_response(
        generic=adb_pb2.AdbResponse.GenericResponse(
            output=output.encode('utf-8', errors='replace')
        )
    )

  def _handle_uninstall_package(
      self, call: adb_pb2.AdbRequest
  ) -> adb_pb2.AdbResponse:
    """Uninstall a package via ``pm uninstall``."""
    pkg = call.uninstall_package.package_name
    output = self._appium_env.shell(f'pm uninstall {pkg}')
    return _ok_response(
        generic=adb_pb2.AdbResponse.GenericResponse(
            output=output.encode('utf-8', errors='replace')
        )
    )

  # -------------------------------------------------------------------------
  # Dispatch table
  # -------------------------------------------------------------------------

  _DISPATCH = {
      'generic': _handle_generic,
      'tap': _handle_tap,
      'press_button': _handle_press_button,
      'input_text': _handle_input_text,
      'start_activity': _handle_start_activity,
      'force_stop': _handle_force_stop,
      'get_current_activity': _handle_get_current_activity,
      'get_orientation': _handle_get_orientation,
      'push': _handle_push,
      'pull': _handle_pull,
      'settings': _handle_settings,
      'package_manager': _handle_package_manager,
      'dumpsys': _handle_dumpsys,
      'send_broadcast': _handle_send_broadcast,
      'install_apk': _handle_install_apk,
      'uninstall_package': _handle_uninstall_package,
  }
