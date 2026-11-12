import os
import re
import json
import imaplib
import email
import urllib.request
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from email.utils import parseaddr

# ---------------------------------------------------------------
# Konfigurace z GitHub Secrets
# ---------------------------------------------------------------
USERNAME = os.environ.get("GMAIL_USER")
PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
IMAP_SERVER = "imap.gmail.com"
REPORT_FILE = "last_report.txt"
STATS_FILE = "stats.json"

# ---------------------------------------------------------------
# NASTAVENÍ
# ---------------------------------------------------------------
DRY_RUN = os.environ.get("DRY_RUN", "1") == "1"
SCAN_DAYS = 14                 # kolik dní zpět se prochází doručená
ARCHIVE_READ_AFTER_DAYS = 7    # přečtené maily starší než X dní se archivují
MAX_BATCH = 300                # max. počet zpráv na jedno třídění
MAX_ARCHIVE = 300              # max. počet starých přečtených mailů archivovaných za noc

# Odesílatelé, které se NIKDY nesmí automaticky měnit.
WHITELIST = [
    # "novak@",
]

# Celé domény nebo části odesílatelů pro okamžitý archiv + štítek Newslettery / Balast
NOISE_SENDERS = [
    "burgerking",
    "revolut",
    "playstation",
    "epicgames",
    "twillight",
]

# Pravidla podle odesílatele: (část adresy, štítek, archivovat z doručené?)
SENDER_RULES = [
    ("resend", "Automatizace", True),
    ("n8n", "Automatizace", True),
    ("sreality", "Bydleni", False),
    ("bezrealitky", "Bydleni", False),
    ("jobs.cz", "JobAgent", False),
]

# Reporty od tvých agentů
AGENT_RULES = [
    ("briefing", "Automatizace", False),
    ("job agent", "JobAgent", False),
    ("realitní makléř", "Bydleni", False),
    ("nhl report", "NHL agent", True),
    ("nhl ranní report", "NHL agent", True),
    ("dnešní zápasy", "Sport", True),
    ("sport dnes", "Sport", True),
    ("za hodinu:", "Sport", True),
]

# Reakce na inzeráty
APPLICATION_RE = re.compile(
    r"zařadili jsme vás|výběrov\w* řízení|hlásíte se do|potvrzení přijetí odpovědi|"
    r"reakce na inzerát|odpověď na (inzerát|pozici)|"
    r"děkujeme za (váš|vaši|vaše) (životopis|odpověď|reakci|zájem)",
    re.IGNORECASE,
)
APPLICATION_LABEL = "OdpovedNaInzeraty"

# GitHub
GITHUB_KEEP_RE = re.compile(
    r"pull request|review|approval|issue|security|action required|\bcode\b|failed",
    re.IGNORECASE,
)

# Pracovní portály
PORTAL_RE = re.compile(
    r"dalších nových pracovních pozic|máte shodu|odpovězte .+ nebo \d+ dalším",
    re.IGNORECASE,
)
PORTAL_LABEL = "PracovniPortaly"

# Finanční výpisy, banky, účty (Revolut výpisy apod.)
FINANCE_RE = re.compile(
    r"výpis z účtu|transakc|platb|faktur|vyúčtování|card payment|faktura",
    re.IGNORECASE,
)

# Klíčová slova pro nákupy, e-shopy, jídlo (Burger King atd.)
SHOPPING_RE = re.compile(
    r"objednávk|zásilkr|balík|balíček|vyzvednutí|doručen|faktura|burgerking|slev[ay]|kupon",
    re.IGNORECASE,
)
SHOPPING_LABEL = "Nakupy"

# Sociální sítě / notifikace
SOCIAL_RE = re.compile(
    r"friend request|sleduje vás|new message|nová zpráva|upozornění z",
    re.IGNORECASE,
)
SOCIAL_LABEL = "SocialniSiete"

# Záložní pravidla podle předmětu
KEYWORD_RULES = [
    (re.compile(r"\b(nhl|hokej)\b"), "NHL agent"),
    (re.compile(r"premier league|champions league|\bsport"), "Sport"),
    (re.compile(r"výběrové řízení|reakce na inzerát|odpověď na"), "OdpovedNaInzeraty"),
    (re.compile(r"\bbyt\b|\bbyty\b|realit|pronájem"), "Bydleni"),
    (FINANCE_RE, "Financee"),
    (SHOPPING_RE, "Nakupy"),
    (re.compile(r"job agent|brigád"), "JobAgent"),
]

# Bezpečnostní upozornění
SECURITY_RE = re.compile(
    r"security alert|zabezpečení|podezřel|změn[ay] hesla|verification code|"
    r"ověřovací kód|two-factor|nové přihlášení|new sign-in|"
    r"oauth application|third-party oauth|personal access token|ssh key|"
    r"verify your device|new device"
)

