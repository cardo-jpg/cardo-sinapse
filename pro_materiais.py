#!/usr/bin/env python3
"""
PRO Materiais — Gerador automático de planos de ação Subido PRO
Busca reunião no Granola, processa com Claude e salva no Google Docs.

Uso: python pro_materiais.py
"""

import os
import sys
import json
import gzip
from pathlib import Path
from dotenv import load_dotenv
import httpx
import anthropic
from googleapiclient.discovery import build
from google.oauth2 import service_account

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

# ── Constantes ────────────────────────────────────────────────────────────────

DRIVE_FOLDER_ID        = "1uFC95IZIRHtfHlLX0iE95fA-5nNU4jfO"
CONSULTORIA_TEMPLATE_ID = "1MTpdFhndUkwbSdNgAzxRJZ-Nvx0SP9W3TFPr9sM_WB4"
GRANOLA_API_BASE       = "https://api.granola.ai/v1"
SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]

# ── Auth ──────────────────────────────────────────────────────────────────────

def get_granola_token() -> str:
    # Local supabase.json is always fresh (kept updated by Granola app).
    # Use it when available; fall back to env var only for remote deploys.
    try:
        path = Path.home() / "Library/Application Support/Granola/supabase.json"
        data = json.loads(path.read_text())
        wt = data.get("workos_tokens", {})
        if isinstance(wt, str):
            wt = json.loads(wt)
        token = wt.get("access_token", "")
        if token:
            return token
    except Exception:
        pass
    return os.getenv("GRANOLA_TOKEN", "")


def get_google_services():
    sa_path = BASE_DIR / "service_account.json"
    creds = service_account.Credentials.from_service_account_file(
        str(sa_path), scopes=SCOPES
    )
    docs  = build("docs",  "v1", credentials=creds)
    drive = build("drive", "v3", credentials=creds)
    return docs, drive

# ── Granola ───────────────────────────────────────────────────────────────────

_GRANOLA_HEADERS_EXTRA = {
    "x-granola-client-id": "granola-desktop",
    "x-granola-version":   "2.0.0",
}

def _granola_post(endpoint: str, payload: dict = None):
    token = get_granola_token()
    r = httpx.post(
        f"{GRANOLA_API_BASE}/{endpoint}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            **_GRANOLA_HEADERS_EXTRA,
        },
        json=payload or {},
        timeout=20,
    )
    return r.json() if r.status_code == 200 else None


def _prosemirror_to_text(node: dict) -> str:
    """Extrai texto plano de um nó ProseMirror recursivamente."""
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return node.get("text", "")
    lines = []
    for child in node.get("content", []):
        text = _prosemirror_to_text(child)
        if text.strip():
            lines.append(text)
    node_type = node.get("type", "")
    sep = "\n" if node_type in ("paragraph", "bulletList", "listItem", "heading") else " "
    return sep.join(lines)


def list_meetings() -> list:
    docs = _granola_post("get-documents")
    if not isinstance(docs, list):
        return []
    meetings = []
    for doc in docs:
        if doc.get("deleted_at"):
            continue
        # Prefer plain/markdown text; fall back to parsing ProseMirror JSON
        notes = doc.get("notes_markdown") or doc.get("notes_plain") or ""
        if not notes:
            raw_notes = doc.get("notes")
            if isinstance(raw_notes, dict):
                notes = _prosemirror_to_text(raw_notes)
        meetings.append({
            "id":    doc.get("id"),
            "title": doc.get("title") or "Sem título",
            "date":  (doc.get("created_at") or "")[:10],
            "notes": notes,
        })
    meetings.sort(key=lambda x: x["date"], reverse=True)
    return meetings[:20]


def get_transcript(meeting_id: str) -> str:
    token = get_granola_token()
    r = httpx.post(
        f"{GRANOLA_API_BASE}/get-document-transcript",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            **_GRANOLA_HEADERS_EXTRA,
        },
        json={"document_id": meeting_id},
        timeout=20,
    )
    if r.status_code != 200:
        return ""
    try:
        segments = json.loads(gzip.decompress(r.content))
    except Exception:
        try:
            segments = json.loads(r.content)
        except Exception:
            return ""
    if not isinstance(segments, list):
        return ""
    lines = []
    for s in segments:
        text = s.get("text", "").strip()
        if text:
            prefix = "Sistema" if s.get("source") == "system" else "Microfone"
            lines.append(f"[{prefix}]: {text}")
    return "\n".join(lines)

# ── Claude ────────────────────────────────────────────────────────────────────

