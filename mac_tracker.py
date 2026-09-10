#!/usr/bin/env python3
"""
mac_tracker.py -- Ag uzerindeki cihazlarin hangi switch portunda oldugunu bulur
ve kaydeder. Cihaz portundan koptuysa "en son ne zaman aktifti" sorusunu
gecmise donuk cevaplar.

NE YAPAR
    Envanterdeki her switch'in MAC adres tablosunu (FDB) SNMP ile okur ve her
    MAC icin "su switch'in su portunda, su VLAN'da" kaydini bir SQLite
    dosyasinda tutar. Ayni MAC ayni portta tekrar gorulurse yeni satir
    ACILMAZ, sadece o kaydin 'last_seen' alani guncellenir. Boylece:

        * DB cihaz sayisi kadar buyur, poll sayisi kadar buyumez.
        * Cihaz agdan koptugunda 'last_seen' donar kalir -> "en son ne zaman
          bu portta aktifti" bilgisi tam olarak budur.
        * Cihaz port degistirdiginde bu hareket mac_moves tablosuna yazilir.

VERITABANI HAKKINDA
    SQLite bir sunucu DEGIL, sadece bir dosya. Ayri kurulum/servis gerekmez.
    --db ile verdigin dosya yoksa otomatik olusturulur, tablolar/index'ler
    kendiliginden kurulur. Ayri bir "veritabani olusturma" adimi YOK.

GEREKSINIMLER
    - net-snmp araclari (snmpbulkwalk varsa o kullanilir, yoksa snmpwalk):
        Linux   : sudo apt install snmp
        Windows : https://www.net-snmp.org/  (veya) choco install net-snmp
    - Switch'lerde read-only SNMP erisimi (v2c community ya da v3 kullanici).
    - Python 3.9+ ve SQLite 3.24+ (UPSERT icin; Python 3.9 ile gelen surum yeterli).

VERI KAYNAGI (--collector)
    snmp : Varsayilan. Asagidaki --mode yontemleriyle FDB'yi SNMP ile okur.
    ssh  : Cihaza SSH ile baglanip 'show mac address-table' ciktisini parse
           eder. VLAN + MAC + port'u tek komutta verir, hicbir MIB destegine
           ihtiyac duymaz. Bazi platformlarda (ornegin EVE-NG/GNS3'teki IOL
           imajlari) Q-BRIDGE MIB yoktur, 'community@vlan' indexlemesi ve
           VLAN context'leri de calismaz -- orada tek calisan yol budur.
           paramiko gerektirir: pip3 install paramiko

MAC TABLOSU OKUMA YONTEMLERI (--mode, sadece --collector snmp icin)
    dot1q  : Standart Q-BRIDGE MIB (dot1qTpFdbPort). VLAN bilgisi OID
             index'inde geldigi icin TEK walk ile butun VLAN'lari verir.
             Marka bagimsizdir ve hizlidir. VARSAYILAN tercih.
    dot1d  : Klasik BRIDGE MIB (dot1dTpFdbPort). VLAN bilgisi tasimadigi icin
             Cisco'da VLAN basina "community@vlan" (v2c) ya da "vlan-<id>"
             context (v3) hilesi ile her VLAN ayri ayri okunur. Eski
             Catalyst'ler icin. Envanterde 'vlans' kolonu SART.
    auto   : Once dot1q dener, bos donerse dot1d'ye duser (varsayilan).

UPLINK/TRUNK PORTLARI
    Bir cihazin MAC'i kendi switch'inin access portunda gorundugu gibi
    aradaki tum switch'lerin uplink portlarinda da gorunur. "Cihaz hangi
    portta" cevabinin dogru olmasi icin tek bir portta --uplink-threshold
    degerinden (varsayilan 10) fazla MAC varsa o port uplink/trunk kabul
    edilir ve kaydedilmez. Hepsini kaydetmek istersen --keep-uplinks ver.

ZAMAN DAMGALARI
    DB'ye her zaman UTC yazilir (2026-09-10T20:54:01Z). Boylece metin
    siralamasi = kronolojik siralama olur (yaz saati degisimi bozamaz).
    Ekranda her zaman yerel saate cevrilerek gosterilir.

KULLANIM
    1) Envanter dosyasi (inventory.csv):
         switch,community,vlans,label
         10.1.1.1,public,,Kat1-SW
         10.1.2.1,public,1;10;20,Kat2-SW-Eski

       'vlans' dot1q modunda bos birakilabilir (bos = tum VLAN'lar).
       dot1d/Cisco modunda taranacak VLAN'lari ';' ile yaz.

    2) Ilk deneme (hatalari gormek icin tek seferlik + ayrintili cikti):
         python mac_tracker.py --once -v

       SNMP ile FDB okunamiyorsa (IOL/IOU gibi kisitli imajlar) SSH yolu:
         export MACTRACK_SSH_PASS='...'
         python mac_tracker.py --once -v --collector ssh --ssh-user admin

    3) Surekli toplama -- ONERILEN YOL: Python'u acik tutmak yerine --once
       modunu cron / Task Scheduler ile her 1-2 dakikada bir calistir. Her
       calisma bagimsiz oldugu icin bir tanesi kacsa sonraki devam eder:
         */1 * * * * /usr/bin/python3 /opt/mac_tracker/mac_tracker.py --once --db /var/lib/mac_tracker.db
       Yine de tek process isteniyorsa:
         python mac_tracker.py --loop --interval 60

    4) Cihaz nerede / en son ne zaman aktifti:
         python mac_tracker.py --lookup AA:BB:CC:DD:EE:FF
         python mac_tracker.py --lookup aabb.ccdd.eeff      (Cisco formati da olur)

    5) Cihazin gecmisi (hangi switch/portlarda dolasti):
         python mac_tracker.py --history AA:BB:CC:DD:EE:FF

    6) Bir portta ne var / bir switch'te neler var:
         python mac_tracker.py --port Gi1/0/5
         python mac_tracker.py --port Gi1/0/5 --switch Kat1-SW
         python mac_tracker.py --list-switch Kat1-SW

    7) Uzun suredir gorulmeyen (kopmus/kayip) cihazlar:
         python mac_tracker.py --stale 7

    8) Bakim:
         python mac_tracker.py --summary
         python mac_tracker.py --prune 365
         python mac_tracker.py --selftest      (ag/DB gerektirmez, mantik testi)
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

SCHEMA_VERSION = 2

# ---------------------------------------------------------------------------
# OID sabitleri
# ---------------------------------------------------------------------------
OID_DOT1D_BASEPORT_IFINDEX = "1.3.6.1.2.1.17.1.4.1.2"   # bridge-port -> ifIndex
OID_IFNAME = "1.3.6.1.2.1.31.1.1.1.1"                    # ifIndex -> "Gi1/0/5"
OID_IFDESCR = "1.3.6.1.2.1.2.2.1.2"                      # ifName bos donerse yedek
OID_DOT1Q_FDB_PORT = "1.3.6.1.2.1.17.7.1.2.2.1.2"        # Q-BRIDGE: vlan+mac -> bridge-port
OID_DOT1Q_FDB_STATUS = "1.3.6.1.2.1.17.7.1.2.2.1.3"      # Q-BRIDGE: vlan+mac -> status
OID_DOT1D_FDB_PORT = "1.3.6.1.2.1.17.4.3.1.2"            # BRIDGE: mac -> bridge-port
OID_DOT1D_FDB_STATUS = "1.3.6.1.2.1.17.4.3.1.3"          # BRIDGE: mac -> status

FDB_STATUS_LEARNED = 3   # other(1) invalid(2) learned(3) self(4) mgmt(5)

# snmpwalk'in deger yerine basabildigi hata metinleri -- int()'e sokulmamali
SNMP_ERROR_MARKERS = (
    "No Such Object",
    "No Such Instance",
    "No more variables",
    "End of MIB",
    "Timeout:",
    "No Response",
    "Wrong Type",
    "authorizationError",
)


class SnmpError(RuntimeError):
    """Bir switch'e SNMP ile ulasilamadi / cevap hatali."""


# ---------------------------------------------------------------------------
# 1) SAF FONKSIYONLAR -- ag ya da DB olmadan test edilebilir
# ---------------------------------------------------------------------------

