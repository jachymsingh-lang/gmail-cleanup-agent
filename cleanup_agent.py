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
MAX_BATCH = 300                # max. počet zpráv na jedno spuštění
 
# Odesílatelé (část adresy), které se NIKDY nesmí automaticky měnit.
WHITELIST = [
    # "novak@",
    # "oavinohrady",
]
 
# Odesílatelé, které nechceš vidět: archiv + přečteno + štítek "Newslettery".
NOISE_SENDERS = [
    # "tipli",
    # "newsletter@",
]
 
# (část adresy odesílatele, štítek, archivovat z doručené?)
SENDER_RULES = [
    ("resend", "Automatizace", True),
    ("n8n", "Automatizace", True),
    ("sreality", "bydleni", False),
    ("bezrealitky", "bydleni", False),
    ("jobs.cz", "JobAgent", False),
]
 
# Klíčová slova v předmětu/obsahu GitHubu, která CHCEŠ nechat v doručené (kódy, PR, akce k řešení)
GITHUB_KEEP_RE = re.compile(r"pull request|review|approval|issue|security|action required|\bcode\b", re.IGNORECASE)
 
# Záložní pravidla podle předmětu (jen štítek, nikdy nearchivují).
KEYWORD_RULES = [
    (re.compile(r"\b(nhl|hokej)\b"), "NHL agent"),
    (re.compile(r"premier league|champions league|\bsport"), "Sport"),
    (re.compile(r"výběrové řízení|reakce na inzerát|odpověď na"), "OdpovedNaInzeraty"),
    (re.compile(r"\bbyt\b|\bbyty\b|realit|pronájem"), "bydleni"),
    (re.compile(r"platb|faktur|transakc"), "financee"),
    (re.compile(r"job agent|brigád"), "JobAgent"),
]
 
# Bezpečnostní upozornění: označí se hvězdičkou a zůstanou v doručené.
SECURITY_RE = re.compile(
    r"security alert|zabezpečení|podezřel|změn[ay] hesla|verification code|"
    r"ověřovací kód|two-factor|nové přihlášení|new sign-in"
)
 
NOISE_LABEL = "Newslettery"
SECURITY_LABEL = "Alerty"
 
# MAZÁNÍ: (hledání v Gmailu, smazat po kolika dnech)
DELETE_RULES = [
    ("label:Newslettery", 7),
    ("label:Alerty", 14),
    ("label:OdpovedNaInzeraty", 60),
]
 
 
# ---------------------------------------------------------------
# Statistiky a 14denní report
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
# Pomocné funkce
# ---------------------------------------------------------------
def fetch_headers(mail, uid):
    res, data = mail.uid(
        "fetch",
        uid,
        "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT LIST-UNSUBSCRIBE PRECEDENCE)])",
    )
    if res != "OK" or not data or not isinstance(data[0], tuple):
        return None
    return email.message_from_bytes(data[0][1])
 
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
 
    if any(p in addr for p in NOISE_SENDERS):
        return {"kind": "noise", "label": NOISE_LABEL, "archive": True,
                "read": True, "subject": subject}
 
    # Speciální pravidlo pro GitHub: běžné notifikace se archivují, důležité s kódem/PR zůstanu v doručené
    if "github.com" in addr or "notifications@github" in addr:
        is_important_code = bool(GITHUB_KEEP_RE.search(subj))
        return {
            "kind": "labeled", 
            "label": "GitHub", 
            "archive": not is_important_code, # Pokud je to kód/PR, nearchivuje se (zůstane v Inboxu)
            "read": not is_important_code,
            "subject": subject
        }
 
    for part, label, archive in SENDER_RULES:
        if part in addr:
            return {"kind": "labeled", "label": label, "archive": archive,
                    "read": archive, "subject": subject}
 
    precedence = str(msg.get("Precedence", "")).lower()
    if msg.get("List-Unsubscribe") or precedence in ("bulk", "list", "junk"):
        return {"kind": "noise", "label": NOISE_LABEL, "archive": True,
                "read": True, "subject": subject}
 
    for rx, label in KEYWORD_RULES:
        if rx.search(subj):
            return {"kind": "labeled", "label": label, "archive": False,
                    "read": False, "subject": subject}
 
    return None
 
