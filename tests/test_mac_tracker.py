"""
mac_tracker.py icin ag/switch gerektirmeyen testler.

Calistirma:
    python -m unittest discover -s tests -v
    python mac_tracker.py --selftest
"""

import contextlib
import io
import os
import sqlite3
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

    def test_comment_block_above_the_header(self):
        # inventory.csv.example tam olarak boyle: aciklama blogu basligin
        # ustunde. Bu satirlar elenmezse ilk yorum satiri baslik sanilir.
        path = self._write(
            "# Kopyala: cp inventory.csv.example inventory.csv\n"
            "# switch : IP ya da hostname\n"
            "\n"
            "switch,community,vlans,label\n"
            "10.1.1.1,public,,Kat1-SW\n")
        entries = mt.parse_inventory_csv(path)
        self.assertEqual([e.switch for e in entries], ["10.1.1.1"])

    def test_shipped_example_file_is_usable(self):
        # 'cp inventory.csv.example inventory.csv' sonrasi arac calismali.
        example = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "inventory.csv.example")
        entries = mt.parse_inventory_csv(example)
        self.assertTrue(entries)
        self.assertIn("router", {e.role for e in entries})

    def test_error_points_at_the_real_file_line(self):
        # Atlanan yorum satirlari satir numarasini kaydirmamali.
        path = self._write(
            "# aciklama\n"
            "switch,community,vlans\n"
            "10.1.1.1,public,10\n"
            "# arada bir yorum\n"
            "10.2.2.2,public,abc\n")
        with self.assertRaises(ValueError) as ctx:
            mt.parse_inventory_csv(path)
        self.assertIn(":5:", str(ctx.exception))

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

# Asagidaki ciktilar EVE-NG'deki gercek bir IOL switch'inden alindi.
IOS_MAC_TABLE = """SW1#show mac address-table
          Mac Address Table
-------------------------------------------

Vlan    Mac Address       Type        Ports
----    -----------       --------    -----
  10    aabb.cc02.1010    DYNAMIC     Et0/1
  10    0050.7966.6826    DYNAMIC     Et0/0
  99    000c.2987.72dc    DYNAMIC     Et0/3
  99    0045.e284.4021    DYNAMIC     Et0/3
  99    5c7d.aef0.323c    DYNAMIC     Et0/3
Total Mac Addresses for this criterion: 5
SW1#"""

IOSXE_MAC_TABLE = """          Mac Address Table
-------------------------------------------

Vlan    Mac Address       Type        Ports
----    -----------       --------    -----
 All    0100.0ccc.cccc    STATIC      CPU
 All    0180.c200.0000    STATIC      CPU
  10    0050.7966.6826    DYNAMIC     Gi1/0/5
  10    0100.5e00.0128    STATIC      Gi1/0/1 Gi1/0/2
  20    a0b1.c2d3.e4f5    DYNAMIC     Po1
Total Mac Addresses for this criterion: 5"""

NXOS_MAC_TABLE = """Legend:
        * - primary entry, G - Gateway MAC, (R) - Routed MAC, O - Overlay MAC
   VLAN     MAC Address      Type      age     Secure NTFY Ports
---------+-----------------+--------+---------+------+----+------------------
* 10       aabb.cc02.1010   dynamic  0         F      F    Eth1/1
* 20       0050.7966.6826   dynamic  0         F      F    Po10
G  -       5c7d.aef0.323c   static   -         F      F    sup-eth1(R)"""