def now_utc() -> str:
    """DB'ye yazilacak zaman damgasi. UTC ve siralanabilir: 2026-09-10T20:54:01Z"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(ts: str) -> datetime:
    """DB'den okunan zaman damgasini datetime'a cevirir (eski offset'li format da kabul)."""
    text = ts.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fmt_local(ts: str) -> str:
    """UTC damgasini ekranda yerel saatle gosterir."""
    try:
        return parse_ts(ts).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        return ts


def human_age(ts: str, ref: datetime | None = None) -> str:
    """'3 gun 4 saat once' gibi okunabilir yas metni."""
    try:
        then = parse_ts(ts)
    except ValueError:
        return "?"
    now = ref or datetime.now(timezone.utc)
    secs = int((now - then).total_seconds())
    if secs < 0:
        return "gelecekte (?)"
    if secs < 60:
        return "az once"
    mins, secs = divmod(secs, 60)
    hours, mins = divmod(mins, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days} gun {hours} saat once"
    if hours:
        return f"{hours} saat {mins} dakika once"
    return f"{mins} dakika once"


def normalize_mac(mac: str) -> str:
    """
    Farkli formatlardaki MAC'i standart AA:BB:CC:DD:EE:FF haline getirir.
    Kabul: aabb.ccdd.eeff (Cisco), aa-bb-cc-dd-ee-ff, aabbccddeeff, aa:bb:...
    """
    cleaned = mac.strip().upper().replace("-", ":").replace(".", "").replace(" ", "")
    if ":" not in cleaned and len(cleaned) == 12:
        cleaned = ":".join(cleaned[i:i + 2] for i in range(0, 12, 2))
    parts = cleaned.split(":")
    if len(parts) != 6:
        raise ValueError(f"Gecersiz MAC formati: {mac!r}")
    out = []
    for part in parts:
        if len(part) not in (1, 2) or any(c not in "0123456789ABCDEF" for c in part):
            raise ValueError(f"Gecersiz MAC formati: {mac!r}")
        out.append(part.rjust(2, "0"))
    return ":".join(out)


def mac_from_oid_suffix(oid: str, with_vlan: bool = False) -> tuple[int | None, str]:
    """
    FDB tablolarinin OID index'inden VLAN ve MAC cikarir.

    dot1dTpFdbPort  (with_vlan=False) -> son 6 alt-tanimlayici MAC'tir:
        .1.3.6.1.2.1.17.4.3.1.2.0.26.203.10.20.30      -> (None, '00:1A:CB:0A:14:1E')
    dot1qTpFdbPort  (with_vlan=True)  -> son 7: VLAN + 6 MAC byte'i:
        .1.3.6.1.2.1.17.7.1.2.2.1.2.10.0.26.203.10.20.30 -> (10, '00:1A:CB:0A:14:1E')
    """
    need = 7 if with_vlan else 6
    parts = oid.strip().lstrip(".").split(".")
    if len(parts) < need:
        raise ValueError(f"OID'de yeterli alt-tanimlayici yok: {oid!r}")
    tail = parts[-need:]
    try:
        numbers = [int(p) for p in tail]
    except ValueError as exc:
        raise ValueError(f"OID suffix'i sayisal degil: {oid!r}") from exc
    vlan = numbers[0] if with_vlan else None
    mac_bytes = numbers[1:] if with_vlan else numbers
    if any(b < 0 or b > 255 for b in mac_bytes):
        raise ValueError(f"OID suffix'i gecerli byte degerleri degil: {oid!r}")
    if vlan is not None and not 0 <= vlan <= 4095:
        raise ValueError(f"OID'deki VLAN degeri gecersiz: {oid!r}")
    return vlan, ":".join(f"{b:02X}" for b in mac_bytes)


def looks_like_snmp_error(value: str) -> bool:
    return any(marker.lower() in value.lower() for marker in SNMP_ERROR_MARKERS)


def safe_int(value: str) -> int | None:
    """SNMP degerini int'e cevirir; cevrilemiyorsa None (patlamaz)."""
    token = value.strip().strip('"').split()[0] if value.strip() else ""
    try:
        return int(token)
    except ValueError:
        return None


def find_uplink_ports(observations: list[Observation], threshold: int) -> set[str]:
    """
    Tek bir portta threshold'dan fazla MAC varsa o port uplink/trunk kabul edilir.
    (Access portunda normalde 1-3 MAC olur: PC + telefon + belki bir VM.)
    threshold <= 0 ise filtreleme kapalidir.
    """
    if threshold <= 0:
        return set()
    per_port: Counter[str] = Counter()
    for obs in observations:
        per_port[obs.port] += 1
    return {port for port, count in per_port.items() if count > threshold}


def count_macs_per_port(observations: list[Observation]) -> dict[str, int]:
    per_port: Counter[str] = Counter()
    for obs in observations:
        per_port[obs.port] += 1
    return dict(per_port)


# ---------------------------------------------------------------------------
# 2) ENVANTER
# ---------------------------------------------------------------------------

@dataclass
class SwitchEntry:
    switch: str                                  # IP ya da hostname
    community: str = ""                          # v2c icin
    vlans: list[int] = field(default_factory=list)
    label: str = ""                               # raporlarda gorunen ad
    version: str = "2c"                           # "2c" | "3"
    ssh_user: str = ""                            # SSH toplayicisi icin (opsiyonel)
    ssh_pass: str = ""                            # tercihen envantere degil ortama koy

    def __post_init__(self):
        if not self.label:
            self.label = self.switch


@dataclass
class Observation:
    """Bir poll sirasinda gorulen tek bir MAC kaydi."""
    mac: str
    vlan: int          # bilinmiyorsa 0
    port: str          # "Gi1/0/5" ya da cozulemezse "bridgeport12"


@dataclass
class SwitchResult:
    entry: SwitchEntry
    observations: list[Observation] = field(default_factory=list)
    uplink_ports: set[str] = field(default_factory=set)
    port_macs: dict[str, int] = field(default_factory=dict)
    mode_used: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def parse_inventory_csv(path: str) -> list[SwitchEntry]:
    """
    inventory.csv formati (baslik satiri zorunlu):
        switch,community,vlans[,label][,version]
        10.1.1.1,public,,Kat1-SW
        10.1.2.1,public,1;10;20,Kat2-SW,2c
    'vlans' ';' ile ayrilmis VLAN listesi; dot1q modunda bos birakilabilir.
    '#' ile baslayan satirlar ve bos satirlar atlanir.
    """
    entries: list[SwitchEntry] = []
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"{path}: dosya bos ya da baslik satiri yok.")
        headers = {(h or "").strip().lower() for h in reader.fieldnames}
        if "switch" not in headers:
            raise ValueError(
                f"{path}: 'switch' kolonu yok. Beklenen baslik: "
                f"switch,community,vlans[,label][,version] -- bulunan: {reader.fieldnames}"
            )
        for lineno, row in enumerate(reader, start=2):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            host = row.get("switch", "")
            if not host or host.startswith("#"):
                continue
            vlans: list[int] = []
            for token in row.get("vlans", "").replace(",", ";").split(";"):
                token = token.strip()
                if not token:
                    continue
                try:
                    vlan = int(token)
                except ValueError as exc:
                    raise ValueError(
                        f"{path}:{lineno}: '{token}' gecerli bir VLAN ID degil."
                    ) from exc
                if not 1 <= vlan <= 4094:
                    raise ValueError(f"{path}:{lineno}: VLAN {vlan} 1-4094 araliginda degil.")
                vlans.append(vlan)
            version = (row.get("version") or "2c").lower().replace("v", "") or "2c"
            if version not in ("2c", "3"):
                raise ValueError(f"{path}:{lineno}: desteklenmeyen SNMP surumu: {version!r} (2c ya da 3)")
            entries.append(
                SwitchEntry(
                    switch=host,
                    community=row.get("community", ""),
                    vlans=vlans,
                    label=row.get("label", ""),
                    version=version,
                    ssh_user=row.get("ssh_user", ""),
                    ssh_pass=row.get("ssh_pass", ""),
                )
            )
    if not entries:
        raise ValueError(f"{path}: icinde kullanilabilir switch satiri bulunamadi.")
    return entries


# ---------------------------------------------------------------------------
# 3) VERITABANI KATMANI -- SNMP gerekmez
# ---------------------------------------------------------------------------

