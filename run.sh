#!/bin/bash
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Arquivo .env criado. Edite-o com sua API key antes de continuar."
  exit 1
fi

if [ ! -d venv ]; then
  echo "Criando ambiente virtual..."
  python3 -m venv venv
fi

source venv/bin/activate
pip install -r requirements.txt -q

echo "Cardô Brain rodando em http://localhost:8000"
uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
