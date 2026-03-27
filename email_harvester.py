#!/usr/bin/env python3
"""
Taiwan B2B Email Harvester

Scrapes publicly known email addresses for one or more domains from:
  1. Company website (BFS crawl)
  2. crt.sh subdomain expansion (crawl employee-facing subdomains)
  3. DuckDuckGo dork search  ("@domain", filetype:pdf, mailing list archives)
  4. GitHub Code Search API  ("@domain" in public repos)
  5. Wayback Machine (archived historical pages)

Use this as a first step before email_guesser.py to:
  - Infer the company's email pattern from real examples
  - Directly match a person's name against the harvested set

Usage:
  python email_harvester.py company.com
  python email_harvester.py company.com subsidiary.com.tw
  python email_harvester.py company.com --name "Amy Chen"
  python email_harvester.py company.com --name "陳美玲 Amy Chen"
  python email_harvester.py company.com --output results.json
"""

import re
import sys
import io
import json
import time
import argparse
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Force UTF-8 output on Windows (avoids cp950 encoding errors with Unicode chars)
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# Reuse shared utilities from email_guesser.py
from email_guesser import (
    scrape_company_website,
    detect_email_domain,
    extract_all_emails,
    get_name_variants,
)

# ─────────────────────────────────────────────────────────────────────────────
# ROLE EMAIL FILTER
# ─────────────────────────────────────────────────────────────────────────────

_ROLE_PREFIXES = {
    "contact", "info", "support", "admin", "sales", "hr", "pr",
    "service", "help", "noreply", "no-reply", "webmaster", "postmaster",
    "abuse", "security", "privacy", "legal", "marketing", "press",
    "careers", "jobs", "billing", "finance", "csr", "ethics", "ethicsreport",
    "inquiry", "enquiry", "enquiries", "inquiries", "general", "office",
    "media", "ir", "investor", "compliance", "audit", "procurement",
}


def _is_role_email(email: str) -> bool:
    local = email.split("@")[0].lower().rstrip("0123456789")
    return local in _ROLE_PREFIXES


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 1: DuckDuckGo dork search
# ─────────────────────────────────────────────────────────────────────────────

def _ddg_search(domain: str) -> list[str]:
    """
    Search DuckDuckGo for pages that mention @domain, extract all email
    addresses found in result snippets and URLs.
    """
    found: list[str] = []
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        queries = [
            f'"@{domain}"',
            f'email site:{domain}',
            f'"@{domain}" filetype:pdf',   # whitepapers, annual reports, conference papers
            # Mailing list archives — DDG indexes these even when direct scraping is blocked
            f'"@{domain}" site:lore.kernel.org OR site:marc.info OR site:mail-archive.com',
        ]
        with DDGS() as ddg:
            for query in queries:
                try:
                    for r in ddg.text(query, max_results=40):
                        text = (r.get("body") or "") + " " + (r.get("href") or "")
                        found += extract_all_emails(text)
                    time.sleep(1.0)  # be polite between queries
                except Exception:
                    continue
    except ImportError:
        print("  [DDG] duckduckgo-search not installed — skipping (pip install duckduckgo-search)")
    except Exception as e:
        print(f"  [DDG] error: {e}")

    return [e for e in set(found) if domain in e.split("@")[1]]


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 2: GitHub Code Search API
# ─────────────────────────────────────────────────────────────────────────────

_GH_HEADERS = {
    "Accept": "application/vnd.github.v3.text-match+json",
    "User-Agent": "Mozilla/5.0",
}


def _github_search(domain: str) -> list[str]:
    """
    Search GitHub's public code index for @domain occurrences.
    No token required (10 req/min unauthenticated).
    """
    found: list[str] = []
    try:
        r = requests.get(
            "https://api.github.com/search/code",
            params={"q": f'"@{domain}"', "per_page": 100},
            headers=_GH_HEADERS,
            timeout=15,
        )
        if r.status_code == 200:
            for item in r.json().get("items", []):
                for match in item.get("text_matches", []):
                    found += extract_all_emails(match.get("fragment", ""))
        elif r.status_code == 403:
            print("  [GitHub] rate-limited — skipping")
        elif r.status_code == 422:
            pass  # query too short / unsupported — silently skip
    except Exception as e:
        print(f"  [GitHub] error: {e}")

    return [e for e in set(found) if domain in e.split("@")[1]]


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 3: crt.sh subdomain expansion
# ─────────────────────────────────────────────────────────────────────────────

