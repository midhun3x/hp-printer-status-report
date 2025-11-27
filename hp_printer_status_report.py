#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import smtplib
import datetime
import xml.etree.ElementTree as ET
from typing import Optional, Dict, Any
from pathlib import Path

from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from pysnmp.hlapi.v3arch.asyncio import (
    get_cmd,
    SnmpEngine,
    CommunityData,
    UdpTransportTarget,
    ContextData,
    ObjectType,
    ObjectIdentity,
)

# =========================
# OIDs
# =========================
OIDS: Dict[str, str] = {
    "Model Name":            "1.3.6.1.2.1.25.3.2.1.3.1",
    "Serial Number":         "1.3.6.1.2.1.43.5.1.1.17.1",
    "Uptime":                "1.3.6.1.2.1.1.3.0",
    "Total Printed Pages":   "1.3.6.1.2.1.43.10.2.1.4.1.1",
}

# =========================
# Config helpers
# =========================
def _xml_text(parent: ET.Element, tag: str, default: Optional[str] = None, required: bool = False) -> Optional[str]:
    node = parent.find(tag)
    text = (node.text.strip() if node is not None and node.text else "")
    if not text:
        if required:
            raise ValueError(f"Missing <{tag}> in XML")
        return default
    return text

def load_config(xml_path: str = "config.xml") -> Dict[str, Any]:
    tree = ET.parse(xml_path)
    root = tree.getroot()

    printers = []
    for p in root.findall("printers/printer"):
        printers.append({
            "ip":        _xml_text(p, "ip", required=True),
            "community": _xml_text(p, "community", "public"),
            "port":      int(_xml_text(p, "port", "161")),
            "version":   _xml_text(p, "version", "2"),
        })

    email_node = root.find("email")
    smtp = email_node.find("smtp")

    email = {
        "host":      _xml_text(smtp, "server", required=True),
        "port":      int(_xml_text(smtp, "port", "587")),
        "username":  _xml_text(smtp, "username", ""),
        "password":  _xml_text(smtp, "password", ""),
        "use_tls":   True,
        "mail_from": _xml_text(email_node, "from", required=True),

        "mail_to":   [a.strip() for a in _xml_text(email_node, "to", required=True).split(",")],
        "mail_cc":   [a.strip() for a in _xml_text(email_node, "cc", "").split(",") if a.strip()],
        "subject":   _xml_text(email_node, "subject", "Printer Counter Report"),
    }
    return {"printers": printers, "email": email}

