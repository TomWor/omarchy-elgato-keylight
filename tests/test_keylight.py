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


class TestFirmwareProbe(unittest.TestCase):
    """400 vs 404 is the whole signal: the device answers 400 to an empty body
    on a route it has, and 404 on one it does not."""

    def test_400_means_route_exists(self):
        with mock.patch.object(g, "call", return_value=(400, b"")):
            self.assertIs(g.firmware_probe(DEVICE), True)

    def test_404_means_unsupported(self):
        with mock.patch.object(g, "call", return_value=(404, b"")):
            self.assertIs(g.firmware_probe(DEVICE), False)

    def test_unreachable_is_unknown_not_false(self):
        with mock.patch.object(g, "call", side_effect=OSError("timed out")):
            self.assertIsNone(g.firmware_probe(DEVICE))

    def test_probe_never_calls_prepare(self):
        # prepare() erases the staging flash. A read-only probe must not touch it.
        with mock.patch.object(g, "call", return_value=(400, b"")) as called:
            g.firmware_probe(DEVICE)
        self.assertNotIn("prepare", called.call_args.args[1])


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
            out = g.cmd_firmware(Args(host=["10.0.0.5"], probe=False))
        light = out["lights"][0]
        self.assertEqual(light["firmwareVersion"], "1.0.3")
        self.assertEqual(light["firmwareBuildNumber"], 222)
        self.assertTrue(light["reachable"])

    def test_skips_probe_unless_asked(self):
        with mock.patch.object(g, "accessory_info", return_value=self.INFO), \
             mock.patch.object(g, "resolve_devices", return_value=([DEVICE], False)), \
             mock.patch.object(g, "firmware_probe") as probe:
            out = g.cmd_firmware(Args(host=["10.0.0.5"], probe=False))
        probe.assert_not_called()
        self.assertIsNone(out["lights"][0]["updateSupported"])

    def test_unreachable_light_is_a_row_not_a_failure(self):
        with mock.patch.object(g, "accessory_info", return_value={}), \
             mock.patch.object(g, "resolve_devices", return_value=([DEVICE], False)):
            out = g.cmd_firmware(Args(host=["10.0.0.5"], probe=True))
        self.assertTrue(out["ok"])
        self.assertFalse(out["lights"][0]["reachable"])

    def test_display_name_wins_over_product_name(self):
        info = {**self.INFO, "displayName": "Left Key"}
        with mock.patch.object(g, "accessory_info", return_value=info), \
             mock.patch.object(g, "resolve_devices", return_value=([DEVICE], False)):
            out = g.cmd_firmware(Args(host=["10.0.0.5"], probe=False))
        self.assertEqual(out["lights"][0]["name"], "Left Key")


