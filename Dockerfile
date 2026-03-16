FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Default: daily briefing mode
CMD ["python3", "agent.py", "--mode", "daily-briefing", "--output", "/tmp/briefing.md"]
