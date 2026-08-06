#!/usr/bin/env python3
"""Unit tests for the Key Light helper.

Run: python3 -m unittest discover -s tests -v   (from the plugin root)

These cover the pure logic — conversions, clamping, avahi parsing, patch
building, response shaping — because that is where a wrong answer silently
drives the hardware out of range. Network paths are exercised with fakes;
nothing here touches a real light.
"""

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

HELPER = os.path.join(os.path.dirname(__file__), "..", "scripts", "keylight")


def load_helper():
    """Import the extensionless CLI as a module."""
    loader = importlib.machinery.SourceFileLoader("keylight_helper", HELPER)
    spec = importlib.util.spec_from_loader("keylight_helper", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


g = load_helper()


class Args:
    """Stand-in for argparse.Namespace with the defaults `set` relies on."""

    def __init__(self, **kw):
        self.on = self.off = self.toggle = False
        self.brightness = self.kelvin = self.temperature = None
        self.host = []
        self.refresh = False
        self.__dict__.update(kw)


class TestClamp(unittest.TestCase):
    def test_within_range_passes_through(self):
        self.assertEqual(g.clamp(50, 0, 100), 50)

    def test_clamps_both_ends(self):
        self.assertEqual(g.clamp(-10, 0, 100), 0)
        self.assertEqual(g.clamp(999, 0, 100), 100)

    def test_rounds_floats(self):
        self.assertEqual(g.clamp(49.6, 0, 100), 50)

    def test_accepts_numeric_strings(self):
        self.assertEqual(g.clamp("42", 0, 100), 42)

    def test_junk_returns_fallback(self):
        self.assertIsNone(g.clamp("abc", 0, 100))
        self.assertIsNone(g.clamp(None, 0, 100))
        self.assertEqual(g.clamp("abc", 0, 100, 7), 7)


class TestTemperatureConversion(unittest.TestCase):
    def test_known_pairs_round_trip(self):
        # 1e6/250 = 4000K exactly; the canonical mid-scale value.
        self.assertEqual(g.mired_to_kelvin(250), 4000)
        self.assertEqual(g.kelvin_to_mired(4000), 250)

    def test_hardware_bounds_map_to_stated_kelvin(self):
        self.assertEqual(g.mired_to_kelvin(g.TEMP_MIN), 7000)
        self.assertEqual(g.mired_to_kelvin(g.TEMP_MAX), 2900)

    def test_kelvin_clamped_to_hardware_range(self):
        # The firmware accepts out-of-range mired verbatim and drives the LEDs
        # outside their calibrated range, so clamping here is the real guard.
        self.assertEqual(g.kelvin_to_mired(99999), g.TEMP_MIN)
        self.assertEqual(g.kelvin_to_mired(1), g.TEMP_MAX)

    def test_mired_clamped_before_conversion(self):
        self.assertEqual(g.mired_to_kelvin(1000), 2900)
        self.assertEqual(g.mired_to_kelvin(10), 7000)

    def test_kelvin_snaps_to_50k(self):
        # Every reachable mired value must present as a round number, or the
        # panel shows jitter like 4998K after the user asked for 5000K.
        for mired in range(g.TEMP_MIN, g.TEMP_MAX + 1):
            self.assertEqual(g.mired_to_kelvin(mired) % 50, 0)

    def test_round_trip_is_stable(self):
        # Re-sending a displayed kelvin must not drift the stored mired.
        for mired in range(g.TEMP_MIN, g.TEMP_MAX + 1):
            once = g.kelvin_to_mired(g.mired_to_kelvin(mired))
            twice = g.kelvin_to_mired(g.mired_to_kelvin(once))
            self.assertEqual(once, twice, f"unstable at mired={mired}")

    def test_invalid_input(self):
        self.assertIsNone(g.kelvin_to_mired("abc"))
        self.assertIsNone(g.kelvin_to_mired(0))
        self.assertIsNone(g.kelvin_to_mired(-5))
        self.assertIsNone(g.mired_to_kelvin("abc"))


class TestParseAvahi(unittest.TestCase):
    RESOLVED = (
        "+;wlan0;IPv6;Elgato\\032Key\\032Light\\0329B11;_elg._tcp;local\n"
        "+;wlan0;IPv4;Elgato\\032Key\\032Light\\0329B11;_elg._tcp;local\n"
        "=;wlan0;IPv6;Elgato\\032Key\\032Light\\0329B11;_elg._tcp;local;"
        "elgato-key-light-9b11.local;192.168.10.153;9123;\"mf=Elgato\"\n"
        "=;wlan0;IPv4;Elgato\\032Key\\032Light\\0329B11;_elg._tcp;local;"
        "elgato-key-light-9b11.local;192.168.10.153;9123;\"mf=Elgato\"\n"
    )

    def test_parses_ipv4_and_unescapes_name(self):
        devices = g.parse_avahi(self.RESOLVED)
        self.assertEqual(len(devices), 1, "IPv6 duplicate should collapse")
        self.assertEqual(devices[0]["host"], "192.168.10.153")
        self.assertEqual(devices[0]["port"], 9123)
        self.assertEqual(devices[0]["name"], "Elgato Key Light 9B11")

    def test_ignores_announcement_only_lines(self):
        announce = "\n".join(
            l for l in self.RESOLVED.splitlines() if l.startswith("+")
        )
        self.assertEqual(g.parse_avahi(announce), [])

    def test_multiple_distinct_lights(self):
        two = self.RESOLVED + (
            "=;wlan0;IPv4;Key\\032Light\\032Air;_elg._tcp;local;"
            "air.local;192.168.10.160;9123;\"mf=Elgato\"\n"
        )
        devices = g.parse_avahi(two)
        self.assertEqual([d["host"] for d in devices],
                         ["192.168.10.153", "192.168.10.160"])

    def test_malformed_input_is_ignored(self):
        self.assertEqual(g.parse_avahi(""), [])
        self.assertEqual(g.parse_avahi("=;too;few\n"), [])
        self.assertEqual(g.parse_avahi("garbage\n\n"), [])

    def test_blank_address_is_skipped(self):
        line = "=;wlan0;IPv4;X;_elg._tcp;local;x.local;;9123;\n"
        self.assertEqual(g.parse_avahi(line), [])

    def test_non_numeric_port_falls_back(self):
        line = "=;wlan0;IPv4;X;_elg._tcp;local;x.local;10.0.0.5;NaN;\n"
        self.assertEqual(g.parse_avahi(line)[0]["port"], g.PORT)


class TestDiscoverTimeout(unittest.TestCase):
    def test_partial_output_survives_timeout(self):
        # A slow browse that already resolved a light must not report "none".
        stdout = TestParseAvahi.RESOLVED
        exc = subprocess.TimeoutExpired(cmd="avahi-browse", timeout=1, output=stdout)
        with mock.patch.object(g.shutil, "which", return_value="/usr/bin/avahi-browse"), \
             mock.patch.object(g.subprocess, "run", side_effect=exc):
            self.assertEqual(len(g.discover()), 1)

    def test_bytes_partial_output_is_decoded(self):
        exc = subprocess.TimeoutExpired(
            cmd="avahi-browse", timeout=1, output=TestParseAvahi.RESOLVED.encode()
        )
        with mock.patch.object(g.shutil, "which", return_value="/usr/bin/avahi-browse"), \
             mock.patch.object(g.subprocess, "run", side_effect=exc):
            self.assertEqual(len(g.discover()), 1)

    def test_missing_avahi_returns_empty(self):
        with mock.patch.object(g.shutil, "which", return_value=None):
            self.assertEqual(g.discover(), [])

    def test_oserror_returns_empty(self):
        with mock.patch.object(g.shutil, "which", return_value="/usr/bin/avahi-browse"), \
             mock.patch.object(g.subprocess, "run", side_effect=OSError("boom")):
            self.assertEqual(g.discover(), [])


class TestParseHost(unittest.TestCase):
    def test_bare_host_gets_default_port(self):
        self.assertEqual(g.parse_host("10.0.0.5"),
                         {"host": "10.0.0.5", "port": 9123, "name": ""})

    def test_explicit_port(self):
        self.assertEqual(g.parse_host("10.0.0.5:1234")["port"], 1234)

    def test_bad_port_falls_back(self):
        self.assertEqual(g.parse_host("10.0.0.5:nope")["port"], g.PORT)

    def test_whitespace_is_trimmed(self):
        self.assertEqual(g.parse_host("  10.0.0.5  ")["host"], "10.0.0.5")


class TestLightView(unittest.TestCase):
    DEVICE = {"host": "10.0.0.5", "port": 9123, "name": "Key"}

    def test_shapes_a_normal_reply(self):
        raw = {"numberOfLights": 1,
               "lights": [{"on": 1, "brightness": 40, "temperature": 250}]}
        view = g.light_view(self.DEVICE, raw)
        self.assertTrue(view["reachable"])
        self.assertTrue(view["on"])
        self.assertEqual(view["brightness"], 40)
        self.assertEqual(view["temperature"], 250)
        self.assertEqual(view["kelvin"], 4000)

    def test_out_of_range_firmware_values_are_clamped(self):
        # A light previously driven out of range by another tool must still
        # present a value the panel's slider can represent.
        raw = {"lights": [{"on": 1, "brightness": 150, "temperature": 400}]}
        view = g.light_view(self.DEVICE, raw)
        self.assertEqual(view["brightness"], 100)
        self.assertEqual(view["temperature"], g.TEMP_MAX)

    def test_empty_and_malformed_replies_do_not_raise(self):
        for raw in ({}, {"lights": []}, {"lights": "nope"}, {"lights": [None]}):
            view = g.light_view(self.DEVICE, raw)
            self.assertFalse(view["on"])
            self.assertEqual(view["brightness"], 0)

    def test_falls_back_to_host_when_unnamed(self):
        view = g.light_view({"host": "10.0.0.5", "port": 9123, "name": ""}, {})
        self.assertEqual(view["name"], "10.0.0.5")


class TestUnreachableView(unittest.TestCase):
    DEVICE = {"host": "10.0.0.9", "port": 9123, "name": "Dead"}

    def test_http_error_reports_status(self):
        err = urllib.error.HTTPError("u", 503, "no", None, None)
        self.assertEqual(g.unreachable_view(self.DEVICE, err)["error"], "HTTP 503")

    def test_value_error_reports_invalid_response(self):
        view = g.unreachable_view(self.DEVICE, ValueError("bad json"))
        self.assertEqual(view["error"], "invalid response")

    def test_keeps_identity_so_row_does_not_vanish(self):
        view = g.unreachable_view(self.DEVICE, OSError("timed out"))
        self.assertFalse(view["reachable"])
        self.assertEqual(view["name"], "Dead")
        self.assertEqual(view["host"], "10.0.0.9")
        # Still a fully-shaped row the panel can render without null checks.
        for key in ("on", "brightness", "temperature", "kelvin"):
            self.assertIn(key, view)


class TestBuildPatch(unittest.TestCase):
    def test_explicit_on_off(self):
        self.assertEqual(g.build_patch(Args(on=True), None), {"on": 1})
        self.assertEqual(g.build_patch(Args(off=True), None), {"on": 0})

    def test_toggle_inverts_current(self):
        self.assertEqual(g.build_patch(Args(toggle=True), {"on": True}), {"on": 0})
        self.assertEqual(g.build_patch(Args(toggle=True), {"on": False}), {"on": 1})

    def test_toggle_without_reading_is_a_noop(self):
        # Guessing a direction would switch the wrong way half the time.
        self.assertEqual(g.build_patch(Args(toggle=True), None), {})

    def test_only_requested_fields_are_sent(self):
        self.assertEqual(g.build_patch(Args(brightness=40), None), {"brightness": 40})
        self.assertEqual(g.build_patch(Args(), None), {})

    def test_values_are_clamped(self):
        self.assertEqual(g.build_patch(Args(brightness=999), None)["brightness"], 100)
        self.assertEqual(g.build_patch(Args(brightness=-5), None)["brightness"], 0)
        self.assertEqual(g.build_patch(Args(kelvin=99999), None)["temperature"], g.TEMP_MIN)

    def test_kelvin_takes_precedence_over_mired(self):
        patch = g.build_patch(Args(kelvin=4000, temperature=300), None)
        self.assertEqual(patch["temperature"], 250)

    def test_raw_mired_is_accepted_and_clamped(self):
        self.assertEqual(g.build_patch(Args(temperature=250), None)["temperature"], 250)
        self.assertEqual(g.build_patch(Args(temperature=999), None)["temperature"], g.TEMP_MAX)

    def test_invalid_values_are_dropped_not_defaulted(self):
        # Sending 0 for unparseable input would black out the light.
        self.assertEqual(g.build_patch(Args(brightness="abc"), None), {})
        self.assertEqual(g.build_patch(Args(kelvin="abc"), None), {})

    def test_combined_patch(self):
        patch = g.build_patch(Args(on=True, brightness=40, kelvin=4000), None)
        self.assertEqual(patch, {"on": 1, "brightness": 40, "temperature": 250})


class TestCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_round_trip(self):
        devices = [{"host": "10.0.0.5", "port": 9123, "name": "Key"}]
        g.write_cache(devices)
        loaded, age = g.read_cache()
        self.assertEqual(loaded, devices)
        self.assertLess(age, 5)

    def test_missing_cache_is_infinitely_stale(self):
        loaded, age = g.read_cache()
        self.assertEqual(loaded, [])
        self.assertEqual(age, float("inf"))

    def test_corrupt_cache_is_ignored(self):
        path = g.cache_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("{not json")
        self.assertEqual(g.read_cache()[0], [])

    def test_entries_without_host_are_dropped(self):
        path = g.cache_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump({"at": 0, "devices": [{"port": 1}, "junk", {"host": "10.0.0.5"}]}, fh)
        self.assertEqual(g.read_cache()[0], [{"host": "10.0.0.5"}])

    def test_unwritable_cache_dir_is_not_fatal(self):
        with mock.patch.object(g.os, "makedirs", side_effect=OSError("ro")):
            g.write_cache([{"host": "10.0.0.5"}])  # must not raise


class TestResolveDevices(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_explicit_hosts_skip_discovery_entirely(self):
        with mock.patch.object(g, "discover") as disc:
            devices, _ = g.resolve_devices(["10.0.0.5"], refresh=False)
            disc.assert_not_called()
        self.assertEqual(devices[0]["host"], "10.0.0.5")

    def test_fresh_cache_avoids_discovery(self):
        g.write_cache([{"host": "10.0.0.5", "port": 9123, "name": "Key"}])
        with mock.patch.object(g, "discover") as disc:
            devices, discovered = g.resolve_devices([], refresh=False)
            disc.assert_not_called()
        self.assertEqual(len(devices), 1)
        self.assertFalse(discovered)

    def test_refresh_forces_discovery(self):
        g.write_cache([{"host": "10.0.0.5", "port": 9123, "name": "Key"}])
        found = [{"host": "10.0.0.6", "port": 9123, "name": "New"}]
        with mock.patch.object(g, "discover", return_value=found):
            devices, discovered = g.resolve_devices([], refresh=True)
        self.assertEqual(devices[0]["host"], "10.0.0.6")
        self.assertTrue(discovered)

    def test_stale_cache_survives_failed_discovery(self):
        # Known lights beat an empty panel when mDNS is momentarily broken.
        g.write_cache([{"host": "10.0.0.5", "port": 9123, "name": "Key"}])
        with mock.patch.object(g, "read_cache",
                               return_value=([{"host": "10.0.0.5"}], 99999)), \
             mock.patch.object(g, "discover", return_value=[]):
            devices, discovered = g.resolve_devices([], refresh=False)
        self.assertEqual(devices[0]["host"], "10.0.0.5")
        self.assertFalse(discovered)


class TestFanOut(unittest.TestCase):
    def test_preserves_input_order(self):
        devices = [{"host": f"10.0.0.{i}"} for i in range(6)]
        result = g.fan_out(devices, lambda d: d["host"])
        self.assertEqual(result, [d["host"] for d in devices])

    def test_empty_input(self):
        self.assertEqual(g.fan_out([], lambda d: d), [])


class TestCommands(unittest.TestCase):
    """End-to-end command shape with the network faked out."""

    LIGHT = {"host": "10.0.0.5", "port": 9123, "name": "Key"}
    REPLY = {"numberOfLights": 1,
             "lights": [{"on": 1, "brightness": 40, "temperature": 250}]}

    def test_state_reports_aggregate_flags(self):
        with mock.patch.object(g, "resolve_devices", return_value=([self.LIGHT], False)), \
             mock.patch.object(g, "request", return_value=self.REPLY):
            out = g.cmd_state(Args())
        self.assertTrue(out["ok"])
        self.assertTrue(out["anyOn"])
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["reachable"], 1)

    def test_state_with_no_devices_is_still_ok(self):
        # The panel needs data, not an exception, when nothing is found.
        with mock.patch.object(g, "resolve_devices", return_value=([], False)):
            out = g.cmd_state(Args())
        self.assertTrue(out["ok"])
        self.assertEqual(out["error"], "no-devices")
        self.assertEqual(out["lights"], [])

    def test_state_survives_one_dead_light(self):
        pair = [self.LIGHT, {"host": "10.0.0.9", "port": 9123, "name": "Dead"}]

        def flaky(device, payload=None):
            if device["host"].endswith(".9"):
                raise OSError("timed out")
            return self.REPLY

        with mock.patch.object(g, "resolve_devices", return_value=(pair, False)), \
             mock.patch.object(g, "request", side_effect=flaky):
            out = g.cmd_state(Args())
        self.assertTrue(out["ok"])
        self.assertEqual(out["reachable"], 1)
        self.assertEqual(len(out["lights"]), 2, "dead light must still be listed")

    def test_set_sends_expected_payload(self):
        seen = {}

        def capture(device, payload=None):
            if payload is not None:
                seen.update(payload)
            return self.REPLY

        with mock.patch.object(g, "resolve_devices", return_value=([self.LIGHT], False)), \
             mock.patch.object(g, "request", side_effect=capture):
            out = g.cmd_set(Args(on=True, brightness=40, kelvin=4000))
        self.assertTrue(out["ok"])
        self.assertEqual(seen["lights"][0],
                         {"on": 1, "brightness": 40, "temperature": 250})

    def test_set_reports_not_ok_when_unreachable(self):
        with mock.patch.object(g, "resolve_devices", return_value=([self.LIGHT], False)), \
             mock.patch.object(g, "request", side_effect=OSError("timed out")):
            out = g.cmd_set(Args(on=True))
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "unreachable")

    def test_set_with_no_devices_fails_cleanly(self):
        with mock.patch.object(g, "resolve_devices", return_value=([], False)):
            out = g.cmd_set(Args(on=True))
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "no-devices")

    def test_empty_patch_does_not_put(self):
        calls = []

        def record(device, payload=None):
            calls.append(payload)
            return self.REPLY

        with mock.patch.object(g, "resolve_devices", return_value=([self.LIGHT], False)), \
             mock.patch.object(g, "request", side_effect=record):
            g.cmd_set(Args())
        self.assertTrue(all(p is None for p in calls), "should GET only, never PUT")


