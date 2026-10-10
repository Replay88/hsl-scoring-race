"""Post the weekend recap for chosen divisions to Discord and/or email, once per weekend.

Configured entirely through environment variables (GitHub Actions secrets):
  DIGEST_DIVISIONS   comma-separated division names, e.g. "2018 Major,2017 Major"
  DISCORD_WEBHOOK    a Discord channel webhook URL (optional)
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, DIGEST_TO, DIGEST_FROM   (optional, for email)
Runs after update.py. Remembers what it sent in cache/digest.json so a weekend goes out once.
"""
import json
import os
import smtplib
import sys
import urllib.request
from email.message import EmailMessage

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SENT = os.path.join(ROOT, "cache", "digest.json")


def main():
    divs = [d.strip() for d in os.environ.get("DIGEST_DIVISIONS", "").split(",") if d.strip()]
    hook, smtp = os.environ.get("DISCORD_WEBHOOK"), os.environ.get("SMTP_HOST")
    if not divs or not (hook or smtp):
        print("digest: nothing configured (set DIGEST_DIVISIONS and DISCORD_WEBHOOK or SMTP_*)")
        return
    index = json.load(open(os.path.join(ROOT, "docs", "data", "index.json")))
    season = next(s for s in index["seasons"] if s["current"])
    sent = json.load(open(SENT)) if os.path.exists(SENT) else {}
    for name in divs:
        d = next((x for x in season["divisions"] if x["name"] == name), None)
        if not d:
            print(f"digest: no division called {name}")
            continue
        D = json.load(open(os.path.join(ROOT, "docs", "data", season["key"], d["key"] + ".json")))
        stamp = D["hotspan"]
        if not D["days"] or sent.get(name) == stamp:
            continue
        text = D["recap"]
        if hook:
            body = json.dumps({"content": "```\n" + text[:1900] + "\n```"}).encode()
            req = urllib.request.Request(hook, data=body, headers={"Content-Type": "application/json", "User-Agent": "hsl-digest"})
            urllib.request.urlopen(req, timeout=30).read()
        if smtp:
            msg = EmailMessage()
            msg["Subject"] = f"HSL {name} weekend recap, {stamp}"
            msg["From"] = os.environ.get("DIGEST_FROM", os.environ.get("SMTP_USER"))
            msg["To"] = os.environ["DIGEST_TO"]
            msg.set_content(text)
            with smtplib.SMTP(smtp, int(os.environ.get("SMTP_PORT", "587"))) as s:
                s.starttls()
                s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
                s.send_message(msg)
        sent[name] = stamp
        print(f"digest: sent {name} ({stamp})")
    json.dump(sent, open(SENT, "w"))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never fail the site update over the digest
        print(f"digest failed: {e}", file=sys.stderr)