SCHEMA = """
-- Bir MAC'in bir switch/port/VLAN uzerindeki varligi. Her poll'da YENI SATIR
-- ACILMAZ; ayni yerde tekrar gorulurse last_seen guncellenir.
CREATE TABLE IF NOT EXISTS mac_locations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mac         TEXT    NOT NULL,
    switch      TEXT    NOT NULL,          -- envanterdeki label
    port        TEXT    NOT NULL,          -- 'Gi1/0/5'
    vlan        INTEGER NOT NULL DEFAULT 0,-- 0 = bilinmiyor
    switch_ip   TEXT    NOT NULL DEFAULT '',
    first_seen  TEXT    NOT NULL,
    last_seen   TEXT    NOT NULL,
    seen_count  INTEGER NOT NULL DEFAULT 1,
    port_macs   INTEGER NOT NULL DEFAULT 1 -- son poll'da bu portta kac MAC vardi
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_mac_location
    ON mac_locations(mac, switch, port, vlan);
CREATE INDEX IF NOT EXISTS idx_loc_mac_lastseen
    ON mac_locations(mac, last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_loc_switch_port
    ON mac_locations(switch, port);
CREATE INDEX IF NOT EXISTS idx_loc_lastseen
    ON mac_locations(last_seen);

-- Cihaz port degistirdiginde bir satir. 'Gecmis' budur; her poll degil.
CREATE TABLE IF NOT EXISTS mac_moves (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mac         TEXT NOT NULL,
    timestamp   TEXT NOT NULL,
    from_switch TEXT,
    from_port   TEXT,
    from_vlan   INTEGER,
    to_switch   TEXT NOT NULL,
    to_port     TEXT NOT NULL,
    to_vlan     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_moves_mac ON mac_moves(mac, timestamp);

-- Her switch icin her poll'un sonucu. "Cihaz mi koptu, switch'e mi
-- ulasamadik" ayrimini yapabilmek icin sart.
CREATE TABLE IF NOT EXISTS poll_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    switch      TEXT    NOT NULL,
    switch_ip   TEXT    NOT NULL DEFAULT '',
    started_at  TEXT    NOT NULL,
    finished_at TEXT    NOT NULL,
    ok          INTEGER NOT NULL,
    mode_used   TEXT    NOT NULL DEFAULT '',
    observed    INTEGER NOT NULL DEFAULT 0,
    recorded    INTEGER NOT NULL DEFAULT 0,
    uplinks     INTEGER NOT NULL DEFAULT 0,
    error       TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_runs_switch ON poll_runs(switch, started_at DESC);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def init_db(db_path: str, must_exist: bool = False) -> sqlite3.Connection:
    """
    Veritabani dosyasini (yoksa olusturarak) acar, tablolari kurar.
    must_exist=True iken dosya yoksa hata verir -- yanlis --db yolu yazip
    "kayit bulunamadi" cevabi almayi engeller.
    """
    if must_exist and db_path != ":memory:" and not os.path.exists(db_path):
        raise SystemExit(
            f"Veritabani dosyasi bulunamadi: {db_path}\n"
            "  --db yolunu kontrol et; once '--once' ile veri toplanmis olmali."
        )
    if sqlite3.sqlite_version_info < (3, 24, 0):
        raise SystemExit(
            f"SQLite 3.24+ gerekiyor (UPSERT icin), mevcut: {sqlite3.sqlite_version}"
        )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


def current_location(conn: sqlite3.Connection, mac: str) -> sqlite3.Row | None:
    """
    MAC'in 'su anki' (ya da en son bilinen) yeri. En yeni last_seen kazanir;
    esitlikte portunda daha az MAC olan kazanir -- access portu trunk'a yeniler.
    """
    cur = conn.execute(
        "SELECT * FROM mac_locations WHERE mac = ? "
        "ORDER BY last_seen DESC, port_macs ASC LIMIT 1",
        (mac,),
    )
    return cur.fetchone()


def upsert_location(
    conn: sqlite3.Connection,
    mac: str,
    switch: str,
    switch_ip: str,
    port: str,
    vlan: int,
    timestamp: str,
    port_macs: int,
) -> None:
    conn.execute(
        """
        INSERT INTO mac_locations
            (mac, switch, port, vlan, switch_ip, first_seen, last_seen, seen_count, port_macs)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
        ON CONFLICT(mac, switch, port, vlan) DO UPDATE SET
            last_seen  = excluded.last_seen,
            switch_ip  = excluded.switch_ip,
            seen_count = seen_count + 1,
            port_macs  = excluded.port_macs
        """,
        (mac, switch, port, vlan, switch_ip, timestamp, timestamp, port_macs),
    )


def record_move(conn: sqlite3.Connection, mac: str, timestamp: str,
                prev: sqlite3.Row | None, switch: str, port: str, vlan: int) -> None:
    conn.execute(
        "INSERT INTO mac_moves (mac, timestamp, from_switch, from_port, from_vlan, "
        "to_switch, to_port, to_vlan) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            mac, timestamp,
            prev["switch"] if prev else None,
            prev["port"] if prev else None,
            prev["vlan"] if prev else None,
            switch, port, vlan,
        ),
    )


def kept_observations(result: SwitchResult, keep_uplinks: bool) -> list[Observation]:
    """Uplink/trunk portlarindaki kayitlari (istenmiyorsa) ayiklar."""
    if keep_uplinks:
        return list(result.observations)
    return [o for o in result.observations if o.port not in result.uplink_ports]


def best_location_per_mac(results: list[SwitchResult],
                          keep_uplinks: bool) -> dict[str, tuple[int, str, str, int]]:
    """
    Bir poll'da ayni MAC birden fazla switch'te gorulur: kendi access portunda
    VE aradaki switch'lerin trunk portlarinda. Cihazin GERCEK yeri, uzerinde en
    az MAC bulunan porttur (access portu her zaman trunk'i yener).

    Doner: mac -> (port_macs, switch, port, vlan)
    """
    best: dict[str, tuple[int, str, str, int]] = {}
    for result in results:
        for obs in kept_observations(result, keep_uplinks):
            candidate = (result.port_macs.get(obs.port, 1), result.entry.label, obs.port, obs.vlan)
            current = best.get(obs.mac)
            if current is None or candidate[0] < current[0]:
                best[obs.mac] = candidate
    return best


def apply_poll_results(conn: sqlite3.Connection, results: list[SwitchResult],
                       timestamp: str, keep_uplinks: bool) -> dict[str, tuple[int, int]]:
    """
    Bir poll'un TUM switch sonuclarini birlikte DB'ye yazar.

    Hareket (port degisikligi) tespiti neden burada, switch bazinda degil:
    ayni MAC ayni poll'da birden fazla switch'te gorunur. Her gozlemi ayri ayri
    "onceki konumla" kiyaslarsan cihaz hic yer degistirmese bile her poll'da
    sahte hareket kaydi uretirsin. Dogrusu: once poll'un tamamindan MAC basina
    en iyi konumu sec, hareketi yalnizca o konum degistiyse yaz.

    Doner: switch label -> (kaydedilen, atlanan_uplink)
    """
    ok_results = [r for r in results if r.ok]
    stats: dict[str, tuple[int, int]] = {}

    # 1) Bu poll'da gorulen MAC'lerin ONCEKI konumlari (upsert'ten once okunmali)
    macs: set[str] = set()
    for result in ok_results:
        macs.update(o.mac for o in kept_observations(result, keep_uplinks))
    previous = {mac: current_location(conn, mac) for mac in macs}

    # 2) Gozlemleri yaz
    for result in ok_results:
        kept = kept_observations(result, keep_uplinks)
        for obs in kept:
            upsert_location(
                conn,
                mac=obs.mac,
                switch=result.entry.label,
                switch_ip=result.entry.switch,
                port=obs.port,
                vlan=obs.vlan,
                timestamp=timestamp,
                port_macs=result.port_macs.get(obs.port, 1),
            )
        stats[result.entry.label] = (len(kept), len(result.observations) - len(kept))
    conn.commit()

    # 3) Hareketleri poll'un tamamina bakarak yaz
    for mac, (_, switch, port, vlan) in best_location_per_mac(ok_results, keep_uplinks).items():
        prev = previous.get(mac)
        if prev is not None and (prev["switch"], prev["port"]) != (switch, port):
            record_move(conn, mac, timestamp, prev, switch, port, vlan)
    conn.commit()
    return stats


def apply_switch_result(conn: sqlite3.Connection, result: SwitchResult,
                        timestamp: str, keep_uplinks: bool) -> tuple[int, int]:
    """Tek switch'lik kisa yol (apply_poll_results uzerinden)."""
    stats = apply_poll_results(conn, [result], timestamp, keep_uplinks)
    return stats.get(result.entry.label, (0, 0))


def record_poll_run(conn: sqlite3.Connection, result: SwitchResult, started_at: str,
                    recorded: int, uplinks: int) -> None:
    conn.execute(
        "INSERT INTO poll_runs (switch, switch_ip, started_at, finished_at, ok, mode_used, "
        "observed, recorded, uplinks, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            result.entry.label, result.entry.switch, started_at, now_utc(),
            1 if result.ok else 0, result.mode_used,
            len(result.observations), recorded, uplinks, result.error[:500],
        ),
    )
    conn.commit()