class TestMainAlwaysEmitsJson(unittest.TestCase):
    """The panel parses stdout unconditionally; it must always get an object."""

    def run_cli(self, argv):
        proc = subprocess.run(
            [sys.executable, HELPER] + argv,
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "XDG_CACHE_HOME": tempfile.mkdtemp()},
        )
        return proc

    def test_unexpected_exception_still_yields_json(self):
        with mock.patch.object(g, "cmd_state", side_effect=RuntimeError("boom")):
            buf = []
            with mock.patch.object(g.sys.stdout, "write", lambda s: buf.append(s)), \
                 mock.patch.object(g.json, "dump",
                                   lambda o, f: buf.append(json.dumps(o))):
                code = g.main(["state"])
        self.assertEqual(code, 0)
        payload = json.loads("".join(b for b in buf if b.strip()))
        self.assertFalse(payload["ok"])
        self.assertIn("RuntimeError", payload["error"])

    def test_discover_emits_parseable_json(self):
        proc = self.run_cli(["discover"])
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(json.loads(proc.stdout)["ok"])

    def test_missing_subcommand_exits_nonzero(self):
        # argparse handles this before main(); a usage error is not JSON,
        # which is correct — that is a developer mistake, not runtime state.
        self.assertNotEqual(self.run_cli([]).returncode, 0)