class TestMacAddressTableParsing(unittest.TestCase):
    """SSH toplayicisinin 'show mac address-table' parse'i (gercek cihaz ciktilari)."""

    def test_ios_iol_output(self):
        observations = mt.parse_mac_address_table(IOS_MAC_TABLE)
        self.assertEqual(len(observations), 5)
        by_mac = {o.mac: o for o in observations}
        pc = by_mac["00:50:79:66:68:26"]
        self.assertEqual((pc.vlan, pc.port), (10, "Et0/0"))
        neighbor = by_mac["AA:BB:CC:02:10:10"]
        self.assertEqual((neighbor.vlan, neighbor.port), (10, "Et0/1"))

    def test_header_and_footer_lines_ignored(self):
        for line in ("Total Mac Addresses for this criterion: 5",
                     "Vlan    Mac Address       Type        Ports",
                     "----    -----------       --------    -----",
                     "          Mac Address Table"):
            self.assertEqual(mt.parse_mac_address_table(line), [])

    def test_static_and_cpu_entries_skipped(self):
        observations = mt.parse_mac_address_table(IOSXE_MAC_TABLE)
        self.assertEqual({o.mac for o in observations},
                         {"00:50:79:66:68:26", "A0:B1:C2:D3:E4:F5"})
        by_mac = {o.mac: o for o in observations}
        self.assertEqual(by_mac["00:50:79:66:68:26"].port, "Gi1/0/5")
        self.assertEqual(by_mac["A0:B1:C2:D3:E4:F5"].port, "Po1")   # port-channel de gecerli

    def test_nxos_output(self):
        observations = mt.parse_mac_address_table(NXOS_MAC_TABLE)
        self.assertEqual(len(observations), 2)                       # static sup-eth1 atlanir
        by_mac = {o.mac: o for o in observations}
        self.assertEqual((by_mac["AA:BB:CC:02:10:10"].vlan,
                          by_mac["AA:BB:CC:02:10:10"].port), (10, "Eth1/1"))
        self.assertEqual(by_mac["00:50:79:66:68:26"].port, "Po10")

    def test_garbage_input_is_safe(self):
        self.assertEqual(mt.parse_mac_address_table(""), [])
        self.assertEqual(mt.parse_mac_address_table("% Invalid input detected at '^' marker."), [])
        self.assertEqual(mt.parse_mac_address_table("Translating \"foo\"...domain server"), [])

    def test_uplink_filter_applies_to_ssh_data(self):
        """IOL ciktisinda Et0/3 uc MAC tasiyor -- esik 2 olursa uplink sayilmali."""
        observations = mt.parse_mac_address_table(IOS_MAC_TABLE)
        self.assertEqual(mt.find_uplink_ports(observations, 2), {"Et0/3"})


class TestSshCollectorWithFakeTransport(unittest.TestCase):
    """collect_switch_ssh -- gercek SSH baglantisi olmadan."""

    def setUp(self):
        self.real_fetch = mt.ssh_fetch_mac_table
        self.addCleanup(setattr, mt, "ssh_fetch_mac_table", self.real_fetch)
        self.entry = mt.SwitchEntry(switch="192.168.122.10", label="SW1")
        self.opts = mt.SshOptions(user="admin", password="x")

    def test_collect(self):
        mt.ssh_fetch_mac_table = lambda entry, opts, verbose=False: IOS_MAC_TABLE
        result = mt.collect_switch_ssh(self.entry, self.opts, uplink_threshold=10)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.mode_used, "ssh")
        self.assertEqual(len(result.observations), 5)
        self.assertEqual(result.port_macs["Et0/3"], 3)

    def test_vlan_filter(self):
        mt.ssh_fetch_mac_table = lambda entry, opts, verbose=False: IOS_MAC_TABLE
        self.entry.vlans = [10]
        result = mt.collect_switch_ssh(self.entry, self.opts, uplink_threshold=10)
        self.assertEqual({o.vlan for o in result.observations}, {10})

    def test_ssh_failure_reported_not_raised(self):
        def boom(entry, opts, verbose=False):
            raise mt.SnmpError("SSH hatasi: AuthenticationException")
        mt.ssh_fetch_mac_table = boom
        result = mt.collect_switch_ssh(self.entry, self.opts, uplink_threshold=10)
        self.assertFalse(result.ok)
        self.assertIn("AuthenticationException", result.error)

    def test_empty_table_is_an_error_not_silent_success(self):
        mt.ssh_fetch_mac_table = lambda entry, opts, verbose=False: "SW1#\nSW1#"
        result = mt.collect_switch_ssh(self.entry, self.opts, uplink_threshold=10)
        self.assertFalse(result.ok)