NOISE_LABEL = "Newslettery"
SECURITY_LABEL = "Alerty"

# MAZÁNÍ do koše: (hledání v Gmailu, po kolika dnech)
DELETE_RULES = [
    ("label:Newslettery", 7),
    ("label:PracovniPortaly", 14),
    ("label:Alerty", 14),
    ("label:OdpovedNaInzeraty", 60),
    ("label:Nakupy", 14), # Smaže staré notifikace z nákupů po 14 dnech
]


# ---------------------------------------------------------------
# Statistiky a reporty
# ---------------------------------------------------------------
def empty_stats():
    return {"checked": 0, "spam": 0, "labeled": 0, "expired": 0, "security": 0, "deleted": 0}

def load_stats():
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return empty_stats()

def save_stats(stats):
    with open(STATS_FILE, "w") as f:
        json.dump(stats, f)

def send_status_email(stats):
    if not RESEND_API_KEY:
        print("Resend API klíč není nastaven, stavový e-mail nelze odeslat.")
        return

    html_content = f"""
    <p>Ahoj Jáchyme,</p>
    <p>tady je tvůj 14denní přehled od Gmail Cleanup Agenta.</p>
    <ul>
        <li><b>Zkontrolováno zpráv:</b> {stats['checked']}</li>
        <li><b>Odklizeno newsletterů/balastu (archiv):</b> {stats['spam']}</li>
        <li><b>Automaticky oštítkováno:</b> {stats['labeled']}</li>
        <li><b>Archivováno starých přečtených:</b> {stats['expired']}</li>
        <li><b>Detekováno bezpečnostních alertů:</b> {stats['security']}</li>
        <li><b>Přesunuto do koše:</b> {stats.get('deleted', 0)}</li>
    </ul>
    <p>Pokračuji v hlídání na pozadí.<br>Tvůj Gmail Agent</p>
    """
    payload = {
        "from": "Gmail Cleaner Agent <onboarding@resend.dev>",
        "to": [USERNAME],
        "subject": "🟢 14denní statistika Gmail Cleanup Agenta",
        "html": html_content,
    }
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req):
            print("14denní statistický e-mail byl úspěšně odeslán.")
    except Exception as e:
        print(f"Chyba při odesílání e-mailu: {e}")

def check_and_send_periodic_report():
    last_date = None
    if os.path.exists(REPORT_FILE):
        with open(REPORT_FILE, "r") as f:
            content = f.read().strip()
            if content:
                try:
                    last_date = datetime.strptime(content, "%Y-%m-%d")
                except ValueError:
                    pass

    now = datetime.now()
    if not last_date or (now - last_date) >= timedelta(days=14):
        send_status_email(load_stats())
        save_stats(empty_stats())
        with open(REPORT_FILE, "w") as f:
            f.write(now.strftime("%Y-%m-%d"))

# ---------------------------------------------------------------
# Pomocné funkce pro IMAP a štítky
# ---------------------------------------------------------------
HEADER_FIELDS = "HEADER.FIELDS (FROM SUBJECT LIST-UNSUBSCRIBE PRECEDENCE)"

def ensure_label_exists(mail, label_name):
    """Zkontroluje, zda štítek v Gmailu existuje, a pokud ne, vytvoří ho."""
    if not label_name:
        return
    try:
        res, folders = mail.list()
        if res == "OK":
            for f in folders:
                decoded = f.decode("utf-8", errors="ignore")
                if f'"{label_name}"' in decoded or f'/{label_name}' in decoded or label_name in decoded:
                    return
        # Pokud štítek nebyl nalezen, vytvoříme ho pomocí XGMEXT / CREATE
        mail.create(f'"{label_name}"')
        print(f"📁 Vytvořen nový štítek v Gmailu: {label_name}")
    except Exception as e:
        # Ignorujeme, pokud už štítek existuje nebo ho nelze vytvořit
        pass

def chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]

def fetch_headers(mail, uid):
    res, data = mail.uid("fetch", uid, f"(BODY.PEEK[{HEADER_FIELDS}])")
    if res != "OK" or not data or not isinstance(data[0], tuple):
        return None
    return email.message_from_bytes(data[0][1])

def fetch_headers_batch(mail, uids, size=50):
    result = {}
    for part in chunks(uids, size):
        try:
            res, data = mail.uid(
                "fetch", b",".join(part).decode(), f"(UID BODY.PEEK[{HEADER_FIELDS}])"
            )
        except Exception as e:
            print(f"Chyba při hromadném stahování hlaviček: {e}")
            continue
        if res != "OK" or not data:
            continue
        for i, item in enumerate(data):
            if not isinstance(item, tuple):
                continue
            m = re.search(rb"UID (\d+)", item[0])
            if not m and i + 1 < len(data) and isinstance(data[i + 1], bytes):
                m = re.search(rb"UID (\d+)", data[i + 1])
            if m:
                result[m.group(1)] = email.message_from_bytes(item[1])
    return result

