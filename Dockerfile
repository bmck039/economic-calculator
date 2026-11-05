# Website image. All setup logic lives in run.py; this only installs requirements.txt
# and runs `python run.py`. Use with compose.yaml (settings come from config.env).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

# Unprivileged user; it owns only the venv (run.py installs a release's requirements there),
# the data folder and /state (the volume holding downloaded releases and the download cache).
RUN useradd --create-home --uid 1000 app \
 && mkdir -p /opt/venv /app/econ_data /state \
 && chown app:app /opt/venv /app/econ_data /state
USER app
WORKDIR /app

# run.py treats /opt/venv as the active venv, so it never creates another one.
COPY requirements.txt .
# --no-compile: skip ~110 MB of .pyc files (modules compile in memory at start instead)
RUN python -m venv /opt/venv && pip install --no-compile -r requirements.txt

COPY . .

# The first start downloads the latest release and builds its data (~10-20 min).
HEALTHCHECK --interval=30s --timeout=5s --start-period=40m --retries=3 CMD python -c \
  "import os, urllib.request as u; b = os.environ.get('BASE_URL_PATH', '').strip('/'); \
   u.urlopen('http://localhost:%s/%s_stcore/health' % (os.environ.get('PORT', '8501'), b + '/' if b else ''), timeout=4)"

CMD ["python", "run.py"]