class TestDot1dPerVlanPortMap(unittest.TestCase):
    """
    Gercek IOL bulgusu: dot1dBasePortIfIndex tablosu da VLAN context'ine bagli.
    Harita her VLAN icin ayri okunmazsa port adi 'bridgeport<N>' olarak kalir.
    """

    def setUp(self):
        self.real_walk = mt.snmp_walk
        self.addCleanup(setattr, mt, "snmp_walk", self.real_walk)
        self.entry = mt.SwitchEntry(switch="10.1.1.1", community="public",
                                    label="SW1", vlans=[10])
        self.opts = mt.SnmpOptions(walk_binary="snmpwalk")

    def test_portmap_read_within_vlan_context(self):
        def fake_walk(entry, opts, oid, vlan=None):
            if oid == mt.OID_DOT1D_BASEPORT_IFINDEX:
                if vlan == 10:                       # VLAN 10 context'i
                    return [(f"{mt.OID_DOT1D_BASEPORT_IFINDEX}.1", "1")]
                return [(f"{mt.OID_DOT1D_BASEPORT_IFINDEX}.3", "3")]   # VLAN 1 context'i
            if oid == mt.OID_IFNAME:
                return [(f"{mt.OID_IFNAME}.1", "Et0/0"), (f"{mt.OID_IFNAME}.3", "Et0/2")]
            if oid == mt.OID_DOT1Q_FDB_PORT:
                return []                            # IOL'de Q-BRIDGE yok
            if oid == mt.OID_DOT1D_FDB_PORT and vlan == 10:
                return [(f"{mt.OID_DOT1D_FDB_PORT}.0.80.121.102.104.38", "1")]
            return []
        mt.snmp_walk = fake_walk

        result = mt.collect_switch(self.entry, self.opts, "auto", 10, False)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.mode_used, "dot1d")
        self.assertEqual(len(result.observations), 1)
        obs = result.observations[0]
        self.assertEqual(obs.mac, "00:50:79:66:68:26")
        self.assertEqual(obs.vlan, 10)
        self.assertEqual(obs.port, "Et0/0")          # 'bridgeport1' OLMAMALI


class TestMultiSwitchPoll(DbTestCase):
    """
    Gercek lab bulgusu: ayni MAC bir poll'da hem kendi access portunda hem de
    aradaki switch'lerin trunk portlarinda gorunur. Hareket tespiti gozlem
    bazinda yapilirsa cihaz hic tasinmasa bile her poll'da sahte 'port
    degisikligi' uretilir.
    """

    def _poll(self, ts, pc_switch="ACCESS_3", pc_port="Et0/2"):
        mac = "00:50:79:66:68:33"
        access = self._result(pc_switch, [mt.Observation(mac, 20, pc_port)], ip="192.168.1.213")
        trunk_obs = [mt.Observation(mac, 20, "Et0/0")] + [
            mt.Observation(f"00:11:22:33:{i:02X}:01", 99, "Et0/0") for i in range(2)]
        trunk = self._result("DIST_SW1", trunk_obs, uplink_threshold=10, ip="192.168.1.201")
        return mt.apply_poll_results(self.conn, [access, trunk], ts, keep_uplinks=False)

    def test_same_mac_on_access_and_trunk_is_not_a_move(self):
        self._poll("2026-09-11T02:13:00Z")
        self._poll("2026-09-11T02:13:44Z")
        self._poll("2026-09-11T02:14:30Z")
        moves = self.conn.execute("SELECT COUNT(*) AS n FROM mac_moves").fetchone()["n"]
        self.assertEqual(moves, 0)

    def test_access_port_is_the_reported_location(self):
        self._poll("2026-09-11T02:13:00Z")
        loc = mt.current_location(self.conn, "00:50:79:66:68:33")
        self.assertEqual((loc["switch"], loc["port"]), ("ACCESS_3", "Et0/2"))

    def test_trunk_sighting_is_still_recorded_as_evidence(self):
        self._poll("2026-09-11T02:13:00Z")
        rows = self.conn.execute(
            "SELECT switch, port FROM mac_locations WHERE mac = ? ORDER BY switch",
            ("00:50:79:66:68:33",)).fetchall()
        self.assertEqual([(r["switch"], r["port"]) for r in rows],
                         [("ACCESS_3", "Et0/2"), ("DIST_SW1", "Et0/0")])

    def test_real_move_between_switches_is_recorded_once(self):
        self._poll("2026-09-11T02:13:00Z")
        self._poll("2026-09-11T02:20:00Z", pc_switch="ACCESS_2", pc_port="Et0/3")
        self._poll("2026-09-11T02:21:00Z", pc_switch="ACCESS_2", pc_port="Et0/3")
        moves = self.conn.execute("SELECT * FROM mac_moves").fetchall()
        self.assertEqual(len(moves), 1)
        self.assertEqual((moves[0]["from_switch"], moves[0]["from_port"]), ("ACCESS_3", "Et0/2"))
        self.assertEqual((moves[0]["to_switch"], moves[0]["to_port"]), ("ACCESS_2", "Et0/3"))

    def test_failed_switch_does_not_block_the_others(self):
        good = self._result("ACCESS_1", [mt.Observation("00:50:79:66:68:30", 10, "Et0/0")])
        bad = self._result("ACCESS_2", [])
        bad.error = "SSH hatasi: timeout"
        stats = mt.apply_poll_results(self.conn, [good, bad], "2026-09-11T02:13:00Z", False)
        self.assertEqual(stats["ACCESS_1"], (1, 0))
        self.assertNotIn("ACCESS_2", stats)
        self.assertIsNotNone(mt.current_location(self.conn, "00:50:79:66:68:30"))