def last_successful_poll(conn: sqlite3.Connection, switch: str) -> str | None:
    cur = conn.execute(
        "SELECT started_at FROM poll_runs WHERE switch = ? AND ok = 1 "
        "ORDER BY started_at DESC LIMIT 1",
        (switch,),
    )
    row = cur.fetchone()
    return row["started_at"] if row else None


# ---------------------------------------------------------------------------
# 4) SNMP KATMANI
# ---------------------------------------------------------------------------

@dataclass
class SnmpOptions:
    """Tum switch'ler icin gecerli SNMP ayarlari."""
    timeout: int = 5             # snmpwalk per-request timeout (-t)
    retries: int = 1             # -r
    walk_timeout: int = 120      # tum walk icin process timeout (saniye)
    v3_user: str = ""
    v3_level: str = "authPriv"   # noAuthNoPriv | authNoPriv | authPriv
    v3_auth_proto: str = "SHA"
    v3_auth_pass: str = ""
    v3_priv_proto: str = "AES"
    v3_priv_pass: str = ""
    walk_binary: str = ""        # bos = otomatik sec


def pick_walk_binary() -> str:
    """snmpbulkwalk varsa onu kullan (v2c/v3'te cok daha hizli), yoksa snmpwalk."""
    for candidate in ("snmpbulkwalk", "snmpwalk"):
        if shutil.which(candidate):
            return candidate
    raise SystemExit(
        "snmpwalk/snmpbulkwalk bulunamadi. Kurulum:\n"
        "  Linux   : sudo apt install snmp\n"
        "  Windows : https://www.net-snmp.org/ ya da 'choco install net-snmp'"
    )


def build_auth_args(entry: SwitchEntry, opts: SnmpOptions, vlan: int | None = None) -> list[str]:
    """
    SNMP kimlik argumanlarini uretir.
    vlan verilirse Cisco'nun VLAN basina FDB okuma yontemi uygulanir:
      v2c -> community@vlan     v3 -> -n vlan-<id> (context)
    """
    if entry.version == "3":
        if not opts.v3_user:
            raise SnmpError("SNMPv3 icin --v3-user vermelisin.")
        args = ["-v3", "-l", opts.v3_level, "-u", opts.v3_user]
        if opts.v3_level in ("authNoPriv", "authPriv"):
            args += ["-a", opts.v3_auth_proto, "-A", opts.v3_auth_pass]
        if opts.v3_level == "authPriv":
            args += ["-x", opts.v3_priv_proto, "-X", opts.v3_priv_pass]
        if vlan is not None:
            args += ["-n", f"vlan-{vlan}"]
        return args
    if not entry.community:
        raise SnmpError("v2c icin envanterde 'community' kolonu bos olmamali.")
    community = f"{entry.community}@{vlan}" if vlan is not None else entry.community
    return ["-v2c", "-c", community]


