---
type: agent
description: Fetches NASA's Astronomy Picture of the Day and writes a page showing the image.
vaults: main
output: NASA Picture of the Day.md
max_iterations: 4
schedule: daily
log: true
Tags: astronomy, nasa, apod, api, markdown, python
Summary: The task is to call the fetch_apod tool to obtain the APOD metadata and then generate a markdown page that includes the title, date, the image (or video link), the explanation, and a credit line, using only the data returned. No additional content, invention, or formatting beyond the specified markdown structure is allowed.
---

# Prompt

You write a daily astronomy page from NASA's Astronomy Picture of the Day (APOD).

Process:
1. Call fetch_apod once. It returns JSON with title, date, explanation, url, hdurl, and media_type.
2. Then write the page as your FINAL message.

Page format — output ONLY the markdown page, no preamble, no code fences:
- `## <title> (<date>)`
- If media_type is "image": the image embedded as `![<title>](<url>)` (use hdurl only if url is missing). If it is a video, link it instead: `[Watch today's video](<url>)`.
- The explanation text as one or two paragraphs.
- A credit line: `*Image credit: NASA APOD*`
Use ONLY tool-returned data. Do NOT invent anything.

# Tools

```python
def fetch_apod() -> str:
    """Fetch NASA's Astronomy Picture of the Day metadata (title, date,
    explanation, image URL) from the official NASA API."""
    import json
    import urllib.request
    with urllib.request.urlopen(
            "https://api.nasa.gov/planetary/apod?api_key=DEMO_KEY",
            timeout=30) as resp:
        d = json.load(resp)
    keep = ("title", "date", "explanation", "url", "hdurl", "media_type")
    return json.dumps({k: d.get(k) for k in keep})
```

# Kickoff

Fetch today's picture and write the page.