# ---------------------------------------------------------------------------
# ARP TOPLAMA
# ---------------------------------------------------------------------------

# Asagidaki ciktilar gercek cihaz formatlaridir.
IOS_ARP_TABLE = """R1#show ip arp
Protocol  Address          Age (min)  Hardware Addr   Type   Interface
Internet  192.168.1.201           -   aabb.cc00.0100  ARPA   Vlan99
Internet  192.168.1.10           12   0050.7966.6800  ARPA   Vlan99
Internet  10.10.10.1              -   aabb.cc00.0110  ARPA   Vlan10
Internet  10.10.10.20             3   0050.7966.6801  ARPA   Vlan10
Internet  10.20.20.30             0   Incomplete      ARPA
R1#"""

IOSXE_ARP_TABLE = """Protocol  Address          Age (min)  Hardware Addr   Type   Interface
Internet  10.30.30.1              -   0050.7966.6810  ARPA   GigabitEthernet0/0/1
Internet  10.30.30.55            41   a0b1.c2d3.e4f5  ARPA   GigabitEthernet0/0/1"""

NXOS_ARP_TABLE = """Flags: * - Adjacencies learnt on non-active FHRP router
       + - Adjacencies synced via CFSoE
       # - Adjacencies Throttled for Glean

IP ARP Table for context default
Total number of entries: 3
Address         Age       MAC Address     Interface       Flags
10.10.10.1      00:12:33  aabb.cc00.0200  Vlan10
10.10.10.21     00:00:14  0050.7966.6802  Vlan10
*10.10.10.22    00:04:02  0050.7966.6803  Eth1/5"""


class TestIpHelpers(unittest.TestCase):
    def test_normalize_ip(self):
        self.assertEqual(mt.normalize_ip(" 192.168.1.10 "), "192.168.1.10")

    def test_invalid_ip_raises(self):
        for bad in ("192.168.1.300", "192.168.1", "abc", "", "10.0.0.1/24"):
            with self.assertRaises(ValueError):
                mt.normalize_ip(bad)

    def test_ip_from_arp_oid(self):
        self.assertEqual(
            mt.ip_from_arp_oid("1.3.6.1.2.1.4.22.1.2.3.192.168.1.10"), "192.168.1.10")

    def test_arp_oid_index_has_ifindex(self):
        self.assertEqual(
            mt.arp_index_from_oid("1.3.6.1.2.1.4.22.1.2.12.10.10.10.20"),
            (12, "10.10.10.20"))

    def test_bad_arp_oid_raises(self):
        for bad in ("1.3.6.1", "1.3.6.1.2.1.4.22.1.2.3.192.168.1.999"):
            with self.assertRaises(ValueError):
                mt.ip_from_arp_oid(bad)

    def test_mac_from_snmp_value_both_formats(self):
        # MIB yuklu: 'aa:bb:...'   MIB yok: 'AA BB CC DD EE FF'
        self.assertEqual(mt.mac_from_snmp_value("00:50:79:66:68:00"), "00:50:79:66:68:00")
        self.assertEqual(mt.mac_from_snmp_value("00 50 79 66 68 00 "), "00:50:79:66:68:00")