def snmp_walk(entry: SwitchEntry, opts: SnmpOptions, oid: str,
              vlan: int | None = None) -> list[tuple[str, str]]:
    """
    Walk yapar, (oid, value) listesi doner. Ulasilamazsa SnmpError firlatir --
    boylece cagiran taraf 'switch hatali' diye kaydeder, sessizce bos veri
    yazmaz. Hata metni iceren satirlar ayiklanir.
    """
    binary = opts.walk_binary or pick_walk_binary()
    cmd = [binary]
    cmd += build_auth_args(entry, opts, vlan)
    cmd += ["-Onq", "-t", str(opts.timeout), "-r", str(opts.retries)]
    if binary == "snmpbulkwalk":
        cmd += ["-Cr", "25"]
    cmd += [entry.switch, oid]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=opts.walk_timeout)
    except subprocess.TimeoutExpired as exc:
        raise SnmpError(
            f"walk {opts.walk_timeout} saniyede bitmedi (oid={oid}); "
            "--walk-timeout degerini artir ya da switch'i kontrol et"
        ) from exc
    except OSError as exc:
        raise SnmpError(f"{binary} calistirilamadi: {exc}") from exc

    stdout = proc.stdout or ""
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0 and not stdout.strip():
        raise SnmpError(stderr or f"{binary} cikis kodu {proc.returncode}")
    if not stdout.strip() and stderr and looks_like_snmp_error(stderr):
        raise SnmpError(stderr)

    pairs: list[tuple[str, str]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or looks_like_snmp_error(line):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        oid_part, value_part = parts
        pairs.append((oid_part, value_part.strip().strip('"')))
    return pairs


def get_bridgeport_to_ifindex(entry: SwitchEntry, opts: SnmpOptions,
                              vlan: int | None = None) -> dict[int, int]:
    """
    dot1dBasePortIfIndex: bridge-port -> ifIndex.

    DIKKAT: Cisco'da bu tablo da VLAN context'ine baglidir -- varsayilan
    community sana yalnizca VLAN 1'deki portlari verir ve bridge-port
    numaralari VLAN'dan VLAN'a farkli olabilir. Bu yuzden dot1d modunda
    harita, FDB ile AYNI VLAN context'i icinde okunmalidir.
    """
    mapping: dict[int, int] = {}
    for oid, value in snmp_walk(entry, opts, OID_DOT1D_BASEPORT_IFINDEX, vlan=vlan):
        bridgeport = safe_int(oid.split(".")[-1])
        ifindex = safe_int(value)
        if bridgeport is None or ifindex is None:
            continue
        mapping[bridgeport] = ifindex
    return mapping


def get_ifname_map(entry: SwitchEntry, opts: SnmpOptions) -> dict[int, str]:
    """ifIndex -> port adi. ifName bos donen cihazlarda ifDescr'a duser."""
    mapping: dict[int, str] = {}
    for oid_base in (OID_IFNAME, OID_IFDESCR):
        try:
            pairs = snmp_walk(entry, opts, oid_base)
        except SnmpError:
            pairs = []
        for oid, value in pairs:
            ifindex = safe_int(oid.split(".")[-1])
            if ifindex is None or not value:
                continue
            mapping.setdefault(ifindex, value)
        if mapping:
            break
    return mapping


def _status_map(entry: SwitchEntry, opts: SnmpOptions, oid: str, with_vlan: bool,
                vlan: int | None = None) -> dict[tuple[int | None, str], int]:
    """FDB status tablosu: (vlan, mac) -> status. Okunamazsa bos doner."""
    statuses: dict[tuple[int | None, str], int] = {}
    try:
        pairs = snmp_walk(entry, opts, oid, vlan=vlan)
    except SnmpError:
        return statuses
    for raw_oid, value in pairs:
        try:
            parsed_vlan, mac = mac_from_oid_suffix(raw_oid, with_vlan=with_vlan)
        except ValueError:
            continue
        status = safe_int(value)
        if status is None:
            continue
        statuses[(parsed_vlan if with_vlan else vlan, mac)] = status
    return statuses


def resolve_port_name(bridgeport: int, portmap: dict[int, int],
                      ifnames: dict[int, str]) -> str:
    """bridge-port -> ifIndex -> 'Gi1/0/5'. Cozulemezse tanimlayici bir yedek ad."""
    ifindex = portmap.get(bridgeport)
    if ifindex is None:
        return f"bridgeport{bridgeport}"
    return ifnames.get(ifindex, f"ifIndex{ifindex}")


def fdb_dot1q(entry: SwitchEntry, opts: SnmpOptions, filter_learned: bool,
              portmap: dict[int, int], ifnames: dict[int, str]) -> list[Observation]:
    """
    Standart Q-BRIDGE okuma: tek walk, tum VLAN'lar (VLAN, OID index'inde).
    """
    statuses = _status_map(entry, opts, OID_DOT1Q_FDB_STATUS, with_vlan=True) if filter_learned else {}
    observations: list[Observation] = []
    for oid, value in snmp_walk(entry, opts, OID_DOT1Q_FDB_PORT):
        try:
            vlan, mac = mac_from_oid_suffix(oid, with_vlan=True)
        except ValueError:
            continue
        bridgeport = safe_int(value)
        if bridgeport is None or bridgeport <= 0:
            continue
        if entry.vlans and vlan not in entry.vlans:
            continue
        if statuses and statuses.get((vlan, mac), FDB_STATUS_LEARNED) != FDB_STATUS_LEARNED:
            continue
        observations.append(
            Observation(mac=mac, vlan=vlan or 0,
                        port=resolve_port_name(bridgeport, portmap, ifnames))
        )
    return observations


def fdb_dot1d(entry: SwitchEntry, opts: SnmpOptions, filter_learned: bool,
              ifnames: dict[int, str], verbose: bool = False) -> list[Observation]:
    """
    Klasik BRIDGE MIB okuma. VLAN bilgisi tasimadigi icin VLAN basina ayri
    sorgu gerekir (Cisco: v2c'de community@vlan, v3'te vlan-<id> context).

    Bridge-port haritasi da her VLAN icin AYRI okunur: Cisco'da bu tablo
    VLAN context'ine bagli ve numaralar VLAN'dan VLAN'a degisebiliyor.
    Envanterde 'vlans' bos ise VLAN'siz tek okuma yapilir (vlan=0).
    """
    observations: list[Observation] = []
    vlan_list: list[int | None] = list(entry.vlans) if entry.vlans else [None]
    for vlan in vlan_list:
        try:
            portmap = get_bridgeport_to_ifindex(entry, opts, vlan=vlan)
        except SnmpError as exc:
            print(f"  [!] {entry.label} vlan={vlan}: bridge-port haritasi okunamadi: {exc}",
                  file=sys.stderr)
            portmap = {}
        statuses = (
            _status_map(entry, opts, OID_DOT1D_FDB_STATUS, with_vlan=False, vlan=vlan)
            if filter_learned else {}
        )
        try:
            pairs = snmp_walk(entry, opts, OID_DOT1D_FDB_PORT, vlan=vlan)
        except SnmpError as exc:
            # Bir VLAN okunamazsa digerlerini iptal etmeyelim.
            print(f"  [!] {entry.label} vlan={vlan}: {exc}", file=sys.stderr)
            continue
        if verbose:
            print(f"  [{entry.label}] vlan={vlan}: {len(pairs)} FDB kaydi, "
                  f"{len(portmap)} bridge-port")
        for oid, value in pairs:
            try:
                _, mac = mac_from_oid_suffix(oid, with_vlan=False)
            except ValueError:
                continue
            bridgeport = safe_int(value)
            if bridgeport is None or bridgeport <= 0:
                continue
            if statuses and statuses.get((vlan, mac), FDB_STATUS_LEARNED) != FDB_STATUS_LEARNED:
                continue
            observations.append(
                Observation(mac=mac, vlan=vlan or 0,
                            port=resolve_port_name(bridgeport, portmap, ifnames))
            )
    return observations


def collect_switch(entry: SwitchEntry, opts: SnmpOptions, mode: str,
                   uplink_threshold: int, filter_learned: bool,
                   verbose: bool = False) -> SwitchResult:
    """
    Bir switch'i SNMP ile okur ve SwitchResult doner. DB'ye DOKUNMAZ -- bu
    sayede birden fazla switch paralel okunabilir, DB yazimi tek thread'de
    kalir. Hata firlatmaz; hatayi result.error icine koyar.
    """
    result = SwitchResult(entry=entry)
    try:
        portmap = get_bridgeport_to_ifindex(entry, opts)
        ifnames = get_ifname_map(entry, opts)
        if verbose:
            print(f"  [{entry.label}] {len(portmap)} bridge-port, {len(ifnames)} arayuz adi")

        observations: list[Observation] = []
        if mode in ("dot1q", "auto"):
            observations = fdb_dot1q(entry, opts, filter_learned, portmap, ifnames)
            result.mode_used = "dot1q"
        if not observations and mode in ("dot1d", "auto"):
            observations = fdb_dot1d(entry, opts, filter_learned, ifnames, verbose)
            result.mode_used = "dot1d"
        if not observations and not portmap and not ifnames:
            raise SnmpError(
                "switch'ten hicbir tablo okunamadi "
                "(SNMP view/community, MIB destegi ya da VLAN listesini kontrol et)"
            )

        result.observations = observations
        result.port_macs = count_macs_per_port(observations)
        result.uplink_ports = find_uplink_ports(observations, uplink_threshold)
    except SnmpError as exc:
        result.error = str(exc)
    except Exception as exc:  # tek switch butun poll'u dusurmesin
        result.error = f"{type(exc).__name__}: {exc}"
    return result


# ---------------------------------------------------------------------------
# 4b) SSH KATMANI -- Cisco IOS 'show mac address-table' (SNMP alternatifi)
# ---------------------------------------------------------------------------
#
# Neden var: bazi platformlarda (ornegin EVE-NG/GNS3'teki IOL/IOU imajlari)
# Q-BRIDGE MIB yok, Cisco'nun 'community@vlan' indexlemesi yok ve VLAN
# context'leri de calismiyor. Bu durumda SNMP ile VLAN basina FDB okumak
# mumkun olmuyor. 'show mac address-table' ise VLAN + MAC + port bilgisini
# tek komutta, hicbir MIB destegine ihtiyac duymadan veriyor.

# Cisco'nun uc formatini da tolere eder:
#   IOS      :   10    aabb.cc02.1010    DYNAMIC     Et0/1
#   IOS-XE   :   10    0050.7966.6826    DYNAMIC     Gi1/0/5
#   NX-OS    : * 10    aabb.cc02.1010   dynamic  0    F    F  Eth1/1
MAC_TABLE_LINE_RE = re.compile(
    r"^\s*[*+]?\s*"
    r"(?P<vlan>\d{1,4}|[Aa]ll|-)\s+"
    r"(?P<mac>[0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\.[0-9a-fA-F]{4}"
    r"|[0-9a-fA-F]{2}(?:[:-][0-9a-fA-F]{2}){5})\s+"
    r"(?P<rest>\S.*)$"
)

# Cihaz gercek bir port degil, dahili bir hedef gosterdiginde
NON_PORT_TOKENS = {"cpu", "router", "switch", "drop", "n/a", "-", "vl1"}


@dataclass
class SshOptions:
    """SSH toplayicisi icin ayarlar (tum switch'ler icin gecerli varsayilanlar)."""
    user: str = ""
    password: str = ""
    enable_password: str = ""
    port: int = 22
    timeout: int = 20
    command: str = "show mac address-table"


def parse_mac_address_table(text: str) -> list[Observation]:
    """
    'show mac address-table' ciktisini Observation listesine cevirir.
    Sadece DYNAMIC kayitlar alinir: statik/CPU kayitlari bir cihazin
    o portta oldugu anlamina gelmez.
    """
    observations: list[Observation] = []
    for line in text.splitlines():
        match = MAC_TABLE_LINE_RE.match(line)
        if not match:
            continue
        rest = match.group("rest").split()
        if not rest:
            continue
        entry_type = rest[0].lower()
        if "dynamic" not in entry_type:
            continue
        port = rest[-1]
        if port.lower() in NON_PORT_TOKENS:
            continue
        try:
            mac = normalize_mac(match.group("mac"))
        except ValueError:
            continue
        vlan_text = match.group("vlan")
        vlan = int(vlan_text) if vlan_text.isdigit() else 0
        observations.append(Observation(mac=mac, vlan=vlan, port=port))
    return observations


def _read_until_idle(channel, idle: float = 1.0, total: float = 20.0) -> str:
    """Kanaldan veri akisi durana kadar oku. IOS prompt'u cesitlilik gosterdigi
    icin prompt yakalamak yerine 'sessizlik' beklemek daha saglam."""
    buffer = []
    deadline = time.monotonic() + total
    last_data = time.monotonic()
    while time.monotonic() < deadline:
        if channel.recv_ready():
            chunk = channel.recv(65535).decode("utf-8", errors="replace")
            buffer.append(chunk)
            last_data = time.monotonic()
        else:
            if time.monotonic() - last_data > idle and buffer:
                break
            time.sleep(0.1)
    return "".join(buffer)


def ssh_fetch_mac_table(entry: SwitchEntry, opts: SshOptions, verbose: bool = False) -> str:
    """
    Switch'e SSH ile baglanip MAC adres tablosunu getirir. Ciktiyi ham metin
    olarak doner. Basarisizlikta SnmpError firlatir (ayni hata yolu kullanilsin
    diye; mesajda yontem belirtilir).
    """
    try:
        import paramiko
    except ImportError as exc:
        raise SnmpError(
            "SSH toplayicisi icin paramiko gerekli: pip3 install paramiko"
        ) from exc

    user = entry.ssh_user or opts.user
    password = entry.ssh_pass or opts.password
    if not user:
        raise SnmpError("SSH icin kullanici adi yok (--ssh-user ya da envanterde ssh_user)")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=entry.switch, port=opts.port, username=user, password=password,
            look_for_keys=False, allow_agent=False, timeout=opts.timeout,
        )
        channel = client.invoke_shell(width=200, height=1000)
        banner = _read_until_idle(channel, idle=0.8, total=opts.timeout)

        # Kullanici modundaysak enable'a gec
        if banner.rstrip().endswith(">") and opts.enable_password:
            channel.send("enable\n")
            _read_until_idle(channel, idle=0.5, total=opts.timeout)
            channel.send(opts.enable_password + "\n")
            _read_until_idle(channel, idle=0.8, total=opts.timeout)

        channel.send("terminal length 0\n")
        _read_until_idle(channel, idle=0.5, total=opts.timeout)

        channel.send(opts.command + "\n")
        output = _read_until_idle(channel, idle=1.2, total=opts.timeout * 3)

        # Eski IOS'larda komut 'show mac-address-table' (tireli) olabiliyor
        if "Invalid input" in output or "% Ambiguous" in output:
            alternate = ("show mac-address-table"
                         if "-" not in opts.command else "show mac address-table")
            if verbose:
                print(f"  [{entry.label}] komut kabul edilmedi, deneniyor: {alternate}")
            channel.send(alternate + "\n")
            output = _read_until_idle(channel, idle=1.2, total=opts.timeout * 3)

        channel.close()
        return output
    except SnmpError:
        raise
    except Exception as exc:
        raise SnmpError(f"SSH hatasi: {type(exc).__name__}: {exc}") from exc
    finally:
        client.close()


