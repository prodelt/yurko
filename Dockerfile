FROM python:3.12-slim-bookworm

WORKDIR /app

# --- TLS: rada.gov.ua рве з'єднання на ClientHello > ~700-900 байт ---------
# python:3.12-slim мовчки переїхав з Debian 12 / OpenSSL 3.0 на Debian 13 /
# OpenSSL 3.5, який додає постквантовий key share X25519MLKEM768 і роздуває
# ClientHello з 324 до 1554 байт. Наслідок — SSLEOFError на кожному другому
# запиті до Ради. Діагностика: docs/research-rada-egress.md
#
# Два незалежні запобіжники:
#   1) база запінена на bookworm (Debian 12), щоб образ більше не «їхав» сам;
#   2) список TLS-груп зафіксовано явно — фікс лишається чинним і після
#      майбутнього оновлення бази.
# Пастка: ssl_conf треба вписувати В ІСНУЮЧУ секцію [openssl_init] — власна
# секція в кінці файлу мовчки не застосовується.
RUN cp /etc/ssl/openssl.cnf /etc/ssl/openssl-small-hello.cnf \
 && sed -i '/^\[openssl_init\]/a ssl_conf = ssl_sect' /etc/ssl/openssl-small-hello.cnf \
 && printf '\n[ssl_sect]\nsystem_default = system_default_sect\n\n[system_default_sect]\nGroups = X25519:P-256:P-384\n' \
      >> /etc/ssl/openssl-small-hello.cnf
ENV OPENSSL_CONF=/etc/ssl/openssl-small-hello.cnf

# Системні залежності для Playwright (опціонально — fallback на print_url якщо нема пам'яті)
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# requirements-hosted.txt — закріплений (uv.lock) набір для розгорнутого сервера:
# базовий stdio-набір плюс Postgres і Playwright. Плагіну Claude Code вони не
# потрібні, тому в requirements.txt їх немає.
COPY requirements-hosted.txt .
RUN pip install --no-cache-dir -r requirements-hosted.txt

# Chromium для Playwright — best-effort (може не вистачити RAM на free tier)
RUN playwright install chromium --with-deps 2>/dev/null || \
    echo "Playwright install skipped — print_url fallback will be used"

COPY . .

# Кеш, який сервер пише під час роботи, — поза текою коду (тікет 51, № 22):
# каталог задається YURKO_CACHE_DIR; ``cache/laws.json`` у теці коду — дані
# поставки, лише для читання.
ENV YURKO_CACHE_DIR=/var/cache/yurko
RUN mkdir -p /var/cache/yurko

EXPOSE 8000

ENV MCP_TRANSPORT=http
ENV PYTHONUNBUFFERED=1

# --- юридичний профіль як властивість самого образу (T479) -------------------
# Render синхронізує `envVars` з render.yaml лише при явному застосуванні
# блупринта; звичайний push деплоїть **код**, а змінні сервісу лишає як були.
# Виміряно 11.09.2026: деплой упав саме через це — новий код із перевіркою
# профілю зустрів старе оточення інженерного стенда й не стартував.
#
# Тому те, що є властивістю образу, а не розгортання, стоїть тут і їде разом із
# кодом. Змінна сервісу, задана в дашборді, перекриває ENV образу — тобто це
# запобіжник, а не спроба відібрати керування.
#
# `YURKO_OWN_DATABASE` сюди свідомо **не** входить: це свідчення власника про
# конкретну базу, і зашити його в образ означало б заявити за власника про
# будь-яку базу, до якої образ колись підключать.
ENV YURKO_PROFILE=legal
ENV YURKO_HOSTED=1
ENV EMBEDDING_PROVIDER=null

CMD ["python", "server.py"]
