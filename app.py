import asyncio
import re
from urllib.parse import urlparse, urlunparse

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

app = FastAPI(title="COMPAMED Bulk Company Data Extractor")
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
BLOCKED_HOSTS = (
    "facebook.com", "instagram.com", "linkedin.com", "youtube.com", "youtu.be",
    "x.com", "twitter.com", "tiktok.com", "whatsapp.com", "messe-duesseldorf.com",
    "compamed-tradefair.com", "compamed.de", "duesseldorf.de"
)

class ExtractRequest(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=100)
    click_extra: bool = False
    extra_button_name: str = Field(default="", max_length=120)

def normalize_profile_url(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        raise ValueError("Empty profile URL")
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    p = urlparse(raw)
    host = (p.hostname or "").lower()
    if not (host == "compamed-tradefair.com" or host.endswith(".compamed-tradefair.com")):
        raise ValueError("Only COMPAMED exhibitor profile URLs are accepted")
    path = p.path.rstrip("/")
    if "/exhprofiles/" not in path:
        raise ValueError("Not a COMPAMED exhibitor profile URL")
    if not path.endswith("/details"):
        path += "/details"
    return urlunparse((p.scheme or "https", p.netloc, path, "", "", ""))

def clean_company_url(raw: str) -> str:
    raw = (raw or "").strip().rstrip(".,;:)]}>")
    if not raw:
        return ""
    if raw.startswith("www."):
        raw = "https://" + raw
    elif not re.match(r"(?i)^https?://", raw):
        if re.match(r"(?i)^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}(?:/.*)?$", raw):
            raw = "https://" + raw
        else:
            return ""
    try:
        p = urlparse(raw)
        host = (p.hostname or "").lower()
        if not host or "." not in host:
            return ""
        if any(host == d or host.endswith("." + d) for d in BLOCKED_HOSTS):
            return ""
        if host.startswith(("localhost", "127.")):
            return ""
        return urlunparse((p.scheme or "https", p.netloc, p.path.rstrip("/"), "", "", ""))
    except Exception:
        return ""

async def click_company_data(page):
    """Open COMPAMED's own Company data tab/panel before reading fields."""
    patterns = [
        page.get_by_role("tab", name=re.compile(r"^company\s+data$", re.I)),
        page.get_by_role("button", name=re.compile(r"^company\s+data$", re.I)),
        page.get_by_text(re.compile(r"^company\s+data$", re.I)),
    ]
    for group in patterns:
        try:
            loc = group.first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=4500)
                await page.wait_for_timeout(700)
                return True
        except Exception:
            pass
    return False

async def click_user_named_button(page, button_name: str):
    """Click an optional user-specified control after Company data has opened."""
    name = (button_name or "").strip()
    if not name:
        return "Skipped: no button name was entered."
    exact = re.compile(r"^\s*" + re.escape(name) + r"\s*$", re.I)
    groups = [
        page.get_by_role("button", name=exact),
        page.get_by_role("tab", name=exact),
        page.get_by_role("link", name=exact),
        page.get_by_text(exact),
    ]
    for group in groups:
        try:
            loc = group.first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=4500)
                await page.wait_for_timeout(650)
                return "Clicked: " + name
        except Exception:
            continue
    return "Button not found: " + name

async def read_visible_company_panel(page):
    """Read only visible popup/dialog or Company Data section, not global page links."""
    return await page.evaluate("""() => {
      const visible = el => {
        if (!el) return false;
        const s = getComputedStyle(el), r = el.getBoundingClientRect();
        return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
      };
      const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
      const selectors = [
        '[role="dialog"]', '[aria-modal="true"]', '.modal', '.modal-content',
        '[class*="dialog" i]', '[class*="modal" i]'
      ];
      let roots = [];
      for (const sel of selectors) {
        try { roots.push(...[...document.querySelectorAll(sel)].filter(visible)); } catch(e) {}
      }
      // A tabbed Company Data card may not use dialog semantics. Find visible section
      // containers that actually contain the Company Data heading.
      const headingNodes = [...document.querySelectorAll('body *')].filter(el =>
        visible(el) && el.children.length === 0 && norm(el.innerText || el.textContent) === 'company data'
      );
      for (const h of headingNodes) {
        let el = h;
        for (let i = 0; i < 8 && el; i++, el = el.parentElement) {
          if (visible(el)) roots.push(el);
        }
      }
      // Prefer the smallest relevant visible root containing company contact clues.
      const candidates = [...new Set(roots)].map(el => ({
        el, text: (el.innerText || '').trim(),
        links: [...el.querySelectorAll('a[href]')].filter(visible).map(a => ({
          href: a.href, text: (a.innerText || a.textContent || '').trim(),
          context: (a.parentElement?.innerText || '').trim()
        }))
      })).filter(x => x.text && x.text.length < 10000 &&
        (/company\s*data/i.test(x.text) || /e-?mail|@|website|webseite|homepage|\bweb\s*:/i.test(x.text)));
      candidates.sort((a,b) => a.text.length - b.text.length);
      if (candidates.length) return {text:candidates[0].text, links:candidates[0].links, source:'visible-panel'};
      // Last-resort extraction: locate a Company Data heading and stop before next tab section.
      const body = document.body.innerText || '';
      const m = body.match(/Company\s+data([\s\S]*?)(?:Social\s+networks|Notes|$)/i);
      if (m) return {text:'Company data ' + m[1], links:[], source:'company-data-section'};
      return {text:'', links:[], source:'none'};
    }""")

