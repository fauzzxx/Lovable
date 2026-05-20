Here's a spread of prompts you can paste straight into the chat box, grouped from simplest to most ambitious. The builder shines most when you're specific about features, pages, and (for backends) the actual endpoints you want — so I've written these to be concrete and copy-paste ready.

**Frontend-only (instant preview, no backend needed)**

- "A landing page for a coffee subscription called Daybreak — sticky nav, full-screen hero with a tagline, 3 plan cards (Light/Regular/Bold) with prices, a testimonials row, and a footer. Warm cream-and-brown palette, rounded corners."
- "A personal portfolio for a UX designer: hero with name and role, an about section, a 6-item project grid with hover effects, and a contact section. Minimal, lots of whitespace, dark mode."
- "A pricing page with a monthly/yearly toggle that updates the prices, three tiers, and a FAQ accordion."

**Full-stack with a Python backend + database (use the Run backend button)**

- "A to-do app with a FastAPI backend that stores tasks in SQLite. I can add, complete, edit, and delete tasks, filter by all/active/done, and they persist across reloads."
- "An expense tracker: add expenses with amount/category/date, see a running total and a category breakdown chart, and store everything in SQLite via a FastAPI backend with /expenses CRUD endpoints."
- "A URL shortener — paste a long URL, get a short code, and visiting /go/{code} redirects. FastAPI backend with SQLite, plus a table showing click counts."
- "A simple kanban board with three columns; cards are draggable between columns and saved to a FastAPI + SQLite backend."

**Apps that call an external API (these light up the "Backend keys" tab)**

- "A weather dashboard: type a city, the FastAPI backend calls the OpenWeather API (key from env var OPENWEATHER_API_KEY) and the page shows current conditions plus a 5-day forecast."
- "A chat UI that talks to OpenAI — the FastAPI backend reads OPENAI_API_KEY from env and streams replies. Clean message bubbles, a typing indicator, and a model selector."
- "A currency converter that fetches live rates from a public exchange-rate API on the backend and converts between any two currencies."

**Real-time / advanced (closest to your voice-agent example)**

- "An outbound voice-agent dashboard like a call center: a phone-number queue I can add to, a live call panel with a transcript area and sentiment/interest meters, and a recent-calls table. FastAPI backend with /call, /call/status, and /calls endpoints plus a /live websocket that pushes transcript and state events to the UI."
- "A live polling app: create a question with options, share it, and watch votes update in real time across clients via a websocket. FastAPI backend, in-memory store."

**Using a reference image**

Attach a screenshot of a site or a hand-drawn mockup with the 🖼 button and say: "Build this layout. Match the colors, spacing, and section order as closely as you can." It'll use the image to guide the design.

**Then refine** — once it's built, keep chatting in the same project: "make the header sticky and add a dark-mode toggle," "add a CSV export button that hits a new /export endpoint," "tighten the spacing and use a blue accent instead of green." Every change is a new version you can roll back.

One tip: the more you name specific pieces — sections, button behavior, endpoint paths, color palette — the closer the first result lands. Want me to pre-load a few of these as starter buttons on the welcome screen so they're one click away?