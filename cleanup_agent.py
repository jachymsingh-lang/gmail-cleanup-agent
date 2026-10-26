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
    <p>tady je tvůj 14denní přehled od Gmail Cleanup Agenta. Schránka je pod kontrolou!</p>
    <ul>
        <li><b>Zkontrolováno zpráv:</b> {stats['checked']}</li>
        <li><b>Uklizeno spamů/balastu:</b> {stats['spam']}</li>
        <li><b>Automaticky oštítkováno:</b> {stats['labeled']}</li>
        <li><b>Expirováno starých notifikací:</b> {stats['expired']}</li>
        <li><b>Detekováno bezpečnostních alertů:</b> {stats['security']}</li>
    </ul>
    <p>Pokračuji v hlídání na pozadí.<br>Tvůj Gmail Agent</p>
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
    print(f"Nalezeno celkem {len(mail_ids)} nepřečtených zpráv.")

    # Zpracujeme v jedné dávce maximálně 300 zpráv pro rychlý a bezpečný průběh
    batch_ids = mail_ids[:300]
    print(f"Zpracovávám tuto dávku: {len(batch_ids)} zpráv.")

    stats = load_stats()
    spam_keywords = ["unsubscribe", "odhlásit", "slevová akce", "vyhra", "kredit zdarma", "casino", "poptávka zdarma", "newsletter"]
    security_keywords = ["security alert", "podezřelé", "změna hesla", "verification code", "overeni", "login", "two-factor"]

    for mail_id in batch_ids:
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
                    sender_lower = sender.lower()
                    
                    # 1. Bezpečnostní hlídač
                    if any(kw in body_lower for kw in security_keywords):
                        print(f"⚠️ Bezpečnostní alert: '{subject}'")
                        mail.store(mail_id, '+FLAGS', '\\Flagged')
                        stats["security"] += 1
                        continue

                    # 2. Spam / Balast -> do koše
                    is_trash = any(keyword in body_lower for keyword in spam_keywords)
                    if is_trash:
                        print(f"🗑️ Balast: '{subject}'")
                        mail.store(mail_id, '+X-GM-LABELS', '\\Trash')
                        stats["spam"] += 1
                    else:
                        # 3. Inteligentní štítkování podle tvých přesných názvů
                        labeled = False
                        
                        if "github" in sender_lower or "github" in subject.lower():
                            mail.store(mail_id, '+X-GM-LABELS', '"gitHub"')
                            labeled = True
                            print(f"🏷️ Oštítkováno: gitHub")
                        elif "job agent" in subject.lower() or "pracovní" in body_lower or "jobs.cz" in body_lower:
                            mail.store(mail_id, '+X-GM-LABELS', '"JobAgent"')
                            labeled = True
                            print(f"🏷️ Oštítkováno: JobAgent")
                        elif "nhl" in body_lower or "hokej" in body_lower or "montreal" in body_lower or "calgary" in body_lower or "boston" in body_lower:
                            mail.store(mail_id, '+X-GM-LABELS', '"NHL agent"')
                            labeled = True
                            print(f"🏷️ Oštítkováno: NHL agent")
                        elif "sport" in subject.lower() or "premier league" in body_lower or "champions league" in body_lower:
                            mail.store(mail_id, '+X-GM-LABELS', '"Sport"')
                            labeled = True
                            print(f"🏷️ Oštítkováno: Sport")
                        elif "odpověď" in body_lower or "reakce na inzerát" in body_lower or "výběrové řízení" in body_lower:
                            mail.store(mail_id, '+X-GM-LABELS', '"OdpovedNaInzeraty"')
                            labeled = True
                            print(f"🏷️ Oštítkováno: OdpovedNaInzeraty")
                        elif "byt" in body_lower or "realit" in body_lower or "sreality" in sender_lower or "pronájem" in body_lower:
                            mail.store(mail_id, '+X-GM-LABELS', '"bydleni"')
                            labeled = True
                            print(f"🏷️ Oštítkováno: bydleni")
                        elif "platba" in body_lower or "faktura" in body_lower or "banka" in sender_lower or "transakce" in body_lower:
                            mail.store(mail_id, '+X-GM-LABELS', '"financee"')
                            labeled = True
                            print(f"🏷️ Oštítkováno: financee")
                        elif "n8n" in sender_lower or "resend" in sender_lower or "github actions" in body_lower or "webhook" in body_lower:
                            mail.store(mail_id, '+X-GM-LABELS', '"Automatizace"')
                            labeled = True
                            print(f"🏷️ Oštítkováno: Automatizace")
                        elif "rodina" in sender_lower or "ahoj" in body_lower:
                            mail.store(mail_id, '+X-GM-LABELS', '"osobni"')
                            labeled = True
                            print(f"🏷️ Oštítkováno: osobni")

                        if labeled:
                            stats["labeled"] += 1

        except Exception as e:
            print(f"Chyba při zpracování zprávy ID {mail_id}: {e}")

    save_stats(stats)
    mail.logout()
    print("Dávkový úklid a štítkování dokončeno.")
    
    check_and_send_periodic_report()

if __name__ == "__main__":
    clean_gmail()