def send_html_email(cfg: Dict[str, Any], html_body: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["From"] = cfg["mail_from"]
    msg["To"] = ", ".join(cfg["mail_to"])
    if cfg["mail_cc"]:
        msg["Cc"] = ", ".join(cfg["mail_cc"])
    msg["Subject"] = cfg["subject"]

    msg.attach(MIMEText(html_body, "html"))
    recipients = cfg["mail_to"] + cfg["mail_cc"]

    with smtplib.SMTP(cfg["host"], cfg["port"], timeout=15) as s:
        if cfg["use_tls"]:
            s.starttls()
        if cfg["username"]:
            s.login(cfg["username"], cfg["password"])
        s.sendmail(cfg["mail_from"], recipients, msg.as_string())

# =========================
# SNMP helpers
# =========================
def _build_auth_data(community: str, version: str) -> CommunityData:
    ver = str(version).lower()
    mp_model = 0 if ver in ("1", "v1") else 1
    return CommunityData(community, mpModel=mp_model)

def _format_timeticks(ticks: int) -> str:
    total_seconds = ticks // 100
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{days}d {hours:02}:{minutes:02}:{seconds:02}"

async def snmp_get_async(ip: str, oid: str, *, port: int, auth_data: CommunityData) -> Optional[Any]:
    try:
        transport = await UdpTransportTarget.create((ip, port), timeout=2.0, retries=1)
        errorIndication, errorStatus, errorIndex, varBinds = await get_cmd(
            SnmpEngine(), auth_data, transport, ContextData(), ObjectType(ObjectIdentity(oid))
        )
        if errorIndication or errorStatus or not varBinds:
            return None
        return varBinds[0][1]
    except Exception:
        return None

async def snmp_get_many_async(printer: Dict[str, Any]) -> Dict[str, Optional[Any]]:
    """Fetch all OIDs for one printer in parallel."""
    auth_data = _build_auth_data(printer["community"], printer["version"])
    tasks = {name: asyncio.create_task(snmp_get_async(printer["ip"], oid, port=printer["port"], auth_data=auth_data))
             for name, oid in OIDS.items()}
    results: Dict[str, Optional[Any]] = {}
    for name, task in tasks.items():
        try:
            results[name] = await task
        except Exception:
            results[name] = None
    return results

def to_display_string(name: str, raw_val: Optional[Any]) -> str:
    if raw_val is None:
        return "N/A"
    if name.lower() == "uptime":
        try:
            return _format_timeticks(int(raw_val))
        except Exception:
            return str(raw_val)
    try:
        pp = getattr(raw_val, "prettyPrint", None)
        return pp() if callable(pp) else str(raw_val)
    except Exception:
        return str(raw_val)

# =========================
# HTML + Logging
# =========================
def build_html(all_results: Dict[str, Dict[str, Any]]) -> str:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    printer_blocks = []
    for ip, data in all_results.items():
        rows = []
        for k in OIDS.keys():
            v = to_display_string(k, data.get(k))
            rows.append(f"""
              <tr>
                <td style="padding:6px;border:1px solid #ccc;background:#f9fafb;font-weight:600;">{k}</td>
                <td style="padding:6px;border:1px solid #ccc;">{v}</td>
              </tr>
            """)
        table_html = "\n".join(rows)

        printer_blocks.append(f"""
          <h3 style="margin:16px 0 8px 0;">Printer: {ip}</h3>
          <table style="width:100%;border-collapse:collapse;font-size:14px;margin-bottom:16px;">
            <thead>
              <tr>
                <th align="left" style="padding:6px;border:1px solid #ccc;background:#eef2ff;">Field</th>
                <th align="left" style="padding:6px;border:1px solid #ccc;background:#eef2ff;">Value</th>
              </tr>
            </thead>
            <tbody>
              {table_html}
            </tbody>
          </table>
        """)

    return f"""<!DOCTYPE html>
<html>
  <body style="margin:0;padding:0;font-family:Segoe UI,Arial,Helvetica,sans-serif;color:#111827;">
    <div style="max-width:720px;margin:24px auto;padding:16px;border:1px solid #e5e7eb;border-radius:8px;">
      <h2 style="margin:0 0 8px 0;">HP Printer Status Page Report : Comany Name, Location</h2>
      <div style="font-size:13px;color:#6b7280;margin-bottom:20px;">
        <div><b>Generated:</b> {ts}</div>
      </div>
      {"".join(printer_blocks)}
      <div class="footer" style="font-size:12px;color:#9ca3af;margin-top:12px;">
            This is an automated report generated by our internal printer monitoring system.<br>
            If this message was marked as junk, please add the sender to your safe list to ensure future delivery.
      </div>
    </div>
  </body>
</html>"""


def save_log(html_body: str, error: bool = False) -> Path:
    ts = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log_dir = Path("log")
    log_dir.mkdir(exist_ok=True)   # create folder if missing
    fname = f"printer_log_{'error_' if error else ''}{ts}.html"
    path = log_dir / fname
    path.write_text(html_body, encoding="utf-8")
    return path


# =========================
# Main
# =========================
async def main_async() -> int:
    cfg = load_config("config.xml")

    # Run all printers in parallel
    tasks = {p["ip"]: asyncio.create_task(snmp_get_many_async(p)) for p in cfg["printers"]}
    all_results = {ip: await task for ip, task in tasks.items()}

    # Check if any printer failed
    error_detected = any(all(v is None for v in data.values()) for data in all_results.values())

    html_body = build_html(all_results)
    log_path = save_log(html_body, error=error_detected)

    if error_detected:
        print(f"❌ Error detected. Log saved at {log_path}. Email not sent.")
    else:
        cfg["email"]["subject"] = f"{cfg['email']['subject']} ({datetime.date.today().isoformat()})"
        send_html_email(cfg["email"], html_body)
        print(f"✅ Email sent. Log also saved at {log_path}")
    return 0

def main() -> int:
    return asyncio.run(main_async())

if __name__ == "__main__":
    raise SystemExit(main())
