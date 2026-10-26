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

def send_status_email():
    if not RESEND_API_KEY:
        print("Resend API klíč není nastaven, stavový e-mail nelze odeslat.")
        return

    url = "https://api.resend.com/emails"
    payload = {
        "from": "Gmail Cleaner Agent <onboarding@resend.dev>",
        "to": [USERNAME],
        "subject": "🟢 Gmail Cleanup Agent - Vše funguje a je OK",
        "html": "<p>Ahoj Jáchyme,</p><p>hlásím se po 14 dnech. Tvůj Gmail Cleanup Agent běží bez problému, schránka je pod kontrolou a všechno je OK!</p>"
    }
    
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json"
    })
    
    try:
        with urllib.request.urlopen(req) as response:
            print("Stavový e-mail (14-denní hlášení) byl úspěšně odeslán.")
    except Exception as e:
        print(f"Chyba při odesílání stavového e-mailu: {e}")

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
    # Pokud soubor neexistuje nebo uběhlo 14 a více dnů
    if not last_date or (now - last_date) >= timedelta(days=14):
        send_status_email()
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

    spam_keywords = ["unsubscribe", "odhlásit", "slevová akce", "vyhra", "kredit zdarma", "casino", "poptávka zdarma", "newsletter"]

    for mail_id in mail_ids:
        try:
            res, msg_data = mail.fetch(mail_id, '(RFC822)')
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    
                    subject, encoding = decode_header(msg["Subject"])[0]
                    if isinstance(subject, bytes):
                        subject = subject.decode(encoding or "utf-8", errors="ignore")
                    
                    sender = msg.get("From")
                    
                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() == "text/plain":
                                body = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                                break
                    else:
                        body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")

                    body_lower = (subject + " " + body).lower()
                    is_trash = any(keyword in body_lower for keyword in spam_keywords)
                    
                    if is_trash:
                        print(f"Detekován balast / spam: '{subject}' od {sender}")
                        mail.store(mail_id, '+X-GM-LABELS', '\\Trash')
                        print(f"-> Přesunuto do koše.")
                    else:
                        print(f"Zpráva v pořádku (ponechána): '{subject}'")

        except Exception as e:
            print(f"Chyba při zpracování zprávy ID {mail_id}: {e}")

    mail.logout()
    print("Úklid Gmailu dokončen.")
    
    # Zkontrolujeme a případně odešleme 14denní hlášení
    check_and_send_periodic_report()

if __name__ == "__main__":
    clean_gmail()
