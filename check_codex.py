"""Explicit live Codex smoke test; one synthetic request, no Discord connection."""
import asyncio
import os
from pathlib import Path
from dotenv import load_dotenv
from providers import CodexProvider

async def main():
    root = Path(__file__).resolve().parent
    load_dotenv(root / ".env")
    provider = CodexProvider(root / "data" / "codex-runtime",
                             os.getenv("CODEX_EXECUTABLE", "codex"),
                             os.getenv("CODEX_MODEL", ""))
    action, text = await provider.complete(
        'Return JSON with action="reply" and text="连接正常". Do not use tools.',
        {"text": "Synthetic connection test; no user conversation."})
    if action != "reply" or not text:
        raise SystemExit("Unexpected response")
    print("Codex structured response OK; no Discord message was sent.")

if __name__ == "__main__":
    asyncio.run(main())

