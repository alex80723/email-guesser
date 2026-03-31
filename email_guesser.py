#!/usr/bin/env python3
"""
Taiwan B2B Email Guesser  (v2)

Fixes:
  1. Wade-Giles romanization for Taiwan (e.g. 瑄 → hsuan, not xuan)
  2. Mixed name format: "Chinese Last+First  English First+Last"
  3. Company website crawled first (BFS); 104/1111 are fallback only
  4. Auto-detects real email domain (e.g. @hlh.com.tw on hengstyle.com)

Usage:
  python email_guesser.py "王小明 Kevin Wang"  "https://www.company.com"
  python email_guesser.py "陳美玲 Amy Chen"    "https://www.bebit-tech.com"
  python email_guesser.py "Kevin Chen"         "https://www.tsmc.com"
  python email_guesser.py "陳美玲"             "https://www.mediatek.com"
"""

import re
import sys
import time
import logging
import os
import socket
import smtplib
import requests
import urllib3
import dns.resolver

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from collections import deque, Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin
from pypinyin import lazy_pinyin, Style

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING  — always writes to email_guesser.log next to this file
# Console output is controlled separately by verbose= in run()
# ─────────────────────────────────────────────────────────────────────────────

_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "email_guesser.log")

logging.basicConfig(
    filename=_LOG_FILE,
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    encoding="utf-8",
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 1. DOMAIN EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────

def extract_domain(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    netloc = urlparse(url).netloc
    return netloc.removeprefix("www.")


def company_hint_from_domain(domain: str) -> str:
    return domain.split(".")[0]


# ─────────────────────────────────────────────────────────────────────────────
# 2. TAIWAN ROMANIZATION  (Wade-Giles, rule-based)
# ─────────────────────────────────────────────────────────────────────────────
#
# Approach: rule-based function covering ALL ~400 Mandarin syllables instead
# of a hand-coded lookup table that only covers ~100.
#
# Three layers:
#   1. _WG_EXCEPTIONS  — genuine edge cases that don't fit regular rules
#   2. _INITIAL_RULES  — systematic initial consonant substitutions
#   3. _FINAL_RULES    — systematic final vowel/nasal substitutions
#
# Key initial rules:
#   b → p    (邦bang→pang, 彬bin→pin)          ← was MISSING
#   d → t    (鼎ding→ting, 大da→ta)             ← was MISSING
#   r → j    (溶rong→jung, 仁ren→jen)           ← was MISSING
#   g → k    (郭guo→kuo)
#   z → ts   (曾zeng→tseng)
#   c → ts   (蔡cai→tsai)
#   x → hs   (許xu→hsu, 瑄xuan→hsuan)
#   zh → ch  (張zhang→chang)
#   q → ch   (錢qian→chien)
#   j → ch   (建jian→chien)
#
# Key final rules:
#   -ong → -ung   (洪hong→hung, 隆long→lung, 東dong→tung)
#   -ian → -ien   (聯lian→lien, 典dian→tien)
#   -ie  → -ieh   (捷jie→chieh, 列lie→lieh)
#   -iong → -iung (jiong→chiung)

# Ordered list of initials to detect (longest match first to avoid zh/z clash)
_INITIALS = ["zh", "ch", "sh", "b", "p", "m", "f", "d", "t", "n", "l",
             "g", "k", "h", "j", "q", "x", "r", "z", "c", "s", "y", "w"]

_INITIAL_RULES: dict[str, str] = {
    "b":  "p",
    "d":  "t",
    "g":  "k",
    "r":  "j",
    "z":  "ts",
    "c":  "ts",
    "x":  "hs",
    "zh": "ch",
    "q":  "ch",
    "j":  "ch",
    # p t k m f n l h s sh ch w y → unchanged (no entry needed)
}

_FINAL_RULES: dict[str, str] = {
    "ong":  "ung",
    "iong": "iung",
    "ian":  "ien",
    "ie":   "ieh",
}

# True exceptions: syllables whose Wade-Giles form cannot be derived from
# the rules above (mainly the "empty rime" syllables and ü-final forms).
_WG_EXCEPTIONS: dict[str, str] = {
    # Retroflex/sibilant empty rimes
    "zhi": "chih",  "chi": "chih",  "shi": "shih",  "ri":  "jih",
    "zi":  "tzu",   "ci":  "tzu",   "si":  "szu",
    # Erhua
    "er":  "erh",
    # ü-initial syllables (j/q/x + ü written without umlaut in Pinyin)
    "ju":  "chu",   "qu":  "chu",   "xu":  "hsu",
    "jue": "chueh", "que": "chueh", "xue": "hsueh",
    "jun": "chun",  "qun": "chun",  "xun": "hsun",
    "juan":"chuan", "quan":"chuan", "xuan":"hsuan",
    # ge has two accepted WG forms; ko is older but ke is common
    "ge":  "ke",
    # zhuo/chuo: WG uses cho
    "zhuo":"cho",
}


def _split_syllable(s: str) -> tuple[str, str]:
    """Split a Pinyin syllable into (initial, final). Returns ('', s) if no initial."""
    for init in _INITIALS:
        if s.startswith(init):
            return init, s[len(init):]
    return "", s


def pinyin_to_wade_giles(syllable: str) -> list[str]:
    """
    Convert a single Pinyin syllable to its Wade-Giles form(s).
    Returns [wg] if different from Pinyin, or [syllable] if they are the same.
    Pinyin is never included alongside a different WG form.
    """
    s = syllable.lower()

    # Layer 1: hard exceptions
    if s in _WG_EXCEPTIONS:
        wg = _WG_EXCEPTIONS[s]
        return [wg] if wg != s else [s]

    # Layer 2: rule-based
    initial, final = _split_syllable(s)
    wg_initial = _INITIAL_RULES.get(initial, initial)
    wg_final   = _FINAL_RULES.get(final, final)
    wg = wg_initial + wg_final

    return [wg] if wg != s else [s]


# ─────────────────────────────────────────────────────────────────────────────
# 3. NAME PROCESSING
# ─────────────────────────────────────────────────────────────────────────────

def is_chinese(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text))


def _chars_to_pinyin(chars: list[str]) -> list[str]:
    return [lazy_pinyin(c, style=Style.NORMAL)[0].lower() for c in chars]


def romanize_chinese_name(name: str) -> list[tuple[str, str]]:
    """
    Convert a Chinese name to (first, last) tuples covering both Pinyin
    and Taiwan (Wade-Giles) romanization.

    Structure assumed: surname-first (last + given), as is standard in TW.
      2-char: 1 surname + 1 given
      3-char: 1 surname + 2 given  (most common)
      4-char: 1+3 or 2+2 split
    """
    chars = [c for c in name.strip() if c.strip()]
    if not chars:
        return []

    pinyin = _chars_to_pinyin(chars)

    # Get Taiwan variants per syllable (Wade-Giles, rule-based)
    tw = [pinyin_to_wade_giles(p) for p in pinyin]

    variants: list[tuple[str, str]] = []
    _seen_variants: set[tuple[str, str]] = set()

    def add(given_chars_indices: list[int], last_chars_indices: list[int]):
        """Enumerate all (first, last) combos from TW-variant lists."""
        # For the surname: pick each variant
        last_options: list[str] = []
        for idx in last_chars_indices:
            for v in tw[idx]:
                if v not in last_options:
                    last_options.append(v)

        # For the given name: join all chars' variants
        # Strategy: combine first variant of each char (joined), hyphenated, first-char-only
        given_syllable_variants = [tw[i] for i in given_chars_indices]

        # Build given-name options
        given_options: list[str] = []
        # 1. Join first-choice variants of each syllable  e.g. meiling
        joined = "".join(v[0] for v in given_syllable_variants)
        given_options.append(joined)

        # 2. Hyphenated first-choice  e.g. mei-ling
        if len(given_chars_indices) > 1:
            hyphen = "-".join(v[0] for v in given_syllable_variants)
            if hyphen != joined:
                given_options.append(hyphen)

        # 3. First syllable only  e.g. mei
        first_syl = given_syllable_variants[0][0]
        if first_syl not in given_options:
            given_options.append(first_syl)

        # 4. All-TW variants joined  (e.g. if 美=mei/mei, 玲=ling/ling → same;
        #    but 小=xiao/hsiao + 明=ming → xiaoming / hsiaoming)
        if len(given_chars_indices) > 0:
            for combo in _cartesian_join(given_syllable_variants):
                if combo not in given_options:
                    given_options.append(combo)

        for last in last_options:
            for first in given_options:
                v = (first, last)
                if v not in _seen_variants:
                    _seen_variants.add(v)
                    variants.append(v)

    def _cartesian_join(syllable_lists: list[list[str]]) -> list[str]:
        """Join every combination of syllable variants."""
        result = [""]
        for opts in syllable_lists:
            result = [r + o for r in result for o in opts]
        return result

    if len(chars) == 2:
        add([1], [0])
    elif len(chars) == 3:
        add([1, 2], [0])
    elif len(chars) == 4:
        add([1, 2, 3], [0])       # 1 surname + 3 given
        add([2, 3],    [0, 1])    # 2 surname + 2 given

    return variants


def parse_english_name(name: str) -> list[tuple[str, str]]:
    name = name.strip()
    variants: list[tuple[str, str]] = []

    if "," in name:
        parts = [p.strip().lower() for p in name.split(",", 1)]
        variants.append((parts[1], parts[0]))
        return variants

    parts = name.lower().split()
    if len(parts) == 1:
        variants.append((parts[0], ""))
    elif len(parts) == 2:
        variants.append((parts[0], parts[1]))
    else:
        first, last = parts[0], parts[-1]
        variants.append((first, last))
        if len(parts) > 2:
            variants.append((first + parts[1][0], last))  # middle initial

    return variants


def parse_mixed_name(name: str) -> tuple[str, str]:
    """
    Split "王小明 Kevin Wang" into ("王小明", "Kevin Wang").
    Returns (chinese_part, english_part), either may be empty.
    """
    chinese_chars = re.findall(r"[\u4e00-\u9fff]+", name)
    english_words = re.findall(r"[a-zA-Z]+", name)
    return "".join(chinese_chars), " ".join(english_words)


def get_name_variants(name: str) -> list[tuple[str, str]]:
    """
    Accepts:
      - Pure Chinese:  "陳美玲"
      - Pure English:  "Kevin Chen"
      - Mixed:         "陳美玲 Amy Chen"  (Chinese Last+First, English First+Last)

    Returns deduplicated list of (first, last) tuples covering all
    Pinyin, Wade-Giles, and English name variants.
    """
    chinese_part, english_part = parse_mixed_name(name)

    variants: list[tuple[str, str]] = []

    if english_part:
        variants += parse_english_name(english_part)

    if chinese_part:
        variants += romanize_chinese_name(chinese_part)

    # Deduplicate preserving order
    seen: set[tuple[str, str]] = set()
    result = []
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            result.append(v)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# 4. EMAIL DISCOVERY  (website-first, 104/1111 as fallback)
# ─────────────────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

@lru_cache(maxsize=16)
def _email_re_for_domain(domain: str) -> re.Pattern:
    return re.compile(rf"[a-zA-Z0-9._%+\-]+@{re.escape(domain)}", re.IGNORECASE)


_PLACEHOLDER_LOCAL_PARTS = {
    "example", "ejemplo", "exemple", "beispiel", "esempio",  # "example" in various languages
    "sample", "test", "yourname", "youremail", "your",
    "name", "user", "username", "email", "mail",
}

def _is_placeholder(email: str) -> bool:
    local = email.split("@")[0].lower()
    return local in _PLACEHOLDER_LOCAL_PARTS

def extract_all_emails(text: str) -> list[str]:
    """Find every email in text, excluding obvious placeholders."""
    return [m.lower() for m in _EMAIL_RE.findall(text) if not _is_placeholder(m)]


def extract_emails_for_domain(text: str, domain: str) -> list[str]:
    return [m.lower() for m in _email_re_for_domain(domain).findall(text)]


_JUNK_DOMAINS = {
    # freemail
    "gmail.com", "yahoo.com", "yahoo.com.tw", "hotmail.com", "outlook.com",
    "icloud.com", "live.com", "msn.com", "qq.com", "163.com", "126.com",
    # placeholder / generic
    "email.com", "example.com", "test.com", "domain.com", "company.com",
    "yourcompany.com", "mail.com",
}

def detect_email_domain(website_domain: str, all_emails: list[str]) -> str:
    """
    If the company uses a different email domain than their website domain
    (e.g. hengstyle.com website but @hlh.com.tw email), detect and return it.
    Falls back to the website domain if no clear signal.
    Excludes freemail, placeholder domains, and placeholder local parts.
    """
    real_emails = [
        e for e in all_emails
        if "@" in e
        and e.split("@")[1] not in _JUNK_DOMAINS
        and not _is_placeholder(e)
    ]
    domains = [e.split("@")[1] for e in real_emails]
    if not domains:
        return website_domain
    freq = Counter(domains)
    most_common, count = freq.most_common(1)[0]
    # Only switch if the detected domain differs AND has strong signal:
    # at least 2 distinct local parts, to avoid flukes
    website_count = freq.get(website_domain, 0)
    if most_common != website_domain and (count >= 2 or website_count == 0):
        distinct_locals = len({e.split("@")[0] for e in real_emails if e.split("@")[1] == most_common})
        if distinct_locals >= 2 or website_count == 0:
            return most_common
    return website_domain


# Priority paths to check on the company website
_PRIORITY_PATHS = [
    "/", "/contact", "/contact-us", "/about", "/about-us", "/team",
    "/people", "/en/contact", "/en/about", "/zh/contact", "/tw/contact",
    "/complaints-info", "/service", "/support", "/ir", "/investor",
    "/press", "/media", "/corporate", "/company", "/profile",
    "/about/contact", "/en/about/contact", "/zh-tw/contact",
]


def scrape_company_website(website_domain: str) -> tuple[list[str], str]:
    """
    BFS crawl of company website.
    1. Checks priority paths first.
    2. From the homepage, extracts all internal links and adds them to the queue.
    3. Stops after visiting MAX_PAGES pages or once emails are found.
    4. Fetches pages in concurrent batches (MAX_WORKERS) with polite rate-limiting.

    Returns (found_emails_for_email_domain, detected_email_domain).
    """
    MAX_PAGES = 35
    MAX_WORKERS = 5
    all_found_emails: list[str] = []
    visited: set[str] = set()
    queued: set[str] = set()
    queue: deque[str] = deque()

    # Some domains only resolve with www. prefix — detect and use it
    bare_base = f"https://{website_domain}"
    www_base = f"https://www.{website_domain}"
    try:
        _probe = requests.get(bare_base, headers=HEADERS, timeout=6, allow_redirects=True, verify=False)
        base = bare_base
    except Exception:
        base = www_base

    for path in _PRIORITY_PATHS:
        url = base + path
        if url not in queued:
            queue.append(url)
            queued.add(url)

    homepage_crawled = False

    def _fetch(url: str):
        try:
            r = requests.get(url, headers=HEADERS, timeout=8, allow_redirects=True, verify=False)
            logger.debug(f"  GET {url} → {r.status_code}")
            return url, r
        except Exception as e:
            logger.debug(f"  ERROR {url} → {e}")
            return url, None

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        while queue and len(visited) < MAX_PAGES:
            # Build a batch of URLs to fetch concurrently
            batch: list[str] = []
            while queue and len(batch) < MAX_WORKERS and len(visited) + len(batch) < MAX_PAGES:
                url = queue.popleft()
                if url not in visited:
                    batch.append(url)
                    visited.add(url)

            if not batch:
                break

            for future in as_completed(pool.submit(_fetch, u) for u in batch):
                url, r = future.result()
                if r is None or r.status_code != 200:
                    continue

                emails = extract_all_emails(r.text)
                if emails:
                    logger.debug(f"    emails found: {emails}")
                all_found_emails.extend(emails)

                # Expand BFS from homepage only (to avoid crawling the whole site)
                if not homepage_crawled and url in (base, base + "/", base + "/index.html"):
                    homepage_crawled = True
                    soup = BeautifulSoup(r.text, "html.parser")
                    for a in soup.find_all("a", href=True):
                        href = a["href"].strip()
                        if href.startswith("/"):
                            full = base + href
                            if full not in visited and full not in queued:
                                queue.append(full)
                                queued.add(full)

            time.sleep(0.3)  # polite rate-limit per batch

    email_domain = detect_email_domain(website_domain, all_found_emails)
    domain_emails = [e for e in all_found_emails if e.split("@")[1] == email_domain]
    return list(set(domain_emails)), email_domain


def search_104(company_hint: str, email_domain: str) -> list[str]:
    found: list[str] = []
    try:
        api = "https://www.104.com.tw/jobs/search/list"
        params = {"ro": 0, "keyword": company_hint, "order": 15,
                  "asc": 0, "page": 1, "mode": "s", "jobsource": "2018indexpoc"}
        hdrs = {**HEADERS, "Referer": "https://www.104.com.tw/"}
        r = requests.get(api, params=params, headers=hdrs, timeout=10)
        logger.debug(f"  104 search '{company_hint}' → {r.status_code}")
        if r.status_code != 200:
            return found
        jobs = r.json().get("data", {}).get("list", [])
        logger.debug(f"  104 jobs returned: {len(jobs)}")
        for job in jobs[:10]:
            job_no = job.get("jobNo", "")
            if not job_no:
                continue
            try:
                detail = requests.get(f"https://www.104.com.tw/job/{job_no}",
                                      headers=hdrs, timeout=8)
                hits = extract_emails_for_domain(detail.text, email_domain)
                if hits:
                    logger.debug(f"    job {job_no} → {hits}")
                found += hits
                time.sleep(0.3)
            except Exception as e:
                logger.debug(f"    job {job_no} error → {e}")
                continue
    except Exception as e:
        logger.warning(f"104 search failed: {e}")
    return list(set(found))


def search_1111(company_hint: str, email_domain: str) -> list[str]:
    found: list[str] = []
    try:
        r = requests.get("https://www.1111.com.tw/search/job",
                         params={"ks": company_hint},
                         headers=HEADERS, timeout=10)
        logger.debug(f"  1111 search '{company_hint}' → {r.status_code}")
        if r.status_code != 200:
            return found
        found += extract_emails_for_domain(r.text, email_domain)
        soup = BeautifulSoup(r.text, "html.parser")
        for link in soup.find_all("a", href=re.compile(r"/job/"))[:5]:
            href = link.get("href", "")
            full_url = urljoin("https://www.1111.com.tw", href)
            try:
                detail = requests.get(full_url, headers=HEADERS, timeout=8)
                hits = extract_emails_for_domain(detail.text, email_domain)
                if hits:
                    logger.debug(f"    1111 listing {full_url} → {hits}")
                found += hits
                time.sleep(0.3)
            except Exception as e:
                logger.debug(f"    1111 listing error → {e}")
                continue
    except Exception as e:
        logger.warning(f"1111 search failed: {e}")
    return list(set(found))


_PAT_FIRST_LAST = re.compile(r"[a-z]+\.[a-z]+")
_PAT_FIRST_USCR = re.compile(r"[a-z]+_[a-z]+")
_PAT_F_LAST     = re.compile(r"[a-z]\.[a-z]+")
_PAT_FIRSTLAST  = re.compile(r"[a-z][a-z]{2,}")
_PAT_FIRST_L    = re.compile(r"[a-z]+\.[a-z]")


def infer_pattern(emails: list[str]) -> str | None:
    if not emails:
        return None
    counts: dict[str, int] = {}
    for email in emails:
        local = email.split("@")[0]
        if _PAT_FIRST_LAST.fullmatch(local): counts["first.last"] = counts.get("first.last", 0) + 1
        if _PAT_FIRST_USCR.fullmatch(local): counts["first_last"] = counts.get("first_last", 0) + 1
        if _PAT_F_LAST.fullmatch(local):     counts["f.last"]     = counts.get("f.last",     0) + 1
        if _PAT_FIRSTLAST.fullmatch(local):  counts["firstlast"]  = counts.get("firstlast",  0) + 1
        if _PAT_FIRST_L.fullmatch(local):    counts["first.l"]    = counts.get("first.l",    0) + 1
    if not counts:
        return None
    return max(counts, key=lambda k: counts[k])


# ─────────────────────────────────────────────────────────────────────────────
# 5. CANDIDATE GENERATION
# ─────────────────────────────────────────────────────────────────────────────

ALL_PATTERNS = [
    "{first}.{last}",
    "{first}{last}",
    "{f}.{last}",
    "{f}{last}",
    "{last}.{first}",
    "{last}{first}",
    "{first}_{last}",
    "{first}.{l}",
    "{first}",
]

PATTERN_MAP = {
    "first.last": "{first}.{last}",
    "first_last": "{first}_{last}",
    "f.last":     "{f}.{last}",
    "firstlast":  "{first}{last}",
    "first.l":    "{first}.{l}",
}


def apply_pattern(template: str, first: str, last: str, domain: str) -> str:
    f = first[0] if first else ""
    l = last[0]  if last  else ""
    return (
        template
        .replace("{first}", first)
        .replace("{last}",  last)
        .replace("{f}",     f)
        .replace("{l}",     l)
        + f"@{domain}"
    )


def generate_candidates(
    name_variants: list[tuple[str, str]],
    domain: str,
    pattern: str | None,
) -> list[str]:
    high: list[str] = []
    rest: list[str] = []
    template = PATTERN_MAP.get(pattern) if pattern else None

    for first, last in name_variants:
        if not first and not last:
            continue
        if template:
            high.append(apply_pattern(template, first, last, domain))
        for t in ALL_PATTERNS:
            rest.append(apply_pattern(t, first, last, domain))

    seen: set[str] = set()
    ordered: list[str] = []
    for c in high + rest:
        local = c.split("@")[0]
        if not local or ".." in local or local.startswith(".") or local.endswith("."):
            continue
        if c not in seen:
            seen.add(c)
            ordered.append(c)
    return ordered


# ─────────────────────────────────────────────────────────────────────────────
# 6. FREE SMTP VERIFICATION
# ─────────────────────────────────────────────────────────────────────────────

def get_mx(domain: str) -> str | None:
    try:
        records = dns.resolver.resolve(domain, "MX")
        best = sorted(records, key=lambda r: r.preference)[0]
        return str(best.exchange).rstrip(".")
    except Exception:
        return None


def _smtp_code_to_status(code: int) -> str:
    if code == 250:
        return "valid"
    elif code in (550, 551, 553):
        return "invalid"
    return "unknown"


def _get_ehlo_fqdn() -> str:
    """Return this machine's FQDN for honest EHLO identification."""
    fqdn = socket.getfqdn()
    # Fall back to something neutral if FQDN is localhost-ish
    if not fqdn or fqdn in ("localhost", "localhost.localdomain"):
        fqdn = "probe.localdomain"
    return fqdn


GREYLIST_RETRY_DELAY = 20

def smtp_batch_probe(emails: list[str], mx_host: str, domain: str
                     ) -> tuple[dict[str, str], bool]:
    """
    Probe all emails + a catch-all canary in a SINGLE SMTP session.
    Uses neutral identity (real FQDN, null sender) to avoid SPF-triggered
    deceptive responses from anti-harvesting servers.

    If ALL probes return 550, suspect greylisting and retry once after delay.

    Returns (results_dict, is_catch_all).
    """
    canary = f"zzz_no_such_user_xqj9@{domain}"
    ehlo_fqdn = _get_ehlo_fqdn()

    def _batch(addrs: list[str]) -> dict[str, str]:
        out = {}
        try:
            with smtplib.SMTP(timeout=10) as s:
                s.connect(mx_host, 25)
                s.ehlo(ehlo_fqdn)
                s.mail("")  # null sender (RFC 5321 §4.5.5)
                for addr in addrs:
                    code, msg = s.rcpt(addr)
                    status = _smtp_code_to_status(code)
                    logger.debug(f"  SMTP {addr} → {code} ({status})")
                    out[addr] = status
        except Exception as e:
            logger.debug(f"  SMTP batch exception: {e}")
            for addr in addrs:
                if addr not in out:
                    out[addr] = "unknown"
        return out

    # Probe canary + all candidates in one session
    all_addrs = [canary] + list(emails)
    results = _batch(all_addrs)

    # Check catch-all: if canary was accepted, server accepts everything
    catch_all = results.pop(canary, "unknown") == "valid"
    if catch_all:
        logger.info("  Catch-all detected (canary accepted) — SMTP results unreliable")
        return results, True

    # Smart retry: if every real probe returned 550, suspect greylisting
    invalids = [e for e, s in results.items() if s == "invalid"]
    if invalids and len(invalids) == len(emails):
        logger.info(f"  All {len(invalids)} probes returned 550 — possible greylisting, "
                     f"retrying in {GREYLIST_RETRY_DELAY}s...")
        time.sleep(GREYLIST_RETRY_DELAY)
        retry = _batch([canary] + invalids)
        # Re-check catch-all on retry too
        if retry.pop(canary, "unknown") == "valid":
            logger.info("  Catch-all detected on retry — SMTP results unreliable")
            return retry, True
        results.update(retry)

    return results, False


# ─────────────────────────────────────────────────────────────────────────────
# 7. MAIN PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

def run(name: str, company_url: str, verbose: bool = True) -> dict:
    logger.info("━" * 60)
    logger.info(f"RUN  name={name!r}  url={company_url!r}")

    def log(msg: str):
        if verbose:
            print(msg)
        logger.info(msg.strip())

    # Step 1: Domain
    website_domain = extract_domain(company_url)
    hint = company_hint_from_domain(website_domain)
    log(f"\n[1] Domain       : {website_domain}  (search hint: '{hint}')")

    # Step 2: Name variants (Pinyin + Wade-Giles + English)
    variants = get_name_variants(name)
    log(f"[2] Name variants: {variants}")

    # Step 3: Pattern discovery — website FIRST, job boards as fallback
    log(f"[3] Discovering email patterns...")
    found: list[str] = []
    email_domain = website_domain

    log(f"    → company website (BFS, up to 35 pages)")
    found, email_domain = scrape_company_website(website_domain)

    if email_domain != website_domain:
        log(f"    ⚠  email domain differs from website: @{email_domain}")

    if not found:
        log(f"    → 104.com.tw (fallback)")
        found += search_104(hint, email_domain)

    if not found:
        log(f"    → 1111.com.tw (fallback)")
        found += search_1111(hint, email_domain)

    found = list(set(found))
    pattern = infer_pattern(found)
    log(f"    Found in wild  : {found or '(none)'}")
    log(f"    Pattern        : {pattern or 'unknown — applying all patterns'}")

    # Step 4: Generate candidates
    candidates = generate_candidates(variants, email_domain, pattern)
    log(f"[4] Candidates    : {len(candidates)} generated")

    # Step 5: SMTP verification
    log(f"[5] SMTP verification via MX...")
    mx = get_mx(email_domain)
    if not mx:
        log("    MX lookup failed — skipping SMTP verification")
        results = [(c, "unverified") for c in candidates]
        catch_all_flag = None
    else:
        log(f"    MX: {mx}")
        batch, catch_all_flag = smtp_batch_probe(candidates, mx, email_domain)
        if catch_all_flag:
            log("    WARNING: catch-all detected — SMTP results unreliable")
            results = [(e, "unverified (catch-all)") for e in candidates]
        else:
            results = [(e, batch.get(e, "unknown")) for e in candidates]

    return {
        "domain":       email_domain,
        "website":      website_domain,
        "pattern":      pattern,
        "found_wild":   found,
        "catch_all":    catch_all_flag,
        "candidates":   results,
    }


def print_results(data: dict):
    print("\n" + "═" * 62)
    print("  RESULTS")
    print("═" * 62)
    print(f"  Website domain : {data['website']}")
    print(f"  Email domain   : {data['domain']}")
    print(f"  Pattern found  : {data['pattern'] or 'none'}")
    print(f"  Emails in wild : {data['found_wild'] or '(none found)'}")
    catch = data.get("catch_all")
    if catch is True:
        print("  Catch-all      : YES — SMTP results not reliable")
    elif catch is False:
        print("  Catch-all      : no")
    print()
    print(f"  {'Candidate Email':<47}  Status")
    print(f"  {'-'*47}  --------")
    for email, status in data["candidates"]:
        marker = " <--" if status == "valid" else ""
        print(f"  {email:<47}  {status}{marker}")
    print("═" * 62)


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    result = run(name=sys.argv[1], company_url=sys.argv[2])
    print_results(result)
