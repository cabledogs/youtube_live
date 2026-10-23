FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# yt-dlp needs a JavaScript runtime to handle YouTube's player code
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno
RUN pip install --no-cache-dir "yt-dlp[default]" pyyaml
WORKDIR /app
COPY app.py local_guide.py ./
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh
EXPOSE 5004
ENTRYPOINT ["./entrypoint.sh"]