_PROMPT_MENTORIA = """Você é um analista de transcrições do programa Subido PRO.

O nome do mentorado é: {nome_aluno}
O número desta mentoria é: {numero_mentoria}
A data da reunião é: {data}

Analise o conteúdo da sessão abaixo e retorne um JSON com esta estrutura exata:

{{
  "nome_aluno": "{nome_aluno}",
  "nome_mentor": "Nome do mentor (extraia do conteúdo)",
  "data": "{data}",
  "numero_mentoria": "{numero_mentoria}",
  "acoes": [
    {{
      "texto": "Texto da ação — objetivo e direto",
      "subacoes": ["detalhe quantitativo em string simples", "outro detalhe"]
    }},
    {{
      "texto": "Ação não-quantitativa",
      "subacoes": []
    }}
  ]
}}

Regras obrigatórias:
- Extraia APENAS ações citadas ou claramente combinadas na reunião
- "subacoes" deve ser uma lista de STRINGS simples (nunca objetos)
- "subacoes": preencha SOMENTE quando a ação for quantitativa (envolva número, meta, frequência ou volume específico)
- Ações não-quantitativas devem ter "subacoes": [] (lista vazia)
- Cada sub-ação é uma string curta detalhando o aspecto quantitativo (qual número, meta ou frequência)
- Retorne APENAS o JSON puro, sem markdown, sem texto extra

CONTEÚDO DA REUNIÃO:
{conteudo}"""

_PROMPT_CONSULTORIA = """Você é um analista de transcrições do programa Subido PRO.

O nome do aluno/cliente é: {nome_aluno}
A data da reunião é: {data}

Analise o conteúdo da sessão abaixo e retorne um JSON com esta estrutura exata:

{{
  "nome_aluno": "{nome_aluno}",
  "nome_consultor": "Nome do consultor (extraia do conteúdo)",
  "data": "{data}",
  "acoes": [
    {{
      "titulo": "Título curto e objetivo da ação",
      "subacoes": ["como executar — passo 1", "passo 2", "passo 3"]
    }}
  ]
}}

Regras obrigatórias:
- Extraia APENAS ações citadas ou claramente combinadas na reunião
- Cada ação deve ter um título curto (máx 1 frase)
- "subacoes": máximo 3 por ação, detalhando como executá-la; use [] se a ação não precisar de detalhamento
- Retorne APENAS o JSON puro, sem markdown, sem texto extra

CONTEÚDO DA REUNIÃO:
{conteudo}"""


def _parse_title(title: str) -> tuple:
    """
    Extrai nome e número da mentoria do título do Granola.
    Ex: '[PRO] Maurício Lunardi - 6ª Mentoria' → ('Maurício Lunardi', '6ª')
    Ex: '[PRO] Mentoria Gabriel' → ('Gabriel', '?')
    """
    import re
    # Remove prefix like [PRO], [HIRE], etc.
    clean = re.sub(r'^\[[^\]]+\]\s*', '', title).strip()

    # Pattern: "Nome - Nª Mentoria"
    m = re.match(r'^(.+?)\s*[-–]\s*(\d+ª)\s*[Mm]entoria', clean)
    if m:
        return m.group(1).strip(), m.group(2)

    # Pattern: "Mentoria Nome" or "Nª Mentoria Nome"
    m = re.match(r'(?:\d+ª\s*)?[Mm]entoria\s+(.+)', clean)
    if m:
        return m.group(1).strip(), "?"

    return clean, "?"


def process_with_claude(conteudo: str, tipo: str, meeting_title: str = "", meeting_date: str = "") -> dict:
    nome_aluno, numero_mentoria = _parse_title(meeting_title) if meeting_title else ("?", "?")
    data = meeting_date[5:].replace("-", "/") if len(meeting_date) >= 7 else "?"  # "2026-03-24" → "03/24"
    # Convert to DD/MM
    if len(data) == 5 and "/" in data:
        parts = data.split("/")
        data = f"{parts[1]}/{parts[0]}"

    ai = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    if tipo == "mentoria":
        prompt = _PROMPT_MENTORIA.format(
            nome_aluno=nome_aluno,
            numero_mentoria=numero_mentoria,
            data=data,
            conteudo=conteudo[:14000],
        )
    else:
        prompt = _PROMPT_CONSULTORIA.format(
            nome_aluno=nome_aluno,
            data=data,
            conteudo=conteudo[:14000],
        )

    resp = ai.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()
    if not text:
        raise ValueError("Claude retornou resposta vazia. O transcript pode estar vazio.")
    # Strip markdown code fence
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    # Extract first JSON block (handles preamble text before the JSON)
    m = re.search(r'\{[\s\S]*\}', text)
    if not m:
        print(f"\nResposta do Claude (primeiros 300 chars):\n{text[:300]}\n")
        raise json.JSONDecodeError("Nenhum JSON encontrado", text, 0)
    try:
        return json.loads(m.group())
    except json.JSONDecodeError:
        print(f"\nResposta do Claude (primeiros 300 chars):\n{text[:300]}\n")
        raise