def collect_switch_ssh(entry: SwitchEntry, opts: SshOptions, uplink_threshold: int,
                       verbose: bool = False) -> SwitchResult:
    """SSH ile MAC tablosunu okur. collect_switch ile ayni SwitchResult'i doner."""
    result = SwitchResult(entry=entry)
    result.mode_used = "ssh"
    try:
        text = ssh_fetch_mac_table(entry, opts, verbose)
        observations = parse_mac_address_table(text)
        if entry.vlans:
            observations = [o for o in observations if o.vlan in entry.vlans]
        if not observations:
            snippet = " | ".join(line.strip() for line in text.splitlines()[-5:] if line.strip())
            raise SnmpError(f"MAC tablosu bos ya da anlasilamadi. Son satirlar: {snippet[:300]}")
        if verbose:
            print(f"  [{entry.label}] SSH: {len(observations)} dinamik kayit")
        result.observations = observations
        result.port_macs = count_macs_per_port(observations)
        result.uplink_ports = find_uplink_ports(observations, uplink_threshold)
    except SnmpError as exc:
        result.error = str(exc)
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    return result


# ---------------------------------------------------------------------------
# 5) POLL ORKESTRASYONU
# ---------------------------------------------------------------------------

def poll_once(conn: sqlite3.Connection, entries: list[SwitchEntry], opts: SnmpOptions,
              mode: str, uplink_threshold: int, keep_uplinks: bool,
              filter_learned: bool, workers: int, verbose: bool = False,
              collector: str = "snmp", ssh_opts: SshOptions | None = None) -> dict:
    """Tum switch'leri (paralel) okur, sonuclari DB'ye yazar, ozet doner."""
    started = now_utc()
    stats = {"switches": len(entries), "ok": 0, "failed": 0,
             "observed": 0, "recorded": 0, "uplink_skipped": 0}

    def work(entry: SwitchEntry) -> SwitchResult:
        if collector == "ssh":
            return collect_switch_ssh(entry, ssh_opts or SshOptions(), uplink_threshold, verbose)
        return collect_switch(entry, opts, mode, uplink_threshold, filter_learned, verbose)

    if workers > 1 and len(entries) > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(work, entries))
    else:
        results = [work(entry) for entry in entries]

    # Tum switch'ler tek seferde yazilir: hareket tespiti poll'un tamamini gormeli.
    per_switch = apply_poll_results(conn, results, started, keep_uplinks)

    for result in results:
        if not result.ok:
            stats["failed"] += 1
            print(f"[{result.entry.label}] HATA: {result.error}", file=sys.stderr)
            record_poll_run(conn, result, started, recorded=0, uplinks=0)
            continue
        recorded, skipped = per_switch.get(result.entry.label, (0, 0))
        record_poll_run(conn, result, started, recorded=recorded, uplinks=skipped)
        stats["ok"] += 1
        stats["observed"] += len(result.observations)
        stats["recorded"] += recorded
        stats["uplink_skipped"] += skipped
        uplink_note = ""
        if result.uplink_ports and not keep_uplinks:
            uplink_note = (f", {skipped} kayit uplink portunda atlandi "
                           f"({len(result.uplink_ports)} port)")
        print(f"[{result.entry.label}] {result.mode_used}: {recorded} MAC kaydedildi{uplink_note}")
    return stats


# ---------------------------------------------------------------------------
# 6) SORGULAR / RAPORLAR
# ---------------------------------------------------------------------------

def cmd_lookup(conn: sqlite3.Connection, mac_text: str, stale_minutes: int) -> None:
    """Cihaz su an hangi portta; koptuysa en son ne zaman aktifti."""
    mac = normalize_mac(mac_text)
    loc = current_location(conn, mac)
    if loc is None:
        print(f"{mac}: hic kayit yok (henuz goruldugu bir poll olmadi).")
        return

    last_ok = last_successful_poll(conn, loc["switch"])
    now = datetime.now(timezone.utc)
    if last_ok is None:
        status = "BILINMIYOR (bu switch hic basarili sorgulanmamis)"
    elif loc["last_seen"] >= last_ok:
        status = "AKTIF (son pollde bu portta goruldu)"
    else:
        switch_age = (now - parse_ts(last_ok)).total_seconds() / 60
        if switch_age > stale_minutes:
            status = (f"BILINMIYOR -- switch {human_age(last_ok, now)} beri "
                      f"sorgulanamiyor, cihaz hakkinda yorum yapilamaz")
        else:
            status = f"KOPMUS (bu portta son aktiflik: {human_age(loc['last_seen'], now)})"

    print(f"MAC          : {mac}")
    print(f"Durum        : {status}")
    print(f"Switch       : {loc['switch']}" + (f" ({loc['switch_ip']})" if loc["switch_ip"] else ""))
    print(f"Port         : {loc['port']}")
    print(f"VLAN         : {loc['vlan'] or '-'}")
    print(f"Ilk gorulme  : {fmt_local(loc['first_seen'])}")
    print(f"Son gorulme  : {fmt_local(loc['last_seen'])}  ({human_age(loc['last_seen'], now)})")
    print(f"Gorulme sayisi: {loc['seen_count']} poll")
    print(f"Portta MAC   : {loc['port_macs']}"
          + ("  (dikkat: cok MAC var, trunk olabilir)" if loc["port_macs"] > 5 else ""))

    others = conn.execute(
        "SELECT switch, port, vlan, last_seen FROM mac_locations WHERE mac = ? AND id != ? "
        "ORDER BY last_seen DESC LIMIT 5",
        (mac, loc["id"]),
    ).fetchall()
    if others:
        print("\nBu MAC ayrica su konumlarda da kayitli (eski yerler / trunk izleri):")
        for row in others:
            print(f"  {fmt_local(row['last_seen'])}  {row['switch']} {row['port']} vlan={row['vlan'] or '-'}")


