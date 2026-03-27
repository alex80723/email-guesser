#!/usr/bin/env python3
"""
SMTP Email Verifier — single-address verification with countermeasures.

Countermeasures:
  1. Catch-all detection (bogus address probe in same session)
  2. Greylisting handling (retry after delay on initial 550)
  3. Neutral EHLO + null sender (avoids SPF-triggered deceptive 250s)
  4. Multi-MX fallback (try all MX hosts by preference)
  5. Proper SMTP code interpretation (250/550/551/553)
  6. Deep mode: DATA-phase probe to catch servers that defer rejection
     past RCPT TO (e.g. Online_Check_Fail)
  7. Single-session probing (catch-all + real check share one connection)
"""

import argparse
import re
import smtplib
import socket
import sys
import time

import dns.resolver

# ── disposable domain list ─────────────────────────────────────────────────────

_DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "guerrillamail.net", "guerrillamail.org",
    "tempmail.com", "temp-mail.org", "throwam.com", "sharklasers.com",
    "yopmail.com", "10minutemail.com", "trashmail.com", "dispostable.com",
    "maildrop.cc", "getairmail.com", "fakeinbox.com", "spam4.me",
}

def _is_disposable(domain: str) -> bool:
    return domain.lower() in _DISPOSABLE_DOMAINS

# ── constants ──────────────────────────────────────────────────────────────────

EHLO_HOSTNAME = socket.getfqdn() or "verify.local"
MAIL_FROM     = ""                  # null sender (RFC 5321 bounce/probe)
SMTP_TIMEOUT  = 10                  # seconds per connection
GREYLIST_WAIT = 20                  # seconds before greylisting retry
BOGUS_USER    = "zzz_no_such_user_xqj9"

# ── helpers ────────────────────────────────────────────────────────────────────

