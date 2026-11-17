# COMPAMED Bulk Company Data Extractor

## Update/deploy
- Replace the contents of the GitHub `website` folder with these files.
- Render uses the `website` root directory and the Dockerfile.
- Netlify uses base directory `website` and publish directory `static`.
- The frontend is configured for `https://website-1-njye.onrender.com`.

## Optional extra click
On the frontend, choose Yes/No (neither is required). If Yes, enter the exact button/tab label. For every profile, the automation opens the COMPAMED Company Data section first, then optionally clicks the named control, and reads visible popup/panel content.

## Limits
- Up to 100 profile URLs per batch.
- Missing fields remain blank; the extractor does not guess.
- COMPAMED page markup or anti-automation changes may require selector updates. Review output before using it.
