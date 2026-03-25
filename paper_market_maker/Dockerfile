FROM python:3.13-slim

WORKDIR /app

COPY paper_market_maker/*.py /app/
COPY paper_market_maker/*.json /app/

ENV PYTHONUNBUFFERED=1
ENV PORT=8080
ENV HOST=0.0.0.0
ENV GATE_MM_AUTO_START=true
ENV GATE_MM_DATA_DIR=/app/data

RUN mkdir -p /app/data

EXPOSE 8080

CMD ["python", "/app/gate_futures_service.py"]
