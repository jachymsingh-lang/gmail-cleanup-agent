#!/usr/bin/env python3
"""Gmail Cleanup Agent
 
- připojí se přes IMAP (imap.gmail.com) pomocí app passwordu
- projde INBOX za posledních LOOKBACK_DAYS dní (výchozí 14)
- detekuje balast a vlastní maily, vytvoří chybějící štítky,
  oštítkuje a FYZICKY odebere z doručené pošty (-X-GM-LABELS (\\Inbox))
- staré věci posílá do koše (podle pravidel TRASH_RULES)
- každých 14 dní pošle hezký HTML report přes Resend API
 
Pouze standardní knihovna, žádné pip install.
"""
 
import base64
import email
import imaplib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr
from html import escape
from collections import Counter
 
# ---------------------------------------------------------------------------
# Konfigurace
# ---------------------------------------------------------------------------
 
IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
 
GMAIL_USER = os.environ.get("GMAIL_USER", "").strip()
APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "false").strip().lower() == "true"
FORCE_REPORT = os.environ.get("FORCE_REPORT", "false").strip().lower() == "true"
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "14") or "14")
 
MAX_TRASH_PER_RUN = 300
BATCH = 100
 
OWN_LABEL = "OdMne"  # Štítek pro maily odeslané tebou
 
def norm_addr(a):
    a = (a or "").strip().lower()
    if "@" not in a:
        return a
    local, domain = a.rsplit("@", 1)
    if domain in ("gmail.com", "googlemail.com"):
        local = local.split("+")[0].replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"
 
OWN_ADDRESSES = {norm_addr(a) for a in
                 [GMAIL_USER] + os.environ.get("OWN_ADDRESSES", "").split(",") if a.strip()}
 
RULES = [
    {"name": "NHL agent", "label": "NHL agent", "force": True,
     "from": [], "subject": ["puck morning", "nhl report"]},
    {"name": "Job Agent", "label": "JobAgent", "force": True,
     "from": [], "subject": ["job agent", "nové nabídky práce"]},
    {"name": "Výběrové řízení", "label": "OdpovedNaInzeraty", "force": True,
     "from": [],
     "subject": ["výběrové řízení", "výběrového řízení", "výběrovém řízení", "vyberoveho rizeni", "thank you for applying",
                 "vaše přihláška", "vase prihlaska", "děkujeme za váš zájem"]},
    {"name": "Burger King", "label": "Balast",
     "from": ["burgerking", "burger king"], "subject": []},
    {"name": "PlayStation", "label": "Balast",
     "from": ["playstation", "sonyentertainmentnetwork", "sony interactive"],
     "subject": []},
    {"name": "Revolut", "label": "Balast",
     "from": ["revolut"], "subject": []},
    {"name": "Google alerty", "label": "Notifikace",
     "from": ["no-reply@accounts.google.com"], "subject": [], "force": True},
]
 
PROTECT_RE = re.compile(
    r"ověřovac|overovac|verification|one[- ]time|\botp\b|security code|\byour\b.{0,30}\bcode\b|bezpečnost"
    r"|heslo|password|přihlášení|sign[- ]in|faktur|invoice|platb|payment|receipt"
    r"|účtenk|objednáv|\border\b|zásilk|delivery|potvrzení",
    re.IGNORECASE,
)
 
SKIP_SUBJECTS = ["gmail cleanup"]
NOREPLY_RE = re.compile(r"(no[-_.]?reply|do[-_.]?not[-_.]?reply|nepisat|mailer-daemon)", re.IGNORECASE)
 
TRASH_RULES = [
    ("Balast starší 14 dní", "label:Balast older_than:14d -is:starred"),
    ("Newslettery starší 30 dní", "label:Newslettery older_than:30d -is:starred"),
    ("Notifikace starší 30 dní", "label:Notifikace older_than:30d -is:starred"),
    ("Google alerty starší 7 dní", "from:no-reply@accounts.google.com older_than:7d -is:starred"),
    ("Odpovědi na inzeráty starší 60 dní", "label:OdpovedNaInzeraty older_than:60d -is:starred"),
    ("Vlastní maily starší 30 dní", "label:OdMne older_than:30d -is:starred"),
]
 
REPORT_LABELS = ["OdMne", "Balast", "Newslettery", "Notifikace", "NHL agent", "JobAgent", "OdpovedNaInzeraty"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
 
def log(msg):
    print(msg, flush=True)
 
def imap_date(d):
    return f"{d.day:02d}-{MONTHS[d.month - 1]}-{d.year}"
 
def imap_utf7_encode(s):
    out, buf = [], []
    def flush():
        if buf:
            raw = "".join(buf).encode("utf-16-be")
            out.append("&" + base64.b64encode(raw).decode().rstrip("=").replace("/", ",") + "-")
            buf.clear()
    for ch in s:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)
 
