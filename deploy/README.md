# Deploy (Fly.io)

The image needs the generated `build/` and `data/` folders (not in git). Stage them next to the code, then:

```bash
cp deploy/Dockerfile deploy/fly.toml deploy/.dockerignore <staging-dir>/
flyctl apps create <app-name>
echo "GEMINI_API_KEY=..." | flyctl secrets import -a <app-name>   # key never goes in the image
flyctl deploy --remote-only --ha=false -a <app-name>
```

`LLM_RUN_CAP_USD=10` in the Dockerfile caps Gemini spend per UTC day (enforced in `llm.py`).