# ── Google Docs helpers ───────────────────────────────────────────────────────

def _doc_plain_text(doc: dict) -> str:
    """Extrai texto plano do objeto documento."""
    out = []
    for el in doc.get("body", {}).get("content", []):
        for pe in el.get("paragraph", {}).get("elements", []):
            out.append(pe.get("textRun", {}).get("content", ""))
    return "".join(out)


def _end_index(doc: dict) -> int:
    """Índice final do corpo do documento (antes do newline final)."""
    content = doc.get("body", {}).get("content", [])
    return content[-1].get("endIndex", 1) - 1

# ── Mentoria: append ao doc existente ─────────────────────────────────────────

def find_mentoria_doc(drive, nome_aluno: str):
    """Busca o doc '[Nome]: Ações práticas da mentoria' na pasta do Drive."""
    query = (
        f"'{DRIVE_FOLDER_ID}' in parents "
        f"and trashed=false "
        f"and mimeType='application/vnd.google-apps.document'"
    )
    result = drive.files().list(q=query, fields="files(id,name)", includeItemsFromAllDrives=True, supportsAllDrives=True).execute()
    files = result.get("files", [])

    nome_lower = nome_aluno.lower()
    # Exact match
    for f in files:
        if f["name"].lower() == f"{nome_lower}: ações práticas da mentoria":
            return f["id"], f["name"]
    # Partial: first name in filename
    primeiro = nome_lower.split()[0]
    for f in files:
        if primeiro in f["name"].lower() and "mentoria" in f["name"].lower():
            return f["id"], f["name"]
    return None, None


def append_mentoria(docs_svc, doc_id: str, dados: dict):
    """Insere nova seção de mentoria no final do documento."""
    numero  = dados.get("numero_mentoria", "?")
    data    = dados.get("data", "")
    mentor  = dados.get("nome_mentor", "")
    acoes   = dados.get("acoes", [])

    lines = [
        f"\n\n{numero} Mentoria",
        f"Data: {data}",
        f"Mentor: {mentor}",
        "",
        "Checklist - Próximos passos e ações:",
    ]
    for acao in acoes:
        lines.append(f"• {acao['texto']}")
        for sub in acao.get("subacoes", []):
            lines.append(f"\t→ {sub}")

    text = "\n".join(lines)

    doc = docs_svc.documents().get(documentId=doc_id).execute()
    end = _end_index(doc)

    docs_svc.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": [
            {"insertText": {"location": {"index": end}, "text": text}}
        ]},
    ).execute()

# ── Consultoria: cópia do template + preenchimento ─────────────────────────────

def _find_section_start(doc: dict, marker: str):
    """Retorna o startIndex do parágrafo que contém o marker."""
    for el in doc.get("body", {}).get("content", []):
        text = "".join(
            pe.get("textRun", {}).get("content", "")
            for pe in el.get("paragraph", {}).get("elements", [])
        )
        if marker in text:
            return el.get("startIndex")
    return None


