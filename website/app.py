import asyncio
import re
from urllib.parse import urlparse, urlunparse

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

app = FastAPI(title="COMPAMED Company Data Extractor")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://ornate-griffin-c7a0d6.netlify.app"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")

EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
URL_RE = re.compile(r"(?i)(?:https?://|www\.)[^\s<>\"'()]+")
SOCIAL_HOSTS = (
    "facebook.com", "instagram.com", "linkedin.com", "youtube.com",
    "youtu.be", "x.com", "twitter.com", "tiktok.com", "whatsapp.com",
    "messe-duesseldorf.com", "compamed-tradefair.com", "compamed.de",
)

class ExtractRequest(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=100)

def normalize_profile_url(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        raise ValueError("Empty profile URL")
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    p = urlparse(raw)
    if not p.hostname or "compamed-tradefair.com" not in p.hostname.lower():
        raise ValueError("Only COMPAMED exhibitor profile URLs are accepted")
    path = p.path.rstrip("/")
    if "/exhprofiles/" not in path:
        raise ValueError("Not a COMPAMED exhibitor profile URL")
    if not path.endswith("/details"):
        path += "/details"
    return urlunparse((p.scheme or "https", p.netloc, path, "", "", ""))

def clean_company_url(raw: str) -> str:
    raw = (raw or "").strip().rstrip(".,;:)]}")
    if not raw:
        return ""
    if raw.startswith("www."):
        raw = "https://" + raw
    elif not re.match(r"(?i)^https?://", raw):
        # Company Data may show a bare domain after "Web:"
        if re.match(r"(?i)^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}(?:/.*)?$", raw):
            raw = "https://" + raw
        else:
            return ""
    try:
        p = urlparse(raw)
        host = (p.hostname or "").lower()
        if not host or "." not in host:
            return ""
        if any(host == d or host.endswith("." + d) for d in SOCIAL_HOSTS):
            return ""
        if host.startswith(("localhost", "127.")):
            return ""
        return urlunparse((p.scheme or "https", p.netloc, p.path.rstrip("/"), "", "", ""))
    except Exception:
        return ""

async def extract_company_panel(page):
    # The exhibitor data is displayed in the Company data tab/panel.
    # Click the tab explicitly; do not scrape the page-wide navigation/footer.
    for selector in [
        'text="Company data"',
        'text="Company Data"',
        'text="Company data"'
    ]:
        try:
            loc = page.locator(selector).first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=3500)
                await page.wait_for_timeout(350)
                break
        except Exception:
            continue

    # Find the smallest visible DOM container containing the Company data heading
    # and its contact fields. This prevents picking COMPAMED's own globe/homepage link.
    panel = await page.evaluate("""() => {
      const norm = s => (s || '').replace(/\\\\s+/g, ' ').trim().toLowerCase();
      const headings = [...document.querySelectorAll('body *')]
        .filter(el => el.children.length === 0 && norm(el.innerText || el.textContent) === 'company data');
      for (const h of headings) {
        let el = h;
        for (let i = 0; i < 9 && el; i++, el = el.parentElement) {
          const txt = (el.innerText || '').trim();
          const low = txt.toLowerCase();
          if (txt.length > 20 && txt.length < 7000 &&
              (low.includes('e-mail') || low.includes('email') || low.includes('@') || low.includes('web:'))) {
            const links = [...el.querySelectorAll('a[href]')].map(a => ({
              href: a.href, text: (a.innerText || a.textContent || '').trim()
            }));
            return {text: txt, links};
          }
        }
      }
      return null;
    }""")
    if panel:
        return panel

    # Fallback: take text only between Company data and the next section heading.
    body = await page.locator("body").inner_text(timeout=5000)
    match = re.search(r"(?is)Company\s+data(.*?)(?:Social\s+networks|Notes|$)", body)
    if match:
        return {"text": "Company data " + match.group(1), "links": []}
    return {"text": "", "links": []}

