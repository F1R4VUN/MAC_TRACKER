#!/usr/bin/env python3
"""
mac_tracker.py

Amac: Ag uzerindeki switch'lerin MAC adres tablosunu periyodik olarak
SNMP ile okuyup bir SQLite veritabanina kaydetmek -- boylece kayip/calinmis
bir cihazin son ne zaman, hangi switch'in hangi portunda gorulduugunu
gecmise donuk sorgulayabilirsin.

VERITABANI HAKKINDA ONEMLI NOT:
    SQLite bir sunucu DEGIL, sadece bir dosya. Ayri bir kurulum/servis
    gerekmez. Bu scripti ilk calistirdiginda --db ile belirttigin dosya
    (varsayilan: mac_tracker.db) yoksa otomatik olusturulur, tablo/index
    kendisi kurulur. "Veritabani olusturma" diye ayri bir adim YOK.

GEREKSINIMLER:
    - Sistemde snmpwalk komutu kurulu olmali (net-snmp).
        Windows : https://www.net-snmp.org/ (veya) choco install net-snmp
        Linux   : sudo apt install snmp
    - Switch'lerde SNMP (v2c) read-only community acik olmali.

KULLANIM:
    1) Envanter dosyasi hazirla (ornek: inventory.csv):
         switch,community,vlans
         10.1.1.1,public,1;10;20
         10.1.2.1,public,1;10;30

    2) Tek seferlik poll (ilk denemede bunu kullan, hatalari gormek kolay):
         python mac_tracker.py --once --inventory inventory.csv --db mac_tracker.db

    3) Surekli calisan mod (Ctrl+C ile durdur):
         python mac_tracker.py --loop --interval 60 --inventory inventory.csv --db mac_tracker.db

       ALTERNATIF (onerilen -- uretimde daha saglam): Python'u surekli acik
       tutmak yerine --once modunu Windows Task Scheduler / cron ile her
       1-2 dakikada bir calistir. Laptop uykuya gecerse ya da ag kesilirse
       surekli-calisan bir loop sessizce durabilir; scheduler kullanirsan
       her calisma bagimsizdir, bir calisma kacsa bile bir sonraki calisir.

    4) Bir MAC'in en son nerede gorundugunu sorgula:
         python mac_tracker.py --lookup AA:BB:CC:DD:EE:FF --db mac_tracker.db

    5) Bir MAC'in tum gecmisini (hangi switch/portlarda dolasti) gor:
         python mac_tracker.py --history AA:BB:CC:DD:EE:FF --db mac_tracker.db
"""

from __future__ import annotations
import argparse
import csv
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# 1) SAF FONKSIYONLAR -- SNMP/DB baglantisi olmadan test edilebilir
# ---------------------------------------------------------------------------

def mac_from_oid_suffix(oid: str) -> str:
    """
    dot1dTpFdbTable'in OID index'i, MAC adresinin 6 byte'ini sondaki 6
    alt-tanimlayici olarak tasir. Ornek:
        .1.3.6.1.2.1.17.4.3.1.2.0.26.203.10.20.30
    -> son 6 sayi: 0.26.203.10.20.30 -> MAC: 00:1A:CB:0A:14:1E
    """
    parts = oid.strip().lstrip(".").split(".")
    if len(parts) < 6:
        raise ValueError(f"OID'de yeterli alt-tanimlayici yok: {oid!r}")
    last6 = parts[-6:]
    try:
        byte_values = [int(p) for p in last6]
    except ValueError as exc:
        raise ValueError(f"OID suffix'i sayisal degil: {oid!r}") from exc
    if any(b < 0 or b > 255 for b in byte_values):
        raise ValueError(f"OID suffix'i gecerli byte degerleri degil: {oid!r}")
    return ":".join(f"{b:02X}" for b in byte_values)


def normalize_mac(mac: str) -> str:
    """Kullanicidan gelen MAC'i (farkli formatlarda olabilir) standart AA:BB:CC:DD:EE:FF haline getirir."""
    cleaned = mac.strip().upper().replace("-", ":").replace(".", "")
    if ":" not in cleaned and len(cleaned) == 12:
        cleaned = ":".join(cleaned[i:i + 2] for i in range(0, 12, 2))
    parts = cleaned.split(":")
    if len(parts) != 6 or any(len(p) != 2 for p in parts):
        raise ValueError(f"Gecersiz MAC formati: {mac!r}")
    return ":".join(parts)


@dataclass
class SwitchEntry:
    switch: str
    community: str
    vlans: list[int] = field(default_factory=list)
    label: str = ""

    def __post_init__(self):
        if not self.label:
            self.label = self.switch


