FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd --system --uid 10001 app && chown -R app /app
USER app
# -m keeps /app on sys.path so `import bot` / `import main` resolve
CMD ["python", "-m", "bot.startup"]
