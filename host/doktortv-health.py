#!/usr/bin/env python3
"""Relevé de santé du NAS DoktorTV pour @DoktorTV_Bot (lancé par doktortv-health.timer).

Écrit $HEALTH_OUT (par défaut /opt/doktortv-bot/data/health.json), lu par le bot.
Lecture seule sur le système : smartctl -n standby ne réveille pas un disque endormi.
Avec --selftest : lance un test SMART court sur chaque disque (timer hebdomadaire).
"""
import glob
import json
import os
import subprocess
import sys
import tempfile
import time

OUT = os.environ.get("HEALTH_OUT", "/opt/doktortv-bot/data/health.json")
ATTRS = {5: "realloc", 187: "uncorrect", 197: "pending", 198: "offline_unc", 199: "crc", 188: "cmd_timeout"}


def sh(*cmd: str) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return ""


def disks() -> list[dict]:
    out = json.loads(sh("lsblk", "-J", "-d", "-b", "-o", "NAME,SIZE,MODEL,SERIAL,ROTA,TRAN,TYPE") or "{}")
    return [d for d in out.get("blockdevices", []) if d.get("type") == "disk" and d.get("tran") in ("sata", "nvme", "sas")]


def mounts() -> dict:
    """disque → (point de montage, total, libre) pour ses partitions montées."""
    res = {}
    out = json.loads(sh("lsblk", "-J", "-b", "-o", "NAME,PKNAME,MOUNTPOINT") or "{}")

    def walk(nodes, parent=None):
        for n in nodes:
            disk = n.get("pkname") or parent or n["name"]
            mp = n.get("mountpoint")
            if mp and mp.startswith(("/srv/dev-disk", "/")) and mp not in ("[SWAP]",):
                try:
                    st = os.statvfs(mp)
                    tot, free = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
                    cur = res.get(disk)
                    if not cur or tot > cur["total"]:
                        res[disk] = {"mount": mp, "total": tot, "free": free}
                except OSError:
                    pass
            walk(n.get("children", []), disk)
    walk(out.get("blockdevices", []))
    return res


def smart(name: str, prev: dict) -> dict:
    raw = sh("smartctl", "-j", "-H", "-A", "-l", "selftest", "-n", "standby", f"/dev/{name}")
    try:
        j = json.loads(raw)
    except ValueError:
        return {**prev, "error": "smartctl illisible"}
    if (j.get("smartctl", {}).get("exit_status", 0) & 2) and "smart_status" not in j:
        # disque en veille : on garde les dernières valeurs connues
        return {**prev, "sleeping": True}
    d = {
        "passed": j.get("smart_status", {}).get("passed"),
        "temp": j.get("temperature", {}).get("current"),
        "hours": j.get("power_on_time", {}).get("hours"),
        "sleeping": False,
    }
    for a in j.get("ata_smart_attributes", {}).get("table", []):
        if a["id"] in ATTRS:
            # Seagate empaquette plusieurs compteurs : le premier nombre de la forme texte
            txt = str(a.get("raw", {}).get("string", "")).split()
            try:
                d[ATTRS[a["id"]]] = int(txt[0]) if txt else a["raw"]["value"]
            except ValueError:
                d[ATTRS[a["id"]]] = a["raw"]["value"]
    n = j.get("nvme_smart_health_information_log")
    if n:
        d.update({"wear_pct": n.get("percentage_used"), "media_errors": n.get("media_errors"),
                  "critical_warning": n.get("critical_warning"),
                  "written_bytes": (n.get("data_units_written") or 0) * 512000,
                  "unsafe_shutdowns": n.get("unsafe_shutdowns")})
    tests = (j.get("ata_smart_self_test_log", {}).get("standard", {}).get("table")
             or j.get("nvme_self_test_log", {}).get("table") or [])
    if tests:
        t = tests[0]
        st = t.get("status", {})
        d["last_test"] = {"type": (t.get("type") or {}).get("string") or t.get("self_test_code", {}).get("string"),
                          "passed": st.get("passed", st.get("value") == 0),
                          "status": st.get("string"),
                          "at_hours": t.get("lifetime_hours") or t.get("power_on_hours")}
    return d


def cpu_temp():
    temps = []
    for f in glob.glob("/sys/class/hwmon/hwmon*/temp*_input"):
        name = open(os.path.join(os.path.dirname(f), "name")).read().strip()
        if name in ("coretemp", "k10temp", "acpitz"):
            try:
                temps.append(int(open(f).read()) / 1000)
            except (OSError, ValueError):
                pass
    return max(temps) if temps else None


def failed_units() -> list[str]:
    out = sh("systemctl", "list-units", "--failed", "--plain", "--no-legend", "--no-pager")
    return [l.split()[0] for l in out.splitlines() if l.strip()]


def main():
    if "--selftest" in sys.argv:
        for d in disks():
            sh("smartctl", "-t", "short", f"/dev/{d['name']}")
        return
    try:
        prev = json.load(open(OUT))
    except (OSError, ValueError):
        prev = {}
    prev_disks = {d["name"]: d for d in prev.get("disks", [])}
    mnt = mounts()
    out_disks = []
    for d in disks():
        p = prev_disks.get(d["name"], {})
        s = smart(d["name"], p.get("smart", {}))
        out_disks.append({"name": d["name"], "model": (d.get("model") or "").strip(), "serial": d.get("serial"),
                          "size": d.get("size"), "rotational": bool(d.get("rota")), "tran": d.get("tran"),
                          "usage": mnt.get(d["name"]), "smart": s})
    data = {"at": time.time(), "cpu_temp": cpu_temp(), "failed_units": failed_units(),
            "uptime_s": float(open("/proc/uptime").read().split()[0]), "disks": out_disks}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(OUT), prefix=".health.")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)
    os.chmod(tmp, 0o644)
    os.replace(tmp, OUT)


if __name__ == "__main__":
    main()