def parse_inventory_csv(path: str) -> list[SwitchEntry]:
    """
    inventory.csv formatini okur:
        switch,community,vlans[,label]
        10.1.1.1,public,1;10;20,Kat1-Switch
    'vlans' noktali virgulle ayrilmis VLAN ID listesidir.
    """
    entries: list[SwitchEntry] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        required = {"switch", "community", "vlans"}
        if reader.fieldnames is None or not required.issubset(set(reader.fieldnames)):
            raise ValueError(
                f"inventory.csv basliklari eksik. Gerekli: {sorted(required)}, "
                f"bulunan: {reader.fieldnames}"
            )
        for row in reader:
            vlan_ids = [int(v) for v in row["vlans"].split(";") if v.strip()]
            entries.append(
                SwitchEntry(
                    switch=row["switch"].strip(),
                    community=row["community"].strip(),
                    vlans=vlan_ids,
                    label=(row.get("label") or "").strip(),
                )
            )
    return entries


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 2) VERITABANI KATMANI -- SQLite, SNMP'siz test edilebilir
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS sightings (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT    NOT NULL,
    switch    TEXT    NOT NULL,
    vlan      INTEGER,
    port      TEXT,
    mac       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sightings_mac ON sightings(mac);
CREATE INDEX IF NOT EXISTS idx_sightings_switch_port ON sightings(switch, port);
"""


def init_db(db_path: str) -> sqlite3.Connection:
    """Veritabani dosyasi yoksa olusturur, tabloyu/index'i kurar. Ayri bir kurulum adimi gerekmez."""
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def record_sighting(conn: sqlite3.Connection, switch: str, vlan: int, port: str, mac: str, timestamp: str | None = None) -> None:
    conn.execute(
        "INSERT INTO sightings (timestamp, switch, vlan, port, mac) VALUES (?, ?, ?, ?, ?)",
        (timestamp or now_iso(), switch, vlan, port, mac),
    )
    conn.commit()


def lookup_last_seen(conn: sqlite3.Connection, mac: str) -> tuple | None:
    mac = normalize_mac(mac)
    cur = conn.execute(
        "SELECT timestamp, switch, vlan, port FROM sightings WHERE mac = ? ORDER BY timestamp DESC LIMIT 1",
        (mac,),
    )
    return cur.fetchone()


def get_history(conn: sqlite3.Connection, mac: str) -> list[tuple]:
    mac = normalize_mac(mac)
    cur = conn.execute(
        "SELECT timestamp, switch, vlan, port FROM sightings WHERE mac = ? ORDER BY timestamp ASC",
        (mac,),
    )
    return cur.fetchall()


# ---------------------------------------------------------------------------
# 3) SNMP KATMANI -- gercek switch/ag gerektirir, snmpwalk binary'sine ihtiyac duyar
# ---------------------------------------------------------------------------

def _check_snmpwalk_available() -> None:
    if shutil.which("snmpwalk") is None:
        raise SystemExit(
            "snmpwalk komutu bulunamadi. Kurulum:\n"
            "  Windows : https://www.net-snmp.org/ adresinden indir, ya da 'choco install net-snmp'\n"
            "  Linux   : sudo apt install snmp"
        )


def _run_snmpwalk(host: str, community: str, oid: str, timeout: int = 3) -> list[tuple[str, str]]:
    """snmpwalk calistirir, (oid, value) ciftlerinin listesini dondurur. Sorunda bos liste + uyari verir."""
    cmd = ["snmpwalk", "-v2c", "-c", community, "-Onq", "-t", str(timeout), "-r", "1", host, oid]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout * 5)
    except subprocess.TimeoutExpired:
        print(f"  [!] {host}: snmpwalk zaman asimina ugradi (oid={oid})", file=sys.stderr)
        return []
    if result.returncode != 0 and not result.stdout.strip():
        print(f"  [!] {host}: snmpwalk hata verdi: {result.stderr.strip()}", file=sys.stderr)
        return []

    pairs = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        oid_part, value_part = parts
        pairs.append((oid_part, value_part.strip().strip('"')))
    return pairs


def get_bridgeport_to_ifindex(host: str, community: str) -> dict[int, int]:
    """dot1dBasePortIfIndex tablosu: bridge-port numarasi -> ifIndex."""
    OID = "1.3.6.1.2.1.17.1.4.1.2"
    result = {}
    for oid, value in _run_snmpwalk(host, community, OID):
        bridgeport = int(oid.split(".")[-1])
        result[bridgeport] = int(value)
    return result


