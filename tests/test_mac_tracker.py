"""
mac_tracker.py icin ag/switch gerektirmeyen testler.

Calistirma:
    python -m unittest discover -s tests -v
    python mac_tracker.py --selftest
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mac_tracker as mt  # noqa: E402


class TestMacNormalize(unittest.TestCase):
    def test_formats(self):
        expected = "AA:BB:CC:DD:EE:FF"
        for value in ("aa:bb:cc:dd:ee:ff", "AA-BB-CC-DD-EE-FF", "aabb.ccdd.eeff",
                      "aabbccddeeff", " aa:bb:cc:dd:ee:ff "):
            self.assertEqual(mt.normalize_mac(value), expected, value)

    def test_short_octets_padded(self):
        self.assertEqual(mt.normalize_mac("0:1a:cb:a:14:1e"), "00:1A:CB:0A:14:1E")

    def test_invalid(self):
        for value in ("aa:bb:cc:dd:ee", "zz:bb:cc:dd:ee:ff", "", "aabbccddee"):
            with self.assertRaises(ValueError):
                mt.normalize_mac(value)


class TestOidParsing(unittest.TestCase):
    def test_dot1d_suffix(self):
        oid = ".1.3.6.1.2.1.17.4.3.1.2.0.26.203.10.20.30"
        self.assertEqual(mt.mac_from_oid_suffix(oid), (None, "00:1A:CB:0A:14:1E"))

    def test_dot1q_suffix_has_vlan(self):
        oid = "1.3.6.1.2.1.17.7.1.2.2.1.2.20.0.26.203.10.20.30"
        self.assertEqual(mt.mac_from_oid_suffix(oid, with_vlan=True), (20, "00:1A:CB:0A:14:1E"))

    def test_bad_byte_value(self):
        with self.assertRaises(ValueError):
            mt.mac_from_oid_suffix("1.3.6.1.2.1.17.4.3.1.2.0.26.203.10.20.300")

    def test_too_short(self):
        with self.assertRaises(ValueError):
            mt.mac_from_oid_suffix("1.3.6.1.2")

    def test_non_numeric(self):
        with self.assertRaises(ValueError):
            mt.mac_from_oid_suffix("1.3.6.1.2.1.17.4.3.1.2.0.26.cb.10.20.30")


class TestSnmpValueHelpers(unittest.TestCase):
    def test_safe_int_never_raises(self):
        self.assertEqual(mt.safe_int("42"), 42)
        self.assertEqual(mt.safe_int(' "7" '), 7)
        self.assertIsNone(mt.safe_int("No Such Object available on this agent at this OID"))
        self.assertIsNone(mt.safe_int(""))

    def test_error_marker_detection(self):
        self.assertTrue(mt.looks_like_snmp_error("No Such Instance currently exists"))
        self.assertTrue(mt.looks_like_snmp_error("Timeout: No Response from 10.1.1.1"))
        self.assertFalse(mt.looks_like_snmp_error("Gi1/0/5"))


class TestUplinkDetection(unittest.TestCase):
    def _obs(self, port, count):
        return [mt.Observation(mac=f"00:00:00:00:{i:02X}:01", vlan=10, port=port)
                for i in range(count)]

    def test_threshold(self):
        observations = self._obs("Gi1/0/1", 2) + self._obs("Gi1/0/24", 40)
        self.assertEqual(mt.find_uplink_ports(observations, 10), {"Gi1/0/24"})

    def test_disabled(self):
        observations = self._obs("Gi1/0/24", 40)
        self.assertEqual(mt.find_uplink_ports(observations, 0), set())

    def test_port_counts(self):
        observations = self._obs("Gi1/0/1", 3) + self._obs("Gi1/0/2", 1)
        self.assertEqual(mt.count_macs_per_port(observations), {"Gi1/0/1": 3, "Gi1/0/2": 1})


class TestInventory(unittest.TestCase):
    def _write(self, text):
        handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
        handle.write(text)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_basic(self):
        path = self._write(
            "switch,community,vlans,label\n"
            "10.1.1.1,public,,Kat1-SW\n"
            "10.1.2.1,public,1;10;20,Kat2-SW\n"
        )
        entries = mt.parse_inventory_csv(path)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].vlans, [])
        self.assertEqual(entries[0].label, "Kat1-SW")
        self.assertEqual(entries[1].vlans, [1, 10, 20])

    def test_label_defaults_to_host(self):
        path = self._write("switch,community,vlans\n10.9.9.9,public,10\n")
        self.assertEqual(mt.parse_inventory_csv(path)[0].label, "10.9.9.9")

    def test_comment_and_blank_rows_skipped(self):
        path = self._write("switch,community,vlans\n#10.1.1.1,public,10\n,,\n10.2.2.2,public,5\n")
        entries = mt.parse_inventory_csv(path)
        self.assertEqual([e.switch for e in entries], ["10.2.2.2"])

    def test_bad_vlan_raises_clear_error(self):
        path = self._write("switch,community,vlans\n10.1.1.1,public,abc\n")
        with self.assertRaises(ValueError):
            mt.parse_inventory_csv(path)

    def test_vlan_out_of_range(self):
        path = self._write("switch,community,vlans\n10.1.1.1,public,9999\n")
        with self.assertRaises(ValueError):
            mt.parse_inventory_csv(path)

    def test_missing_switch_column(self):
        path = self._write("ip,community,vlans\n10.1.1.1,public,10\n")
        with self.assertRaises(ValueError):
            mt.parse_inventory_csv(path)


class TestTimestamps(unittest.TestCase):
    def test_utc_is_sortable(self):
        earlier = "2026-09-10T20:00:00Z"
        later = "2026-09-10T21:00:00Z"
        self.assertLess(earlier, later)          # metin siralamasi = kronolojik

    def test_parse_old_offset_format(self):
        dt = mt.parse_ts("2026-09-10T23:00:00+03:00")
        self.assertEqual(dt.astimezone(timezone.utc).hour, 20)

    def test_human_age(self):
        now = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(mt.human_age("2026-09-10T11:58:00Z", now), "2 dakika once")
        self.assertEqual(mt.human_age("2026-09-10T11:59:30Z", now), "az once")
        self.assertIn("gun", mt.human_age("2026-09-05T11:00:00Z", now))


class DbTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = mt.init_db(":memory:")
        self.addCleanup(self.conn.close)

    def _result(self, label, observations, uplink_threshold=10, ip="10.0.0.1"):
        entry = mt.SwitchEntry(switch=ip, community="public", label=label)
        result = mt.SwitchResult(entry=entry, observations=observations, mode_used="dot1q")
        result.port_macs = mt.count_macs_per_port(observations)
        result.uplink_ports = mt.find_uplink_ports(observations, uplink_threshold)
        return result


class TestLocationStorage(DbTestCase):
    def test_repeat_polls_do_not_create_new_rows(self):
        obs = [mt.Observation("AA:BB:CC:DD:EE:FF", 10, "Gi1/0/5")]
        for index in range(5):
            result = self._result("SW1", obs)
            mt.apply_switch_result(self.conn, result, f"2026-09-10T20:0{index}:00Z", False)
        rows = self.conn.execute("SELECT * FROM mac_locations").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["seen_count"], 5)
        self.assertEqual(rows[0]["first_seen"], "2026-09-10T20:00:00Z")
        self.assertEqual(rows[0]["last_seen"], "2026-09-10T20:04:00Z")

    def test_port_change_creates_move_record(self):
        mt.apply_switch_result(
            self.conn, self._result("SW1", [mt.Observation("AA:BB:CC:DD:EE:FF", 10, "Gi1/0/5")]),
            "2026-09-10T20:00:00Z", False)
        mt.apply_switch_result(
            self.conn, self._result("SW1", [mt.Observation("AA:BB:CC:DD:EE:FF", 10, "Gi1/0/9")]),
            "2026-09-10T20:01:00Z", False)
        moves = self.conn.execute("SELECT * FROM mac_moves").fetchall()
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0]["from_port"], "Gi1/0/5")
        self.assertEqual(moves[0]["to_port"], "Gi1/0/9")
        # Eski konum kaydi silinmez: "o portta en son ne zaman aktifti" bilgisi korunur.
        old = self.conn.execute(
            "SELECT last_seen FROM mac_locations WHERE port = 'Gi1/0/5'").fetchone()
        self.assertEqual(old["last_seen"], "2026-09-10T20:00:00Z")

    def test_same_port_no_move_record(self):
        for ts in ("2026-09-10T20:00:00Z", "2026-09-10T20:01:00Z"):
            mt.apply_switch_result(
                self.conn, self._result("SW1", [mt.Observation("AA:BB:CC:DD:EE:FF", 10, "Gi1/0/5")]),
                ts, False)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM mac_moves").fetchone()["n"], 0)

    def test_uplink_macs_skipped(self):
        observations = [mt.Observation("AA:BB:CC:DD:EE:FF", 10, "Gi1/0/5")]
        observations += [mt.Observation(f"00:11:22:33:{i:02X}:01", 10, "Gi1/0/48")
                         for i in range(30)]
        recorded, skipped = mt.apply_switch_result(
            self.conn, self._result("SW1", observations), "2026-09-10T20:00:00Z", False)
        self.assertEqual((recorded, skipped), (1, 30))
        ports = [r["port"] for r in self.conn.execute("SELECT port FROM mac_locations")]
        self.assertEqual(ports, ["Gi1/0/5"])

    def test_keep_uplinks_flag(self):
        observations = [mt.Observation(f"00:11:22:33:{i:02X}:01", 10, "Gi1/0/48")
                        for i in range(30)]
        recorded, skipped = mt.apply_switch_result(
            self.conn, self._result("SW1", observations), "2026-09-10T20:00:00Z", True)
        self.assertEqual((recorded, skipped), (30, 0))

    def test_access_port_wins_over_trunk_for_current_location(self):
        mac = "AA:BB:CC:DD:EE:FF"
        trunk = [mt.Observation(mac, 10, "Te1/1/1")] + [
            mt.Observation(f"00:11:22:33:{i:02X}:01", 10, "Te1/1/1") for i in range(8)]
        mt.apply_switch_result(self.conn, self._result("CORE", trunk, uplink_threshold=0),
                               "2026-09-10T20:00:00Z", True)
        mt.apply_switch_result(self.conn, self._result("SW1", [mt.Observation(mac, 10, "Gi1/0/5")]),
                               "2026-09-10T20:00:00Z", False)
        loc = mt.current_location(self.conn, mac)
        self.assertEqual((loc["switch"], loc["port"]), ("SW1", "Gi1/0/5"))


class TestDeviceStatus(DbTestCase):
    """'Cihaz aktif mi, koptu mu, yoksa switch'e mi ulasamiyoruz' ayrimi."""

    def _poll(self, label, observations, ts, ok=True, error=""):
        result = self._result(label, observations)
        if not ok:
            result.error = error or "Timeout: No Response"
            result.observations = []
        recorded, skipped = (0, 0)
        if result.ok:
            recorded, skipped = mt.apply_switch_result(self.conn, result, ts, False)
        mt.record_poll_run(self.conn, result, ts, recorded, skipped)

    def test_device_active_when_seen_in_latest_poll(self):
        obs = [mt.Observation("AA:BB:CC:DD:EE:FF", 10, "Gi1/0/5")]
        self._poll("SW1", obs, "2026-09-10T20:00:00Z")
        loc = mt.current_location(self.conn, "AA:BB:CC:DD:EE:FF")
        self.assertEqual(loc["last_seen"], mt.last_successful_poll(self.conn, "SW1"))

    def test_device_gone_but_switch_reachable(self):
        obs = [mt.Observation("AA:BB:CC:DD:EE:FF", 10, "Gi1/0/5")]
        self._poll("SW1", obs, "2026-09-10T20:00:00Z")
        self._poll("SW1", [], "2026-09-10T20:05:00Z")          # cihaz kopmus
        loc = mt.current_location(self.conn, "AA:BB:CC:DD:EE:FF")
        last_ok = mt.last_successful_poll(self.conn, "SW1")
        self.assertLess(loc["last_seen"], last_ok)              # -> KOPMUS
        self.assertEqual(loc["last_seen"], "2026-09-10T20:00:00Z")

    def test_failed_poll_does_not_count_as_success(self):
        obs = [mt.Observation("AA:BB:CC:DD:EE:FF", 10, "Gi1/0/5")]
        self._poll("SW1", obs, "2026-09-10T20:00:00Z")
        self._poll("SW1", [], "2026-09-10T20:05:00Z", ok=False)  # switch ulasilamiyor
        last_ok = mt.last_successful_poll(self.conn, "SW1")
        self.assertEqual(last_ok, "2026-09-10T20:00:00Z")        # -> durum BILINMIYOR degil AKTIF
        loc = mt.current_location(self.conn, "AA:BB:CC:DD:EE:FF")
        self.assertGreaterEqual(loc["last_seen"], last_ok)

    def test_prune_removes_old_rows(self):
        old_ts = (datetime.now(timezone.utc) - timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._poll("SW1", [mt.Observation("AA:BB:CC:DD:EE:FF", 10, "Gi1/0/5")], old_ts)
        self._poll("SW1", [mt.Observation("11:22:33:44:55:66", 10, "Gi1/0/6")], mt.now_utc())
        mt.cmd_prune(self.conn, 365)
        macs = [r["mac"] for r in self.conn.execute("SELECT mac FROM mac_locations")]
        self.assertEqual(macs, ["11:22:33:44:55:66"])


class TestCollectSwitchWithFakeSnmp(unittest.TestCase):
    """collect_switch'i sahte snmp_walk ile test eder -- gercek switch gerekmez."""

    def setUp(self):
        self.real_walk = mt.snmp_walk
        self.addCleanup(setattr, mt, "snmp_walk", self.real_walk)
        self.opts = mt.SnmpOptions(walk_binary="snmpbulkwalk")
        self.entry = mt.SwitchEntry(switch="10.1.1.1", community="public", label="SW1")

    def _install(self, table):
        def fake_walk(entry, opts, oid, vlan=None):
            if oid not in table:
                raise mt.SnmpError("No Such Object available on this agent at this OID")
            return table[oid]
        mt.snmp_walk = fake_walk

    def test_dot1q_path_maps_port_names(self):
        self._install({
            mt.OID_DOT1D_BASEPORT_IFINDEX: [
                (f"{mt.OID_DOT1D_BASEPORT_IFINDEX}.5", "10105"),
            ],
            mt.OID_IFNAME: [(f"{mt.OID_IFNAME}.10105", "Gi1/0/5")],
            mt.OID_DOT1Q_FDB_PORT: [
                (f"{mt.OID_DOT1Q_FDB_PORT}.10.0.26.203.10.20.30", "5"),
            ],
            mt.OID_DOT1Q_FDB_STATUS: [
                (f"{mt.OID_DOT1Q_FDB_STATUS}.10.0.26.203.10.20.30", "3"),
            ],
        })
        result = mt.collect_switch(self.entry, self.opts, "auto", 10, True)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.mode_used, "dot1q")
        self.assertEqual(len(result.observations), 1)
        obs = result.observations[0]
        self.assertEqual((obs.mac, obs.vlan, obs.port), ("00:1A:CB:0A:14:1E", 10, "Gi1/0/5"))

    def test_non_learned_entries_filtered(self):
        self._install({
            mt.OID_DOT1D_BASEPORT_IFINDEX: [(f"{mt.OID_DOT1D_BASEPORT_IFINDEX}.5", "10105")],
            mt.OID_IFNAME: [(f"{mt.OID_IFNAME}.10105", "Gi1/0/5")],
            mt.OID_DOT1Q_FDB_PORT: [
                (f"{mt.OID_DOT1Q_FDB_PORT}.10.0.26.203.10.20.30", "5"),
                (f"{mt.OID_DOT1Q_FDB_PORT}.10.0.26.203.10.20.31", "5"),
            ],
            mt.OID_DOT1Q_FDB_STATUS: [
                (f"{mt.OID_DOT1Q_FDB_STATUS}.10.0.26.203.10.20.30", "3"),   # learned
                (f"{mt.OID_DOT1Q_FDB_STATUS}.10.0.26.203.10.20.31", "4"),   # self -> atlanir
            ],
        })
        result = mt.collect_switch(self.entry, self.opts, "auto", 10, True)
        self.assertEqual([o.mac for o in result.observations], ["00:1A:CB:0A:14:1E"])

    def test_falls_back_to_dot1d_when_dot1q_empty(self):
        self.entry.vlans = [10]
        self._install({
            mt.OID_DOT1D_BASEPORT_IFINDEX: [(f"{mt.OID_DOT1D_BASEPORT_IFINDEX}.7", "10107")],
            mt.OID_IFNAME: [(f"{mt.OID_IFNAME}.10107", "Fa0/7")],
            mt.OID_DOT1Q_FDB_PORT: [],
            mt.OID_DOT1D_FDB_PORT: [(f"{mt.OID_DOT1D_FDB_PORT}.0.26.203.10.20.30", "7")],
            mt.OID_DOT1D_FDB_STATUS: [(f"{mt.OID_DOT1D_FDB_STATUS}.0.26.203.10.20.30", "3")],
        })
        result = mt.collect_switch(self.entry, self.opts, "auto", 10, True)
        self.assertEqual(result.mode_used, "dot1d")
        self.assertEqual(result.observations[0].port, "Fa0/7")

    def test_snmp_error_does_not_crash_and_is_reported(self):
        self._install({})          # hicbir OID cevap vermiyor
        result = mt.collect_switch(self.entry, self.opts, "auto", 10, True)
        self.assertFalse(result.ok)
        self.assertTrue(result.error)

    def test_garbage_values_do_not_crash(self):
        """Eski surumde 'No Such Object' degeri int()'e gidip script'i dusuruyordu."""
        self._install({
            mt.OID_DOT1D_BASEPORT_IFINDEX: [
                (f"{mt.OID_DOT1D_BASEPORT_IFINDEX}.5", "No Such Object available"),
                (f"{mt.OID_DOT1D_BASEPORT_IFINDEX}.6", "10106"),
            ],
            mt.OID_IFNAME: [(f"{mt.OID_IFNAME}.10106", "Gi1/0/6")],
            mt.OID_DOT1Q_FDB_PORT: [
                (f"{mt.OID_DOT1Q_FDB_PORT}.10.0.26.203.10.20.30", "5"),   # cozulemez -> bridgeport5
                (f"{mt.OID_DOT1Q_FDB_PORT}.10.0.26.203.10.20.31", "6"),   # Gi1/0/6
            ],
            mt.OID_DOT1Q_FDB_STATUS: [],
        })
        result = mt.collect_switch(self.entry, self.opts, "auto", 10, False)
        self.assertTrue(result.ok, result.error)
        self.assertEqual({o.port for o in result.observations}, {"bridgeport5", "Gi1/0/6"})

    def test_vlan_filter_applied_in_dot1q(self):
        self.entry.vlans = [20]
        self._install({
            mt.OID_DOT1D_BASEPORT_IFINDEX: [(f"{mt.OID_DOT1D_BASEPORT_IFINDEX}.5", "10105")],
            mt.OID_IFNAME: [(f"{mt.OID_IFNAME}.10105", "Gi1/0/5")],
            mt.OID_DOT1Q_FDB_PORT: [
                (f"{mt.OID_DOT1Q_FDB_PORT}.10.0.26.203.10.20.30", "5"),
                (f"{mt.OID_DOT1Q_FDB_PORT}.20.0.26.203.10.20.31", "5"),
            ],
            mt.OID_DOT1Q_FDB_STATUS: [],
        })
        result = mt.collect_switch(self.entry, self.opts, "dot1q", 10, False)
        self.assertEqual([o.vlan for o in result.observations], [20])


