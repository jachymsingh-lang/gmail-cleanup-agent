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
SCAN_DAYS = 14
ARCHIVE_READ_AFTER_DAYS = 7
MAX_BATCH = 300
MAX_ARCHIVE = 300

WHITELIST = []

# Balast, který jde okamžitě do archivu a štítku Newslettery
NOISE_SENDERS = [
    "burgerking",
    "revolut",
    "playstation",
    "epicgames",
    "twillight",
    "noreply",
]

SENDER_RULES = [
    ("resend", "Automatizace", True),
    ("n8n", "Automatizace", True),
    ("sreality", "Bydleni", False),
    ("bezrealitky", "Bydleni", False),
    ("jobs.cz", "JobAgent", False),
]

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

APPLICATION_RE = re.compile(
    r"zařadili jsme vás|výběrov\w* řízení|hlásíte se do|potvrzení přijetí odpovědi|"
    r"reakce na inzerát|odpověď na (inzerát|pozici)|"
    r"děkujeme za (váš|vaši|vaše) (životopis|odpověď|reakci|zájem)",
    re.IGNORECASE,
)
APPLICATION_LABEL = "OdpovedNaInzeraty"

GITHUB_KEEP_RE = re.compile(
    r"pull request|review|approval|issue|security|action required|\bcode\b|failed",
    re.IGNORECASE,
)

PORTAL_RE = re.compile(
    r"dalších nových pracovních pozic|máte shodu|odpovězte .+ nebo \d+ dalším",
    re.IGNORECASE,
)

FINANCE_RE = re.compile(
    r"výpis z účtu|transakc|platb|faktur|vyúčtování|card payment|faktura",
    re.IGNORECASE,
)

SHOPPING_RE = re.compile(
    r"objednávk|zásilkr|balík|balíček|vyzvednutí|doručen|faktura|burgerking|slev[ay]|kupon",
    re.IGNORECASE,
)

KEYWORD_RULES = [
    (re.compile(r"\b(nhl|hokej)\b", re.IGNORECASE), "NHL agent"),
    (re.compile(r"premier league|champions league|\bsport", re.IGNORECASE), "Sport"),
    (re.compile(r"výběrové řízení|reakce na inzerát|odpověď na", re.IGNORECASE), "OdpovedNaInzeraty"),
    (re.compile(r"\bbyt\b|\bbyty\b|realit|pronájem", re.IGNORECASE), "Bydleni"),
    (FINANCE_RE, "Financee"),
    (SHOPPING_RE, "Nakupy"),
    (re.compile(r"job agent|brigád", re.IGNORECASE), "JobAgent"),
]

SECURITY_RE = re.compile(
    r"security alert|zabezpečení|podezřel|změn[ay] hesla|verification code|"
    r"ověřovací kód|two-factor|nové přihlášení|new sign-in|"
    r"oauth application|third-party oauth|personal access token|ssh key|"
    r"verify your device|new device",
    re.IGNORECASE
)

NOISE_LABEL = "Newslettery"
SECURITY_LABEL = "Alerty"

