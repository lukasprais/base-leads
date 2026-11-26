#!/bin/zsh
# Abre a Base de Leads em http://localhost:8090
cd "$(dirname "$0")"
[ -x bin/gmaps ] || ./instalar.sh
(sleep 2 && open http://localhost:8090) &
exec uv run uvicorn app:app --host 127.0.0.1 --port 8090
