Here are prompts to paste into the builder. I've grouped them by what deploys cleanly to Vercel vs. what's better kept local.

**Static — deploy to Vercel perfectly**

- "A landing page for a fitness app called PulseFit — hero, 3 feature cards, pricing toggle, FAQ accordion, footer. Dark theme, lime accent."
- "A portfolio site for a photographer: full-bleed image grid with lightbox, about section, contact form."
- "A pomodoro timer with start/pause/reset, session counter, and a settings panel for work/break lengths."

**Backend (request/response) — works great on Vercel serverless**

- "A contact form with a FastAPI endpoint that validates input and returns success/error. Show inline messages."
- "A markdown-to-HTML converter: textarea on the left, live preview on the right, conversion done by a FastAPI /convert endpoint."
- "A weather lookup: type a city, FastAPI backend calls a weather API (key from env var WEATHER_API_KEY) and shows current conditions."

**Backend (stateful/real-time) — test locally, deploy to Render/Railway, not Vercel**

- "A to-do app with FastAPI + SQLite that persists tasks." *(SQLite won't persist on Vercel.)*
- "A live chat room using a FastAPI WebSocket." *(WebSockets don't run on Vercel serverless.)*

Tip: be specific about sections, button behavior, endpoint paths, and colors — the more detail, the closer the first result lands. Then refine with follow-ups like "add dark mode" or "add a /export CSV endpoint."