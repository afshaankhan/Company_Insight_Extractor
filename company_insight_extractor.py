"""
Company Intelligence Extractor
--------------------------------
Given one or more public URLs (e.g., a LinkedIn/Crunchbase/Wellfound profile
OR the company's own website), this script:

1) Tries to discover the company's official site (via DuckDuckGo).
2) Crawls a small set of "high-signal" pages (/, /about, /team, /leadership, /people, /company).
3) Parses visible text, OpenGraph/meta tags, and JSON-LD to extract:
   - Company name, website, description, founded year, location hints
   - Founder/CEO names (best-effort) and emails (best-effort)
   - Social links (LinkedIn, X/Twitter, Facebook, Instagram)

It writes results to output.xlsx and prints a compact terminal summary.

Notes:
- This is a best-effort heuristic scraper; it is *not* perfect.
- Public LinkedIn pages are JS-heavy; we mostly rely on the official site or third-party sources.
- Respect target sites’ Terms of Service and robots.txt.
- Add your own caching / retries if you’re doing large batches.
"""

import re
import sys
import json
import time
import tldextract
import requests
import pandas as pd
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin, unquote
from duckduckgo_search import DDGS
from readability import Document  # currently unused; keep if you plan to summarize long pages
from slugify import slugify
from collections import deque

# -------------------------
# Config / Tunables
# -------------------------
HEADERS = {
    # Pretend to be a modern browser so some CDNs won’t block us immediately.
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36")
}
TIMEOUT = 20                # requests timeout (seconds)
REQUEST_PAUSE = 1.0         # polite pause between requests (seconds)
MAX_BYTES = 2_000_000       # max bytes to read per response to avoid huge downloads
CANDIDATE_PATHS = ["", "about", "team", "leadership", "people", "company"]  # "high-signal" pages to fetch

# Regexes & keyword lists
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
JSON_LD_RE = re.compile(r"application/ld\+json", re.I)
# e.g., "Seattle, WA" / "New York, NY" style matches
US_LIKE_LOCATION_RE = re.compile(r"\b([A-Z][a-zA-Z]+(?:[\s\-][A-Z][a-zA-Z]+)*)\s*,\s*([A-Z]{2})\b")
ROLE_KEYWORDS = {
    # crude proximity-based role detection near an email address
    "ceo": ["ceo", "chief executive officer"],
    "founder": ["founder", "co-founder", "cofounder"]
}

def safe_get(url):
    """
    Make a GET request with streaming + byte cap + lenient decoding.
    Returns a lightweight response-like object with .status_code, .url, .text
    or None on failure.

    We stream to avoid loading multi-MB assets; we also guard against missing
    encoding headers by decoding with 'ignore' on errors.
    """
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True, stream=True)
        content = b""
        size = 0
        for chunk in r.iter_content(8192):
            size += len(chunk)
            if size > MAX_BYTES:
                break  # stop reading after cap
            content += chunk
        r.close()

        # Build a tiny response object to avoid leaking the full requests.Response
        resp = type("Resp", (), {})()
        resp.status_code = r.status_code
        resp.url = r.url  # final URL after redirects

        # Best-effort decoding
        try:
            resp.text = content.decode(r.encoding or "utf-8", errors="ignore")
        except Exception:
            resp.text = content.decode("utf-8", errors="ignore")
        return resp
    except Exception:
        return None

def get_internal_links(html, base_url):
    """
    Extract absolute, same-domain links from an HTML page. (Not used in the
    current strategy but handy if you expand to a deeper crawl.)
    """
    soup = BeautifulSoup(html, "lxml")
    links = set()
    base_domain = urlparse(base_url).netloc
    for a in soup.find_all("a", href=True):
        href = a['href']
        if href.startswith("/"):
            full_url = urljoin(base_url, href)
        elif href.startswith(base_url):
            full_url = href
        else:
            continue
        if urlparse(full_url).netloc == base_domain:
            links.add(full_url.split('#')[0])
    return links