DELETE_RULES = [
    ("label:Newslettery", 7),
    ("label:PracovniPortaly", 14),
    ("label:Alerty", 14),
    ("label:OdpovedNaInzeraty", 60),
    ("label:Nakupy", 14),
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
        return
    html_content = f"""
    <p>Ahoj Jáchyme,</p>
    <p>tady je tvůj přehled od Gmail Cleanup Agenta.</p>
    <ul>
        <li><b>Zkontrolováno zpráv:</b> {stats['checked']}</li>
        <li><b>Odklizeno balastu (archiv):</b> {stats['spam']}</li>
        <li><b>Oštítkováno:</b> {stats['labeled']}</li>
        <li><b>Smazáno do koše:</b> {stats.get('deleted', 0)}</li>
    </ul>
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
            pass
    except Exception:
        pass

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
# Pomocné funkce pro IMAP
# ---------------------------------------------------------------
HEADER_FIELDS = "HEADER.FIELDS (FROM SUBJECT LIST-UNSUBSCRIBE PRECEDENCE)"

def ensure_label_exists(mail, label_name):
    if not label_name:
        return
    try:
        mail.create(f'"{label_name}"')
    except Exception:
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
        except Exception:
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
        return {"kind": "security", "label": SECURITY_LABEL, "archive": False, "read": False, "subject": subject}

    if any(p in addr for p in NOISE_SENDERS) or PORTAL_RE.search(subj):
        return {"kind": "noise", "label": NOISE_LABEL, "archive": True, "read": True, "subject": subject}

    if "github.com" in addr or "notifications@github" in addr:
        is_important = bool(GITHUB_KEEP_RE.search(subj))
        return {"kind": "labeled", "label": "GitHub", "archive": not is_important, "read": not is_important, "subject": subject}

    if APPLICATION_RE.search(subj):
        return {"kind": "labeled", "label": APPLICATION_LABEL, "archive": True, "read": False, "subject": subject}

    for word, label, archive in AGENT_RULES:
        if word in subj:
            return {"kind": "labeled", "label": label, "archive": archive, "read": archive, "subject": subject}

    for part, label, archive in SENDER_RULES:
        if part in addr:
            return {"kind": "labeled", "label": label, "archive": archive, "read": archive, "subject": subject}

    precedence = str(msg.get("Precedence", "")).lower()
    if msg.get("List-Unsubscribe") or precedence in ("bulk", "list", "junk"):
        return {"kind": "noise", "label": NOISE_LABEL, "archive": True, "read": True, "subject": subject}

    for rx, label in KEYWORD_RULES:
        if rx.search(subj) or rx.search(addr):
            return {"kind": "labeled", "label": label, "archive": True, "read": True, "subject": subject}

    return None

def batch_store(mail, uids, command, value):
    for part in chunks(uids, 100):
        try:
            mail.uid("store", b",".join(part).decode(), command, value)
        except Exception:
            pass

def archive_uids(mail, uids):
    if not uids:
        return
    batch_store(mail, uids, "-X-GM-LABELS", "(\\Inbox)")

def apply_groups(mail, groups):
    if DRY_RUN:
        return
    for (label, archive, read, flag), uids in groups.items():
        if label:
            ensure_label_exists(mail, label)
            batch_store(mail, uids, "+X-GM-LABELS", f'("{label}")')
        if read:
            batch_store(mail, uids, "+FLAGS", "(\\Seen)")
        if archive:
            archive_uids(mail, uids)

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

def imap_date(dt):
    return dt.strftime("%d-%b-%Y")

# ---------------------------------------------------------------
# Hlavní úklid
# ---------------------------------------------------------------
def clean_gmail():
    print(f"Připojuji se k Gmailu...")
    mail = imaplib.IMAP4_SSL(IMAP_SERVER)
    mail.login(USERNAME, PASSWORD)
    mail.select("INBOX")

    stats = load_stats()
    since = imap_date(datetime.now() - timedelta(days=SCAN_DAYS))
    res, data = mail.uid("search", None, "SINCE", since)
    uids = data[0].split()[-MAX_BATCH:] if res == "OK" and data and data[0] else []
    print(f"Zpracovávám {len(uids)} zpráv z Inboxu.")

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
                action.get("archive", True),
                action.get("read", True),
                False,
            )
            groups.setdefault(key, []).append(uid)
            stats["labeled"] += 1
            if action.get("kind") == "noise":
                stats["spam"] += 1
        except Exception as e:
            print(f"Chyba u UID {uid}: {e}")

    apply_groups(mail, groups)

    all_mail = find_all_mail(mail)
    trash_folder = find_folder(mail, "\\Trash")
    if all_mail:
        mail.select(f'"{all_mail}"')
        for query, days in DELETE_RULES:
            q = f'"{query} older_than:{days}d -is:starred"'
            found = search_raw(mail, q)[:MAX_BATCH]
            if found:
                trash_uids(mail, found, q, trash_folder)
                stats["deleted"] = stats.get("deleted", 0) + len(found)

    mail.logout()
    save_stats(stats)
    print("Hotovo, schránka vyčištěna.")
    check_and_send_periodic_report()

if __name__ == "__main__":
    clean_gmail()