class TestArpTableParsing(unittest.TestCase):
    """'show ip arp' parse'i -- gercek cihaz ciktilari uzerinden."""

    def test_ios_output(self):
        arps = mt.parse_ip_arp_table(IOS_ARP_TABLE)
        by_ip = {a.ip: a for a in arps}
        self.assertEqual(len(arps), 4)          # 'Incomplete' satiri sayilmaz
        self.assertEqual(by_ip["10.10.10.20"].mac, "00:50:79:66:68:01")
        self.assertEqual(by_ip["10.10.10.20"].interface, "Vlan10")

    def test_incomplete_entries_skipped(self):
        # Cozulememis ARP istegi, o IP'de bir cihaz oldugu anlamina gelmez.
        arps = mt.parse_ip_arp_table(IOS_ARP_TABLE)
        self.assertNotIn("10.20.20.30", {a.ip for a in arps})

    def test_router_own_svi_is_kept(self):
        # Age '-' olan kayitlar router'in kendi arayuz adresleridir; gateway'in
        # MAC'ini bilmek ise ise yarar.
        arps = {a.ip: a for a in mt.parse_ip_arp_table(IOS_ARP_TABLE)}
        self.assertEqual(arps["10.10.10.1"].mac, "AA:BB:CC:00:01:10")

    def test_iosxe_long_interface_names(self):
        arps = {a.ip: a for a in mt.parse_ip_arp_table(IOSXE_ARP_TABLE)}
        self.assertEqual(arps["10.30.30.55"].mac, "A0:B1:C2:D3:E4:F5")
        self.assertEqual(arps["10.30.30.55"].interface, "GigabitEthernet0/0/1")

    def test_nxos_output(self):
        arps = mt.parse_ip_arp_table(NXOS_ARP_TABLE)
        by_ip = {a.ip: a for a in arps}
        self.assertEqual(len(arps), 3)
        # Basliklar, bayrak aciklamalari ve 'Total number of entries' satiri elenmeli
        self.assertEqual(by_ip["10.10.10.22"].mac, "00:50:79:66:68:03")
        self.assertEqual(by_ip["10.10.10.22"].interface, "Eth1/5")
        # NX-OS'un 'Age' kolonu (00:12:33) MAC sanilmamali
        self.assertEqual(by_ip["10.10.10.1"].mac, "AA:BB:CC:00:02:00")

    def test_garbage_input_is_safe(self):
        self.assertEqual(mt.parse_ip_arp_table(""), [])
        self.assertEqual(mt.parse_ip_arp_table("% Invalid input detected"), [])
        self.assertEqual(mt.parse_ip_arp_table("999.1.1.1  -  aabb.cc00.0100  ARPA  Vl1"), [])


class TestInventoryRole(unittest.TestCase):
    def _write(self, text):
        handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
        handle.write(text)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_default_role_is_switch(self):
        entries = mt.parse_inventory_csv(self._write(
            "switch,community,vlans,label\n10.1.1.1,public,,SW1\n"))
        self.assertEqual(entries[0].role, "switch")
        self.assertTrue(entries[0].collects_macs)
        self.assertFalse(entries[0].collects_arp)

    def test_router_and_both(self):
        entries = mt.parse_inventory_csv(self._write(
            "switch,community,vlans,label,version,role\n"
            "10.1.0.1,public,,GW,2c,router\n"
            "10.1.0.2,public,,L3SW,2c,both\n"))
        router, l3 = entries
        self.assertEqual((router.collects_macs, router.collects_arp), (False, True))
        self.assertEqual((l3.collects_macs, l3.collects_arp), (True, True))

    def test_invalid_role_raises_clear_error(self):
        with self.assertRaises(ValueError) as ctx:
            mt.parse_inventory_csv(self._write(
                "switch,community,vlans,label,version,role\n10.1.0.1,public,,GW,2c,firewall\n"))
        self.assertIn("role", str(ctx.exception))