def normalize_base(url):
    """
    Normalize a URL to 'scheme://host/' (strip path/query/fragment).
    Adds 'https://' if scheme is missing.
    """
    if not url:
        return None
    if not url.startswith("http"):
        url = "https://" + url
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}/"

def domain_from_url(url):
    """
    Extract the registered domain (e.g., 'example.com' from 'https://sub.example.com').
    """
    if not url:
        return None
    ext = tldextract.extract(url)
    return ext.registered_domain

def ddg_find_official_site(company_name):
    """
    Use DuckDuckGo to guess the official site for a given company name.
    Skips social/profiles (LinkedIn, X, etc.). Returns normalized base URL or None.
    """
    with DDGS() as ddgs:
        q = f"{company_name} official site"
        for r in ddgs.text(q, max_results=5):
            u = r.get("href") or r.get("url")
            if not u:
                continue
            if any(s in u for s in [
                "linkedin.com", "twitter.com", "facebook.com", "instagram.com",
                "crunchbase.com", "wellfound.com", "angel.co", "apollo.io"
            ]):
                continue
            return normalize_base(u)
    return None

def try_public_crunchbase(company_name):
    """
    Try to find a public Crunchbase org page and parse a 'Headquarters Location'
    hint out of the text. Returns (location, resolved_url) or (None, None).
    """
    with DDGS() as ddgs:
        q = f"site:crunchbase.com/organization {company_name}"
        for r in ddgs.text(q, max_results=3):
            url = r.get("href") or r.get("url")
            if not url:
                continue
            resp = safe_get(url)
            time.sleep(REQUEST_PAUSE)
            if not resp or resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "lxml")
            text = soup.get_text(" ", strip=True)
            m = re.search(r"Headquarters Location\s*([^\n]+?)\s{2,}", text)
            if m:
                loc = m.group(1).strip()
                loc = re.sub(r"\s{2,}.*", "", loc).strip()  # trim trailing blocks
                return (loc, resp.url)
    return (None, None)

def extract_jsonld_locations(soup):
    """
    Scan all <script type="application/ld+json"> tags and collect any postal addresses
    found in Organization/Place JSON-LD objects.
    """
    locs = []
    for tag in soup.find_all("script", type=JSON_LD_RE):
        try:
            data = json.loads(tag.string or "{}")
            if isinstance(data, list):
                for d in data:
                    locs.extend(_jsonld_to_locations(d))
            else:
                locs.extend(_jsonld_to_locations(data))
        except Exception:
            continue
    return [l for l in locs if l]

def _jsonld_to_locations(data):
    """
    Helper to extract address strings from a JSON-LD dict (Organization/Place).
    """
    out = []
    if not isinstance(data, dict):
        return out

    # Organization-ish records
    if data.get("@type") in ("Organization", "LocalBusiness", "Corporation"):
        addr = data.get("address")
        if isinstance(addr, dict):
            parts = [
                addr.get("streetAddress", ""),
                addr.get("addressLocality", ""),
                addr.get("addressRegion", ""),
                addr.get("postalCode", ""),
                addr.get("addressCountry", ""),
            ]
            out.append(", ".join([p for p in parts if p]).strip(", "))

    # Place records
    if data.get("@type") == "Place":
        addr = data.get("address")
        if isinstance(addr, dict):
            parts = [
                addr.get("streetAddress", ""),
                addr.get("addressLocality", ""),
                addr.get("addressRegion", ""),
                addr.get("postalCode", ""),
                addr.get("addressCountry", ""),
            ]
            out.append(", ".join([p for p in parts if p]).strip(", "))

    # Graph container
    if "@graph" in data and isinstance(data["@graph"], list):
        for g in data["@graph"]:
            out.extend(_jsonld_to_locations(g))
    return out

def extract_footer_location_guess(soup):
    """
    Best-effort location hint from footer or whole-page text:
    - Looks for 'City, ST'
    - Falls back to 'Headquarters/HQ: <text>'
    """
    footer = soup.find("footer")
    hay = footer.get_text(" ", strip=True) if footer else soup.get_text(" ", strip=True)
    m = US_LIKE_LOCATION_RE.search(hay)
    if m:
        return f"{m.group(1)}, {m.group(2)}"
    m2 = re.search(r"(Headquarters|HQ)\s*[:\-]?\s*([A-Za-z ,\-]+)", hay, re.I)
    if m2:
        return m2.group(2).strip(" -:")
    return None