class TestFlashFirmware(unittest.TestCase):
    """The flash protocol. These are the tests that matter most: every failure
    mode here is one that can leave a light needing a re-flash to recover."""

    def setUp(self):
        # Real timing would make the suite take minutes; the delays are pacing,
        # not logic, so stub them out.
        patcher = mock.patch.object(g.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_sends_prepare_then_chunks_then_execute(self):
        image = b"x" * (g.FW_CHUNK * 2 + 17)
        calls = []

        def fake(device, path, method="GET", payload=None, body=None, timeout=None):
            calls.append((method, path, payload, len(body) if body else 0))
            return (202 if "data" in path else 200), b""

        with mock.patch.object(g, "call", side_effect=fake):
            g.flash_firmware(DEVICE, image)

        self.assertEqual(calls[0][:3], ("PUT", "firmware-update/prepare",
                                        {"size": len(image)}))
        self.assertEqual(calls[-1][:2], ("POST", "firmware-update/execute"))
        # Offsets must be contiguous and cover the image exactly once.
        offsets = [int(p.split("offset=")[1]) for _, p, _, _ in calls if "data" in p]
        sizes = [n for _, p, _, n in calls if "data" in p]
        self.assertEqual(offsets, [0, g.FW_CHUNK, g.FW_CHUNK * 2])
        self.assertEqual(sizes, [g.FW_CHUNK, g.FW_CHUNK, 17])
        self.assertEqual(sum(sizes), len(image))

    def test_408_retries_the_same_offset(self):
        # 408 means "still flushing the last chunk", not "chunk lost".
        image = b"y" * g.FW_CHUNK
        replies = iter([(200, b""), (408, b""), (408, b""), (202, b""), (200, b"")])
        seen = []

        def fake(device, path, method="GET", payload=None, body=None, timeout=None):
            seen.append(path)
            return next(replies)

        with mock.patch.object(g, "call", side_effect=fake):
            g.flash_firmware(DEVICE, image)
        data_calls = [p for p in seen if "data" in p]
        self.assertEqual(data_calls, ["firmware-update/data?offset=0"] * 3)

    def test_dropped_connection_resends_same_offset(self):
        image = b"z" * g.FW_CHUNK
        replies = [(200, b""), OSError("connection reset"), (202, b""), (200, b"")]
        it = iter(replies)

        def fake(*a, **kw):
            item = next(it)
            if isinstance(item, Exception):
                raise item
            return item

        with mock.patch.object(g, "call", side_effect=fake):
            g.flash_firmware(DEVICE, image)  # must not raise

    def test_only_202_counts_as_accepted(self):
        # A 200 on a data chunk is not an acknowledgement; treating it as one
        # would silently skip a chunk and produce a corrupt image.
        image = b"q" * g.FW_CHUNK

        def fake(device, path, method="GET", payload=None, body=None, timeout=None):
            return (200, b"") if "data" in path else (200, b"")

        with mock.patch.object(g, "call", side_effect=fake):
            with self.assertRaises(RuntimeError) as ctx:
                g.flash_firmware(DEVICE, image)
        self.assertIn("offset 0", str(ctx.exception))

    def test_gives_up_after_retry_budget(self):
        image = b"w" * g.FW_CHUNK

        def fake(device, path, method="GET", payload=None, body=None, timeout=None):
            return (408, b"") if "data" in path else (200, b"")

        with mock.patch.object(g, "call", side_effect=fake) as called:
            with self.assertRaises(RuntimeError):
                g.flash_firmware(DEVICE, image)
        data_calls = [c for c in called.call_args_list if "data" in c.args[1]]
        self.assertEqual(len(data_calls), g.FW_MAX_RETRIES)

    def test_rejected_prepare_aborts_before_sending_anything(self):
        with mock.patch.object(g, "call", return_value=(404, b"")) as called:
            with self.assertRaises(RuntimeError) as ctx:
                g.flash_firmware(DEVICE, b"a" * g.FW_CHUNK)
        self.assertIn("prepare rejected", str(ctx.exception))
        self.assertEqual(called.call_count, 1)

    def test_rejected_execute_is_reported(self):
        def fake(device, path, method="GET", payload=None, body=None, timeout=None):
            if "prepare" in path:
                return 200, b""
            if "data" in path:
                return 202, b""
            return 400, b"bad signature"

        with mock.patch.object(g, "call", side_effect=fake):
            with self.assertRaises(RuntimeError) as ctx:
                g.flash_firmware(DEVICE, b"a" * g.FW_CHUNK)
        self.assertIn("not accepted", str(ctx.exception))
        self.assertIn("bad signature", str(ctx.exception))


class TestFlashCommandGuards(unittest.TestCase):
    """The command wrapper. Every one of these guards has to fire *before*
    prepare() erases the light's flash."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp()
        os.close(fd)
        self.addCleanup(lambda: os.unlink(self.path))

    def write(self, size):
        with open(self.path, "wb") as fh:
            fh.write(b"\0" * size)
        return self.path

    def test_missing_file(self):
        out = g.cmd_firmware_flash(
            Args(host="10.0.0.5", image="/no/such/file", yes=True))
        self.assertFalse(out["ok"])
        self.assertIn("cannot read image", out["error"])

    def test_refuses_without_yes(self):
        path = self.write(g.FW_MIN_SIZE + 1)
        with mock.patch.object(g, "accessory_info", return_value={"productName": "K"}), \
             mock.patch.object(g, "flash_firmware") as flash:
            out = g.cmd_firmware_flash(Args(host="10.0.0.5", image=path, yes=False))
        self.assertFalse(out["ok"])
        self.assertIn("--yes", out["error"])
        flash.assert_not_called()

    def test_refuses_implausible_size(self):
        path = self.write(128)
        with mock.patch.object(g, "flash_firmware") as flash:
            out = g.cmd_firmware_flash(Args(host="10.0.0.5", image=path, yes=True))
        self.assertFalse(out["ok"])
        flash.assert_not_called()

    def test_refuses_when_light_silent(self):
        path = self.write(g.FW_MIN_SIZE + 1)
        with mock.patch.object(g, "accessory_info", return_value={}), \
             mock.patch.object(g, "flash_firmware") as flash:
            out = g.cmd_firmware_flash(Args(host="10.0.0.5", image=path, yes=True))
        self.assertFalse(out["ok"])
        flash.assert_not_called()

    def test_reports_before_and_after_on_success(self):
        path = self.write(g.FW_MIN_SIZE + 1)
        infos = iter([
            {"firmwareVersion": "1.0.2", "firmwareBuildNumber": 218},
            {"firmwareVersion": "1.0.3", "firmwareBuildNumber": 222},
        ])
        with mock.patch.object(g, "accessory_info", side_effect=lambda d: next(infos)), \
             mock.patch.object(g, "flash_firmware"), \
             mock.patch.object(g.time, "sleep"):
            out = g.cmd_firmware_flash(Args(host="10.0.0.5", image=path, yes=True))
        self.assertTrue(out["ok"])
        self.assertEqual(out["before"]["firmwareVersion"], "1.0.2")
        self.assertEqual(out["after"]["firmwareVersion"], "1.0.3")
        self.assertIsNone(out["warning"])

    def test_flash_failure_is_reported_with_progress_log(self):
        path = self.write(g.FW_MIN_SIZE + 1)
        with mock.patch.object(g, "accessory_info", return_value={"firmwareVersion": "1.0.3"}), \
             mock.patch.object(g, "flash_firmware",
                               side_effect=RuntimeError("chunk at offset 4096 rejected")):
            out = g.cmd_firmware_flash(Args(host="10.0.0.5", image=path, yes=True))
        self.assertFalse(out["ok"])
        self.assertIn("offset 4096", out["error"])
        self.assertIn("before", out)

    def test_silent_light_after_flash_warns_rather_than_claiming_success(self):
        path = self.write(g.FW_MIN_SIZE + 1)
        calls = iter([{"firmwareVersion": "1.0.2"}])

        def info(device):
            try:
                return next(calls)
            except StopIteration:
                return {}

        # Skip the 90s wait; the point is what it reports when nothing answers.
        with mock.patch.object(g, "accessory_info", side_effect=info), \
             mock.patch.object(g, "flash_firmware"), \
             mock.patch.object(g.time, "sleep"), \
             mock.patch.object(g.time, "time", side_effect=[0, 100]):
            out = g.cmd_firmware_flash(Args(host="10.0.0.5", image=path, yes=True))
        self.assertTrue(out["ok"])
        self.assertIsNone(out["after"])
        self.assertIn("power-cycle", out["warning"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
