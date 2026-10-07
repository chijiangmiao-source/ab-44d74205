FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOST=0.0.0.0 \
    APP_PORT=8080

WORKDIR /srv

COPY app ./app
COPY tests ./tests
COPY verify ./verify
RUN chmod +x ./verify && python -m compileall -q app tests

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=3s --retries=5 \
  CMD python -c "import json,os,urllib.request as u; r=u.urlopen('http://127.0.0.1:'+os.environ.get('APP_PORT','8080')+'/health',timeout=2); assert json.loads(r.read())['status']=='ok'"

CMD ["python", "-m", "app.server"]