def extract_company_name_from_site(soup, fallback_domain):
    """
    Attempt to get a clean company name from <title>, else fall back to domain.
    """
    title = (soup.title.string or "").strip() if soup.title else ""
    title = re.sub(r"\s*[\-\|\·•].*$", "", title).strip()  # strip taglines after separators
    if title and len(title) > 1:
        return title
    domain = fallback_domain.replace("www.", "")
    return slugify(domain, separator=" ").title()

def extract_emails_and_roles(html, company_domain):
    """
    Find emails on the page, filter to same-domain if possible, and try to infer
    whether any email appears near role keywords (CEO/Founder) in the text window.
    """
    emails = set(EMAIL_RE.findall(html))
    if company_domain:
        emails = {e for e in emails if e.lower().endswith("@" + company_domain.lower())}

    role_map = {"ceo": None, "founder": None}
    lower_html = html.lower()

    for e in emails:
        idx = lower_html.find(e.lower())
        # 200 chars on both sides = crude proximity window
        left = lower_html[max(0, idx-200): idx]
        right = lower_html[idx: idx+200]
        window = left + right

        if any(k in window for k in ROLE_KEYWORDS["ceo"]) and role_map["ceo"] is None:
            role_map["ceo"] = e
        if any(k in window for k in ROLE_KEYWORDS["founder"]) and role_map["founder"] is None:
            role_map["founder"] = e

    # Pick a "best" CEO email; if we didn't detect a role, just take any
    ceo_email = role_map["ceo"] or (list(emails)[0] if emails else None)
    founder_email = role_map["founder"]
    if founder_email == ceo_email:
        founder_email = None
    return ceo_email, founder_email

def extract_people_locations_from_text(text):
    """
    Grab up to three location tokens from raw text:
    - Exact 'City, ST' matches
    - Common US city name presence as a weaker hint
    """
    locs = set()
    for m in US_LIKE_LOCATION_RE.finditer(text):
        locs.add(f"{m.group(1)}, {m.group(2)}")

    for city in ["Chicago", "New York", "San Francisco", "Austin", "Seattle", "St. Louis",
                 "Los Angeles", "Boston", "Miami", "Denver", "Philadelphia", "Dallas", "Atlanta"]:
        if re.search(rf"\b{re.escape(city)}\b", text):
            locs.add(city)
    return list(locs)[:3]

def crawl_company_pages(base_url):
    """
    Fetch a small set of predetermined pages from a site and return [(url, html)].
    This keeps crawling predictable and fast.
    """
    pages = []
    for p in CANDIDATE_PATHS:
        target = urljoin(base_url, p)
        resp = safe_get(target)
        time.sleep(REQUEST_PAUSE)
        if not resp or resp.status_code != 200:
            continue
        pages.append((resp.url, resp.text))
    return pages

# -------- Source-specific helpers (LinkedIn / Wellfound / Apollo / Crunchbase) --------
def source_hint_company_name(url, html):
    """
    Try to infer a company name from the source page:
    1) <title> if it's short/sane
    2) URL slug (e.g., '/company/some-startup' -> 'Some Startup')
    """
    if html:
        soup = BeautifulSoup(html, "lxml")
        if soup.title and soup.title.string:
            t = soup.title.string.strip()
            t = re.sub(r"\s*[\-\|\·•].*$", "", t).strip()
            if 1 < len(t) <= 80:
                return t

    path = urlparse(url).path.strip("/")
    slug = path.split("/")[-1]
    slug = slug.replace("company/", "").replace("organizations/", "")
    slug = slug.replace("about", "").replace("overview", "").replace("jobs", "")
    slug = slug.strip("-_/")
    slug = unquote(slug)
    if slug:
        return slugify(slug, separator=" ").title()
    return None

