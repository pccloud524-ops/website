import asyncio, re, os
from urllib.parse import urlparse, urljoin
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

app = FastAPI(title="COMPAMED Bulk Company Data Extractor")
app.mount("/static", StaticFiles(directory="static"), name="static")

EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"']+")

BLOCKED_HOSTS = (
    "compamed-tradefair.com", "messe-duesseldorf.com", "facebook.com",
    "instagram.com", "linkedin.com", "youtube.com", "x.com", "twitter.com"
)

class ExtractRequest(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=100)

def clean_url(value: str) -> str:
    value = value.strip().rstrip(".,;)")
    if not value:
        return ""
    if value.startswith("www."):
        value = "https://" + value
    if not value.lower().startswith(("http://", "https://")):
        return ""
    try:
        p = urlparse(value)
        if not p.hostname:
            return ""
        return f"{p.scheme}://{p.netloc}"
    except Exception:
        return ""

def is_company_site(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
        return bool(host) and not any(host == h or host.endswith("." + h) for h in BLOCKED_HOSTS)
    except Exception:
        return False

async def extract_one(browser, url: str, sem: asyncio.Semaphore):
    async with sem:
        result = {"profile": url, "website": "", "email": "", "status": "Not found", "note": ""}
        page = await browser.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=35000)
            await page.wait_for_timeout(1200)

            # Prefer the Company Data tab/button when present.
            for label in ["Company Data", "Company data", "Company information", "Company Information"]:
                try:
                    locator = page.get_by_text(label, exact=True).first
                    if await locator.count() and await locator.is_visible():
                        await locator.click(timeout=2500)
                        await page.wait_for_timeout(900)
                        break
                except Exception:
                    pass

            collected_text = []
            collected_links = []

            async def collect_from_frame(frame):
                try:
                    txt = await frame.locator("body").inner_text(timeout=5000)
                    if txt:
                        collected_text.append(txt)
                    links = await frame.locator("a[href]").evaluate_all(
                        "(els) => els.map(a => ({href:a.href, text:(a.innerText||'').trim()}))"
                    )
                    collected_links.extend(links or [])
                except Exception:
                    pass

            # Collect main page and all accessible frames (including Company Data iframe).
            await collect_from_frame(page.main_frame)
            for frame in page.frames:
                if frame != page.main_frame:
                    await collect_from_frame(frame)

            text_blob = "\n".join(collected_text)
            emails = []
            for e in EMAIL_RE.findall(text_blob):
                e = e.strip(".,;:")
                if e.lower() not in [x.lower() for x in emails]:
                    emails.append(e)

            candidate_urls = []
            for item in collected_links:
                href = item.get("href", "")
                label = item.get("text", "")
                normalized = clean_url(href)
                if normalized and is_company_site(normalized):
                    # Prefer links labelled website / homepage / company site.
                    score = 2 if re.search(r"website|homepage|webseite|home page", label, re.I) else 1
                    candidate_urls.append((score, normalized))
            for raw in URL_RE.findall(text_blob):
                normalized = clean_url(raw)
                if normalized and is_company_site(normalized):
                    candidate_urls.append((1, normalized))

            if candidate_urls:
                candidate_urls.sort(key=lambda x: x[0], reverse=True)
                result["website"] = candidate_urls[0][1]
            if emails:
                # Filter obvious platform/support addresses where possible.
                filtered = [e for e in emails if not any(x in e.lower() for x in ["messe-duesseldorf", "compamed"])]
                result["email"] = (filtered or emails)[0]

            if result["website"] or result["email"]:
                result["status"] = "Found"
                if not result["website"] or not result["email"]:
                    result["note"] = "Only the available field was found."
            else:
                result["note"] = "No website/email detected. The profile may block automated access or load data dynamically."
        except PlaywrightTimeoutError:
            result["status"] = "Error"
            result["note"] = "Page timed out."
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

@app.post("/api/extract")
async def extract(req: ExtractRequest):
    urls = []
    for raw in req.urls:
        u = raw.strip()
        if not u:
            continue
        p = urlparse(u)
        if p.scheme not in ("http", "https") or not p.netloc:
            urls.append(u)
        else:
            urls.append(u)
    if not urls:
        raise HTTPException(400, "Paste at least one valid profile URL.")
    if len(urls) > 100:
        raise HTTPException(400, "Maximum 100 profile links per batch.")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        sem = asyncio.Semaphore(3)
        try:
            results = await asyncio.gather(*(extract_one(browser, u, sem) for u in urls))
        finally:
            await browser.close()
    return {"total": len(results), "found": sum(1 for x in results if x["status"] == "Found"), "results": results}

@app.get("/health")
async def health():
    return {"ok": True}
