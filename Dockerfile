FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8080
# ONE worker on purpose: all context + conversation state lives in memory.
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT} --workers 1