class TestArpStorage(DbTestCase):
    def _arp_result(self, label, pairs, ip="192.168.1.254"):
        entry = mt.SwitchEntry(switch=ip, community="public", label=label, role="router")
        arps = [mt.ArpEntry(ip=i, mac=m, interface=iface) for i, m, iface in pairs]
        return mt.ArpResult(entry=entry, arps=arps, mode_used="arp-ssh")

    def test_repeat_polls_do_not_create_new_rows(self):
        result = self._arp_result("GW", [("10.10.10.20", "00:50:79:66:68:01", "Vlan10")])
        for index in range(4):
            mt.apply_arp_results(self.conn, [result], f"2026-09-11T10:0{index}:00Z")
        rows = self.conn.execute("SELECT * FROM arp_sightings").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["seen_count"], 4)
        self.assertEqual(rows[0]["first_seen"], "2026-09-11T10:00:00Z")
        self.assertEqual(rows[0]["last_seen"], "2026-09-11T10:03:00Z")

    def test_new_mac_for_same_ip_keeps_history(self):
        mt.apply_arp_results(self.conn, [self._arp_result(
            "GW", [("10.10.10.20", "00:50:79:66:68:01", "Vlan10")])], "2026-09-11T10:00:00Z")
        mt.apply_arp_results(self.conn, [self._arp_result(
            "GW", [("10.10.10.20", "00:50:79:66:68:09", "Vlan10")])], "2026-09-11T11:00:00Z")
        rows = self.conn.execute(
            "SELECT * FROM arp_sightings WHERE ip = '10.10.10.20'").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(mt.current_mac_for_ip(self.conn, "10.10.10.20")["mac"],
                         "00:50:79:66:68:09")

    def test_failed_result_writes_nothing(self):
        bad = self._arp_result("GW", [("10.10.10.20", "00:50:79:66:68:01", "Vlan10")])
        bad.error = "SSH hatasi: timeout"
        stats = mt.apply_arp_results(self.conn, [bad], "2026-09-11T10:00:00Z")
        self.assertEqual(stats, {})
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM arp_sightings").fetchone()["n"], 0)

    def test_ip_for_mac_lookup(self):
        mt.apply_arp_results(self.conn, [self._arp_result(
            "GW", [("10.10.10.20", "00:50:79:66:68:01", "Vlan10")])], "2026-09-11T10:00:00Z")
        self.assertEqual(
            mt.current_ip_for_mac(self.conn, "00:50:79:66:68:01")["ip"], "10.10.10.20")
        self.assertIsNone(mt.current_ip_for_mac(self.conn, "00:00:00:00:00:01"))

    def test_lookup_ip_says_so_when_mac_is_in_no_fdb(self):
        # ARP IP'yi MAC'e cevirdi ama cihazin switch'i envanterde degil:
        # "kayit yok" yerine nedenini soylemeli.
        mt.apply_arp_results(self.conn, [self._arp_result(
            "GW", [("10.10.10.20", "00:50:79:66:68:01", "Vlan10")])], "2026-09-11T10:00:00Z")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            mt.cmd_lookup_ip(self.conn, "10.10.10.20", 15)
        output = buffer.getvalue()
        self.assertIn("00:50:79:66:68:01", output)
        self.assertIn("hicbir switch", output)

    def test_prune_removes_old_arp_rows(self):
        old = (datetime.now(timezone.utc) - timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%SZ")
        mt.apply_arp_results(self.conn, [self._arp_result(
            "GW", [("10.10.10.20", "00:50:79:66:68:01", "Vlan10")])], old)
        mt.cmd_prune(self.conn, 365)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM arp_sightings").fetchone()["n"], 0)


class TestArpCollectors(unittest.TestCase):
    """ARP toplayicilari -- gercek SSH/SNMP baglantisi olmadan."""

    def setUp(self):
        self.real_fetch = mt.ssh_fetch_arp_table
        self.addCleanup(setattr, mt, "ssh_fetch_arp_table", self.real_fetch)
        self.real_walk = mt.snmp_walk
        self.addCleanup(setattr, mt, "snmp_walk", self.real_walk)
        self.entry = mt.SwitchEntry(switch="192.168.1.254", community="public",
                                    label="GW", role="router")

    def test_ssh_collect(self):
        mt.ssh_fetch_arp_table = lambda entry, opts, verbose=False: IOS_ARP_TABLE
        result = mt.collect_arp_ssh(self.entry, mt.SshOptions(user="admin"))
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.mode_used, "arp-ssh")
        self.assertEqual(len(result.arps), 4)

    def test_ssh_failure_reported_not_raised(self):
        def boom(entry, opts, verbose=False):
            raise mt.SnmpError("SSH hatasi: AuthenticationException")
        mt.ssh_fetch_arp_table = boom
        result = mt.collect_arp_ssh(self.entry, mt.SshOptions(user="admin"))
        self.assertFalse(result.ok)
        self.assertIn("AuthenticationException", result.error)

    def test_ssh_empty_table_is_an_error_not_silent_success(self):
        mt.ssh_fetch_arp_table = lambda entry, opts, verbose=False: "R1#\nR1#"
        result = mt.collect_arp_ssh(self.entry, mt.SshOptions(user="admin"))
        self.assertFalse(result.ok)

    def _fake_walk(self, types=None):
        phys = {
            "1.3.6.1.2.1.4.22.1.2.7.10.10.10.1": "aa:bb:cc:00:01:10",
            "1.3.6.1.2.1.4.22.1.2.7.10.10.10.20": "00 50 79 66 68 01",
            "1.3.6.1.2.1.4.22.1.2.7.10.10.10.99": "00:50:79:66:68:99",
        }
        type_rows = types if types is not None else {}

        def walk(entry, opts, oid, vlan=None):
            if oid == mt.OID_IPNETTOMEDIA_PHYS:
                return list(phys.items())
            if oid == mt.OID_IPNETTOMEDIA_TYPE:
                return list(type_rows.items())
            if oid == mt.OID_IFNAME:
                return [(f"{mt.OID_IFNAME}.7", "Vlan10")]
            return []
        return walk

    def test_snmp_collect_maps_ip_mac_and_interface(self):
        mt.snmp_walk = self._fake_walk()
        result = mt.collect_arp_snmp(self.entry, mt.SnmpOptions(walk_binary="snmpwalk"))
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.mode_used, "arp-snmp")
        by_ip = {a.ip: a for a in result.arps}
        self.assertEqual(by_ip["10.10.10.20"].mac, "00:50:79:66:68:01")
        self.assertEqual(by_ip["10.10.10.20"].interface, "Vlan10")

    def test_snmp_invalid_entries_filtered(self):
        mt.snmp_walk = self._fake_walk(
            types={"1.3.6.1.2.1.4.22.1.4.7.10.10.10.99": str(mt.ARP_TYPE_INVALID)})
        result = mt.collect_arp_snmp(self.entry, mt.SnmpOptions(walk_binary="snmpwalk"))
        self.assertNotIn("10.10.10.99", {a.ip for a in result.arps})
        self.assertEqual(len(result.arps), 2)

    def test_snmp_empty_table_is_an_error(self):
        mt.snmp_walk = lambda entry, opts, oid, vlan=None: []
        result = mt.collect_arp_snmp(self.entry, mt.SnmpOptions(walk_binary="snmpwalk"))
        self.assertFalse(result.ok)

    def test_snmp_garbage_values_do_not_crash(self):
        def walk(entry, opts, oid, vlan=None):
            if oid == mt.OID_IPNETTOMEDIA_PHYS:
                return [("1.3.6.1.2.1.4.22.1.2.7.10.10.10.1", "No Such Object"),
                        ("bozuk.oid", "zzzz"),
                        ("1.3.6.1.2.1.4.22.1.2.7.10.10.10.5", "00:50:79:66:68:05")]
            return []
        mt.snmp_walk = walk
        result = mt.collect_arp_snmp(self.entry, mt.SnmpOptions(walk_binary="snmpwalk"))
        self.assertTrue(result.ok, result.error)
        self.assertEqual([a.ip for a in result.arps], ["10.10.10.5"])


