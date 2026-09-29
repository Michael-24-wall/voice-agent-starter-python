import re
from pathlib import Path

path = Path(r"C:\Users\Michael\Desktop\calldesk\deployment\browser\app.js")
text = path.read_text(encoding="utf-8")

# Edit 1 — remove the early token fetch
old1 = """    // The API key never reaches the page; this token expires in 60 seconds.
    const res = await fetch('/token')
    if (!res.ok) {
      setStatus('error', 'could not mint a token, check the API key')
      reset()
      return
    }
    const { token } = await res.json()

"""
new1 = """    // Token is fetched later, right before connecting — it expires in 60s.

"""
if old1 not in text:
    print("Edit 1: pattern not found — skipping")
else:
    text = text.replace(old1, new1, 1)
    print("Edit 1: removed early token fetch")

# Edit 2 — insert the fresh fetch before the WebSocket
old2 = """    const url = new URL('wss://agents.assemblyai.com/v1/ws')
    url.searchParams.set('token', token)
    ws = new WebSocket(url)
"""
new2 = """    // Fetch a fresh token right before connecting. Tokens expire in 60
    // seconds, and the mic permission prompt above can take that long.
    const res = await fetch('/token')
    if (!res.ok) {
      setStatus('error', 'could not mint a token, check the API key')
      reset()
      return
    }
    const { token } = await res.json()

    const url = new URL('wss://agents.assemblyai.com/v1/ws')
    url.searchParams.set('token', token)
    ws = new WebSocket(url)
"""
if old2 not in text:
    print("Edit 2: pattern not found — skipping")
else:
    text = text.replace(old2, new2, 1)
    print("Edit 2: inserted fresh token fetch before WebSocket")

# Edit 3 — log the close code
old3 = "    ws.onclose = () => { setStatus('idle'); reset() }"
new3 = """    ws.onclose = (e) => {
      console.log('WebSocket closed:', e.code, e.reason)
      setStatus('idle')
      reset()
    }"""
if old3 not in text:
    print("Edit 3: pattern not found — skipping")
else:
    text = text.replace(old3, new3, 1)
    print("Edit 3: added close-code logger")

path.write_text(text, encoding="utf-8")
print("Done. Saved to " + str(path))
