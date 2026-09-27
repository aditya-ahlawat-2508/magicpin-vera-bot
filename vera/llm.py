"""Provider-agnostic async LLM client (raw HTTP, no SDKs).

Configure with env vars:
  LLM_PROVIDER = anthropic | openai | gemini | groq | deepseek | openrouter   (default: auto-detect from keys)
  LLM_API_KEY  = key  (or the provider-specific ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY / GROQ_API_KEY ...)
  LLM_MODEL    = optional model override
If nothing is configured, llm_available() is False and the bot runs fully deterministic.
"""
from __future__ import annotations

import json
import os
import re

import httpx

DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-4-5",
    "openai": "gpt-4o",
    "gemini": "gemini-2.0-flash",
    "groq": "llama-3.3-70b-versatile",
    "deepseek": "deepseek-chat",
    "openrouter": "anthropic/claude-sonnet-4.5",
}
KEY_ENVS = {
    "anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY", "deepseek": "DEEPSEEK_API_KEY", "openrouter": "OPENROUTER_API_KEY",
}


def _config() -> tuple[str | None, str | None, str | None]:
    prov = (os.getenv("LLM_PROVIDER") or "").lower().strip() or None
    key = os.getenv("LLM_API_KEY")
    if prov and not key:
        key = os.getenv(KEY_ENVS.get(prov, ""), None)
    if not prov:
        for p, env in KEY_ENVS.items():
            if os.getenv(env):
                prov, key = p, os.getenv(env)
                break
    model = os.getenv("LLM_MODEL") or (DEFAULT_MODELS.get(prov) if prov else None)
    return prov, key, model


def llm_available() -> bool:
    prov, key, _ = _config()
    return bool(prov and key)


def model_name() -> str:
    prov, key, model = _config()
    return f"{prov}:{model}" if prov and key else "deterministic-playbooks"


async def complete(system: str, user: str, timeout: float = 12.0, max_tokens: int = 700) -> str | None:
    prov, key, model = _config()
    if not (prov and key):
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if prov == "anthropic":
                r = await client.post("https://api.anthropic.com/v1/messages",
                                      headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                                      json={"model": model, "max_tokens": max_tokens, "temperature": 0, "system": system,
                                            "messages": [{"role": "user", "content": user}]})
                r.raise_for_status()
                return "".join(b.get("text", "") for b in r.json().get("content", []))
            if prov == "gemini":
                r = await client.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                                      json={"systemInstruction": {"parts": [{"text": system}]},
                                            "contents": [{"role": "user", "parts": [{"text": user}]}],
                                            "generationConfig": {"temperature": 0, "maxOutputTokens": max_tokens}})
                r.raise_for_status()
                return r.json()["candidates"][0]["content"]["parts"][0]["text"]
            url = {"openai": "https://api.openai.com/v1/chat/completions",
                   "groq": "https://api.groq.com/openai/v1/chat/completions",
                   "deepseek": "https://api.deepseek.com/v1/chat/completions",
                   "openrouter": "https://openrouter.ai/api/v1/chat/completions"}[prov]
            r = await client.post(url, headers={"Authorization": f"Bearer {key}"},
                                  json={"model": model, "temperature": 0, "max_tokens": max_tokens, "seed": 7,
                                        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
    except Exception:
        return None


def parse_json(text: str | None) -> dict | None:
    if not text:
        return None
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