def create_consultoria_doc(drive_svc, docs_svc, dados: dict) -> tuple[str, str]:
    """
    Copia o template de consultoria e preenche com os dados.
    Retorna (doc_id, doc_name).
    """
    nome_aluno    = dados.get("nome_aluno", "Aluno")
    nome_consultor = dados.get("nome_consultor", "")
    data          = dados.get("data", "")
    acoes         = dados.get("acoes", [])

    # 1. Copia o template para a pasta
    novo_nome = f"{nome_aluno}: Consultoria {data}"
    copied = drive_svc.files().copy(
        fileId=CONSULTORIA_TEMPLATE_ID,
        body={"name": novo_nome, "parents": [DRIVE_FOLDER_ID]},
        supportsAllDrives=True,
    ).execute()
    novo_id = copied["id"]

    # 2. Substitui campos do cabeçalho
    header_requests = [
        {"replaceAllText": {
            "containsText": {"text": "CONSULTORIA 00", "matchCase": False},
            "replaceText": f"CONSULTORIA — {nome_aluno}",
        }},
        {"replaceAllText": {
            "containsText": {"text": "Mentorado: ", "matchCase": False},
            "replaceText": f"Mentorado: {nome_aluno}",
        }},
        {"replaceAllText": {
            "containsText": {"text": "Consultor: ", "matchCase": False},
            "replaceText": f"Consultor: {nome_consultor}",
        }},
    ]
    docs_svc.documents().batchUpdate(
        documentId=novo_id, body={"requests": header_requests}
    ).execute()

    # 3. Apaga todo o conteúdo do template a partir de "AÇÕES PRÁTICAS DA CONSULTORIA"
    #    e insere o conteúdo real das ações
    doc = docs_svc.documents().get(documentId=novo_id).execute()
    section_start = _find_section_start(doc, "AÇÕES PRÁTICAS DA CONSULTORIA")
    end = _end_index(doc)

    if section_start is not None:
        # Remove tudo após o título da seção
        title_end = section_start + len("AÇÕES PRÁTICAS DA CONSULTORIA") + 2  # +2 for \n
        if title_end < end:
            docs_svc.documents().batchUpdate(
                documentId=novo_id,
                body={"requests": [
                    {"deleteContentRange": {"range": {"startIndex": title_end, "endIndex": end}}}
                ]},
            ).execute()

    # 4. Insere as ações reais
    doc = docs_svc.documents().get(documentId=novo_id).execute()
    insert_at = _end_index(doc)

    action_lines = ["\n"]
    for i, acao in enumerate(acoes, 1):
        action_lines.append(f"\n{i}) {acao['titulo']}")
        for sub in acao.get("subacoes", [])[:3]:
            action_lines.append(f"\n• {sub}")

    action_text = "".join(action_lines)

    docs_svc.documents().batchUpdate(
        documentId=novo_id,
        body={"requests": [
            {"insertText": {"location": {"index": insert_at}, "text": action_text}}
        ]},
    ).execute()

    return novo_id, novo_nome

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("\nPRO Materiais — Subido PRO\n")

    # 1. Busca reuniões no Granola
    print("Buscando reuniões no Granola...")
    meetings = list_meetings()
    if not meetings:
        print("Nenhuma reunião encontrada. Verifique o token do Granola.")
        sys.exit(1)

    print("\nReuniões recentes:")
    for i, m in enumerate(meetings[:10], 1):
        label = "(tem notas)" if m["notes"] else "(só transcript)"
        print(f"  {i}. [{m['date']}] {m['title']} {label}")

    choice = input("\nNúmero da reunião: ").strip()
    try:
        meeting = meetings[int(choice) - 1]
    except (ValueError, IndexError):
        print("Seleção inválida.")
        sys.exit(1)

    # 2. Conteúdo da reunião
    conteudo = meeting["notes"]
    if not conteudo:
        print("Buscando transcript...")
        conteudo = get_transcript(meeting["id"])
    if not conteudo:
        print("Esta reunião não tem notas nem transcript disponível.")
        sys.exit(1)

    # 3. Tipo
    tipo = input("\nTipo — mentoria ou consultoria: ").strip().lower()
    if tipo not in ("mentoria", "consultoria"):
        print("Tipo inválido.")
        sys.exit(1)

    # 4. Processa com Claude
    print("\nProcessando com Claude...")
    try:
        dados = process_with_claude(conteudo, tipo, meeting["title"], meeting["date"])
    except json.JSONDecodeError as e:
        print(f"Claude não retornou JSON válido: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Erro ao processar: {e}")
        sys.exit(1)

    # 5. Preview
    print(f"\nAluno:  {dados.get('nome_aluno')}")
    print(f"Data:   {dados.get('data')}")
    if tipo == "mentoria":
        print(f"Sessão: {dados.get('numero_mentoria')}  |  Mentor: {dados.get('nome_mentor')}")
    else:
        print(f"Consultor: {dados.get('nome_consultor')}")

    print(f"\nAções ({len(dados.get('acoes', []))}):")
    for i, a in enumerate(dados.get("acoes", []), 1):
        if tipo == "mentoria":
            print(f"  {i}. {a['texto']}")
            for s in a.get("subacoes", []):
                print(f"       → {s}")
        else:
            print(f"  {i}. {a['titulo']}")
            for s in a.get("subacoes", []):
                print(f"       • {s}")

    confirm = input("\nSalvar no Google Docs? (s/n): ").strip().lower()
    if confirm != "s":
        print("Cancelado.")
        return

    # 6. Google Docs
    docs_svc, drive_svc = get_google_services()

    if tipo == "mentoria":
        nome_aluno = dados.get("nome_aluno", "")
        doc_id, doc_name = find_mentoria_doc(drive_svc, nome_aluno)
        if not doc_id:
            print(f"\nDoc não encontrado para '{nome_aluno}' na pasta do Drive.")
            print("Verifique se o nome no doc corresponde ao nome extraído.")
            sys.exit(1)
        print(f"\nDoc encontrado: {doc_name}")
        append_mentoria(docs_svc, doc_id, dados)
        print(f"\nMentoria adicionada com sucesso!")
        print(f"https://docs.google.com/document/d/{doc_id}/edit")

    else:
        print("\nCriando doc de consultoria...")
        doc_id, doc_name = create_consultoria_doc(drive_svc, docs_svc, dados)
        print(f"\nConsultoria criada: {doc_name}")
        print(f"https://docs.google.com/document/d/{doc_id}/edit")


if __name__ == "__main__":
    main()
