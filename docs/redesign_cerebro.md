# Redesign do Cérebro — Workspace de Atendimento

> Cérebro como workspace de atendimento ao cliente: **chat + painel de contexto lateral + troca rápida via URL/⌘K**.

## Arquitetura alvo

```
┌────────┬──────────────────┬─────────────────────────────────┐
│ nav    │ PAINEL CONTEXTO  │ CHAT                            │
│ global │ (~280px)         │ header: [● SIGLA — Nome ▾] ⌘K   │
│        │ ▦ Performance    │         + Nova chat             │
│        │ ▦ WhatsApp       │ estado vazio: 4 sugestões       │
│        │ ▦ Atas           │ conversa em andamento           │
│        │ ▦ Tarefas        │ input + enviar                  │
└────────┴──────────────────┴─────────────────────────────────┘
```

- URL contextual: `/cerebro/<sigla>` carrega o cliente.
- ⌘K em qualquer lugar abre command palette pra trocar de cliente.
- Sugestões do estado vazio mudam conforme dados reais.

## Stack atual (baseline)

- **FastAPI** (`backend/main.py`), Anthropic SDK (`claude-sonnet-4-6`), Postgres, Jinja2.
- Cérebro: `GET /conversar` → `chat.html` (vanilla JS, ~1091 linhas). Core: `POST /chat` (não-streaming, sem prompt caching).
- Cliente identificado por **`sigla`** (tabela `clientes`, `UNIQUE`). Seleção hoje client-side (`localStorage`).
- Conversas: arquivos JSON em `CONVS_DIR`.
- Única tool: `criar_tarefa_clickup`.

## Fontes de dados por cliente (sigla)

| Painel | Fonte | Status |
|---|---|---|
| Ficha/Performance | tabela `clientes` (`valor_mensal`, `verba_midia`, `performance`, `saude`…) | ✅ existe |
| Performance (Ads) | `documents/*.md` (`google_ads_*`) | ✅ via arquivo |
| WhatsApp recente | `whatsapp_context_para_sigla(sigla)` | ✅ query pronta |
| WhatsApp não-lidas | heurística (msgs após última resposta da agência) | ⚠️ criar (sem migration no MVP) |
| Atas | `documents` recursivo (`[SIGLA] > Atas de Reunião`) | ✅ query pronta |
| Tarefas abertas | custom field **"Cliente"** (dropdown) | ⚠️ criar query |

`_sinapse_client_context(sigla)` já agrega ficha + docs + atas + WhatsApp (blob pra LLM).

## Decisões

1. **Tarefas → cliente**: custom field "Cliente" (dropdown) — padrão estabelecido, sem paralelo.
2. **Prompt caching**: SIM — `cache_control` no bloco de documentos do system prompt.
3. **WhatsApp não-lidas**: heurística (msgs após última resposta da agência), sem migration no MVP.
4. **Alpine**: SIM — migrar de vanilla pra Alpine (painel reativo + ⌘K + estado de chat).

## Riscos

- **Performance**: painel faz N queries/request (ficha + WhatsApp + atas recursivo + tarefas). Cachear (TTL curto) + carregar async.
- **Custo/latência LLM**: system prompt reconstrói todos os documents a cada msg → **prompt caching** mitiga.
- **WAHA/Granola**: dependências externas; degradar bem se caírem (queries best-effort).

## Plano de commits

- **Commit 1** — Investigação (este doc).
- **Commit 2** — Estrutura base: rota `/cerebro/{sigla}` + redirect `/conversar`, `chat.html` reescrito em Alpine (3 colunas), tokens globais, cards/sugestões mockados, drawer de conversas no header. Sem integrações reais.
- **Commit 3** — Integrações reais dos 4 cards (endpoint `/api/cerebro/{sigla}/context`).
- **Commit 4** — ⌘K funcional + URL contextual + troca de cliente.
- **Commit 5** — Sugestões inteligentes (geradas do contexto real).