def parse_wellfound_location(html):
    """
    On Wellfound/AngelList public pages, attempt a simple 'Location: ...' scrape
    or fall back to JSON-LD.
    """
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)
    m = re.search(r"Location\s*([A-Za-z ,\-]+)", text, re.I)
    if m:
        return m.group(1).strip(" -:")
    locs = extract_jsonld_locations(soup)
    return locs[0] if locs else None

# ----------------- Core pipeline -----------------
def process_company_from_source_url(src_url):
    """
    Orchestrate the full flow for a single input URL:
    - Fetch source
    - Infer source company name
    - Try to find official website
    - Crawl the website's "important" pages
    - Extract company/person fields per page
    - Return a list of row dicts (one row per crawled page)
    """
    src_url = src_url.strip()
    dom = domain_from_url(src_url) or ""

    print(f"[DEBUG] Fetching source URL: {src_url}")
    resp = safe_get(src_url)
    time.sleep(REQUEST_PAUSE)
    if resp:
        print(f"[DEBUG] Source status code: {resp.status_code}")
        print(f"[DEBUG] Source final URL: {resp.url}")
        print(f"[DEBUG] Source HTML length: {len(resp.text) if hasattr(resp, 'text') else 0}")
    else:
        print("[DEBUG] Failed to fetch source URL.")
    html = resp.text if (resp and resp.status_code == 200) else ""
    src_company = source_hint_company_name(src_url, html) or ""

    # Try to extract a location hint directly from the source page
    company_location_on_li_hint = ""
    if "crunchbase.com" in dom:
        # Reserved for a richer Crunchbase parser if you add one later.
        pass
    elif "wellfound.com" in dom or "angel.co" in dom:
        loc = parse_wellfound_location(html) if html else None
        if loc:
            company_location_on_li_hint = loc
    elif "linkedin.com" in dom:
        # Public LinkedIn is JS-driven; we’ll rely on the official site crawling instead.
        company_location_on_li_hint = ""

    # If we have a readable name, search for the official site; else search using the URL
    company_name_for_search = src_company if src_company else src_url

    try:
        official_site = ddg_find_official_site(company_name_for_search)
    except Exception as e:
        print(f"[DEBUG] DuckDuckGo search failed or was rate-limited: {e}")
        official_site = None
    print(f"[DEBUG] Official site found: {official_site}")

    # If we couldn't find the official site, crawl the provided source URL directly
    crawl_target = official_site if official_site else src_url
    print(f"[DEBUG] Crawling pages from: {crawl_target}")
    pages = crawl_company_pages(crawl_target)
    print(f"[DEBUG] Number of pages fetched: {len(pages)}")
    for i, (url, page_html) in enumerate(pages):
        print(f"[DEBUG] Page {i+1} URL: {url}, HTML length: {len(page_html)}")

    # We’ll (re)visit only a few important paths for consistency and speed.
    print(f"[DEBUG] Starting full site crawl from: {crawl_target}")
    sys.stdout.flush()

    # Helper to read common meta tags
    def extract_meta(soup, prop):
        tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
        return tag["content"].strip() if tag and tag.has_attr("content") else ""

    important_paths = ["", "about", "team", "leadership", "people", "company"]
    results = []

    import re  # (local import OK; already imported at top)

    for path in important_paths:
        url = urljoin(crawl_target, path)
        print(f"[DEBUG] Visiting: {url}")
        sys.stdout.flush()

        resp = safe_get(url)
        if not resp or resp.status_code != 200:
            print(f"[DEBUG] Failed to fetch or bad status for: {url}")
            sys.stdout.flush()
            continue
        html = resp.text
        print(f"[DEBUG] HTML length for {url}: {len(html)}")
        sys.stdout.flush()
        soup = BeautifulSoup(html, "lxml")

        # -------- JSON-LD: try to find an Organization/Corporation/Person record --------
        jsonld_tags = soup.find_all("script", type="application/ld+json")
        jsonld = None
        if jsonld_tags:
            for tag in jsonld_tags:
                try:
                    data = json.loads(tag.string)
                    if isinstance(data, dict) and (data.get("@type") in ("Organization", "Corporation", "Person") or "name" in data):
                        jsonld = data
                        break
                except Exception:
                    continue

        # -------- OpenGraph / Meta fallbacks --------
        og_title = extract_meta(soup, "og:title")
        og_desc = extract_meta(soup, "og:description")
        meta_title = extract_meta(soup, "title")
        meta_desc = extract_meta(soup, "description")

        text = soup.get_text("\n", strip=True)  # visible text as a long string

        # Helper for single-value regex extraction
        def extract_field(pattern, text, group=1):
            m = re.search(pattern, text, re.I)
            if not m:
                return ""
            try:
                return m.group(group).strip()
            except IndexError:
                return m.group(0).strip()

        # Company Name: JSON-LD -> OG -> meta -> first <h1>
        resolved_company_name = jsonld.get("name", "") if jsonld else ""
        if not resolved_company_name:
            resolved_company_name = og_title or meta_title or ""
        if not resolved_company_name:
            h1 = soup.find("h1")
            if h1:
                resolved_company_name = h1.get_text(strip=True)

        # Description: JSON-LD -> OG -> meta -> first <p>
        web_loc_hint = jsonld.get("description", "") if jsonld else ""
        if not web_loc_hint:
            web_loc_hint = og_desc or meta_desc or ""
        if not web_loc_hint:
            p = soup.find("p")
            if p:
                web_loc_hint = p.get_text(strip=True)

        # Founded year (common patterns)
        founded = extract_field(r"Founded:?\s*([0-9]{4})", text)
        if not founded:
            founded = extract_field(r"Batch:?\s*[^0-9]*([0-9]{4})", text)

        # Location: look for "Location: ..." or a known city keyword as weak hint
        location = extract_field(r"Location:?\s*([A-Za-z ,\-]+)", text)
        if not location:
            location = extract_field(r"San Francisco|New York|London|Berlin|Boston|Los Angeles|Austin|Seattle|Miami|Chicago|Toronto|Remote", text)

        # Website: naked URL in text or anchor that looks like a 'website' link
        website = extract_field(r"https?://[\w\.-]+", text)
        if not website:
            a = soup.find("a", href=True, text=re.compile(r"coinbase\.com|website|visit", re.I))
            if a:
                website = a["href"]

        # Very rough founder/CEO name scrape from on-page text & common patterns
        founders = []
        ceo_name = ""
        ceo_email = ""
        founder_name = ""
        founder_email = ""

        for m in re.finditer(r"Founders?\s*\n*([A-Za-z .,'-]+)", text):
            name = m.group(1).strip()
            if name:
                founders.append(name)

        ceo_match = re.search(r"([A-Za-z .,'-]+)[^\n]*\bCEO\b", text, re.I)
        if ceo_match:
            ceo_name = ceo_match.group(1)
        elif founders:
            ceo_name = founders[0]

        founder_name = founders[0] if founders else ""

        # If we see LinkedIn links, we sometimes find names in prominent heading divs (site-specific heuristic)
        name_pattern = re.compile(r"^[A-Z][a-zA-Z .,'-]{2,}$")
        non_name_words = set(["directory", "company", "jobs", "news", "team", "about", "public", "crypto", "web3", "location", "status", "batch", "size", "partner"])

        linkedin_found = any("linkedin.com" in (a.get("href") or "") for a in soup.find_all("a", href=True))
        if linkedin_found:
            for div in soup.find_all("div", class_=lambda c: c and "text-xl" in c and "font-bold" in c):
                div_name = div.get_text(" ", strip=True)
                if name_pattern.match(div_name) and div_name.lower() not in non_name_words:
                    if not founder_name:
                        founder_name = div_name
                    if not ceo_name:
                        ceo_name = div_name
                    break

            # Very rare: emails embedded in mailto: or query params on LinkedIn anchors
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "linkedin.com" not in href:
                    continue
                if "mailto:" in href:
                    email_match = re.search(r"mailto:([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+)", href)
                    if email_match:
                        founder_email = founder_email or email_match.group(1)
                        ceo_email = ceo_email or email_match.group(1)
                email_match = re.search(r"[?&]email=([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+)", href)
                if email_match:
                    founder_email = founder_email or email_match.group(1)
                    ceo_email = ceo_email or email_match.group(1)

        # Email extraction with role proximity
        company_domain = domain_from_url(url)
        ceo_email, founder_email = extract_emails_and_roles(html, company_domain)

        # Collect social links found on page
        social_links = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if any(s in href for s in ["linkedin.com", "twitter.com", "facebook.com", "instagram.com"]):
                social_links.add(href)

        # --- DEBUG OUTPUT (optional; keep for transparency while iterating) ---
        print(f"\n[DEBUG] Crawled: {url}")
        print(f"  Text (first 500 chars): {text[:500]}")
        print(f"  JSON-LD: {json.dumps(jsonld, indent=2) if jsonld else '[none]'}")
        print(f"  OpenGraph Title: {og_title}")
        print(f"  OpenGraph Desc: {og_desc}")
        print(f"  Meta Title: {meta_title}")
        print(f"  Meta Desc: {meta_desc}")
        print(f"  Company Name: {resolved_company_name}")
        print(f"  Description: {web_loc_hint}")
        print(f"  Founded: {founded}")
        print(f"  Location: {location}")
        print(f"  Website: {website}")
        print(f"  CEO Name: {ceo_name}")
        print(f"  Founder Name: {founder_name}")
        print(f"  CEO Email: {ceo_email}")
        print(f"  Founder Email: {founder_email}")
        print(f"  Social Links: {', '.join(social_links) if social_links else '[none]'}")

        results.append({
            "Page URL": url,
            "Company Name": resolved_company_name,
            "Company URL": website,
            "Description": web_loc_hint,
            "Founded": founded,
            "Location": location,
            "Social Links": ", ".join(social_links),
            "CEO Name": ceo_name,
            "CEO Email": ceo_email,
            "Founder Name": founder_name,
            "Founder Email": founder_email
        })

    print(f"[DEBUG] Crawled {len(results)} important pages.")

    # Deduplicate rows by Page URL (keeps first seen)
    seen = set()
    deduped = []
    for r in results:
        if r["Page URL"] not in seen:
            deduped.append(r)
            seen.add(r["Page URL"])
    return deduped

