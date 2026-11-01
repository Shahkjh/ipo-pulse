# IPO Pulse

A weekly five-line note on new IPO filings with SEBI, written by Claude from real data.
Live: https://bespoke-souffle-de95ef.netlify.app

**Flow:** SEBI register → snapshot (`data/`) → compare with last week → Claude writes 5 lines → `site/index.html` → Netlify

## One-time setup
1. **API key:** repo **Settings → Secrets and variables → Actions → New repository secret**
   - OpenAI: name `OPENAI_API_KEY`, value from platform.openai.com/api-keys
   - or Claude: name `ANTHROPIC_API_KEY`, value from console.anthropic.com
   - (OpenAI is used if both are set. With neither, or if the API call fails, a simpler rule-based note is written.)
   - Optional variable `OPENAI_MODEL` to change the model (default `gpt-4o-mini`).
2. **Netlify:** open the `bespoke-souffle-de95ef` project → **Project configuration → Build & deploy → Link repository** → pick this repo.
   `netlify.toml` already sets the publish folder to `site`, with no build command.
3. **Test:** **Actions → IPO Pulse weekly → Run workflow**. After that it runs every Monday at 7:00 IST.

Optional repository variables: `FEEDBACK_URL` (defaults to the Google Form), `SITE_URL`.

## Run locally
```
pip install -r requirements.txt
ANTHROPIC_API_KEY=... python ipo_pulse.py
SEBI_HTML_FILE=tests/sebi_fixture.html python ipo_pulse.py --today 2026-10-29   # offline test
```

## Notes
- Reads page 1 of SEBI's register (25 rows), which covers a normal week; a rush week can spill onto page 2.
- One request per week. If SEBI changes its layout, the run stops with "No rows parsed".
