"""
Cardô Brain — Sincronização Google Ads
Puxa dados de performance das contas dos clientes e salva em documents/
Execute: python sync_google_ads.py

Modo 1 — Windsor.ai (ativo enquanto Google Ads API não está aprovada):
  WINDSOR_SRW_URL  (URL completa do conector Windsor)

Modo 2 — Google Ads API direta (após aprovação do token):
  GOOGLE_ADS_DEVELOPER_TOKEN
  GOOGLE_ADS_CLIENT_ID
  GOOGLE_ADS_CLIENT_SECRET
  GOOGLE_ADS_REFRESH_TOKEN
  GOOGLE_ADS_SRW_CUSTOMER_ID
"""
import os
import httpx
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

DOCS_DIR = Path(__file__).parent / "documents"
DOCS_DIR.mkdir(exist_ok=True)

# Contas dos clientes (preencher com os Customer IDs reais)
CLIENTES = {
    "SRW": {
        "nome": "Speedrack West",
        "customer_id": os.getenv("GOOGLE_ADS_SRW_CUSTOMER_ID", ""),
        "windsor_url": os.getenv("WINDSOR_SRW_URL", ""),
    },
    # Adicionar outros clientes aqui conforme integrar
    # "HEAD": {
    #     "nome": "Headlight Co",
    #     "customer_id": os.getenv("GOOGLE_ADS_HEAD_CUSTOMER_ID", ""),
    #     "windsor_url": os.getenv("WINDSOR_HEAD_URL", ""),
    # },
}

# ── Windsor.ai ────────────────────────────────────────────────────────────────

def fetch_windsor(url: str) -> list:
    """Busca dados do Google Ads via Windsor.ai e agrega por campanha."""
    with httpx.Client(timeout=30) as c:
        r = c.get(url)
        r.raise_for_status()
        rows = r.json().get("data", [])

    campaigns = {}
    for row in rows:
        name = row.get("campaign", "Desconhecida")
        if name not in campaigns:
            campaigns[name] = {"clicks": 0, "conversions": 0, "spend": 0.0, "keywords": set()}
        campaigns[name]["clicks"]      += int(row.get("clicks", 0) or 0)
        campaigns[name]["conversions"] += float(row.get("conversions", 0) or 0)
        campaigns[name]["spend"]       += float(row.get("spend", 0) or 0)
        kw = row.get("keyword_text", "")
        if kw:
            campaigns[name]["keywords"].add(kw)

    results = []
    for name, d in sorted(campaigns.items(), key=lambda x: -x[1]["spend"]):
        cpa  = round(d["spend"] / d["conversions"], 2) if d["conversions"] else 0
        cpc  = round(d["spend"] / d["clicks"], 2)      if d["clicks"]      else 0
        results.append({
            "campaign":    name,
            "clicks":      d["clicks"],
            "conversions": round(d["conversions"], 1),
            "spend":       round(d["spend"], 2),
            "cpa":         cpa,
            "cpc":         cpc,
            "keywords":    sorted(d["keywords"])[:20],  # top 20 keywords
        })
    return results

def format_windsor_markdown(nome: str, sigla: str, campaigns: list, updated_at: str) -> str:
    total_clicks = sum(c["clicks"] for c in campaigns)
    total_spend  = sum(c["spend"]  for c in campaigns)
    total_conv   = sum(c["conversions"] for c in campaigns)
    total_cpa    = round(total_spend / total_conv, 2) if total_conv else 0

    lines = [
        f"# Google Ads — {nome} [{sigla}]",
        f"**Período:** últimos 30 dias | **Fonte:** Windsor.ai | **Atualizado em:** {updated_at}",
        "",
        "## Resumo da Conta",
        "| Métrica | Valor |",
        "|---|---|",
        f"| Cliques totais | {total_clicks:,} |",
        f"| Custo total | ${total_spend:,.2f} |",
        f"| Conversões | {total_conv} |",
        f"| CPA médio | ${total_cpa:,.2f} |",
        "",
        "## Campanhas (últimos 30 dias)",
        "| Campanha | Cliques | Conv. | Custo | CPC | CPA |",
        "|---|---|---|---|---|---|",
    ]
    for c in campaigns:
        lines.append(
            f"| {c['campaign']} | {c['clicks']:,} | {c['conversions']} | "
            f"${c['spend']:,.2f} | ${c['cpc']:,.2f} | ${c['cpa']:,.2f} |"
        )

    lines += ["", "## Keywords Ativas (por campanha)"]
    for c in campaigns:
        if c["keywords"]:
            lines.append(f"\n**{c['campaign']}**")
            lines.append(", ".join(c["keywords"]))

    return "\n".join(lines) + "\n"

def get_client():
    from google.ads.googleads.client import GoogleAdsClient
    credentials = {
        "developer_token": os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN"),
        "client_id": os.getenv("GOOGLE_ADS_CLIENT_ID"),
        "client_secret": os.getenv("GOOGLE_ADS_CLIENT_SECRET"),
        "refresh_token": os.getenv("GOOGLE_ADS_REFRESH_TOKEN"),
        "use_proto_plus": True,
    }
    login_customer_id = os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID")
    if login_customer_id:
        credentials["login_customer_id"] = login_customer_id
    return GoogleAdsClient.load_from_dict(credentials)

