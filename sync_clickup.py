"""
Cardô Brain — Sincronização ClickUp
Puxa o conteúdo da CardôPédia e da Documentação e salva em documents/
Execute: python sync_clickup.py
"""
import os
import httpx
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("CLICKUP_API_KEY")
WORKSPACE_ID = os.getenv("CLICKUP_WORKSPACE_ID", "36996433")
DOCS_DIR = Path(__file__).parent / "documents"
DOCS_DIR.mkdir(exist_ok=True)

HEADERS = {"Authorization": API_KEY, "Content-Type": "application/json"}

# Documentos a sincronizar
SOURCES = [
    {
        "doc_id": "1391ah-37011",
        "name": "cardopedia",
        # Páginas relevantes: Manual do Colaborador + Playbooks (excluindo estudos individuais)
        "sections": {
            # Manual do Colaborador
            "Manual - Código de Cultura - Propósito":           "1391ah-38731",
            "Manual - Código de Cultura - Valores":             "1391ah-38751",
            "Manual - Código de Cultura - Nossa Expectativa":   "1391ah-38771",
            "Manual - Código de Cultura - Sua Expectativa":     "1391ah-38791",
            "Manual - Missão e Visão":                          "1391ah-38811",
            "Manual - Comportamentos Esperados":                "1391ah-38831",
            "Manual - Comportamentos Não Tolerados":            "1391ah-38851",
            "Manual - Padrões de Comunicação":                  "1391ah-38911",
            "Manual - Ferramentas - ClickUp":                   "1391ah-38991",
            "Manual - Ferramentas - Google Drive":              "1391ah-39011",
            "Manual - Ferramentas - Tráfego Pago":              "1391ah-39051",
            "Manual - Ferramentas - Automações e IA":           "1391ah-39071",
            "Manual - Rituais e Rotinas":                       "1391ah-39111",
            # Playbooks
            "Playbook - Comercial - SDR":                       "1391ah-42151",
            "Playbook - Nossos Entregáveis - Serviços Recorrentes": "1391ah-40411",
            "Playbook - Nossos Entregáveis - Serviços Pontuais":    "1391ah-40431",
            "Playbook - Tráfego Pago - Rotinas":                "1391ah-32331",
            "Playbook - Tráfego Pago - Google Ads":             "1391ah-32371",
            "Playbook - Tráfego Pago - Meta Ads":               "1391ah-32411",
            "Playbook - Tráfego Pago - Dashboards":             "1391ah-18871",
            "Playbook - CS - Atas de Reuniões com Clientes":    "1391ah-40711",
            "Playbook - CS - Funções":                          "1391ah-44651",
            "Playbook - CRM - Implementação":                   "1391ah-36131",
            "Playbook - CRM - Revisão Diária":                  "1391ah-32591",
            "Playbook - CRM - Análise Semanal":                 "1391ah-32611",
        }
    },
    {
        "doc_id": "1391ah-49691",
        "name": "documentacao_clientes",
        "sections": {
            # Atas de Reunião — Clientes
            "Ata - Cliente CC":                                 "1391ah-33591",
            "Ata - Cliente HIRE":                               "1391ah-42471",
            "Ata - Cliente LM":                                 "1391ah-43631",
            "Ata - Cliente NF":                                 "1391ah-33711",
            "Ata - Cliente SPL":                                "1391ah-48611",
            # Atas Internas
            "Ata - Reunião Semanal Interna":                    "1391ah-36371",
            "Ata - 1on1":                                       "1391ah-37931",
            # Arquivos de Clientes
            "Arquivo - CC - Análises":                          "1391ah-33851",
            "Arquivo - CC - Conteúdos":                         "1391ah-32811",
            "Arquivo - HIRE - Diretrizes do Lançamento":        "1391ah-43491",
            "Arquivo - LM - Briefing Inicial":                  "1391ah-44571",
            "Arquivo - LM - Copy Landing Page":                 "1391ah-43611",
            "Arquivo - LM - Criativos":                         "1391ah-44691",
            "Arquivo - SPL - Briefing Inicial":                 "1391ah-47931",
            # Fichas de Clientes
            "Ficha - CC - Conexão Cirúrgica":                   "1391ah-48811",
            "Ficha - NF - Gráfica NF":                          "1391ah-48831",
            "Ficha - HIRE - Hire Brazil":                       "1391ah-49031",
            "Ficha - PV - Patrícia Voggt":                       "1391ah-49051",
            "Ficha - PRO - Subido PRO":                         "1391ah-48891",
            "Ficha - SRW - Speedrack West":                     "1391ah-48911",
            "Ficha - HDLT - Headlight Co":                      "1391ah-48931",
            "Ficha - SCALE - Scale Army":                       "1391ah-49731",
            "Ata - Cliente DFT":                                "1391ah-50411",
            "Ficha - DFT - DFT Logística":                      "1391ah-50431",
        }
    }
]


def fetch_page(doc_id: str, page_id: str) -> str:
    url = f"https://api.clickup.com/api/v3/workspaces/{WORKSPACE_ID}/docs/{doc_id}/pages/{page_id}"
    r = httpx.get(url, headers=HEADERS, params={"content_format": "text/md"}, timeout=30)
    if r.status_code != 200:
        return f"[Erro ao buscar página: {r.status_code}]"
    data = r.json()
    return data.get("content", "")


def sync():
    if not API_KEY:
        print("CLICKUP_API_KEY não encontrada no .env")
        return

    total = 0
    for source in SOURCES:
        output_path = DOCS_DIR / f"{source['name']}.md"
        lines = [f"# {source['name'].upper().replace('_', ' ')}\n\n"]

        print(f"\nSincronizando: {source['name']}")
        for section_name, page_id in source["sections"].items():
            print(f"  → {section_name}...")
            content = fetch_page(source["doc_id"], page_id)
            lines.append(f"## {section_name}\n\n")
            if content.strip():
                lines.append(content.strip() + "\n\n")
            else:
                lines.append("*(sem conteúdo)*\n\n")

        output_path.write_text("".join(lines), encoding="utf-8")
        print(f"  Salvo em: {output_path}")
        total += 1

    print(f"\nSincronização concluída. {total} arquivos salvos em documents/")


if __name__ == "__main__":
    sync()