def decode_subject(msg):
    try:
        return str(make_header(decode_header(msg.get("Subject", "") or "")))
    except Exception:
        return msg.get("Subject", "") or ""

def classify(msg):
    sender = msg.get("From", "") or ""
    addr = parseaddr(sender)[1].lower()
    subject = decode_subject(msg)
    subj = subject.lower()

    if any(w in addr for w in WHITELIST):
        return None

    if SECURITY_RE.search(subj):
        return {"kind": "security", "label": SECURITY_LABEL, "archive": False,
                "read": False, "subject": subject}

    # Okamžitý balast / noise senders (Revolut, Burger King, PlayStation atd.)
    if any(p in addr for p in NOISE_SENDERS):
        return {"kind": "noise", "label": NOISE_LABEL, "archive": True,
                "read": True, "subject": subject}

    if "github.com" in addr or "notifications@github" in addr:
        is_important = bool(GITHUB_KEEP_RE.search(subj))
        return {
            "kind": "labeled",
            "label": "GitHub",
            "archive": not is_important,
            "read": not is_important,
            "subject": subject,
        }

    if APPLICATION_RE.search(subj):
        return {"kind": "labeled", "label": APPLICATION_LABEL, "archive": True,
                "read": False, "subject": subject}

    for word, label, archive in AGENT_RULES:
        if word in subj:
            return {"kind": "labeled", "label": label, "archive": archive,
                    "read": archive, "subject": subject}

    for part, label, archive in SENDER_RULES:
        if part in addr:
            return {"kind": "labeled", "label": label, "archive": archive,
                    "read": archive, "subject": subject}

    if PORTAL_RE.search(subj):
        return {"kind": "noise", "label": PORTAL_LABEL, "archive": True,
                "read": True, "subject": subject}

    precedence = str(msg.get("Precedence", "")).lower()
    if msg.get("List-Unsubscribe") or precedence in ("bulk", "list", "junk"):
        # Pokud je to zjevný newsletter s odhlašovacím tlačítkem, šoupneme do Newsletterů
        return {"kind": "noise", "label": NOISE_LABEL, "archive": True,
                "read": True, "subject": subject}

    for rx, label in KEYWORD_RULES:
        if rx.search(subj) or rx.search(addr):
            # Nákupy a Finance necháme v doručené nebo archivujeme podle potřeby (zde False = zůstává v doručené do přečtení)
            return {"kind": "labeled", "label": label, "archive": False,
                    "read": False, "subject": subject}

    return None

def batch_store(mail, uids, command, value, size=100):
    failed = 0
    for part in chunks(uids, size):
        try:
            res, _ = mail.uid("store", b",".join(part).decode(), command, value)
        except Exception as e:
            print(f"Chyba při hromadné změně: {e}")
            failed += len(part)
            continue
        if res != "OK":
            failed += len(part)
    return failed

def still_in_selected(mail, uids):
    remaining = []
    for part in chunks(uids, 100):
        res, data = mail.uid("search", None, "UID", b",".join(part).decode())
        if res == "OK" and data and data[0]:
            remaining.extend(data[0].split())
    return remaining

def archive_uids(mail, uids):
    if not uids:
        return 0
    batch_store(mail, uids, "-X-GM-LABELS", "(\\Inbox)")
    mail.noop()
    left = still_in_selected(mail, uids)
    if left:
        batch_store(mail, left, "+FLAGS", "(\\Deleted)")
        for part in chunks(left, 100):
            try:
                mail.uid("expunge", b",".join(part).decode())
            except Exception as e:
                print(f"Chyba při odstranění z doručené: {e}")
        mail.noop()
        left = still_in_selected(mail, uids)
    return len(left)

def apply_groups(mail, groups):
    if DRY_RUN:
        return 0
    failed = 0
    for (label, archive, read, flag), uids in groups.items():
        if label:
            ensure_label_exists(mail, label)
            failed += batch_store(mail, uids, "+X-GM-LABELS", f'("{label}")')
        if read:
            failed += batch_store(mail, uids, "+FLAGS", "(\\Seen)")
        if flag:
            failed += batch_store(mail, uids, "+FLAGS", "(\\Flagged)")
        if archive:
            failed += archive_uids(mail, uids)
    return failed

def find_folder(mail, special_use):
    res, folders = mail.list()
    if res != "OK":
        return None
    for raw in folders:
        line = raw.decode("utf-8", errors="ignore")
        if special_use in line:
            return line.split(' "/" ')[-1].strip().strip('"')
    return None