async def extract_one(browser, raw_url, sem, click_extra, extra_button_name):
    async with sem:
        result = {"profile": raw_url, "website": "", "email": "", "status": "Not found", "note": ""}
        page = await browser.new_page()
        try:
            profile_url = normalize_profile_url(raw_url)
            await page.goto(profile_url, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.get_by_text(re.compile(r"^company\s+data$", re.I)).first.wait_for(state="visible", timeout=9000)
            except Exception:
                await page.wait_for_timeout(1000)

            opened = await click_company_data(page)
            if not opened:
                # If the site already exposes Company Data content, the read step can still continue.
                await page.wait_for_timeout(400)

            if click_extra:
                result["note"] = await click_user_named_button(page, extra_button_name)

            panel = await read_visible_company_panel(page)
            text = panel.get("text", "")
            if not text:
                result["note"] = (result["note"] + " | " if result["note"] else "") + "No visible Company Data popup/panel was detected."
                return result

            emails = []
            for email in EMAIL_RE.findall(text):
                email = email.strip(".,;:")
                if email.lower() not in {x.lower() for x in emails}:
                    emails.append(email)
            if emails:
                result["email"] = emails[0]

            candidates = []
            # Only links explicitly labelled Website/Web/Homepage or placed beside such a label.
            for item in panel.get("links", []):
                href, label, context = item.get("href", ""), item.get("text", ""), item.get("context", "")
                if re.search(r"website|webseite|homepage|\bweb\b", label + " " + context, re.I):
                    url = clean_company_url(href)
                    if url:
                        candidates.append((5, url))
            # Explicitly parse labelled text lines only; never take arbitrary page URLs.
            for line in text.splitlines():
                m = re.search(r"(?i)\b(?:website|webseite|homepage|web)\s*:?\s*(.+)$", line.strip())
                if m:
                    tail = m.group(1).strip()
                    vals = URL_RE.findall(tail)
                    if vals:
                        for val in vals:
                            url = clean_company_url(val)
                            if url:
                                candidates.append((10, url))
                    else:
                        url = clean_company_url(tail)
                        if url:
                            candidates.append((10, url))
            if candidates:
                candidates.sort(key=lambda x: x[0], reverse=True)
                seen = set()
                for _, candidate in candidates:
                    if candidate not in seen:
                        result["website"] = candidate
                        break
                    seen.add(candidate)

            if result["website"] or result["email"]:
                result["status"] = "Found"
                if not result["website"] or not result["email"]:
                    result["note"] = (result["note"] + " | " if result["note"] else "") + "Only the available Company Data field was found."
            else:
                result["note"] = (result["note"] + " | " if result["note"] else "") + "Popup/panel read, but no labelled website or email was detected."
        except ValueError as exc:
            result["status"] = "Error"
            result["note"] = str(exc)
        except PlaywrightTimeoutError:
            result["status"] = "Error"
            result["note"] = "Profile or popup timed out while loading."
        except Exception as exc:
            result["status"] = "Error"
            result["note"] = str(exc)[:180]
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
    normalized, errors = [], []
    for raw in req.urls:
        try:
            normalized.append(normalize_profile_url(raw))
        except ValueError as exc:
            errors.append({"profile": raw, "website": "", "email": "", "status": "Error", "note": str(exc)})
    unique = list(dict.fromkeys(normalized))
    original = {}
    for raw in req.urls:
        try:
            original.setdefault(normalize_profile_url(raw), raw)
        except ValueError:
            pass
    if unique:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            try:
                # Conservative concurrency keeps popup interactions stable on small instances.
                sem = asyncio.Semaphore(3)
                results = await asyncio.gather(*[
                    extract_one(browser, original.get(u, u), sem, req.click_extra, req.extra_button_name)
                    for u in unique
                ])
            finally:
                await browser.close()
    else:
        results = []
    results.extend(errors)
    return {"total": len(results), "found": sum(1 for r in results if r["status"] == "Found"), "results": results}
