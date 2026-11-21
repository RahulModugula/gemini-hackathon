"""Call each candidate Gemini model with GEMINI_API_KEY from env; print which answer.
The key is never printed (error text is scrubbed)."""
import os
import sys

from google import genai

MODELS = ["gemini-4-hydrogen", "gemini-4-neon", "gemini-4-carbon", "gemini-4-argon"]


def main():
    key = os.environ.get("GEMINI_API_KEY", "").strip()  # SDK strips too; scrub what is sent
    if not key:
        sys.exit("GEMINI_API_KEY not set")
    client = genai.Client(api_key=key)
    ok = []
    for m in MODELS:
        try:
            r = client.models.generate_content(model=m, contents="Reply with the single word: pong")
            print(f"OK    {m}: {(r.text or '').strip()[:40]!r}")
            ok.append(m)
        except Exception as e:
            msg = str(e).replace(key, "***").splitlines()[0][:160]
            print(f"FAIL  {m}: {type(e).__name__}: {msg}")
    print("answering:", ok or "none")


if __name__ == "__main__":
    main()