DEVICE = {"host": "10.0.0.5", "port": 9123}


class TestIdentify(unittest.TestCase):
    def test_success(self):
        with mock.patch.object(g, "call", return_value=(200, b"")):
            out = g.cmd_identify(Args(host="10.0.0.5"))
        self.assertTrue(out["ok"])
        self.assertTrue(out["supported"])

    def test_404_is_unsupported_not_an_error(self):
        # An older firmware without the route is not a fault to report as one.
        with mock.patch.object(g, "call", return_value=(404, b"")):
            out = g.cmd_identify(Args(host="10.0.0.5"))
        self.assertFalse(out["ok"])
        self.assertFalse(out["supported"])

    def test_unreachable_reports_reason(self):
        with mock.patch.object(g, "call", side_effect=OSError("no route to host")):
            out = g.cmd_identify(Args(host="10.0.0.5"))
        self.assertFalse(out["ok"])
        self.assertIn("no route", out["error"])

    def test_uses_post(self):
        with mock.patch.object(g, "call", return_value=(200, b"")) as called:
            g.cmd_identify(Args(host="10.0.0.5"))
        self.assertEqual(called.call_args.kwargs["method"], "POST")


class TestFirmwareReport(unittest.TestCase):
    INFO = {
        "productName": "Elgato Key Light",
        "firmwareVersion": "1.0.3",
        "firmwareBuildNumber": 222,
        "hardwareBoardType": 53,
        "serialNumber": "BW30J1A01511",
        "displayName": "",
    }

    def test_reports_versions(self):
        with mock.patch.object(g, "accessory_info", return_value=self.INFO), \
             mock.patch.object(g, "resolve_devices", return_value=([DEVICE], False)):
            out = g.cmd_firmware(Args(host=["10.0.0.5"]))
        light = out["lights"][0]
        self.assertEqual(light["firmwareVersion"], "1.0.3")
        self.assertEqual(light["firmwareBuildNumber"], 222)
        self.assertTrue(light["reachable"])

    def test_is_read_only(self):
        # Reporting a version must never poke a write endpoint: the light's
        # update routes erase its staging flash just by being addressed.
        with mock.patch.object(g, "accessory_info", return_value=self.INFO), \
             mock.patch.object(g, "resolve_devices", return_value=([DEVICE], False)), \
             mock.patch.object(g, "call") as called:
            g.cmd_firmware(Args(host=["10.0.0.5"]))
        called.assert_not_called()

    def test_unreachable_light_is_a_row_not_a_failure(self):
        with mock.patch.object(g, "accessory_info", return_value={}), \
             mock.patch.object(g, "resolve_devices", return_value=([DEVICE], False)):
            out = g.cmd_firmware(Args(host=["10.0.0.5"]))
        self.assertTrue(out["ok"])
        self.assertFalse(out["lights"][0]["reachable"])

    def test_display_name_wins_over_product_name(self):
        info = {**self.INFO, "displayName": "Left Key"}
        with mock.patch.object(g, "accessory_info", return_value=info), \
             mock.patch.object(g, "resolve_devices", return_value=([DEVICE], False)):
            out = g.cmd_firmware(Args(host=["10.0.0.5"]))
        self.assertEqual(out["lights"][0]["name"], "Left Key")


if __name__ == "__main__":
    unittest.main(verbosity=2)
