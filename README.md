# mac_tracker

Ag uzerindeki bir cihazin **hangi switch'in hangi portunda** oldugunu SNMP ile
bulur, kaydeder; cihaz portundan koptuysa **en son ne zaman aktif oldugunu**
gecmise donuk soyler.

Kayip/calinmis laptop, "bu IP'yi kim kullaniyordu", "bu cihaz hangi portta
takiliydi" tipi sorular icin.

## Nasil calisir

Her poll'da her switch'in MAC adres tablosu (FDB) okunur ve her MAC icin
`switch + port + VLAN` kaydi tutulur. **Ayni MAC ayni portta tekrar
gorulurse yeni satir acilmaz**, sadece o kaydin `last_seen` alani guncellenir:

* Veritabani cihaz sayisi kadar buyur, poll sayisi kadar buyumez.
* Cihaz agdan koptugunda `last_seen` oldugu yerde donar -> **"en son ne zaman
  bu portta aktifti"** cevabi tam olarak budur.
* Cihaz port degistirirse bu hareket `mac_moves` tablosuna yazilir.

```
switch (SNMP) --> mac_tracker.py --> mac_tracker.db (SQLite dosyasi)
                                        mac_locations : mac + switch + port + first_seen/last_seen
                                        mac_moves     : port degisiklikleri
                                        poll_runs     : her switch'in her poll sonucu
```

## Kurulum

```bash
# 1) net-snmp araclari (snmpbulkwalk varsa otomatik olarak o kullanilir)
sudo apt install snmp                 # Linux
# Windows: https://www.net-snmp.org/  ya da  choco install net-snmp

# 2) Envanter
cp inventory.csv.example inventory.csv
$EDITOR inventory.csv

# 3) Test (ag gerektirmez, 42 test)
python3 mac_tracker.py --selftest
```

Python 3.9+ yeterli, **harici pip paketi yok**. SQLite ayri bir servis degil,
sadece bir dosya: `--db` ile verdigin dosya yoksa tablolariyla birlikte
otomatik olusturulur.

Switch tarafinda gereken tek sey read-only SNMP erisimi:

```
! Cisco IOS - v2c
snmp-server community <RO-STRING> RO 10
access-list 10 permit <toplayici-ip>

! Cisco IOS - v3 (onerilen)
snmp-server group RO-GRP v3 priv
snmp-server user mactrack RO-GRP v3 auth sha <AUTH-PASS> priv aes 128 <PRIV-PASS>
```

## Kullanim

### Veri toplama

```bash
# Ilk deneme (ayrintili cikti, hatalari gormek kolay)
python3 mac_tracker.py --once -v

# ONERILEN: cron / Task Scheduler ile her dakika. Her calisma bagimsiz oldugu
# icin biri kacsa sonraki devam eder; uyuyan laptop / kopan ag loop'u sessizce
# oldurmez.
*/1 * * * * /usr/bin/python3 /opt/mac_tracker/mac_tracker.py --once --db /var/lib/mac_tracker.db --inventory /opt/mac_tracker/inventory.csv

# Tek process isteniyorsa
python3 mac_tracker.py --loop --interval 60
```

### Sorgulama

```bash
# Cihaz nerede / koptuysa en son ne zaman aktifti
python3 mac_tracker.py --lookup aabb.ccdd.eeff      # Cisco formati da olur
python3 mac_tracker.py --lookup AA:BB:CC:DD:EE:FF

# Tum konum gecmisi + port degisiklikleri
python3 mac_tracker.py --history AA:BB:CC:DD:EE:FF

# Bir portta / bir switch'te neler gorulmus
python3 mac_tracker.py --port Gi1/0/5
python3 mac_tracker.py --port Gi1/0/5 --switch Kat1-SW
python3 mac_tracker.py --list-switch Kat1-SW

# 7 gunden beri hic gorulmeyen cihazlar (kayip cihaz listesi)
python3 mac_tracker.py --stale 7

# DB ve switch durumu ozeti / bakim
python3 mac_tracker.py --summary
python3 mac_tracker.py --prune 365
```

Ornek `--lookup` cikisi:

```
MAC          : AA:BB:CC:DD:EE:FF
Durum        : KOPMUS (bu portta son aktiflik: 3 gun 0 saat once)
Switch       : Kat1-SW (10.1.1.1)
Port         : Gi1/0/9
VLAN         : 10
Ilk gorulme  : 2026-09-07 21:10:48 +0300
Son gorulme  : 2026-09-07 21:10:48 +0300  (3 gun 0 saat once)
Gorulme sayisi: 1 poll
Portta MAC   : 1
```

`Durum` uc deger alir:

| Durum | Anlami |
|---|---|
| `AKTIF` | Son basarili poll'da bu portta goruldu |
| `KOPMUS` | Switch sorgulanabiliyor ama cihaz artik FDB'de yok -> `Son gorulme` gercek kopma zamanidir |
| `BILINMIYOR` | Switch'e ulasilamiyor; cihaz hakkinda yorum yapilamaz (cihaz kopmasi ile switch kopmasi karistirilmaz) |

## Onemli detaylar

**Veri kaynagi (`--collector`)**