def fetch_campaign_data(client, customer_id: str, days: int = 30) -> list:
    """Busca dados de campanha dos últimos N dias."""
    ga_service = client.get_service("GoogleAdsService")
    end_date = datetime.today()
    start_date = end_date - timedelta(days=days)

    query = f"""
        SELECT
            campaign.name,
            campaign.status,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.cost_per_conversion,
            metrics.ctr,
            metrics.average_cpc
        FROM campaign
        WHERE segments.date BETWEEN '{start_date.strftime('%Y-%m-%d')}' AND '{end_date.strftime('%Y-%m-%d')}'
          AND campaign.status != 'REMOVED'
        ORDER BY metrics.cost_micros DESC
    """

    response = ga_service.search(customer_id=customer_id.replace("-", ""), query=query)
    results = []
    for row in response:
        cost = row.metrics.cost_micros / 1_000_000
        cpa = row.metrics.cost_per_conversion / 1_000_000 if row.metrics.conversions else 0
        cpc = row.metrics.average_cpc / 1_000_000
        results.append({
            "campaign": row.campaign.name,
            "status": row.campaign.status.name,
            "impressions": int(row.metrics.impressions),
            "clicks": int(row.metrics.clicks),
            "cost": round(cost, 2),
            "conversions": round(row.metrics.conversions, 1),
            "cpa": round(cpa, 2),
            "ctr": round(row.metrics.ctr * 100, 2),
            "cpc": round(cpc, 2),
        })
    return results

def fetch_account_summary(client, customer_id: str, days: int = 30) -> dict:
    """Busca resumo geral da conta."""
    ga_service = client.get_service("GoogleAdsService")
    end_date = datetime.today()
    start_date = end_date - timedelta(days=days)

    query = f"""
        SELECT
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.cost_per_conversion,
            metrics.ctr
        FROM customer
        WHERE segments.date BETWEEN '{start_date.strftime('%Y-%m-%d')}' AND '{end_date.strftime('%Y-%m-%d')}'
    """
    response = ga_service.search(customer_id=customer_id.replace("-", ""), query=query)
    totals = {"impressions": 0, "clicks": 0, "cost": 0, "conversions": 0}
    for row in response:
        totals["impressions"] += row.metrics.impressions
        totals["clicks"] += row.metrics.clicks
        totals["cost"] += row.metrics.cost_micros / 1_000_000
        totals["conversions"] += row.metrics.conversions

    totals["cost"] = round(totals["cost"], 2)
    totals["conversions"] = round(totals["conversions"], 1)
    totals["ctr"] = round((totals["clicks"] / totals["impressions"] * 100) if totals["impressions"] else 0, 2)
    totals["cpc"] = round((totals["cost"] / totals["clicks"]) if totals["clicks"] else 0, 2)
    totals["cpa"] = round((totals["cost"] / totals["conversions"]) if totals["conversions"] else 0, 2)
    return totals

def format_markdown(nome: str, sigla: str, summary: dict, campaigns: list, days: int, updated_at: str) -> str:
    lines = [
        f"# Google Ads — {nome} [{sigla}]",
        f"**Período:** últimos {days} dias | **Atualizado em:** {updated_at}",
        "",
        "## Resumo da Conta",
        f"| Métrica | Valor |",
        f"|---|---|",
        f"| Impressões | {summary['impressions']:,} |",
        f"| Cliques | {summary['clicks']:,} |",
        f"| CTR | {summary['ctr']}% |",
        f"| Custo total | $ {summary['cost']:,.2f} |",
        f"| CPC médio | $ {summary['cpc']:,.2f} |",
        f"| Conversões | {summary['conversions']} |",
        f"| CPA | $ {summary['cpa']:,.2f} |",
        "",
        "## Campanhas Ativas",
        "| Campanha | Status | Impressões | Cliques | CTR | Custo | Conv. | CPA |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for c in campaigns:
        lines.append(
            f"| {c['campaign']} | {c['status']} | {c['impressions']:,} | {c['clicks']:,} | "
            f"{c['ctr']}% | ${c['cost']:,.2f} | {c['conversions']} | ${c['cpa']:,.2f} |"
        )
    return "\n".join(lines) + "\n"

def sync():
    updated_at = datetime.now().strftime("%d/%m/%Y %H:%M")

    for sigla, info in CLIENTES.items():
        print(f"Sincronizando Google Ads: {info['nome']} ({sigla})...")

        # ── Modo Windsor (prioritário enquanto API não está aprovada) ──────────
        if info.get("windsor_url"):
            try:
                campaigns = fetch_windsor(info["windsor_url"])
                content = format_windsor_markdown(info["nome"], sigla, campaigns, updated_at)
                out = DOCS_DIR / f"google_ads_{sigla.lower()}.md"
                out.write_text(content, encoding="utf-8")
                print(f"  [Windsor] Salvo em: {out}")
                continue
            except Exception as e:
                print(f"  [Windsor] Erro: {e} — tentando Google Ads API...")

        # ── Modo Google Ads API direta ─────────────────────────────────────────
        missing = [k for k in ["GOOGLE_ADS_DEVELOPER_TOKEN", "GOOGLE_ADS_CLIENT_ID",
                   "GOOGLE_ADS_CLIENT_SECRET", "GOOGLE_ADS_REFRESH_TOKEN"] if not os.getenv(k)]
        if missing:
            print(f"  Credenciais faltando no .env: {', '.join(missing)} — pulando")
            continue
        if not info["customer_id"]:
            print(f"  GOOGLE_ADS_{sigla}_CUSTOMER_ID não configurado — pulando")
            continue

        try:
            client = get_client()
            summary = fetch_account_summary(client, info["customer_id"])
            campaigns = fetch_campaign_data(client, info["customer_id"])
            content = format_markdown(info["nome"], sigla, summary, campaigns, 30, updated_at)
            out = DOCS_DIR / f"google_ads_{sigla.lower()}.md"
            out.write_text(content, encoding="utf-8")
            print(f"  [API] Salvo em: {out}")
        except Exception as e:
            print(f"  [API] Erro: {e}")

    print("Concluído.")

if __name__ == "__main__":
    sync()