def imap_utf7_decode(s):
    out, i = [], 0
    while i < len(s):
        if s[i] == "&":
            j = s.find("-", i)
            if j == -1:
                out.append(s[i:])
                break
            chunk = s[i + 1:j]
            if chunk == "":
                out.append("&")
            else:
                b64 = chunk.replace(",", "/")
                b64 += "=" * (-len(b64) % 4)
                out.append(base64.b64decode(b64).decode("utf-16-be"))
            i = j + 1
        else:
            out.append(s[i])
            i += 1
    return "".join(out)
 
def quote(name):
    enc = imap_utf7_encode(name).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{enc}"'
 
def quote_raw(raw):
    return '"' + raw.replace("\\", "\\\\").replace('"', '\\"') + '"'
 
def decode_hdr(value):
    try:
        return str(make_header(decode_header(str(value or ""))))
    except Exception:
        return str(value or "")
 
def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]
 
def label_query_name(label):
    return label.replace(" ", "-")
 
def connect():
    for attempt in range(1, 4):
        try:
            imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
            imap.login(GMAIL_USER, APP_PASSWORD)
            return imap
        except Exception as e:
            log(f"Připojení selhalo (pokus {attempt}/3): {e}")
            time.sleep(5 * attempt)
    sys.exit(1)
 
LIST_RE = re.compile(r'\((?P<flags>[^)]*)\)\s+(?:"(?P<delim>[^"]*)"|NIL)\s+(?P<name>.+)$')
 
def list_folders(imap):
    typ, data = imap.list()
    folders = []
    if typ != "OK":
        return folders
    for item in data:
        if not isinstance(item, bytes):
            continue
        m = LIST_RE.match(item.decode("utf-8", "replace"))
        if not m:
            continue
        name = m.group("name").strip()
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        flags = set(m.group("flags").split())
        folders.append((flags, name, imap_utf7_decode(name)))
    return folders
 
def special_folder(folders, flag, fallback):
    for flags, raw, _decoded in folders:
        if flag in flags:
            return raw
    return fallback
 
def existing_labels(folders):
    return {decoded.lower() for _f, _r, decoded in folders
            if not decoded.startswith("[Gmail]") and decoded.upper() != "INBOX"}
 
def ensure_label(imap, label, existing):
    if label.lower() in existing:
        return True
    if DRY_RUN:
        log(f"  [dry-run] vytvořil bych štítek: {label}")
        return True
    typ, data = imap.create(quote(label))
    if typ == "OK":
        log(f"  + vytvořen štítek: {label}")
        existing.add(label.lower())
        return True
    if data and b"ALREADYEXISTS" in b" ".join(d for d in data if isinstance(d, bytes)).upper():
        existing.add(label.lower())
        return True
    return False
 
def search_uids(imap, *criteria):
    typ, data = imap.uid("SEARCH", None, *criteria)
    if typ != "OK" or not data or not data[0]:
        return []
    return data[0].split()
 
def fetch_headers(imap, uids):
    result = []
    fields = "(FLAGS X-GM-MSGID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT LIST-UNSUBSCRIBE PRECEDENCE)])"
    for batch in chunks(uids, BATCH):
        typ, data = imap.uid("FETCH", b",".join(batch).decode(), fields)
        if typ != "OK":
            continue
        j = 0
        while j < len(data):
            item = data[j]
            if isinstance(item, tuple):
                trailer = ""
                if j + 1 < len(data) and isinstance(data[j + 1], bytes):
                    trailer = data[j + 1].decode("utf-8", "replace")
                info = item[0].decode("utf-8", "replace") + " " + trailer
                m_uid = re.search(r"UID (\d+)", info)
                m_fl = re.search(r"FLAGS \(([^)]*)\)", info)
                m_id = re.search(r"X-GM-MSGID (\d+)", info)
                if m_uid:
                    msg = email.message_from_bytes(item[1])
                    display, addr = parseaddr(str(msg.get("From", "")))
                    precedence = str(msg.get("Precedence", "")).lower()
                    result.append({
                        "uid": m_uid.group(1),
                        "msgid": m_id.group(1) if m_id else None,
                        "flags": (m_fl.group(1) if m_fl else ""),
                        "addr": addr.lower(),
                        "display": decode_hdr(display),
                        "subject": decode_hdr(msg.get("Subject", "")),
                        "bulk": bool(msg.get("List-Unsubscribe")) or precedence in ("bulk", "list", "junk"),
                    })
            j += 1
    return result
 
