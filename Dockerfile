# Hermes Portal in a container.
#
# The project is standard library only, so the image is a stock Python plus the
# one read-only tool the health domain shells out to: `lsof`, which answers "who
# holds a TCP port right now".  Without it that single collection carries a note
# instead of data; everything else is unaffected.
#
# Nothing about *which* Hermes is baked in here.  The container resolves
# everything from the mounts and the flags the compose file passes, so one image
# serves any Hermes home:
#
#   /hermes   the Hermes home to read (mounted read-only)
#   /vault    the Obsidian vault to index (mounted read-only)
#   /graphs   a code graph database for the graph domain (mounted read-only)
#   /state    the only writable path: the favourites document
#
# Python 3.12 because the project requires >= 3.11 (`datetime.UTC`).
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends lsof \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# The container's deployment contract, overridable per-mount from compose:
# read this Hermes home, serve on loopback, keep favourites outside the
# read-only home.
ENTRYPOINT ["python3", "-m", "hermes.portal"]
CMD ["--hermes-home", "/hermes", \
     "--vault", "/vault", \
     "--graph-db", "/graphs/graph.db", \
     "--state", "/state/state.json", \
     "--host", "127.0.0.1", \
     "--port", "8087"]
