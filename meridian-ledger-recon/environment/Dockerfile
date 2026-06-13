# meridian-ledger-recon environment.
# Python 3.11 slim, stdlib only (sqlite3, json) — no pip installs, no network
# needed at build or run time beyond pulling this pinned base image.
FROM python:3.11.9-slim-bookworm@sha256:8fb099199b9f2d70342674bd9dbccd3ed03a258f26bbd1d556822c6dfc60c317

WORKDIR /app

COPY app/  /app/app/
COPY docs/ /app/docs/
COPY data/ /app/data/

# Materialize the stale snapshot exactly as production did: by running the
# currently shipped (spec-violating) rebuilder over the published event log.
# A pristine copy is kept as a read-only reconciliation baseline.
RUN mkdir -p /app/state \
    && python3 /app/app/ledger.py rebuild \
         --events /app/data/events.ndjson \
         --db /app/state/ledger.db \
    && cp /app/state/ledger.db /app/state/baseline_stale.db \
    && chmod 444 /app/state/baseline_stale.db

CMD ["bash"]
