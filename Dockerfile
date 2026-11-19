FROM python:3.13-alpine

RUN adduser -D user \
 && mkdir -p /opt/rdgen \
 && chown user:user /opt/rdgen
USER user

WORKDIR /opt/rdgen

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN python manage.py migrate

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD wget -Y off --spider http://127.0.0.1:8000/

CMD ["/home/user/.local/bin/gunicorn", "-c", "gunicorn.conf.py", "rdgen.wsgi:application"]