class TestPollWithRoles(DbTestCase):
    """role kolonu: switch'ten FDB, router'dan ARP okunur."""

    def setUp(self):
        super().setUp()
        for name in ("collect_switch_ssh", "collect_arp_ssh"):
            self.addCleanup(setattr, mt, name, getattr(mt, name))
        self.mac_calls = []
        self.arp_calls = []

        def fake_mac(entry, opts, uplink_threshold, verbose=False):
            self.mac_calls.append(entry.label)
            result = mt.SwitchResult(entry=entry, mode_used="ssh",
                                     observations=[mt.Observation("00:50:79:66:68:01", 10, "Et0/3")])
            result.port_macs = {"Et0/3": 1}
            return result

        def fake_arp(entry, opts, verbose=False):
            self.arp_calls.append(entry.label)
            return mt.ArpResult(entry=entry, mode_used="arp-ssh",
                                arps=[mt.ArpEntry("10.10.10.20", "00:50:79:66:68:01", "Vlan10")])

        mt.collect_switch_ssh = fake_mac
        mt.collect_arp_ssh = fake_arp
        self.entries = [
            mt.SwitchEntry(switch="192.168.1.211", label="ACCESS_1"),
            mt.SwitchEntry(switch="192.168.1.254", label="GW", role="router"),
        ]

    def _poll(self):
        return mt.poll_once(self.conn, self.entries, mt.SnmpOptions(), "auto", 10,
                            False, True, workers=1, collector="ssh",
                            ssh_opts=mt.SshOptions(user="admin"))

    def test_router_is_not_asked_for_a_mac_table(self):
        stats = self._poll()
        self.assertEqual(self.mac_calls, ["ACCESS_1"])
        self.assertEqual(self.arp_calls, ["GW"])
        self.assertEqual(stats["recorded"], 1)
        self.assertEqual(stats["arp_recorded"], 1)

    def test_ip_resolves_to_switch_port(self):
        self._poll()
        arp = mt.current_mac_for_ip(self.conn, "10.10.10.20")
        self.assertEqual(arp["mac"], "00:50:79:66:68:01")
        loc = mt.current_location(self.conn, arp["mac"])
        self.assertEqual((loc["switch"], loc["port"]), ("ACCESS_1", "Et0/3"))

    def test_arp_run_does_not_count_as_a_mac_poll(self):
        # 'Cihaz koptu mu, switch'e mi ulasamiyoruz' ayrimi yalnizca FDB
        # okumalarina bakmali: ARP'in basarili olmasi FDB'yi okudugumuzu
        # gostermez.
        self._poll()
        self.assertIsNone(mt.last_successful_poll(self.conn, "GW"))
        self.assertIsNotNone(mt.last_successful_poll(self.conn, "GW", kind="arp"))
        self.assertIsNotNone(mt.last_successful_poll(self.conn, "ACCESS_1"))


