import os
import imaplib
import email
from email.header import decode_header
from datetime import datetime, timedelta
import urllib.request
import json

# Konfigurace z GitHub Secrets
USERNAME = os.environ.get("GMAIL_USER")
PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
IMAP_SERVER = "imap.gmail.com"
REPORT_FILE = "last_report.txt"
STATS_FILE = "stats.json"

def load_stats():
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {"checked": 0, "spam": 0, "labeled": 0, "expired": 0, "security": 0}

def save_stats(stats):
    with open(STATS_FILE, "w") as f:
        json.dump(stats, f)

def reset_stats():
    return {"checked": 0, "spam": 0, "labeled": 0, "expired": 0, "security": 0}

def send_status_email(stats):
    if not RESEND_API_KEY:
        print("Resend API klíč není nastaven, stavový e-mail nelze odeslat.")
        return

    url = "https://api.resend.com/emails"
    html_content = f"""
    <p>Ahoj Jáchyme,</p>
    <p>tady je tvůj 14denní přehled od Gmail Cleanup Agenta. Všechno funguje a schránka je pod kontrolou!</p>
    <ul>
        <li><b>Zkontrolováno zpráv:</b> {stats['checked']}</li>
        <li><b>Uklizeno spamů/balastu:</b> {stats['spam']}</li>
        <li><b>Automaticky oštítkováno:</b> {stats['labeled']}</li>
        <li><b>Expirováno starých notifikací:</b> {stats['expired']}</li>
        <li><b>Detekováno bezpečnostních alertů:</b> {stats['security']}</li>
    </ul>
    <p>Pokračuji v hlídání na pozadí. S pozdravem,<br>Tvůj Gmail Agent</p>
    """

    payload = {
        "from": "Gmail Cleaner Agent <onboarding@resend.dev>",
        "to": [USERNAME],
        "subject": "🟢 14denní statistika Gmail Cleanup Agenta",
        "html": html_content
    }
    
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json"
    })
    
    try:
        with urllib.request.urlopen(req) as response:
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
        stats = load_stats()
        send_status_email(stats)
        
        # Reset statistik a uložení nového data hlášení
        save_stats(reset_stats())
        with open(REPORT_FILE, "w") as f:
            f.write(now.strftime("%Y-%m-%d"))

def clean_gmail():
    print("Pripojuji se k Gmailu...")
    mail = imaplib.IMAP4_SSL(IMAP_SERVER)
    mail.login(USERNAME, PASSWORD)
    
    mail.select("INBOX")
    status, messages = mail.search(None, '(UNSEEN)')
    if status != 'OK':
        print("Zadne nove zpravy k zkontrolovani.")
        mail.logout()
        check_and_send_periodic_report()
        return

    mail_ids = messages[0].split()
    print(f"Nalezeno {len(mail_ids)} nepřečtených zpráv k analýze.")

    stats = load_stats()
    spam_keywords = ["unsubscribe", "odhlásit", "slevová akce", "vyhra", "kredit zdarma", "casino", "poptávka zdarma", "newsletter"]
    security_keywords = ["security alert", "podezřelé", "změna hesla", "verification code", "overeni", "login", "two-factor"]

    for mail_id in mail_ids:
        try:
            stats["checked"] += 1
            res, msg_data = mail.fetch(mail_id, '(RFC822)')
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    
                    subject, encoding = decode_header(msg["Subject"])[0]
                    if isinstance(subject, bytes):
                        subject = subject.decode(encoding or "utf-8", errors="ignore")
                    
                    sender = msg.get("From", "")
                    
                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() == "text/plain":
                                body = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                                break
                    else:
                        body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")

                    body_lower = (subject + " " + body).lower()
                    
                    # 1. Bezpečnostní hlídač (Security Watchdog)
                    if any(kw in body_lower for kw in security_keywords):
                        print(f"⚠️ Detekován bezpečnostní alert: '{subject}'")
                        mail.store(mail_id, '+FLAGS', '\\Flagged') # Přidá hvězdičku
                        stats["security"] += 1
                        continue

                    # 2. Detekce spamu a balastu
                    is_trash = any(keyword in body_lower for keyword in spam_keywords)
                    if is_trash:
                        print(f"Detekován balast / spam: '{subject}'")
                        mail.store(mail_id, '+X-GM-LABELS', '\\Trash')
                        stats["spam"] += 1
                    else:
                        # 3. Automatické štítkování
                        sender_lower = sender.lower()
                        if "github" in sender_lower or "github" in subject.lower():
                            mail.store(mail_id, '+X-GM-LABELS', 'GitHub')
                            stats["labeled"] += 1
                            print(f"-> Oštítkováno jako GitHub")
                        elif "job agent" in subject.lower():
                            mail.store(mail_id, '+X-GM-LABELS', 'Job Agent')
                            stats["labeled"] += 1
                            print(f"-> Oštítkováno jako Job Agent")
                        elif "sport" in subject.lower() or "resend" in sender_lower:
                            mail.store(mail_id, '+X-GM-LABELS', 'Sport')
                            stats["labeled"] += 1
                            print(f"-> Oštítkováno jako Sport")

        except Exception as e:
            print(f"Chyba při zpracování zprávy ID {mail_id}: {e}")

    # 4. Auto-Expire: Archivace starých notifikací (starších než 14 dnů)
    try:
        date_limit = (datetime.now() - timedelta(days=14)).strftime("%d-%b-%Y")
        # Hledáme staré zprávy s příznakem GitHub/Notifikace
        status_old, old_msgs = mail.search(None, f'(BEFORE {date_limit})')
        if status_old == 'OK' and old_msgs[0]:
            old_ids = old_msgs[0].split()
            for old_id in old_ids[:20]: # Omezení na dávku 20 zpráv pro plynulost
                mail.store(old_id, '+X-GM-LABELS', '\\Archived') # Odebere z doručené pošty
                stats["expired"] += len(old_ids)
    except Exception as e:
        print(f"Chyba při auto-expire starých zpráv: {e}")

    save_stats(stats)
    mail.logout()
    print("Úklid Gmailu a aktualizace statistik dokončena.")
    
    # Kontrola a případné odeslání 14denního hlášení
    check_and_send_periodic_report()

if __name__ == "__main__":
    clean_gmail()