def classify(m):
    hay_from = f"{m['addr']} {m['display']}".casefold()
    subj = m["subject"].casefold()
 
    if any(s in subj for s in SKIP_SUBJECTS):
        return None, "skip"
    
    # Detekce mailů odeslaných tebou
    if norm_addr(m["addr"]) in OWN_ADDRESSES:
        return OWN_LABEL, "odesláno mnou"

    if "\\Flagged" in m["flags"]:
        return None, "označeno hvězdičkou"
 
    for rule in RULES:
        hit = (any(s in hay_from for s in rule["from"]) or any(s in subj for s in rule["subject"]))
        if hit:
            if not rule.get("force") and PROTECT_RE.search(m["subject"]):
                return None, "chráněný předmět"
            return rule["label"], rule["name"]
 
    if PROTECT_RE.search(m["subject"]):
        return None, "chráněný předmět"
    if NOREPLY_RE.search(m["addr"]):
        return "Notifikace", "noreply"
    if m["bulk"]:
        return "Newslettery", "newsletter"
    return None, None
 
def archive_and_label(imap, plan, existing, all_mail_raw):
    for label in plan.keys():
        ensure_label(imap, label, existing)
    if DRY_RUN:
        return set()
 
    typ, _ = imap.select(quote_raw(all_mail_raw))
    if typ != "OK":
        return set()
 
    done = set()
    for label, items in plan.items():
        uids = []
        for m in items:
            if not m.get("msgid"):
                continue
            found = search_uids(imap, "X-GM-MSGID", m["msgid"])
            if found:
                uids.append(found[0].decode())
        for chunk in chunks(uids, 200):
            uidset = ",".join(chunk)
            imap.uid("STORE", uidset, "+X-GM-LABELS", f"({quote(label)})")
            imap.uid("STORE", uidset, "-X-GM-LABELS", "(\\Inbox)")
            done.update(chunk)
    return done
 
def verify_archived(imap, all_mail_raw, done):
    if not done:
        return
    typ, _ = imap.select(quote_raw(all_mail_raw), readonly=True)
    if typ != "OK":
        return
    in_inbox = {u.decode() for u in search_uids(imap, "X-GM-RAW", '"in:inbox"')}
    leftover = in_inbox & done
    if leftover:
        log(f"\n! Varování: {len(leftover)} mailů má stále štítek Inbox.")
    else:
        log(f"\nOvěřeno: všechny zpracované maily jsou pryč z Inboxu.")
 
def trash_old(imap, all_mail_raw):
    typ, _ = imap.select(quote_raw(all_mail_raw), readonly=DRY_RUN)
    if typ != "OK":
        return 0
    total = 0
    for title, query in TRASH_RULES:
        if total >= MAX_TRASH_PER_RUN:
            break
        uids = [u.decode() for u in search_uids(imap, "X-GM-RAW", f'"{query}"')]
        uids = uids[:MAX_TRASH_PER_RUN - total]
        if not uids:
            continue
        log(f"Koš - {title}: {len(uids)} mailů")
        if DRY_RUN:
            continue
        for chunk in chunks(uids, 200):
            typ, data = imap.uid("STORE", ",".join(chunk), "+X-GM-LABELS", "(\\Trash)")
            if typ == "OK":
                total += len(chunk)
    return total if not DRY_RUN else 0
 
def should_send_report():
    return FORCE_REPORT or date.today().toordinal() % 14 == 0
 
def count_query(imap, query):
    return len(search_uids(imap, "X-GM-RAW", f'"{query}"'))
 
PALETTE = ["#6366f1", "#f59e0b", "#10b981", "#ef4444", "#0ea5e9", "#8b5cf6", "#ec4899"]
 
