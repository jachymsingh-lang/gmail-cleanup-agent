import os
import re
import json
import imaplib
import email
import urllib.request
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from email.utils import parseaddr

USERNAME = os.environ.get("GMAIL_USER")
PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
IMAP_SERVER = "imap.gmail.com"

SCAN_DAYS = 14

NOISE_SENDERS = ["burgerking", "revolut", "playstation", "epicgames", "noreply"]

def clean_gmail():
    print("--- STARTUJEMES GMAIL CLEANUP AGENT ---")
    if not USERNAME or not PASSWORD:
        print("CHYBA: Chybí přihlašovací údaje GMAIL_USER nebo GMAIL_APP_PASSWORD!")
        return

    mail = imaplib.IMAP4_SSL(IMAP_SERVER)
    mail.login(USERNAME, PASSWORD)
    mail.select("INBOX")

    since = (datetime.now() - timedelta(days=SCAN_DAYS)).strftime("%d-%b-%Y")
    res, data = mail.uid("search", None, "SINCE", since)
    
    if res != "OK" or not data or not data[0]:
        print("Žádné zprávy k zastižení v Inboxu.")
        mail.logout()
        return

    uids = data[0].split()
    print(f"Nalezeno celkem {len(uids)} zpráv v Inboxu za posledních {SCAN_DAYS} dní.")

    for uid in uids:
        res, msg_data = mail.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
        if res != "OK":
            continue
        
        raw_msg = b""
        for response_part in msg_data:
            if isinstance(response_part, tuple):
                raw_msg += response_part[1]
        
        msg = email.message_from_bytes(raw_msg)
        sender = msg.get("From", "") or ""
        addr = parseaddr(sender)[1].lower()
        subject_raw = msg.get("Subject", "") or ""
        try:
            subject = str(make_header(decode_header(subject_raw)))
        except:
            subject = subject_raw

        print(f"Zpráva UID {uid.decode()} | Od: {addr} | Předmět: {subject}")

        # Kontrola, zda jde o balast (Burger King, Revolut, PlayStation atd.)
        is_noise = any(p in addr for p in NOISE_SENDERS) or "newsletter" in addr or "noreply" in addr

        if is_noise:
            print(f"   -> 📪 Detekován balast! Přidávám štítek 'Newslettery' a archivuji (odstraňuji z Inboxu).")
            # Přidat štítek Newslettery
            mail.uid("store", uid, "+X-GM-LABELS", '("Newslettery")')
            # Označit jako přečtené
            mail.uid("store", uid, "+FLAGS", "(\\Seen)")
            # Odstranit z Inboxu (archivovat)
            mail.uid("store", uid, "-X-GM-LABELS", "(\\Inbox)")
        else:
            print(f"   -> 🟢 Ponechávám v Inboxu.")

    mail.logout()
    print("--- HOTOVO ---")

if __name__ == "__main__":
    clean_gmail()