def cmd_history(conn: sqlite3.Connection, mac_text: str) -> None:
    """Cihazin butun konumlari + port degistirme hareketleri."""
    mac = normalize_mac(mac_text)
    rows = conn.execute(
        "SELECT * FROM mac_locations WHERE mac = ? ORDER BY last_seen DESC", (mac,)
    ).fetchall()
    if not rows:
        print(f"{mac}: hic kayit yok.")
        return
    print(f"{mac} -- {len(rows)} konum kaydi:\n")
    print(f"{'SWITCH':<22} {'PORT':<16} {'VLAN':>5}  {'ILK GORULME':<26} {'SON GORULME':<26} {'POLL':>7}")
    print("-" * 112)
    for row in rows:
        print(f"{row['switch'][:22]:<22} {row['port'][:16]:<16} {row['vlan'] or 0:>5}  "
              f"{fmt_local(row['first_seen']):<26} {fmt_local(row['last_seen']):<26} {row['seen_count']:>7}")

    moves = conn.execute(
        "SELECT * FROM mac_moves WHERE mac = ? ORDER BY timestamp ASC", (mac,)
    ).fetchall()
    if moves:
        print(f"\nPort degisiklikleri ({len(moves)}):")
        for move in moves:
            src = (f"{move['from_switch']} {move['from_port']}"
                   if move["from_switch"] else "(ilk kayit)")
            print(f"  {fmt_local(move['timestamp'])}  {src}  ->  {move['to_switch']} {move['to_port']}")


def cmd_port(conn: sqlite3.Connection, port: str, switch: str | None) -> None:
    """Belirli bir portta gorulmus MAC'ler."""
    sql = "SELECT * FROM mac_locations WHERE port = ?"
    params: list = [port]
    if switch:
        sql += " AND switch = ?"
        params.append(switch)
    sql += " ORDER BY last_seen DESC"
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        print(f"Port '{port}'" + (f" / switch '{switch}'" if switch else "") + " icin kayit yok.")
        return
    now = datetime.now(timezone.utc)
    print(f"{len(rows)} kayit:\n")
    print(f"{'MAC':<19} {'SWITCH':<22} {'VLAN':>5}  {'SON GORULME':<26} YAS")
    print("-" * 96)
    for row in rows:
        print(f"{row['mac']:<19} {row['switch'][:22]:<22} {row['vlan'] or 0:>5}  "
              f"{fmt_local(row['last_seen']):<26} {human_age(row['last_seen'], now)}")


def cmd_list_switch(conn: sqlite3.Connection, switch: str) -> None:
    rows = conn.execute(
        "SELECT * FROM mac_locations WHERE switch = ? ORDER BY port, last_seen DESC", (switch,)
    ).fetchall()
    if not rows:
        print(f"'{switch}' icin kayit yok. (Envanterdeki 'label' degeriyle ara.)")
        return
    now = datetime.now(timezone.utc)
    print(f"{switch} -- {len(rows)} kayit:\n")
    print(f"{'PORT':<16} {'MAC':<19} {'VLAN':>5}  {'SON GORULME':<26} YAS")
    print("-" * 96)
    for row in rows:
        print(f"{row['port'][:16]:<16} {row['mac']:<19} {row['vlan'] or 0:>5}  "
              f"{fmt_local(row['last_seen']):<26} {human_age(row['last_seen'], now)}")