def prompt_urls():
    """
    Interactive prompt to collect HTTP/HTTPS URLs from stdin.
    Stops on empty line or EOF. Skips non-URL input.
    """
    urls = []
    while True:
        try:
            line = input("> ").strip()
        except EOFError:
            break
        if not line:
            break
        if not line.startswith("http"):
            print("  (Skipping: not a URL)")
            continue
        urls.append(line)
    return urls

def main():
    """
    Entry point:
    - Prompt user for one or more URLs
    - Process each URL
    - Save combined results to 'output.xlsx'
    - Print a concise success/fail ticked summary to terminal
    """
    urls = prompt_urls()
    if not urls:
        print("No URLs provided. Exiting.")
        return

    all_results = []
    for u in urls:
        try:
            print(f"Processing: {u}")
            out = process_company_from_source_url(u)
            if isinstance(out, list):
                all_results.extend(out)
            elif isinstance(out, dict):
                all_results.append(out)
        except Exception as e:
            # On failure, append a placeholder row so the run still yields a file
            print(f"[ERROR] Exception for {u}: {e}")
            all_results.append({
                "Company Location on LinkedIn": "",
                "Company Name": "",
                "Company URL": "",
                "CEO Email": "",
                "CEO Location": "",
                "CoFounder Email": "",
                "CoFounder Location": ""
            })

    df = pd.DataFrame(all_results) if all_results else pd.DataFrame([{}])
    df.to_excel("output.xlsx", index=False)
    print("Done. Wrote output.xlsx")

    # Terminal summary with ✅/❌ per field for quick visual QA
    print("\nExtracted Results:")
    for i, row in df.iterrows():
        print(f"\nResult {i+1}:")
        for col in df.columns:
            val = row[col]
            tick = '✅' if val else '❌'
            print(f"  {col}: {tick} {val if val else '[empty]'}")

if __name__ == "__main__":
    main()
