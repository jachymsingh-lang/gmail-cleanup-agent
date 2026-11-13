#!/usr/bin/env python3
"""Gmail Cleanup Agent

- připojí se přes IMAP (imap.gmail.com) pomocí app passwordu
- projde INBOX za posledních LOOKBACK_DAYS dní (výchozí 14)
- detekuje balast (reklamy, noreply, newslettery, ...), vytvoří chybějící štítky,
  oštítkuje a FYZICKY odebere z doručené pošty (-X-GM-LABELS (\\Inbox))
- staré věci posílá do koše (podle pravidel TRASH_RULES)
- každých 14 dní pošle report přes Resend API

Pouze standardní knihovna, žádné pip install.

Env proměnné:
  GMAIL_USER, GMAIL_APP_PASSWORD   (povinné)
  RESEND_API_KEY, REPORT_EMAIL     (volitelné, pro report)
  RESEND_FROM                      (volitelné, výchozí onboarding@resend.dev)
  DRY_RUN=true                     (nic nemění, jen vypisuje)
  FORCE_REPORT=true                (pošle report mimo 14denní cyklus)
  LOOKBACK_DAYS=14
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

MAX_TRASH_PER_RUN = 300  # pojistka proti masovému smazání
BATCH = 100

# Pravidla se vyhodnocují shora dolů, vyhrává první shoda.
#   label   - štítek v Gmailu (vytvoří se, pokud neexistuje)
#   from    - podřetězce v adrese/jménu odesílatele (malá písmena)
#   subject - podřetězce v předmětu (malá písmena)
#   force   - True = ignoruje ochranu důležitých předmětů (PROTECT_RE)
RULES = [
    # --- tvoji agenti (uprav podle skutečných předmětů/odesílatelů) ---
    {"name": "NHL agent", "label": "NHL agent", "force": True,
     "from": [], "subject": ["puck morning", "nhl report"]},
    {"name": "Job Agent", "label": "JobAgent", "force": True,
     "from": [], "subject": ["job agent", "nové nabídky práce"]},
    # --- odpovědi na inzeráty ---
    {"name": "Výběrové řízení", "label": "OdpovedNaInzeraty", "force": True,
     "from": [],
     "subject": ["výběrového řízení", "vyberoveho rizeni", "thank you for applying",
                 "vaše přihláška", "vase prihlaska", "děkujeme za váš zájem"]},
    # --- balast ---
    {"name": "Burger King", "label": "Balast",
     "from": ["burgerking", "burger king"], "subject": []},
    {"name": "PlayStation", "label": "Balast",
     "from": ["playstation", "sonyentertainmentnetwork", "sony interactive"],
     "subject": []},
    {"name": "Revolut", "label": "Balast",
     "from": ["revolut"], "subject": []},
    {"name": "Google alerty", "label": "Notifikace",
     "from": ["no-reply@accounts.google.com"], "subject": []},
]

# Předměty, které se u obecných pravidel (noreply/newsletter/značky) NEarchivují.
PROTECT_RE = re.compile(
    r"ověřovac|overovac|verification|one[- ]time|\botp\b|security code|bezpečnost"
    r"|heslo|password|přihlášení|sign[- ]in|faktur|invoice|platb|payment|receipt"
    r"|účtenk|objednáv|\border\b|zásilk|delivery|potvrzení",
    re.IGNORECASE,
)

# Maily s tímto předmětem se nikdy nesahá (např. vlastní report agenta).
SKIP_SUBJECTS = ["gmail cleanup"]

NOREPLY_RE = re.compile(r"(no[-_.]?reply|do[-_.]?not[-_.]?reply|nepisat|mailer-daemon)",
                        re.IGNORECASE)

# Mazání do koše: Gmail dotaz (X-GM-RAW) hledaný v "Všechny zprávy".
TRASH_RULES = [
    ("Balast starší 14 dní", "label:Balast older_than:14d -is:starred"),
    ("Newslettery starší 30 dní", "label:Newslettery older_than:30d -is:starred"),
    ("Notifikace starší 30 dní", "label:Notifikace older_than:30d -is:starred"),
    ("Google alerty starší 7 dní", "from:no-reply@accounts.google.com older_than:7d -is:starred"),
    ("Odpovědi na inzeráty starší 60 dní", "label:OdpovedNaInzeraty older_than:60d -is:starred"),
]

# Štítky, které se objeví v reportu
REPORT_LABELS = ["Balast", "Newslettery", "Notifikace", "NHL agent", "JobAgent",
                 "OdpovedNaInzeraty"]

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ---------------------------------------------------------------------------
# Pomocné funkce
# ---------------------------------------------------------------------------

def log(msg):
    print(msg, flush=True)


def imap_date(d):
    """Datum pro IMAP SEARCH vždy s anglickou zkratkou měsíce (nezávislé na locale)."""
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
    """Název složky/štítku pro IMAP příkaz."""
    enc = imap_utf7_encode(name).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{enc}"'


def decode_hdr(value):
    try:
        return str(make_header(decode_header(str(value or ""))))
    except Exception:
        return str(value or "")


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def label_query_name(label):
    """Jak se štítek píše v Gmail dotazu (mezery -> pomlčky)."""
    return label.replace(" ", "-")


# ---------------------------------------------------------------------------
# IMAP
# ---------------------------------------------------------------------------

def connect():
    last_err = None
    for attempt in range(1, 4):
        try:
            imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
            imap.login(GMAIL_USER, APP_PASSWORD)
            return imap
        except imaplib.IMAP4.error as e:
            # špatné heslo - opakování nepomůže
            log(f"IMAP login selhal: {e}")
            sys.exit(1)
        except Exception as e:  # síť, timeout...
            last_err = e
            log(f"Připojení selhalo (pokus {attempt}/3): {e}")
            time.sleep(5 * attempt)
    log(f"Nepodařilo se připojit: {last_err}")
    sys.exit(1)


LIST_RE = re.compile(r'\((?P<flags>[^)]*)\)\s+(?:"(?P<delim>[^"]*)"|NIL)\s+(?P<name>.+)$')


def list_folders(imap):
    """Vrátí seznam (flags:set, raw_name, decoded_name)."""
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
    # "already exists" není chyba
    if data and b"ALREADYEXISTS" in b" ".join(d for d in data if isinstance(d, bytes)).upper():
        existing.add(label.lower())
        return True
    log(f"  ! štítek {label} se nepodařilo vytvořit: {data}")
    return False


def search_uids(imap, *criteria):
    typ, data = imap.uid("SEARCH", None, *criteria)
    if typ != "OK" or not data or not data[0]:
        return []
    return data[0].split()


def fetch_headers(imap, uids):
    """Vrátí list dictů: uid, flags, addr, display, subject, bulk."""
    result = []
    fields = "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT LIST-UNSUBSCRIBE PRECEDENCE)])"
    for batch in chunks(uids, BATCH):
        typ, data = imap.uid("FETCH", b",".join(batch).decode(), fields)
        if typ != "OK":
            log(f"  ! FETCH selhal: {data}")
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
                if m_uid:
                    msg = email.message_from_bytes(item[1])
                    display, addr = parseaddr(str(msg.get("From", "")))
                    precedence = str(msg.get("Precedence", "")).lower()
                    result.append({
                        "uid": m_uid.group(1),
                        "flags": (m_fl.group(1) if m_fl else ""),
                        "addr": addr.lower(),
                        "display": decode_hdr(display),
                        "subject": decode_hdr(msg.get("Subject", "")),
                        "bulk": bool(msg.get("List-Unsubscribe"))
                                or precedence in ("bulk", "list", "junk"),
                    })
            j += 1
    return result


# ---------------------------------------------------------------------------
# Klasifikace
# ---------------------------------------------------------------------------

def classify(m):
    """Vrátí (label, důvod) nebo (None, důvod/None)."""
    hay_from = f"{m['addr']} {m['display']}".casefold()
    subj = m["subject"].casefold()

    if any(s in subj for s in SKIP_SUBJECTS):
        return None, "skip"
    if "\\Flagged" in m["flags"]:
        return None, "označeno hvězdičkou"

    for rule in RULES:
        hit = (any(s in hay_from for s in rule["from"])
               or any(s in subj for s in rule["subject"]))
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


# ---------------------------------------------------------------------------
# Akce
# ---------------------------------------------------------------------------

def archive_and_label(imap, plan, existing):
    """plan: {label: [msg, ...]} -> vrátí množinu skutečně archivovaných UID."""
    archived = set()
    for label, items in plan.items():
        log(f"\nŠtítek '{label}': {len(items)} mailů")
        for m in items:
            log(f"  - {m['addr']} | {m['subject'][:70]}")
        if DRY_RUN:
            ensure_label(imap, label, existing)
            continue
        if not ensure_label(imap, label, existing):
            continue
        uids = [m["uid"] for m in items]
        for chunk in chunks(uids, 200):
            uidset = ",".join(chunk)
            typ, data = imap.uid("STORE", uidset, "+X-GM-LABELS", f"({quote(label)})")
            if typ != "OK":
                log(f"  ! přidání štítku selhalo: {data}")
                continue
            typ, data = imap.uid("STORE", uidset, "-X-GM-LABELS", "(\\Inbox)")
            if typ != "OK":
                log(f"  ! odebrání z Inboxu selhalo: {data}")
                continue
            archived.update(chunk)
    return archived


def verify_archived(imap, since, archived):
    """Znovu projde INBOX a ověří, že archivované maily už v něm nejsou."""
    if not archived:
        return
    imap.noop()
    typ, _ = imap.select("INBOX")
    if typ != "OK":
        return
    still = {u.decode() for u in search_uids(imap, "SINCE", imap_date(since))}
    leftover = still & archived
    if leftover:
        log(f"\n! Varování: {len(leftover)} mailů zůstalo v Inboxu i po archivaci.")
    else:
        log(f"\nOvěřeno: {len(archived)} mailů už v Inboxu není.")


def trash_old(imap, all_mail_raw):
    """Pošle do koše maily podle TRASH_RULES. Vrátí počet."""
    typ, _ = imap.select(quote(all_mail_raw), readonly=DRY_RUN)
    if typ != "OK":
        log(f"! Nelze otevřít složku Všechny zprávy ({all_mail_raw})")
        return 0
    total = 0
    for title, query in TRASH_RULES:
        if total >= MAX_TRASH_PER_RUN:
            log("Dosažen limit mazání pro tento běh.")
            break
        uids = [u.decode() for u in search_uids(imap, "X-GM-RAW", f'"{query}"')]
        uids = uids[:MAX_TRASH_PER_RUN - total]
        if not uids:
            continue
        log(f"\nKoš - {title}: {len(uids)} mailů")
        if DRY_RUN:
            continue
        for chunk in chunks(uids, 200):
            typ, data = imap.uid("STORE", ",".join(chunk), "+X-GM-LABELS", "(\\Trash)")
            if typ == "OK":
                total += len(chunk)
            else:
                log(f"  ! přesun do koše selhal: {data}")
    return total if not DRY_RUN else 0


# ---------------------------------------------------------------------------
# Report (Resend)
# ---------------------------------------------------------------------------

def should_send_report():
    return FORCE_REPORT or date.today().toordinal() % 14 == 0


def count_query(imap, query):
    return len(search_uids(imap, "X-GM-RAW", f'"{query}"'))


def build_report(imap, all_mail_raw, run_stats):
    imap.select(quote(all_mail_raw), readonly=True)
    rows = []
    for label in REPORT_LABELS:
        n = count_query(imap, f"label:{label_query_name(label)} newer_than:14d")
        rows.append((label, n))
    received = count_query(imap, "newer_than:14d -in:sent -in:drafts")
    in_inbox = count_query(imap, "in:inbox newer_than:14d")

    rows_html = "".join(f"<tr><td>{l}</td><td align='right'><b>{n}</b></td></tr>"
                        for l, n in rows)
    html = f"""
    <h2>Gmail Cleanup Agent - 14denní report</h2>
    <p>Přijato za 14 dní: <b>{received}</b><br>
       Z toho stále v doručené poště: <b>{in_inbox}</b></p>
    <h3>Oštítkováno za 14 dní</h3>
    <table cellpadding="4">{rows_html}</table>
    <h3>Dnešní běh</h3>
    <p>Archivováno: <b>{run_stats['archived']}</b><br>
       Do koše: <b>{run_stats['trashed']}</b></p>
    <p style="color:#888;font-size:12px">Vygenerováno {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC</p>
    """
    return html


def send_resend(subject, html):
    key = os.environ.get("RESEND_API_KEY", "").strip()
    to = os.environ.get("REPORT_EMAIL", "").strip() or GMAIL_USER
    sender = os.environ.get("RESEND_FROM", "").strip() or "Gmail Cleanup Agent <onboarding@resend.dev>"
    if not key:
        log("RESEND_API_KEY není nastaven - report se neodesílá.")
        return
    payload = json.dumps({"from": sender, "to": [to], "subject": subject, "html": html}).encode()
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            # bez User-Agentu Resend/Cloudflare občas vrací 403
            "User-Agent": "gmail-cleanup-agent/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            log(f"Report odeslán (HTTP {resp.status}).")
    except urllib.error.HTTPError as e:
        log(f"Resend chyba {e.code}: {e.read().decode('utf-8', 'replace')}")
    except Exception as e:
        log(f"Resend chyba: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not GMAIL_USER or not APP_PASSWORD:
        log("Chybí GMAIL_USER nebo GMAIL_APP_PASSWORD.")
        sys.exit(1)

    log(f"Gmail Cleanup Agent | DRY_RUN={DRY_RUN} | lookback={LOOKBACK_DAYS} dní")
    imap = connect()
    run_stats = {"archived": 0, "trashed": 0}
    try:
        folders = list_folders(imap)
        all_mail_raw = special_folder(folders, "\\All", "[Gmail]/All Mail")
        existing = existing_labels(folders)

        # 1) Inbox -> štítky + archivace
        since = date.today() - timedelta(days=LOOKBACK_DAYS)
        typ, _ = imap.select("INBOX", readonly=DRY_RUN)
        if typ != "OK":
            log("Nelze otevřít INBOX.")
            sys.exit(1)
        uids = search_uids(imap, "SINCE", imap_date(since))
        log(f"V Inboxu za posledních {LOOKBACK_DAYS} dní: {len(uids)} mailů")

        messages = fetch_headers(imap, uids)
        plan, skipped = {}, 0
        for m in messages:
            label, reason = classify(m)
            if label:
                m["reason"] = reason
                plan.setdefault(label, []).append(m)
            else:
                skipped += 1

        log(f"K archivaci: {sum(len(v) for v in plan.values())} | ponecháno v Inboxu: {skipped}")
        archived = archive_and_label(imap, plan, existing)
        run_stats["archived"] = len(archived)
        verify_archived(imap, since, archived)

        # 2) Staré věci do koše
        run_stats["trashed"] = trash_old(imap, all_mail_raw)

        # 3) Report každých 14 dní
        if should_send_report():
            if DRY_RUN:
                log("\n[dry-run] report by se odeslal, ale dry run ho nepošle.")
            else:
                html = build_report(imap, all_mail_raw, run_stats)
                send_resend("Gmail Cleanup Agent - 14denní report", html)

        summary = (f"Archivováno: {run_stats['archived']} | "
                   f"Do koše: {run_stats['trashed']} | DRY_RUN={DRY_RUN}")
        log("\n" + summary)
        gh_summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if gh_summary:
            with open(gh_summary, "a", encoding="utf-8") as f:
                f.write(f"### Gmail Cleanup Agent\n\n{summary}\n")
    finally:
        try:
            imap.logout()
        except Exception:
            pass


if __name__ == "__main__":
    main()