class TestAuthArgs(unittest.TestCase):
    def test_v2c_plain(self):
        entry = mt.SwitchEntry(switch="10.1.1.1", community="public")
        self.assertEqual(mt.build_auth_args(entry, mt.SnmpOptions()), ["-v2c", "-c", "public"])

    def test_v2c_vlan_indexed_community(self):
        entry = mt.SwitchEntry(switch="10.1.1.1", community="public")
        self.assertEqual(
            mt.build_auth_args(entry, mt.SnmpOptions(), vlan=10),
            ["-v2c", "-c", "public@10"])

    def test_v3_context_for_vlan(self):
        entry = mt.SwitchEntry(switch="10.1.1.1", version="3")
        opts = mt.SnmpOptions(v3_user="ro", v3_level="authPriv",
                              v3_auth_pass="a", v3_priv_pass="p")
        args = mt.build_auth_args(entry, opts, vlan=10)
        self.assertIn("-v3", args)
        self.assertIn("vlan-10", args)

    def test_missing_community_raises(self):
        entry = mt.SwitchEntry(switch="10.1.1.1", community="")
        with self.assertRaises(mt.SnmpError):
            mt.build_auth_args(entry, mt.SnmpOptions())


if __name__ == "__main__":
    unittest.main(verbosity=2)
