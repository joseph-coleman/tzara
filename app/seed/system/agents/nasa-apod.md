---
type: agent
description: Fetches NASA's Astronomy Picture of the Day and writes a page showing the image.
vaults: main
output: NASA Picture of the Day.md
max_iterations: 4
schedule: daily
log: true
Tags: astronomy, nasa, apod, api, markdown, python
GenerateMetadata: false
Summary: This agent shows how to create a custom tool with python and then instruct the agent to use that tool on a daily schedule. This also demonstrates saving output to a specific file, as well as generating logs so you can inspect behavior.
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
