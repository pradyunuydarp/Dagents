# Link previews and icons

This folder holds the sources of the images the three published sites use in
link previews and browser tabs. A link to a site shared on LinkedIn, WhatsApp,
Slack or X shows that site's card. The card is the image named by the page's
`og:image` tag.

## Sources

| File | What it is |
|---|---|
| `framework.html` | The card for the framework site |
| `healthcare.html` | The card for the stroke triage demo |
| `nl2sql.html` | The card for the NL2SQL demo |
| `card.css` | Styles the three cards share. Every colour comes from a design system token. |
| `mark.svg` | The logo mark for light backgrounds, for 64 px and larger |
| `mark-dark.svg` | The same mark for dark backgrounds |
| `mark-32.svg` | The light mark with thicker lines and larger dots, for 32 px |
| `favicon.svg` | The tab icon: the heaviest lines and dots, for 16 px. It switches to the dark mark when the browser prefers a dark colour scheme. |
| `social-meta.mjs` | The Vite plugin that writes the published address into each page's link preview tags |
| `rendered.json` | Written by the render script: the SHA-256 of every output and of every file it was made from. Do not edit it. |

The cards show structure and purpose only: no record counts, scores, dates or
results, because those change. Each card's footer shows its site's address,
which the render script reads from `env/.env.published`.

## Outputs

`scripts/render_social_cards.mjs` writes these files into the `public/` folder
of each frontend (`site/`, `apps/healthcare-demo/frontend/`,
`services/nl2sql-demo/frontend/`). Vite copies `public/` into the build.

| Output | Made from | Size |
|---|---|---|
| `social-preview.png` | that site's card | 1200 x 630 |
| `favicon.svg` | a copy of `favicon.svg` | any |
| `favicon-32.png` | `mark-32.svg` | 32 x 32 |
| `apple-touch-icon.png` | `mark.svg`, with square corners because iOS rounds the icon itself | 180 x 180 |
| `icon-512.png` | `mark.svg`, listed in each site's `site.webmanifest` | 512 x 512 |

Each site's `social-preview.png` is its own card. The four icons are the same
file in all three folders: the copies in `apps/healthcare-demo/frontend/public/`
and `services/nl2sql-demo/frontend/public/` are copies of the ones in
`site/public/`. The healthcare demo keeps its own copies because
`apps/healthcare-demo/` must stay extractable into its own repository.

`scripts/sync_design_system.py --write` copies `social-meta.mjs` into each
frontend, next to its `vite.config.ts`.

## Change a card or the mark

1. Edit the source here. Open the card's HTML file in a browser to see it. The
   browser uses your installed fonts; the render script uses Archivo and IBM
   Plex Mono from the `@fontsource` packages.
2. Render the outputs. The script needs the framework site's packages
   (`cd site && npm install`, which installs the fonts) and Chromium:

   ```bash
   node scripts/render_social_cards.mjs           # renders only what changed
   node scripts/render_social_cards.mjs --force   # renders everything again
   ```

3. Look at every PNG that changed.
4. Commit the sources, the outputs and `rendered.json` together.

`tests/test_social_previews.py` fails when a source, the design system, the
render script, a font version or a published address changed after the last
render. It also fails when a card is not 1200 x 630 or is over 300 KB, because
WhatsApp drops larger preview images.

The Pages workflow checks the published side with
`scripts/check_link_previews.py`: each address before the builds, each built
page before the upload, and each live `og:image` after the deploy.