def get_ifname_map(host: str, community: str) -> dict[int, str]:
    """IF-MIB ifName tablosu: ifIndex -> port adi (orn. 'Gi1/0/5')."""
    OID = "1.3.6.1.2.1.31.1.1.1.1"
    result = {}
    for oid, value in _run_snmpwalk(host, community, OID):
        ifindex = int(oid.split(".")[-1])
        result[ifindex] = value
    return result


def get_fdb_entries(host: str, community: str, vlan: int) -> list[tuple[str, int]]:
    """
    dot1dTpFdbPort tablosunu VLAN'a ozel community (community@vlan) ile okur.
    Doner: [(mac, bridgeport), ...]
    """
    OID = "1.3.6.1.2.1.17.4.3.1.2"
    vlan_community = f"{community}@{vlan}"
    entries = []
    for oid, value in _run_snmpwalk(host, vlan_community, OID):
        try:
            mac = mac_from_oid_suffix(oid)
            bridgeport = int(value)
        except ValueError:
            continue
        if bridgeport <= 0:
            continue
        entries.append((mac, bridgeport))
    return entries


# ---------------------------------------------------------------------------
# 4) POLL ORKESTRASYONU
# ---------------------------------------------------------------------------

def poll_switch(entry: SwitchEntry, conn: sqlite3.Connection) -> int:
    """Bir switch'in tum VLAN'larini tarar, bulunan sighting'leri DB'ye yazar. Kac kayit yazildigini dondurur."""
    print(f"[{entry.label}] sorgulaniyor ({entry.switch}) ...")

    portindex_map = get_bridgeport_to_ifindex(entry.switch, entry.community)
    ifname_map = get_ifname_map(entry.switch, entry.community)

    if not portindex_map:
        print(f"  [!] {entry.label}: dot1dBasePortIfIndex okunamadi, switch atlaniyor.", file=sys.stderr)
        return 0

    count = 0
    ts = now_iso()
    for vlan in entry.vlans:
        fdb = get_fdb_entries(entry.switch, entry.community, vlan)
        for mac, bridgeport in fdb:
            ifindex = portindex_map.get(bridgeport)
            portname = ifname_map.get(ifindex, f"ifIndex{ifindex}") if ifindex else f"bridgeport{bridgeport}"
            record_sighting(conn, entry.label, vlan, portname, mac, timestamp=ts)
            count += 1
    print(f"  -> {count} kayit yazildi.")
    return count


def poll_once(inventory_path: str, db_path: str) -> None:
    _check_snmpwalk_available()
    entries = parse_inventory_csv(inventory_path)
    conn = init_db(db_path)
    total = 0
    for entry in entries:
        total += poll_switch(entry, conn)
    conn.close()
    print(f"Tamamlandi. Toplam {total} sighting kaydedildi -> {db_path}")


# ---------------------------------------------------------------------------
# 5) CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Switch MAC adres tablolarini SNMP ile takip eder.")
    parser.add_argument("--inventory", default="inventory.csv", help="Switch envanter CSV dosyasi")
    parser.add_argument("--db", default="mac_tracker.db", help="SQLite veritabani dosyasi")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Tek seferlik poll yap ve cik (varsayilan)")
    mode.add_argument("--loop", action="store_true", help="Surekli calis, --interval'da bir poll et")
    mode.add_argument("--lookup", metavar="MAC", help="Bir MAC'in en son nerede gorundugunu sorgula")
    mode.add_argument("--history", metavar="MAC", help="Bir MAC'in tum gecmisini goster")

    parser.add_argument("--interval", type=int, default=60, help="--loop modunda poll araligi (saniye)")

    args = parser.parse_args()

    if args.lookup:
        conn = init_db(args.db)
        row = lookup_last_seen(conn, args.lookup)
        if row is None:
            print(f"{args.lookup} icin hic kayit bulunamadi.")
        else:
            ts, switch, vlan, port = row
            print(f"Son gorulme: {ts}")
            print(f"  Switch : {switch}")
            print(f"  VLAN   : {vlan}")
            print(f"  Port   : {port}")
        conn.close()
        return

    if args.history:
        conn = init_db(args.db)
        rows = get_history(conn, args.history)
        if not rows:
            print(f"{args.history} icin hic kayit bulunamadi.")
        else:
            print(f"{args.history} icin {len(rows)} kayit:")
            for ts, switch, vlan, port in rows:
                print(f"  {ts}  {switch:20s} vlan={vlan:<5} port={port}")
        conn.close()
        return

    if args.loop:
        import time
        print(f"Surekli mod: her {args.interval} saniyede bir poll. Durdurmak icin Ctrl+C.")
        try:
            while True:
                poll_once(args.inventory, args.db)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nDurduruldu.")
        return

    # varsayilan / --once
    poll_once(args.inventory, args.db)


if __name__ == "__main__":
    main()