# Subdomains that are clearly infrastructure, not employee-facing pages
_INFRA_PREFIXES = (
    "mail.", "smtp.", "pop.", "imap.", "ftp.", "mx.", "ns.", "ns1.", "ns2.",
    "vpn.", "remote.", "webmail.", "autodiscover.", "cpanel.", "whm.",
    "api.", "cdn.", "static.", "img.", "images.", "assets.", "dev.", "staging.",
    "test.", "beta.", "alpha.", "status.", "monitor.", "ping.",
)


def _crt_subdomains(domain: str) -> list[str]:
    """
    Query crt.sh Certificate Transparency logs for known subdomains.
    Filters out infrastructure subdomains and returns only those likely
    to host employee-facing content (contact, team, people pages).
    """
    try:
        r = requests.get(
            "https://crt.sh/",
            params={"q": f"%.{domain}", "output": "json"},
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if r.status_code != 200:
            return []

        seen: set[str] = set()
        results: list[str] = []
        for entry in r.json():
            for name in entry.get("name_value", "").split("\n"):
                name = name.strip().lower().lstrip("*.")
                if (name and name != domain and name != f"www.{domain}"
                        and name.endswith(f".{domain}")
                        and not any(name.startswith(p) for p in _INFRA_PREFIXES)
                        and name not in seen):
                    seen.add(name)
                    results.append(name)
        return results[:8]  # cap to avoid runaway crawling
    except Exception:
        return []


def _crawl_subdomains(subdomains: list[str], email_domain: str) -> list[str]:
    """Quick crawl of subdomain homepages for emails."""
    from email_guesser import HEADERS, extract_all_emails
    found: list[str] = []
    for sub in subdomains:
        for url in [f"https://{sub}/", f"https://{sub}/contact", f"https://{sub}/about"]:
            try:
                r = requests.get(url, headers=HEADERS, timeout=6,
                                 allow_redirects=True, verify=False)
                if r.status_code == 200:
                    emails = [e for e in extract_all_emails(r.text)
                              if e.split("@")[1] == email_domain]
                    found += emails
                    if emails:
                        break  # found something on this subdomain, move on
                time.sleep(0.2)
            except Exception:
                continue
    return found


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 4: Wayback Machine (archived historical pages)
# ─────────────────────────────────────────────────────────────────────────────

_WAYBACK_PRIORITY = ("contact", "about", "team", "people", "staff", "member", "profile", "directory")


def _wayback_search(domain: str, max_fetch: int = 8) -> list[str]:
    """
    Query the Wayback Machine CDX API for archived HTML pages, then fetch
    a sample — prioritising contact/team pages — to find emails that may
    have been removed from the live site.
    """
    found: list[str] = []

    # Step 1: Get list of archived HTML URLs (collapsed by URL so each path appears once)
    try:
        r = requests.get(
            "https://web.archive.org/cdx/search/cdx",
            params={
                "url":      f"{domain}/*",
                "output":   "json",
                "fl":       "original,timestamp",
                "filter":   "statuscode:200",
                "limit":    150,
                "collapse": "urlkey",
            },
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if r.status_code != 200:
            return []
        rows = r.json()[1:]  # row 0 is the field header
    except requests.exceptions.Timeout:
        print("  [Wayback] CDX timed out — skipping")
        return []
    except Exception as e:
        print(f"  [Wayback] CDX error: {e}")
        return []

    if not rows:
        return []

    # Step 2: Sort — priority pages first, then the rest
    priority = [row for row in rows
                if any(kw in row[0].lower() for kw in _WAYBACK_PRIORITY)]
    rest     = [row for row in rows if row not in priority]
    to_fetch = (priority + rest)[:max_fetch]

    # Step 3: Fetch each archived snapshot and extract emails
    for original_url, timestamp in to_fetch:
        # id_ modifier returns the raw original page without Wayback rewriting
        wb_url = f"https://web.archive.org/web/{timestamp}id_/{original_url}"
        try:
            r = requests.get(wb_url, timeout=12,
                             headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                emails = [e for e in extract_all_emails(r.text)
                          if domain in e.split("@")[1]]
                found += emails
            time.sleep(0.5)
        except Exception:
            continue

    return list(set(found))


# ─────────────────────────────────────────────────────────────────────────────
# MAIN HARVEST FUNCTION
# ─────────────────────────────────────────────────────────────────────────────

def harvest_domain(website_domain: str) -> dict:
    """
    Harvest all publicly known emails for a domain using five sources.

    Returns:
        {
            "website_domain": str,
            "email_domain":   str,   # may differ (e.g. hengstyle.com → hlh.com.tw)
            "all":            list,  # every unique email found
            "personal":       list,  # non-role emails
            "role":           list,  # role/generic emails
            "sources": {
                "website":  int,   # BFS crawl of main domain
                "subdomains": int, # crt.sh subdomain crawl
                "ddg":      int,   # DuckDuckGo (general + pdf)
                "github":   int,   # GitHub Code Search
                "wayback":  int,   # Wayback Machine archives
            }
        }
    """
    print(f"\n{'-'*60}")
    print(f"  Harvesting: {website_domain}")
    print(f"{'-'*60}")

    # ── Source 1: Company website BFS crawl ──────────────────────────────────
    print("  [1/5] Website BFS crawl...")
    site_emails, email_domain = scrape_company_website(website_domain)
    print(f"        → {len(site_emails)} emails  (email domain: {email_domain})")
    if email_domain != website_domain:
        print(f"        [!] email domain differs from website: @{email_domain}")

    # ── Source 2: crt.sh subdomain expansion ─────────────────────────────────
    print("  [2/5] crt.sh subdomain search...")
    subdomains = _crt_subdomains(website_domain)
    sub_emails: list[str] = []
    if subdomains:
        print(f"        found {len(subdomains)} subdomains: {', '.join(subdomains)}")
        sub_emails = _crawl_subdomains(subdomains, email_domain)
    print(f"        → {len(sub_emails)} emails")

    # ── Source 3: DuckDuckGo dork (general + PDF) ────────────────────────────
    print("  [3/5] DuckDuckGo dork search (web + PDF)...")
    ddg_raw = _ddg_search(email_domain)
    if email_domain != website_domain:
        ddg_raw += _ddg_search(website_domain)
    ddg_emails = [e for e in set(ddg_raw) if e.split("@")[1] == email_domain]
    print(f"        → {len(ddg_emails)} emails")

    # ── Source 4: GitHub Code Search ──────────────────────────────────────────
    print("  [4/5] GitHub Code Search...")
    gh_emails = _github_search(email_domain)
    print(f"        → {len(gh_emails)} emails")

    # ── Source 5: Wayback Machine ─────────────────────────────────────────────
    print("  [5/5] Wayback Machine (archived pages)...")
    wb_emails = _wayback_search(website_domain)
    if email_domain != website_domain:
        wb_emails += _wayback_search(email_domain)
    wb_emails = [e for e in set(wb_emails) if e.split("@")[1] == email_domain]
    print(f"        → {len(wb_emails)} emails")

    # ── Combine and classify ──────────────────────────────────────────────────
    all_emails = list(set(site_emails + sub_emails + ddg_emails + gh_emails + wb_emails))
    personal   = sorted([e for e in all_emails if not _is_role_email(e)])
    role       = sorted([e for e in all_emails if _is_role_email(e)])

    return {
        "website_domain": website_domain,
        "email_domain":   email_domain,
        "all":            sorted(all_emails),
        "personal":       personal,
        "role":           role,
        "sources": {
            "website":    len(site_emails),
            "subdomains": len(sub_emails),
            "ddg":        len(ddg_emails),
            "github":     len(gh_emails),
            "wayback":    len(wb_emails),
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# NAME MATCHING
# ─────────────────────────────────────────────────────────────────────────────

def match_emails_to_name(emails: list[str], name: str) -> list[dict]:
    """
    Filter/rank harvested emails against a person's name.

    Match levels:
      strong    — email local part contains BOTH first and last name components
      last-only — email local part contains the last name component only
      first-only— email local part contains the first name component only
    """
    variants = get_name_variants(name)
    if not variants:
        return []

    matches: list[dict] = []
    seen: set[str] = set()

    for email in emails:
        local = email.split("@")[0].lower()
        best_level = None
        best_first = ""
        best_last  = ""

        for first, last in variants:
            first = first.lower()
            last  = last.lower()
            has_first = bool(first) and first in local
            has_last  = bool(last)  and last  in local

            if has_first and has_last:
                level = "strong"
            elif has_last:
                level = "last-only"
            elif has_first:
                level = "first-only"
            else:
                continue

            # Prefer stronger matches
            rank = {"strong": 0, "last-only": 1, "first-only": 2}
            if best_level is None or rank[level] < rank[best_level]:
                best_level = level
                best_first = first
                best_last  = last

        if best_level and email not in seen:
            seen.add(email)
            matches.append({
                "email":  email,
                "level":  best_level,
                "first":  best_first,
                "last":   best_last,
            })

    return sorted(matches, key=lambda x: {"strong": 0, "last-only": 1, "first-only": 2}[x["level"]])


# ─────────────────────────────────────────────────────────────────────────────
# OUTPUT
# ─────────────────────────────────────────────────────────────────────────────

def print_harvest(data: dict, name: str | None = None):
    src = data["sources"]
    total = len(data["all"])
    print(f"\n{'='*60}")
    print(f"  HARVEST RESULTS: {data['website_domain']}")
    print(f"{'='*60}")
    print(f"  Email domain   : {data['email_domain']}")
    print(f"  Sources        : website={src['website']}  subdomains={src['subdomains']}  "
          f"ddg={src['ddg']}  github={src['github']}  wayback={src['wayback']}")
    print(f"  Total found    : {total}  ({len(data['personal'])} personal, {len(data['role'])} role)")

    if data["personal"]:
        print(f"\n  Personal emails ({len(data['personal'])}):")
        for e in data["personal"]:
            print(f"    {e}")
    else:
        print("\n  Personal emails: (none found)")

    if data["role"]:
        print(f"\n  Role/generic emails ({len(data['role'])}):")
        for e in data["role"]:
            print(f"    {e}")

    if name:
        matches = match_emails_to_name(data["all"], name)
        print(f"\n  Name matches for {name!r}:")
        if matches:
            for m in matches:
                tag = f"[{m['level'].upper()}]"
                detail = f"({m['first']} + {m['last']})" if m['first'] and m['last'] else f"({m['last'] or m['first']})"
                print(f"    {tag:<14}  {m['email']}  {detail}")
        else:
            print("    (no matches)")

    print(f"{'='*60}")


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Harvest all publicly known emails for one or more domains."
    )
    parser.add_argument("domains", nargs="+", help="Domain(s) to harvest, e.g. company.com")
    parser.add_argument("--name",   default=None, help='Filter by person name, e.g. "Amy Chen" or "陳美玲 Amy Chen"')
    parser.add_argument("--output", default=None, help="Write results to this JSON file")
    args = parser.parse_args()

    all_results: dict[str, dict] = {}

    for domain in args.domains:
        domain = domain.lower().removeprefix("http://").removeprefix("https://").removeprefix("www.").rstrip("/")
        result = harvest_domain(domain)
        if args.name:
            result["name_matches"] = match_emails_to_name(result["all"], args.name)
        all_results[domain] = result
        print_harvest(result, name=args.name)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(all_results, f, ensure_ascii=False, indent=2)
        print(f"\nResults written to {args.output}")


if __name__ == "__main__":
    main()