def _valid_email(addr: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", addr))


def get_mx_hosts(domain: str) -> list[str]:
    """Return MX hosts sorted by preference (lowest first)."""
    try:
        records = dns.resolver.resolve(domain, "MX")
        return [str(r.exchange).rstrip(".") for r in sorted(records, key=lambda r: r.preference)]
    except Exception as e:
        print(f"  [!] MX lookup failed: {e}")
        return []


def _code_to_status(code: int) -> str:
    if code == 250:
        return "valid"
    if code in (550, 551, 553):
        return "invalid"
    return "unknown"


def _deep_data_probe(s: smtplib.SMTP, email: str) -> tuple[int, str]:
    """
    Go through the DATA phase to trigger deferred server-side checks
    (e.g. Online_Check, LDAP lookup) that only run at delivery time.

    Sends a minimal, nearly empty probe message. The server's final
    response after "." reveals whether the mailbox truly accepts mail.
    """
    # RSET to clear previous RCPT state, start a clean transaction
    s.rset()
    s.mail(MAIL_FROM)
    code, _ = s.rcpt(email)
    if code != 250:
        return code, _code_to_status(code)

    # Enter DATA phase
    code, _ = s.data(
        b"From: <>\r\n"
        b"To: <" + email.encode() + b">\r\n"
        b"Subject: verify\r\n"
        b"\r\n"
        b".\r\n"
    )
    return code, _code_to_status(code)


# ── core verification ─────────────────────────────────────────────────────────

def verify(email: str, deep: bool = False) -> dict:
    """
    Verify a single email address via SMTP.

    Both catch-all probe and real probe happen in the SAME SMTP session
    (single connection, single MAIL FROM, two RCPT TOs) to avoid
    greylisting / rate-limiting artifacts from separate connections.

    If deep=True and RCPT TO returns 250, a DATA-phase probe follows
    to catch servers that defer rejection past the RCPT TO stage.
    """
    result = {
        "email":      email,
        "status":     "unknown",
        "mx_host":    None,
        "catch_all":  None,
        "smtp_code":  None,
        "detail":     "",
        "disposable": _is_disposable(email.split("@")[1]),
    }

    if not _valid_email(email):
        result["detail"] = "Malformed email address"
        return result

    domain = email.split("@")[1]

    # 1. MX lookup
    mx_hosts = get_mx_hosts(domain)
    if not mx_hosts:
        result["detail"] = f"No MX records for {domain}"
        return result

    # Try each MX host until one responds
    for mx in mx_hosts:
        result["mx_host"] = mx
        print(f"  [*] Trying MX: {mx}")

        # ── Single-session: catch-all probe + real probe ───────────────────
        try:
            with smtplib.SMTP(timeout=SMTP_TIMEOUT) as s:
                s.connect(mx, 25)
                s.ehlo(EHLO_HOSTNAME)
                try:
                    import ssl
                    ctx = ssl.create_default_context()
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                    s.starttls(context=ctx, server_hostname=mx)
                    s.ehlo(EHLO_HOSTNAME)
                    print(f"  [*] STARTTLS established")
                except Exception:
                    print(f"  [*] STARTTLS not available, continuing plaintext")
                s.mail(MAIL_FROM)

                # 2. Catch-all detection (bogus RCPT in same session)
                bogus = f"{BOGUS_USER}@{domain}"
                ca_code, _ = s.rcpt(bogus)
                ca_status = _code_to_status(ca_code)
                print(f"  [*] Catch-all probe ({bogus}) -> {ca_code} ({ca_status})")

                is_catch_all = (ca_status == "valid")
                result["catch_all"] = is_catch_all

                if is_catch_all and not deep:
                    result["status"] = "catch_all"
                    result["detail"] = (
                        f"Server accepts any recipient (catch-all). "
                        f"Cannot confirm whether {email} truly exists. "
                        f"Use --deep to probe via DATA phase."
                    )
                    return result

                # 3. Real email probe (same session, same MAIL FROM)
                code, _ = s.rcpt(email)
                status = _code_to_status(code)
                print(f"  [*] RCPT {email} -> {code} ({status})")
                result["smtp_code"] = code

                # 4. Deep probe: verify via DATA phase
                #    Critical for catch-all servers and servers with
                #    deferred checks (e.g. Online_Check_Fail)
                if deep and status == "valid":
                    print(f"  [*] Deep mode: DATA-phase probe...")
                    dcode, dstatus = _deep_data_probe(s, email)
                    print(f"  [*] DATA {email} -> {dcode} ({dstatus})")
                    if dstatus == "invalid":
                        status = "invalid"
                        code = dcode
                        result["smtp_code"] = dcode
                        result["detail"] = f"RCPT accepted but DATA rejected (deferred check)"
                    elif is_catch_all and dstatus == "valid":
                        # Even DATA accepted — true catch-all or valid addr
                        status = "catch_all"
                        result["detail"] = (
                            f"Server accepts at both RCPT and DATA phase. "
                            f"Catch-all confirmed."
                        )

        except Exception as e:
            print(f"  [!] SMTP session failed on {mx}: {e}")
            continue   # try next MX

        # ── 5. Greylisting retry (fresh session) ──────────────────────────
        if status == "invalid" and "deferred" not in result.get("detail", ""):
            print(f"  [*] Got 550 — possible greylisting, retrying in {GREYLIST_WAIT}s...")
            time.sleep(GREYLIST_WAIT)
            try:
                with smtplib.SMTP(timeout=SMTP_TIMEOUT) as s:
                    s.connect(mx, 25)
                    s.ehlo(EHLO_HOSTNAME)
                    s.mail(MAIL_FROM)
                    code, _ = s.rcpt(email)
                    status = _code_to_status(code)
                    print(f"  [*] Retry RCPT {email} -> {code} ({status})")
                    result["smtp_code"] = code
            except Exception as e:
                print(f"  [!] Retry failed: {e}")
                result["detail"] = f"Greylisting retry failed: {e}"
                return result

        result["status"] = status
        if not result["detail"]:
            result["detail"] = f"SMTP {code} from {mx}"
        return result

    # All MX hosts failed
    result["detail"] = "All MX hosts unreachable"
    return result


# ── CLI ────────────────────────────────────────────────────────────────────────

_STATUS_LABELS = {
    "valid":     "[VALID]",
    "invalid":   "[INVALID]",
    "catch_all": "[CATCH-ALL]",
    "unknown":   "[UNKNOWN]",
}

def main():
    ap = argparse.ArgumentParser(description="SMTP email address verifier")
    ap.add_argument("email", help="Email address to verify")
    ap.add_argument("--deep", action="store_true",
                    help="DATA-phase probe to catch deferred rejections "
                         "(sends a minimal probe message)")
    args = ap.parse_args()

    email = args.email.strip()
    print(f"\n  Verifying: {email}" + (" (deep mode)" if args.deep else "") + "\n")

    r = verify(email, deep=args.deep)

    label     = _STATUS_LABELS.get(r["status"], r["status"])
    overall   = "✓" if r["status"] == "valid" else ("⚠" if r["status"] == "catch_all" else "✗")
    fmt_ok    = _valid_email(email)
    mx_ok     = bool(r["mx_host"])
    disp_ok   = not r["disposable"]
    deliv_ok  = r["status"] == "valid"

    deliv_detail = ""
    if r["status"] == "catch_all":    deliv_detail = "catch-all domain"
    elif r["smtp_code"]:              deliv_detail = f"SMTP {r['smtp_code']}"
    elif not mx_ok:                   deliv_detail = "no MX records"

    def chk(ok, true_str="Valid", false_str="Invalid"):
        return ("✓ " + true_str) if ok else ("✗ " + false_str)

    print(f"\n  {overall}  {label}  —  {email}\n")
    print(f"  {'Email Format':<20}  {chk(fmt_ok)}")
    print(f"  {'Domain Status':<20}  {chk(mx_ok)}{'  (MX: ' + r['mx_host'] + ')' if r['mx_host'] else ''}")
    print(f"  {'Disposable':<20}  {chk(disp_ok, 'No', 'Yes')}")
    print(f"  {'Deliverability':<20}  {chk(deliv_ok)}{'  (' + deliv_detail + ')' if deliv_detail else ''}")
    print()

    sys.exit(0 if r["status"] == "valid" else 1)


if __name__ == "__main__":
    main()