def cmd_stale(conn: sqlite3.Connection, days: int) -> None:
    """N gunden beri hic gorulmeyen cihazlar -- kayip/kopmus cihaz listesi."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        """
        SELECT mac, switch, port, vlan, MAX(last_seen) AS last_seen
        FROM mac_locations GROUP BY mac
        HAVING MAX(last_seen) < ? ORDER BY last_seen DESC
        """,
        (cutoff,),
    ).fetchall()
    if not rows:
        print(f"{days} gunden beri gorulmeyen cihaz yok.")
        return
    now = datetime.now(timezone.utc)
    print(f"{days} gunden beri gorulmeyen {len(rows)} cihaz "
          f"(en son bulundugu yer ile birlikte):\n")
    print(f"{'MAC':<19} {'SON GORULME':<26} {'YAS':<22} SON KONUM")
    print("-" * 104)
    for row in rows:
        print(f"{row['mac']:<19} {fmt_local(row['last_seen']):<26} "
              f"{human_age(row['last_seen'], now):<22} {row['switch']} {row['port']}")


def cmd_summary(conn: sqlite3.Connection) -> None:
    macs = conn.execute("SELECT COUNT(DISTINCT mac) AS n FROM mac_locations").fetchone()["n"]
    locs = conn.execute("SELECT COUNT(*) AS n FROM mac_locations").fetchone()["n"]
    moves = conn.execute("SELECT COUNT(*) AS n FROM mac_moves").fetchone()["n"]
    print(f"Farkli MAC      : {macs}")
    print(f"Konum kaydi     : {locs}")
    print(f"Port degisikligi: {moves}")
    rows = conn.execute(
        """
        SELECT switch, MAX(started_at) AS son, SUM(ok) AS basarili, COUNT(*) AS toplam
        FROM poll_runs GROUP BY switch ORDER BY switch
        """
    ).fetchall()
    if not rows:
        print("\nHenuz hic poll yapilmamis.")
        return
    now = datetime.now(timezone.utc)
    print(f"\n{'SWITCH':<22} {'SON POLL':<26} {'YAS':<22} BASARILI/TOPLAM")
    print("-" * 100)
    for row in rows:
        print(f"{row['switch'][:22]:<22} {fmt_local(row['son']):<26} "
              f"{human_age(row['son'], now):<22} {row['basarili']}/{row['toplam']}")
    failed = conn.execute(
        "SELECT switch, error, started_at FROM poll_runs WHERE ok = 0 "
        "ORDER BY started_at DESC LIMIT 5"
    ).fetchall()
    if failed:
        print("\nSon hatalar:")
        for row in failed:
            print(f"  {fmt_local(row['started_at'])}  {row['switch']}: {row['error']}")


def cmd_prune(conn: sqlite3.Connection, days: int) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    locs = conn.execute("DELETE FROM mac_locations WHERE last_seen < ?", (cutoff,)).rowcount
    moves = conn.execute("DELETE FROM mac_moves WHERE timestamp < ?", (cutoff,)).rowcount
    runs = conn.execute("DELETE FROM poll_runs WHERE started_at < ?", (cutoff,)).rowcount
    conn.commit()
    conn.execute("VACUUM")
    print(f"{days} gunden eski kayitlar silindi: {locs} konum, {moves} hareket, {runs} poll kaydi.")


# ---------------------------------------------------------------------------
# 7) CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Cihazlarin hangi switch portunda oldugunu takip eder (SNMP ya da SSH).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Ornek: mac_tracker.py --once -v   |   mac_tracker.py --lookup aabb.ccdd.eeff",
    )
    parser.add_argument("--inventory", default="inventory.csv", help="Switch envanter CSV dosyasi")
    parser.add_argument("--db", default="mac_tracker.db", help="SQLite veritabani dosyasi")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Tek seferlik poll yap ve cik (varsayilan)")
    mode.add_argument("--loop", action="store_true", help="Surekli calis, --interval'da bir poll et")
    mode.add_argument("--lookup", metavar="MAC", help="Cihaz hangi portta / koptuysa son ne zaman aktifti")
    mode.add_argument("--history", metavar="MAC", help="Cihazin tum konum gecmisi")
    mode.add_argument("--port", metavar="PORT", help="Bu portta gorulen MAC'leri listele")
    mode.add_argument("--list-switch", metavar="SWITCH", help="Bu switch'te gorulen tum MAC'ler")
    mode.add_argument("--stale", type=int, metavar="GUN", help="N gunden beri gorulmeyen cihazlar")
    mode.add_argument("--summary", action="store_true", help="DB ve poll durumu ozeti")
    mode.add_argument("--prune", type=int, metavar="GUN", help="N gunden eski kayitlari sil")
    mode.add_argument("--selftest", action="store_true", help="Ag/DB gerektirmeyen mantik testleri")

    parser.add_argument("--switch", metavar="SWITCH", help="--port ile birlikte switch filtresi")
    parser.add_argument("--interval", type=int, default=60, help="--loop poll araligi (saniye)")
    parser.add_argument("--collector", choices=["snmp", "ssh"], default="snmp",
                        help="Veri kaynagi: snmp (varsayilan) ya da ssh ('show mac address-table')")
    parser.add_argument("--mode", choices=["auto", "dot1q", "dot1d"], default="auto",
                        help="SNMP'de FDB okuma yontemi (varsayilan auto: once dot1q, sonra dot1d)")
    parser.add_argument("--uplink-threshold", type=int, default=10,
                        help="Bir portta bundan fazla MAC varsa uplink/trunk say (0 = kapali)")
    parser.add_argument("--keep-uplinks", action="store_true",
                        help="Uplink portlarindaki MAC'leri de kaydet")
    parser.add_argument("--no-status-filter", action="store_true",
                        help="dot1dTpFdbStatus=learned filtresini kapat (bir walk daha az)")
    parser.add_argument("--stale-minutes", type=int, default=15,
                        help="Switch bu sureden beri sorgulanamiyorsa cihaz durumu BILINMIYOR")
    parser.add_argument("--workers", type=int, default=8, help="Paralel sorgulanacak switch sayisi")
    parser.add_argument("-v", "--verbose", action="store_true", help="Ayrintili cikti")

    snmp = parser.add_argument_group("SNMP ayarlari")
    snmp.add_argument("--timeout", type=int, default=5, help="Istek basina SNMP timeout (-t)")
    snmp.add_argument("--retries", type=int, default=1, help="SNMP yeniden deneme (-r)")
    snmp.add_argument("--walk-timeout", type=int, default=120,
                      help="Tek bir walk icin toplam sure siniri (saniye)")
    snmp.add_argument("--v3-user", default=os.environ.get("SNMP_V3_USER", ""))
    snmp.add_argument("--v3-level", default="authPriv",
                      choices=["noAuthNoPriv", "authNoPriv", "authPriv"])
    snmp.add_argument("--v3-auth-proto", default="SHA")
    snmp.add_argument("--v3-auth-pass", default=os.environ.get("SNMP_V3_AUTH_PASS", ""),
                      help="Komut satiri yerine SNMP_V3_AUTH_PASS ortam degiskenini kullan")
    snmp.add_argument("--v3-priv-proto", default="AES")
    snmp.add_argument("--v3-priv-pass", default=os.environ.get("SNMP_V3_PRIV_PASS", ""),
                      help="Komut satiri yerine SNMP_V3_PRIV_PASS ortam degiskenini kullan")

    ssh = parser.add_argument_group("SSH ayarlari (--collector ssh)")
    ssh.add_argument("--ssh-user", default=os.environ.get("MACTRACK_SSH_USER", ""))
    ssh.add_argument("--ssh-pass", default=os.environ.get("MACTRACK_SSH_PASS", ""),
                     help="Komut satiri yerine MACTRACK_SSH_PASS ortam degiskenini kullan")
    ssh.add_argument("--ssh-enable-pass", default=os.environ.get("MACTRACK_SSH_ENABLE", ""),
                     help="Gerekiyorsa enable parolasi (MACTRACK_SSH_ENABLE)")
    ssh.add_argument("--ssh-port", type=int, default=22)
    ssh.add_argument("--ssh-timeout", type=int, default=20)
    ssh.add_argument("--ssh-command", default="show mac address-table",
                     help="Calistirilacak komut (eski IOS: 'show mac-address-table')")
    return parser


def ssh_options_from_args(args) -> SshOptions:
    return SshOptions(
        user=args.ssh_user,
        password=args.ssh_pass,
        enable_password=args.ssh_enable_pass,
        port=args.ssh_port,
        timeout=args.ssh_timeout,
        command=args.ssh_command,
    )


def snmp_options_from_args(args) -> SnmpOptions:
    return SnmpOptions(
        timeout=args.timeout,
        retries=args.retries,
        walk_timeout=args.walk_timeout,
        v3_user=args.v3_user,
        v3_level=args.v3_level,
        v3_auth_proto=args.v3_auth_proto,
        v3_auth_pass=args.v3_auth_pass,
        v3_priv_proto=args.v3_priv_proto,
        v3_priv_pass=args.v3_priv_pass,
    )


def load_inventory_or_exit(path: str) -> list[SwitchEntry]:
    """Envanteri okur; hatada traceback yerine anlasilir bir mesaj verir."""
    try:
        return parse_inventory_csv(path)
    except FileNotFoundError:
        raise SystemExit(
            f"Envanter dosyasi bulunamadi: {path}\n"
            "  Ornek icerik:\n"
            "    switch,community,vlans,label\n"
            "    10.1.1.1,public,,Kat1-SW"
        )
    except (ValueError, OSError, csv.Error) as exc:
        raise SystemExit(f"Envanter okunamadi: {exc}")


def run_poll_session(args, loop: bool) -> None:
    entries = load_inventory_or_exit(args.inventory)
    opts = snmp_options_from_args(args)
    ssh_opts = ssh_options_from_args(args)
    if args.collector == "snmp":
        opts.walk_binary = pick_walk_binary()
        how = f"snmp/{args.mode} ({opts.walk_binary})"
    else:
        how = f"ssh ({ssh_opts.command})"
    conn = init_db(args.db)
    print(f"{len(entries)} switch, yontem={how}, db={args.db}")
    try:
        while True:
            start = time.monotonic()
            stats = poll_once(
                conn, entries, opts, args.mode, args.uplink_threshold,
                args.keep_uplinks, not args.no_status_filter, args.workers, args.verbose,
                collector=args.collector, ssh_opts=ssh_opts,
            )
            print(
                f"Poll bitti: {stats['ok']}/{stats['switches']} switch ok, "
                f"{stats['recorded']} MAC kaydi, {stats['uplink_skipped']} uplink kaydi atlandi"
                + (f", {stats['failed']} switch HATALI" if stats["failed"] else "")
            )
            if not loop:
                return
            # Poll suresini dusurerek bekle -- boylece aralik kaymaz.
            sleep_for = max(1.0, args.interval - (time.monotonic() - start))
            time.sleep(sleep_for)
    except KeyboardInterrupt:
        print("\nDurduruldu.")
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.selftest:
        return run_selftest()

    # Sadece okuyan komutlar: DB dosyasi yoksa uyar, bos DB olusturup
    # "kayit yok" deme.
    read_only_commands = {
        "lookup": args.lookup, "history": args.history, "port": args.port,
        "list_switch": args.list_switch,
    }
    if any(read_only_commands.values()) or args.summary or args.stale is not None or args.prune is not None:
        conn = init_db(args.db, must_exist=True)
        try:
            if args.lookup:
                cmd_lookup(conn, args.lookup, args.stale_minutes)
            elif args.history:
                cmd_history(conn, args.history)
            elif args.port:
                cmd_port(conn, args.port, args.switch)
            elif args.list_switch:
                cmd_list_switch(conn, args.list_switch)
            elif args.stale is not None:
                cmd_stale(conn, args.stale)
            elif args.prune is not None:
                cmd_prune(conn, args.prune)
            else:
                cmd_summary(conn)
        except ValueError as exc:           # hatali MAC formati vb.
            print(f"Hata: {exc}", file=sys.stderr)
            return 2
        finally:
            conn.close()
        return 0

    run_poll_session(args, loop=args.loop)
    return 0


# ---------------------------------------------------------------------------
# 8) SELFTEST -- ag ve gercek switch gerektirmez
# ---------------------------------------------------------------------------

def run_selftest() -> int:
    import unittest
    loader = unittest.TestLoader()
    try:
        from tests import test_mac_tracker  # repo icindeki test dosyasi
        suite = loader.loadTestsFromModule(test_mac_tracker)
    except ImportError:
        print("tests/test_mac_tracker.py bulunamadi; dahili hizli kontroller yapiliyor.")
        suite = unittest.TestSuite()
        assert mac_from_oid_suffix("1.3.6.1.2.1.17.4.3.1.2.0.26.203.10.20.30") == (None, "00:1A:CB:0A:14:1E")
        assert mac_from_oid_suffix("1.3.6.1.2.1.17.7.1.2.2.1.2.10.0.26.203.10.20.30", True) == (10, "00:1A:CB:0A:14:1E")
        assert normalize_mac("aabb.ccdd.eeff") == "AA:BB:CC:DD:EE:FF"
        print("Dahili kontroller gecti.")
        return 0
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