| Kaynak | Anlami |
|---|---|
| `snmp` | **Varsayilan.** FDB'yi SNMP ile okur (asagidaki `--mode`). |
| `ssh` | Cihaza SSH ile baglanip `show mac address-table` ciktisini parse eder. VLAN + MAC + port tek komutta gelir, MIB destegine ihtiyac yoktur. `pip3 install paramiko` gerekir. |

SSH yolu ne zaman gerekir: bazi platformlarda -- ozellikle EVE-NG/GNS3'teki
**IOL/IOU imajlari** -- Q-BRIDGE MIB yoktur, Cisco'nun `community@vlan`
indexlemesi calismaz ve VLAN context'leri de yoktur. Bu durumda SNMP ile
VLAN basina FDB okumak mumkun olmaz; `--collector ssh` calisan tek yoldur.
Gercek Catalyst/Nexus'ta SNMP yolu tercih edilir (daha hafif, kimlik
bilgisi yonetimi daha basit).

```bash
# Switch'te SSH acik olmali:
#   ip domain-name lab.local
#   crypto key generate rsa modulus 1024
#   username admin privilege 15 secret <parola>
#   line vty 0 4 / login local / transport input ssh

export MACTRACK_SSH_PASS='...'
export MACTRACK_SSH_ENABLE='...'          # gerekiyorsa enable parolasi
python3 mac_tracker.py --once -v --collector ssh --ssh-user admin

# Eski IOS'larda komut tireli:
python3 mac_tracker.py --once --collector ssh --ssh-user admin --ssh-command "show mac-address-table"
```

Parse edici IOS, IOS-XE ve NX-OS ciktilarini tanir; yalnizca `DYNAMIC`
kayitlari alir (`STATIC`/`CPU`/`sup-eth1` gibi satirlar bir cihazin o portta
oldugu anlamina gelmez).

**MAC tablosu okuma yontemi (`--mode`, sadece `--collector snmp`)**

| Mod | Anlami |
|---|---|
| `dot1q` | Standart Q-BRIDGE MIB (`dot1qTpFdbPort`). VLAN, OID index'inde geldigi icin **tek walk tum VLAN'lari** verir. Marka bagimsiz ve hizli. |
| `dot1d` | Klasik BRIDGE MIB. VLAN tasimadigi icin VLAN basina ayri sorgu: Cisco'da v2c `community@vlan`, v3'te `vlan-<id>` context. Eski Catalyst'ler icin. Envanterde `vlans` sart. Bridge-port -> ifIndex haritasi da VLAN context'ine bagli oldugu icin her VLAN'da ayri okunur. |
| `auto` | **Varsayilan.** Once `dot1q`, bos donerse `dot1d`. |

**Uplink/trunk portlari.** Bir cihazin MAC'i kendi access portunda gorundugu
gibi aradaki tum switch'lerin uplink portlarinda da gorunur. "Cihaz hangi
portta" cevabinin dogru olmasi icin tek portta `--uplink-threshold`
degerinden (varsayilan 10) fazla MAC varsa o port trunk kabul edilip
kaydedilmez. Hepsini kaydetmek icin `--keep-uplinks`.

**Sadece ogrenilmis kayitlar.** `dot1qTpFdbStatus`/`dot1dTpFdbStatus`
okunur, yalnizca `learned(3)` kaydedilir; switch'in kendi MAC'i (`self`) ve
statik kayitlar cihaz sayilmaz. Bir walk tasarrufu icin
`--no-status-filter` ile kapatilabilir.

**"Son aktiflik" ne kadar hassas?** FDB kaydi, MAC aging-time suresince
(Cisco varsayilani 300 sn) tabloda kalir. Yani `last_seen`, son paketten
aging-time kadar ileride olabilir. 60 saniyelik poll araligi pratikte
yeterli hassasiyeti verir.

**Zaman damgalari.** DB'ye her zaman UTC yazilir (`2026-09-10T20:54:01Z`),
boylece metin siralamasi = kronolojik siralama olur (yaz saati degisimi
siralamayi bozamaz). Ekranda yerel saate cevrilerek gosterilir.

**Guvenlik.** Envanterdeki community string'i duz metindir; `inventory.csv`
`.gitignore`'da ve dosya izinlerini sikilastirmak iyi olur
(`chmod 600 inventory.csv`). SNMPv3 icin sifreyi komut satirina yazmak yerine
ortam degiskeni kullan:

```bash
export SNMP_V3_USER=mactrack
export SNMP_V3_AUTH_PASS='...'
export SNMP_V3_PRIV_PASS='...'
python3 mac_tracker.py --once          # envanterde ilgili satirlarda version=3
```

**Performans.** Switch'ler paralel sorgulanir (`--workers`, varsayilan 8);
DB yazimi tek thread'de ve switch basina tek transaction icinde yapilir.
Walk icin `snmpbulkwalk` varsa otomatik secilir.

## Testler

```bash
python3 mac_tracker.py --selftest              # ya da
python3 -m unittest discover -s tests -t . -v
```

Testler gercek switch gerektirmez; SNMP katmani sahte `snmp_walk` ile,
veritabani katmani `:memory:` DB ile test edilir.

## Tum secenekler

```bash
python3 mac_tracker.py --help
```