async def extract_one(browser, raw_url: str, sem: asyncio.Semaphore):
    async with sem:
        result = {
            "profile": raw_url, "website": "", "email": "",
            "status": "Not found", "note": ""
        }
        page = await browser.new_page()
        try:
            profile_url = normalize_profile_url(raw_url)
            await page.goto(profile_url, wait_until="domcontentloaded", timeout=25000)
            try:
                await page.get_by_text(re.compile(r"^Company data$", re.I)).first.wait_for(
                    state="visible", timeout=7000
                )
            except Exception:
                await page.wait_for_timeout(900)

            panel = await extract_company_panel(page)
            text = panel.get("text", "")
            if not text:
                result["note"] = "Company data panel was not found or did not load."
                return result

            # Extract email ONLY from Company data panel.
            emails = []
            for email in EMAIL_RE.findall(text):
                email = email.strip(".,;:")
                if email.lower() not in {x.lower() for x in emails}:
                    emails.append(email)
            if emails:
                result["email"] = emails[0]

            # Extract website ONLY from links/text inside Company data panel.
            candidates = []
            for item in panel.get("links", []):
                href = item.get("href", "")
                label = item.get("text", "")
                url = clean_company_url(href)
                if url:
                    score = 3 if re.search(r"website|webseite|homepage|^web$", label, re.I) else 1
                    candidates.append((score, url))

            # Parse explicit Web / Website / Homepage labelled text, not arbitrary URLs.
            for line in text.splitlines():
                if re.search(r"(?i)\b(web|website|homepage|webseite)\s*:", line):
                    tail = re.split(r"(?i)\\b(?:web|website|homepage|webseite)\s*:", line, maxsplit=1)[-1]
                    for raw in URL_RE.findall(tail):
                        url = clean_company_url(raw)
                        if url:
                            candidates.append((5, url))
                    # Also handle bare domain after Web:
                    if not candidates:
                        url = clean_company_url(tail.strip())
                        if url:
                            candidates.append((5, url))

            if candidates:
                candidates.sort(key=lambda x: x[0], reverse=True)
                # Deduplicate while preserving the highest-priority candidate.
                seen = set()
                for _, candidate in candidates:
                    if candidate not in seen:
                        result["website"] = candidate
                        break
                    seen.add(candidate)

            if result["website"] or result["email"]:
                result["status"] = "Found"
                if not result["website"] or not result["email"]:
                    result["note"] = "Only the field shown in the Company data panel was available."
            else:
                result["note"] = "Company data panel loaded, but no website/email was listed."
        except ValueError as exc:
            result["status"] = "Error"
            result["note"] = str(exc)
        except PlaywrightTimeoutError:
            result["status"] = "Error"
            result["note"] = "Profile timed out while loading."
        except Exception as exc:
            result["status"] = "Error"
            result["note"] = str(exc)[:160]
        finally:
            await page.close()
        return result

@app.get("/", response_class=HTMLResponse)
async def home():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/health")
async def health():
    return {"ok": True}

@app.post("/api/extract")
async def extract(req: ExtractRequest):
    normalized = []
    errors = []
    for raw in req.urls:
        try:
            normalized.append(normalize_profile_url(raw))
        except ValueError as exc:
            errors.append({"profile": raw, "website": "", "email": "", "status": "Error", "note": str(exc)})
    if not normalized and errors:
        return {"total": len(errors), "found": 0, "results": errors}

    # Deduplicate normalized profile URLs, but preserve original URLs in the output.
    unique = list(dict.fromkeys(normalized))
    original_by_normalized = {}
    for raw in req.urls:
        try:
            original_by_normalized.setdefault(normalize_profile_url(raw), raw)
        except ValueError:
            pass

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            sem = asyncio.Semaphore(5)
            tasks = [
                extract_one(browser, original_by_normalized.get(u, u), sem)
                for u in unique
            ]
            results = await asyncio.gather(*tasks)
        finally:
            await browser.close()

    results.extend(errors)
    return {
        "total": len(results),
        "found": sum(1 for r in results if r["status"] == "Found"),
        "results": results
    }


