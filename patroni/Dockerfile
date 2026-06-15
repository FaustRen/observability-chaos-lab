FROM postgres:15-bookworm

# ── 安裝 Python 3、pip 與 Patroni
# --break-system-packages 是在 PEP 668（Debian Bookworm+）環境下系統 pip 的必要旗標
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-dev \
    curl \
    && pip3 install --no-cache-dir --break-system-packages patroni psycopg2-binary etcd3gw python-etcd \
    && apt-get purge -y python3-dev \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* /root/.cache

# ── 建立必要目錄
RUN mkdir -p /scripts /etc/patroni

# ── 複製設定與腳本（context = 專案根目錄）
COPY patroni/patroni.yml      /etc/patroni/patroni.yml
COPY patroni/post_bootstrap.sh /scripts/post_bootstrap.sh
COPY patroni/entrypoint.sh    /entrypoint.sh

RUN chmod +x /entrypoint.sh /scripts/post_bootstrap.sh

EXPOSE 5432 8008

ENTRYPOINT ["/entrypoint.sh"]