def build_report(imap, all_mail_raw, run_stats):
    typ, _ = imap.select(quote_raw(all_mail_raw), readonly=True)
    if typ != "OK":
        raise RuntimeError(f"Nelze otevřít {all_mail_raw}")
    rows = [(label, count_query(imap, f"label:{label_query_name(label)} newer_than:14d"))
            for label in REPORT_LABELS]
    received = count_query(imap, "newer_than:14d -in:sent -in:drafts")
    in_inbox = count_query(imap, "in:inbox newer_than:14d")
    max_n = max([n for _l, n in rows] + [1])
 
    def tile(value, caption, color):
        return (f'<td align="center" width="25%" style="background:#f3f4f6;border-radius:10px;padding:14px 6px;">'
                f'<div style="font-size:26px;font-weight:700;color:{color};line-height:1.1">{value}</div>'
                f'<div style="font-size:11px;color:#6b7280;margin-top:5px">{caption}</div></td>')
 
    gap = '<td width="8"></td>'
    tiles = (f'<table width="100%" cellpadding="0" cellspacing="0" style="margin:18px 0"><tr>'
             f'{tile(received, "přijato za 14 dní", "#111827")}{gap}'
             f'{tile(in_inbox, "stále v doručené", "#ef4444")}{gap}'
             f'{tile(run_stats["archived"], "archivováno dnes", "#10b981")}{gap}'
             f'{tile(run_stats["trashed"], "do koše dnes", "#f59e0b")}'
             f'</tr></table>')
 
    bars = ""
    for i, (label, n) in enumerate(rows):
        pct = max(round(n / max_n * 100), 3) if n else 0
        color = PALETTE[i % len(PALETTE)]
        bars += (f'<tr><td style="padding:7px 0;font-size:14px;color:#111827;width:36%">{escape(label)}</td>'
                 f'<td style="padding:7px 10px;width:48%"><div style="background:#e5e7eb;border-radius:4px;height:8px">'
                 f'<div style="background:{color};width:{pct}%;height:8px;border-radius:4px"></div></div></td>'
                 f'<td align="right" style="padding:7px 0;font-size:14px;font-weight:600;color:#111827;width:16%">{n}</td></tr>')
 
    now = datetime.now(timezone.utc)
    return f"""
<div style="background:#f9fafb;padding:24px 12px;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif">
  <table align="center" width="100%" cellpadding="0" cellspacing="0"
         style="max-width:560px;background:#ffffff;border-radius:14px;overflow:hidden;border:1px solid #e5e7eb">
    <tr><td style="background:#111827;padding:22px 26px">
      <div style="font-size:12px;letter-spacing:1px;color:#9ca3af;text-transform:uppercase">Gmail Cleanup Agent</div>
      <div style="font-size:21px;font-weight:700;color:#ffffff;margin-top:4px">14denní report</div>
      <div style="font-size:12px;color:#9ca3af;margin-top:4px">{(now - timedelta(days=14)):%d. %m.} – {now:%d. %m. %Y}</div>
    </td></tr>
    <tr><td style="padding:6px 26px 26px">
      {tiles}
      <h3 style="font-size:15px;margin:8px 0 4px;color:#111827">Oštítkováno za 14 dní</h3>
      <table width="100%" cellpadding="0" cellspacing="0">{bars}</table>
      <p style="color:#9ca3af;font-size:11px;margin:26px 0 0">Vygenerováno {now:%Y-%m-%d %H:%M} UTC</p>
    </td></tr>
  </table>
</div>
"""
 
def send_resend(subject, html):
    key = os.environ.get("RESEND_API_KEY", "").strip()
    to = os.environ.get("REPORT_EMAIL", "").strip() or GMAIL_USER
    sender = os.environ.get("RESEND_FROM", "").strip() or "Gmail Cleanup Agent <onboarding@resend.dev>"
    if not key:
        return
    payload = json.dumps({"from": sender, "to": [to], "subject": subject, "html": html}).encode()
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "gmail-cleanup-agent/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30):
            log("Report odeslán.")
    except Exception as e:
        log(f"Resend chyba: {e}")
 
def main():
    if not GMAIL_USER or not APP_PASSWORD:
        sys.exit(1)
 
    imap = connect()
    run_stats = {"archived": 0, "trashed": 0}
    try:
        folders = list_folders(imap)
        all_mail_raw = special_folder(folders, "\\All", "[Gmail]/All Mail")
        existing = existing_labels(folders)
 
        since = date.today() - timedelta(days=LOOKBACK_DAYS)
        typ, _ = imap.select("INBOX", readonly=DRY_RUN)
        if typ != "OK":
            sys.exit(1)
        uids = search_uids(imap, "SINCE", imap_date(since))
        
        messages = fetch_headers(imap, uids)
        plan, skipped = {}, 0
        for m in messages:
            label, reason = classify(m)
            if label:
                m["reason"] = reason
                plan.setdefault(label, []).append(m)
            else:
                skipped += 1
 
        archived = archive_and_label(imap, plan, existing, all_mail_raw)
        run_stats["archived"] = len(archived)
        verify_archived(imap, all_mail_raw, archived)
 
        run_stats["trashed"] = trash_old(imap, all_mail_raw)
 
        if should_send_report():
            if not DRY_RUN:
                try:
                    html = build_report(imap, all_mail_raw, run_stats)
                    send_resend("Gmail Cleanup Agent - 14denní report", html)
                except Exception as e:
                    log(f"Chyba reportu: {e}")
    finally:
        imap.logout()
 
if __name__ == "__main__":
    main()
