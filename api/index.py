"""Vercel serverless entry point — re-exports the FastAPI app from the repo root.

Note: Vercel runs this as a stateless serverless function, so vera/store.py's
in-memory state is NOT guaranteed to persist across requests the way the
single-worker uvicorn process (Dockerfile/render.yaml) guarantees it. Fine for
a quick demo; for judged/production use, prefer Render (render.yaml) or swap
STORE for an external store.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402
