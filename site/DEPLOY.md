# Deploying the A.O.P.S. landing page to Vercel

The `site/` directory contains a single-file static landing page for the A.O.P.S.
project. The page is ~25 KB of HTML with embedded CSS/JS and a 1.4 MB demo GIF.

## Option A — Vercel CLI (one-shot)

```bash
npm i -g vercel
cd aops-sre-pipeline
vercel              # links the project, deploys to a preview URL
vercel --prod       # deploys to production
```

## Option B — GitHub + Vercel auto-deploy (recommended)

1. Push the repo to GitHub (already done — `adventurewave-labs/aops-sre-pipeline`).
2. Go to https://vercel.com/new
3. Import the `aops-sre-pipeline` repo.
4. Vercel auto-detects the `vercel.json` and uses `site/` as the output directory.
5. Click **Deploy**. The site is live in ~30 seconds.

## Option C — One-click "Deploy to Vercel" button

The landing page has a button that links to:

```
https://vercel.com/new/clone?repository-url=https://github.com/adventurewave-labs/aops-sre-pipeline
```

This clones the repo into your GitHub and deploys it to your Vercel account in one click.

## What gets served

| Path | Asset | Size |
|---|---|---|
| `/` | `site/index.html` | ~25 KB |
| `/aops-demo.gif` | the 9-second asciinema GIF | 1.4 MB |
| `/aops-demo-poster.png` | static poster frame (OG image) | 135 KB |

## Custom domain

In the Vercel dashboard → Settings → Domains → add your domain. The site
has no server-side state so it scales to global edge with zero config.