def apply_action(mail, uid, label=None, archive=False, read=False, flag=False):
    if DRY_RUN:
        return
    if label:
        mail.uid("store", uid, "+X-GM-LABELS", f'("{label}")')
    if archive:
        mail.uid("store", uid, "-X-GM-LABELS", "(\\Inbox)")
    if read:
        mail.uid("store", uid, "+FLAGS", "(\\Seen)")
    if flag:
        mail.uid("store", uid, "+FLAGS", "(\\Flagged)")
 
def find_all_mail(mail):
    res, folders = mail.list()
    if res != "OK":
        return None
    for raw in folders:
        line = raw.decode("utf-8", errors="ignore")
        if "\\All" in line:
            return line.split(' "/" ')[-1].strip().strip('"')
    return None
 
def trash(mail, uid):
    if DRY_RUN:
        return
    mail.uid("store", uid, "+X-GM-LABELS", "(\\Trash)")
 
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
 
    since = imap_date(datetime.now() - timedelta(days=SCAN_DAYS))
    res, data = mail.uid("search", None, "UNFLAGGED", "SINCE", since)
    uids = data[0].split()[-MAX_BATCH:] if res == "OK" else []
    print(f"Zpracovávám {len(uids)} zpráv z posledních {SCAN_DAYS} dní.")
 
    for uid in uids:
        try:
            stats["checked"] += 1
            msg = fetch_headers(mail, uid)
            if msg is None:
                continue
            action = classify(msg)
            if action is None:
                continue
 
            kind = action["kind"]
            subject = action["subject"]
            apply_action(
                mail, uid,
                label=action.get("label"),
                archive=action.get("archive", False),
                read=action.get("read", False),
                flag=action.get("flag", False),
            )
            if kind == "security":
                stats["security"] += 1
                print(f"⚠️ Bezpečnostní alert: '{subject}'")
            elif kind == "noise":
                stats["spam"] += 1
                print(f"📪 Newsletter/balast -> archiv: '{subject}'")
            else:
                stats["labeled"] += 1
                where = "archiv" if action.get("archive") else "zůstává v doručené"
                print(f"🏷️ {action['label']} ({where}): '{subject}'")
        except Exception as e:
            print(f"Chyba při zpracování zprávy UID {uid}: {e}")
 
    cutoff = imap_date(datetime.now() - timedelta(days=ARCHIVE_READ_AFTER_DAYS))
    res, data = mail.uid("search", None, "SEEN", "UNFLAGGED", "BEFORE", cutoff)
    old_uids = data[0].split()[:MAX_BATCH] if res == "OK" else []
    print(f"Starých přečtených k archivaci: {len(old_uids)}")
    for uid in old_uids:
        try:
            apply_action(mail, uid, archive=True)
            stats["expired"] += 1
        except Exception as e:
            print(f"Chyba při archivaci UID {uid}: {e}")
 
    all_mail = find_all_mail(mail)
    if all_mail:
        mail.select(f'"{all_mail}"')
        for query, days in DELETE_RULES:
            q = f'"{query} older_than:{days}d -is:starred"'
            res, data = mail.uid("search", None, "X-GM-RAW", q)
            found = data[0].split()[:MAX_BATCH] if res == "OK" else []
            print(f"Mazání [{query}, starší než {days} dní]: {len(found)} zpráv")
            for i, uid in enumerate(found):
                try:
                    if DRY_RUN and i < 20:
                        msg = fetch_headers(mail, uid)
                        if msg is not None:
                            print(f"   🗑️ by šlo do koše: '{decode_subject(msg)}'")
                    trace_trash = trash(mail, uid)
                    stats["deleted"] = stats.get("deleted", 0) + 1
                except Exception as e:
                    print(f"Chyba při mazání UID {uid}: {e}")
    else:
        print("Složka Všechny zprávy nenalezena, mazání přeskočeno.")
 
    save_stats(stats)
    mail.logout()
    print("Hotovo.")
    check_and_send_periodic_report()
 
if __name__> "__main__":
    clean_gmail()
