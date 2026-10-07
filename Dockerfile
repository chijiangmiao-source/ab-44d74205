FROM python:3.11-slim

WORKDIR /app
COPY app ./app
COPY acceptance ./acceptance
COPY tests ./tests
COPY server.py ./

ENV PORT=8080
EXPOSE 8080

CMD ["python3", "server.py"]