def find_all_mail(mail):
    return find_folder(mail, "\\All")

def search_raw(mail, query):
    res, data = mail.uid("search", None, "X-GM-RAW", query)
    return data[0].split() if res == "OK" and data and data[0] else []

def trash_uids(mail, uids, query, trash_folder):
    batch_store(mail, uids, "+X-GM-LABELS", "(\\Trash)")
    mail.noop()
    wanted = set(uids)
    left = [u for u in search_raw(mail, query) if u in wanted]
    if left and trash_folder:
        for part in chunks(left, 100):
            try:
                mail.uid("copy", b",".join(part).decode(), f'"{trash_folder}"')
            except Exception as e:
                print(f"Chyba při přesunu do koše: {e}")
        mail.noop()
        left = [u for u in search_raw(mail, query) if u in wanted]
    return len(left)

def imap_date(dt):
    return dt.strftime("%d-%b-%Y")

# ---------------------------------------------------------------
# Hlavní úklid
# ---------------------------------------------------------------
def clean_gmail():
    mode = "DRY RUN (nic se nemění)" if DRY_RUN else "ostrý režim"
    print(f"Připojuji se k Gmailu... [{mode}]")
    mail = imaplib.IMAP4_SSL(IMAP_SERVER)
    mail.login(USERNAME, PASSWORD)
    mail.select("INBOX")

    stats = load_stats()
    failed_total = 0

    since = imap_date(datetime.now() - timedelta(days=SCAN_DAYS))
    res, data = mail.uid("search", None, "UNFLAGGED", "SINCE", since)
    uids = data[0].split()[-MAX_BATCH:] if res == "OK" else []
    print(f"Zpracovávám {len(uids)} zpráv z posledních {SCAN_DAYS} dní.")

    headers = fetch_headers_batch(mail, uids)
    groups = {}

    for uid in uids:
        try:
            stats["checked"] += 1
            msg = headers.get(uid) or fetch_headers(mail, uid)
            if msg is None:
                continue
            action = classify(msg)
            if action is None:
                continue

            key = (
                action.get("label"),
                action.get("archive", False),
                action.get("read", False),
                action.get("flag", False),
            )
            groups.setdefault(key, []).append(uid)

            kind = action["kind"]
            subject = action["subject"]
            if kind == "security":
                stats["security"] += 1
                print(f"⚠️ Bezpečnostní alert: '{subject}'")
            elif kind == "noise":
                stats["spam"] += 1
                print(f"📪 {action['label']} -> archiv: '{subject}'")
            else:
                stats["labeled"] += 1
                where = "archiv" if action.get("archive") else "zůstává v doručené"
                print(f"🏷️ {action['label']} ({where}): '{subject}'")
        except Exception as e:
            print(f"Chyba při zpracování zprávy UID {uid}: {e}")

    failed_total += apply_groups(mail, groups)

    cutoff = imap_date(datetime.now() - timedelta(days=ARCHIVE_READ_AFTER_DAYS))
    res, data = mail.uid("search", None, "SEEN", "UNFLAGGED", "BEFORE", cutoff)
    old_uids = data[0].split()[:MAX_ARCHIVE] if res == "OK" else []
    print(f"Starých přečtených k archivaci: {len(old_uids)}")
    if old_uids and not DRY_RUN:
        failed = archive_uids(mail, old_uids)
        failed_total += failed
        stats["expired"] += len(old_uids) - failed

    all_mail = find_all_mail(mail)
    trash_folder = find_folder(mail, "\\Trash")
    if all_mail:
        mail.select(f'"{all_mail}"')
        for query, days in DELETE_RULES:
            q = f'"{query} older_than:{days}d -is:starred"'
            found = search_raw(mail, q)[:MAX_BATCH]
            print(f"Mazání [{query}, starší než {days} dní]: {len(found)} zpráv")
            if DRY_RUN:
                for uid in found[:20]:
                    msg = fetch_headers(mail, uid)
                    if msg is not None:
                        print(f"   🗑️ by šlo do koše: '{decode_subject(msg)}'")
            elif found:
                failed = trash_uids(mail, found, q, trash_folder)
                failed_total += failed
                stats["deleted"] = stats.get("deleted", 0) + len(found) - failed
    else:
        print("Složka Všechny zprávy nenalezena, mazání přeskočeno.")

    mail.logout()

    if DRY_RUN:
        print("Hotovo (zkouška): nic se nezměnilo, statistiky se neukládají a report se neposílá.")
        return

    if failed_total:
        print(f"⚠️ Nepodařilo se provést {failed_total} změn.")
    else:
        print("✅ Všechny změny ověřeny.")

    save_stats(stats)
    print("Hotovo.")
    check_and_send_periodic_report()

if __name__ == "__main__":
    clean_gmail()
