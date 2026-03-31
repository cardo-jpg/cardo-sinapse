"""
Cardô Brain — Sincronização Google Sheets (Google Ads Reports)
Lê planilhas exportadas do Google Ads e salva em documents/

Configure no .env:
  GOOGLE_SERVICE_ACCOUNT_JSON=caminho para o arquivo JSON da service account

Execute: python sync_sheets.py
"""
import os
import json
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

DOCS_DIR = Path(__file__).parent / "documents"
DOCS_DIR.mkdir(exist_ok=True)

# Planilhas dos clientes
PLANILHAS = {
    "SRW": {
        "nome": "Speedrack West",
        "sheet_id": "1KB2xF6pjdFrPV1p0XosgEQVt_kGERxzoMVDOIZ_4274",
        "aba": None,  # None = primeira aba
    },
    "NF_GOOGLE": {
        "nome": "Gráfica NF — Google Ads",
        "sheet_id": "1On5aMG2xz5IHpFWiireipOFjXTmLCYGYKovH4EzQypI",
        "aba": "Google Ads",
    },
    "NF_META": {
        "nome": "Gráfica NF — Meta Ads (Público)",
        "sheet_id": "15Puh4cxcFvw4pQd6fWJ9gujhwcJ87W6-o8XOa5p1Azk",
        "aba": "Público",
    },
    # ── HIRE Brazil ──────────────────────────────────────────────────────────
    "HIRE_ROAS": {
        "nome": "Hire Brazil — ROAS por Funil (Somatório)",
        "sheet_id": "1l6_bsucWh3CZKhBZpqBykPJuYT3GQ5ZehAR5IAXd8kg",
        "aba": "Somatório",
    },
    "HIRE_IG_MALU": {
        "nome": "Hire Brazil — Funil IG Malu",
        "sheet_id": "1l6_bsucWh3CZKhBZpqBykPJuYT3GQ5ZehAR5IAXd8kg",
        "aba": "IG Malu",
    },
    "HIRE_IG_HIRE": {
        "nome": "Hire Brazil — Funil IG Hire",
        "sheet_id": "1l6_bsucWh3CZKhBZpqBykPJuYT3GQ5ZehAR5IAXd8kg",
        "aba": "IG Hire",
    },
    "HIRE_EBOOK5": {
        "nome": "Hire Brazil — Funil Ebook 5 Oportunidades",
        "sheet_id": "1l6_bsucWh3CZKhBZpqBykPJuYT3GQ5ZehAR5IAXd8kg",
        "aba": "Ebook - 5 op.",
    },
    "HIRE_EBOOK7": {
        "nome": "Hire Brazil — Funil Ebook 7 Erros",
        "sheet_id": "1l6_bsucWh3CZKhBZpqBykPJuYT3GQ5ZehAR5IAXd8kg",
        "aba": "Ebook - 7 erros",
    },
    "HIRE_MALU_SEG": {
        "nome": "Hire Brazil — Seguidores Malu",
        "sheet_id": "1SVz6Eti4E6hkOpgVjOeYkQ3XvDuYuVYmSWOj_cWYvPM",
        "aba": "Dados",
    },
    "HIRE_HIRE_SEG": {
        "nome": "Hire Brazil — Seguidores HIRE",
        "sheet_id": "1XWIvqBx1TXjoFtiIViW_lW4L0EpbXAQsmU6vbvUMVIk",
        "aba": "Dados",
    },
    # ── Patrícia Voggt ────────────────────────────────────────────────────────
    "PV_SEG": {
        "nome": "Patrícia Voggt — Seguidores Instagram",
        "sheet_id": "1FqaWfL76VcYKeYGVbqO7qwi3EUqwj7YaLEL23Dlqgd8",
        "aba": "Dados",
    },
    "PV_SEG_REL": {
        "nome": "Patrícia Voggt — Relatório Semanal Seguidores",
        "sheet_id": "1FqaWfL76VcYKeYGVbqO7qwi3EUqwj7YaLEL23Dlqgd8",
        "aba": "Relatório Segidores",
    },
    "PV_SRI_ADS": {
        "nome": "Patrícia Voggt — SRI Campanhas FB Ads",
        "sheet_id": "1jklY6oOH4m6-d_3uEvMR-05o3y3MRMRf_QkgYl8HkkE",
        "aba": "Campanhas FBAds",
    },
    "PV_SRI_LEADS": {
        "nome": "Patrícia Voggt — SRI Dashboard Leads",
        "sheet_id": "1305ZbnsMYoVuf8gFNJN2ZLCKhFQVx_K1-UzvGqEosSw",
        "aba": "Respostas ao formulário 1",
    },
    "PV_MP": {
        "nome": "Patrícia Voggt — Campanha MP Vendas",
        "sheet_id": "1uPI8qKC4DNpel4TYRjGJ4-3AxKIs9okvOV9S1CZ1Zro",
        "aba": "Vendas",
    },
    # Adicionar outros clientes conforme integrar:
    # "HEAD": {
    #     "nome": "Headlight Co",
    #     "sheet_id": "ID_DA_PLANILHA",
    #     "aba": None,
    # },
}

def get_service():
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    json_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "service_account.json")
    creds = Credentials.from_service_account_file(
        json_path,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    return build("sheets", "v4", credentials=creds)

def read_sheet(service, sheet_id: str, aba: str = None) -> list:
    range_name = aba if aba else "A:Z"
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=range_name
    ).execute()
    return result.get("values", [])

def rows_to_markdown(nome: str, sigla: str, rows: list, updated_at: str) -> str:
    if not rows:
        return f"# Google Ads — {nome} [{sigla}]\n\nSem dados disponíveis.\n"

    lines = [
        f"# Google Ads — {nome} [{sigla}]",
        f"**Atualizado em:** {updated_at}",
        "",
    ]

    # Cabeçalho
    headers = rows[0]
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("|" + "|".join(["---"] * len(headers)) + "|")

    # Dados
    for row in rows[1:]:
        # Preenche colunas faltantes
        while len(row) < len(headers):
            row.append("")
        lines.append("| " + " | ".join(row) + " |")

    lines.append("")
    return "\n".join(lines)

def sync():
    json_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "service_account.json")
    if not Path(json_path).exists():
        print(f"Arquivo de credenciais não encontrado: {json_path}")
        print("Siga as instruções para criar a service account e coloque o JSON na pasta do projeto.")
        return

    try:
        service = get_service()
    except Exception as e:
        print(f"Erro ao autenticar: {e}")
        return

    updated_at = datetime.now().strftime("%d/%m/%Y %H:%M")

    for sigla, info in PLANILHAS.items():
        print(f"Sincronizando planilha: {info['nome']} ({sigla})...")
        try:
            rows = read_sheet(service, info["sheet_id"], info["aba"])
            content = rows_to_markdown(info["nome"], sigla, rows, updated_at)
            out = DOCS_DIR / f"google_ads_{sigla.lower()}.md"
            out.write_text(content, encoding="utf-8")
            print(f"  {len(rows)-1} linhas salvas em: {out}")
        except Exception as e:
            print(f"  Erro: {e}")

    print("Concluído.")

if __name__ == "__main__":
    sync()
