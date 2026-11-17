# COMPAMED Bulk Company Data Extractor

## What it does
- Paste up to 100 exhibitor profile URLs (one per line).
- Opens each profile with a hosted Chromium browser using Playwright.
- Attempts to open/read Company Data and accessible frames.
- Extracts a company website and email where exposed.
- Copy Website + Email as tab-separated text for Excel, or download CSV.

## Deploy online (Render)
1. Create a GitHub repository and upload the contents of this folder.
2. Sign in to Render and choose **New + → Web Service**.
3. Connect the repository. Render detects `render.yaml` / Dockerfile.
4. Deploy. Wait for the build to finish.
5. Open the generated `https://....onrender.com` URL.

The Docker image installs Chromium and its system dependencies. Do not deploy as a static Netlify site: the browser automation requires a backend server.

## Notes and limitations
- COMPAMED may block automated browsing, require consent, or load profile details in a protected iframe. When the site does not expose the data to the server browser, the result remains blank/Not found.
- The extractor does not guess missing website or email values.
- Run 100 links per batch; avoid rapid repeated batches.
- This tool is intended for legitimate business contact research. Follow COMPAMED's terms and applicable privacy/anti-spam rules.