class TestSchemaMigration(unittest.TestCase):
    """Eski surumle acilmis bir DB, kind kolonu eklenerek kullanilabilmeli."""

    def test_kind_column_added_to_existing_db(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        old = sqlite3.connect(handle.name)
        old.executescript(
            "CREATE TABLE poll_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "switch TEXT NOT NULL, switch_ip TEXT NOT NULL DEFAULT '', "
            "started_at TEXT NOT NULL, finished_at TEXT NOT NULL, ok INTEGER NOT NULL, "
            "mode_used TEXT NOT NULL DEFAULT '', observed INTEGER NOT NULL DEFAULT 0, "
            "recorded INTEGER NOT NULL DEFAULT 0, uplinks INTEGER NOT NULL DEFAULT 0, "
            "error TEXT NOT NULL DEFAULT '');"
            "INSERT INTO poll_runs (switch, started_at, finished_at, ok) "
            "VALUES ('SW1', '2026-09-01T10:00:00Z', '2026-09-01T10:00:02Z', 1);"
        )
        old.commit()
        old.close()

        conn = mt.init_db(handle.name)
        self.addCleanup(conn.close)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(poll_runs)")}
        self.assertIn("kind", columns)
        # Eski satirlar MAC pollu sayilir, ARP degil.
        self.assertEqual(conn.execute("SELECT kind FROM poll_runs").fetchone()["kind"], "mac")
        self.assertIsNotNone(mt.last_successful_poll(conn, "SW1"))
        # Ikinci acilis bir sey degistirmemeli.
        self.assertEqual(mt.migrate_db(conn), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
