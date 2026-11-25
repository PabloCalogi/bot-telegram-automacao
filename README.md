# 🧙‍♂️ Clube do Mago — Bot de Ofertas Amazon para Telegram

Bot automatizado em Python para monitorar ofertas na **Amazon Brasil**, aplicar links de afiliado (**Amazon Associados**) e publicar, atualizar e gerenciar promoções em um canal do **Telegram** — rodando automaticamente via **GitHub Actions**.

---

## ✨ Funcionalidades

- **Monitoramento Curado (`products.txt`)**: acompanha uma lista fixa de ASINs selecionados (produtos universais e de giro rápido).
- **Descoberta Automática de Ofertas (Opcional)**: varre páginas de *Mais Vendidos* da Amazon Brasil para encontrar novos ASINs em promoção dentro de uma faixa de preço configurável, filtrando também voltagem, vestuário, nota e volume de avaliações.
- **Publicação Rica no Telegram**: envia foto do produto, preço original riscado, preço atual, percentual de desconto e botão inline com a tag de afiliado (`AMAZON_ASSOCIATE_TAG`).
- **Atualização e Exclusão Automática**:
  - Salva `message_id`, preço e tipo de mídia no **SQLite** (`data/offers.sqlite3`).
  - Edita automaticamente a mensagem no canal quando o preço de uma oferta ativa muda.
  - Exclui automaticamente a mensagem quando o produto fica sem estoque ou cai abaixo do desconto mínimo (`MIN_DISCOUNT_PERCENT`).
- **Histórico de Menor Preço**: registra o menor valor já visto por ASIN e adiciona automaticamente o selo **`🚨 MENOR PREÇO REGISTRADO`** quando o produto atinge um novo piso histórico.
- **Resiliência e Segurança**:
  - Scraping com `curl_cffi` e retentativa automática para falhas temporárias de rede/SSL.
  - Proteção contra *rate limit* do Telegram (`HTTP 429 / RetryAfter`) com espera automática e intervalo entre envios.
  - Logs limpos (`httpx`/`httpcore` silenciados para não expor o `TELEGRAM_BOT_TOKEN`).
- **Alertas de Saúde no Privado**: envia um resumo automático para o administrador (`TELEGRAM_ADMIN_CHAT_ID`) caso uma execução tenha 3+ erros, taxa de erro $\ge 30\%$ ou falha crítica.
- **Execução na Nuvem (GitHub Actions)**: agendado para rodar periodicamente em horário ativo (08h às 23h BRT), com persistência do banco SQLite via cache do workflow.

---

## 🗂️ Estrutura do Projeto

```text
.
├── .github/
│   └── workflows/
│       └── bot.yml              # Workflow agendado no GitHub Actions
├── app/
│   ├── amazon/
│   │   ├── client.py            # Scraping, parsing de preço/estoque e retry SSL
│   │   └── discovery.py         # Descoberta automática de ASINs (Mais Vendidos)
│   ├── config.py                # Leitura de variáveis de ambiente
│   ├── main.py                  # Ponto de entrada, orquestração e alertas
│   ├── publisher.py             # Envio, edição, exclusão e RetryAfter no Telegram
│   └── storage.py               # Persistência SQLite (ofertas + histórico de preços)
├── data/
│   └── offers.sqlite3           # Banco local (ignorado no Git)
├── tests/                       # Suíte de testes automatizados (pytest)
├── .env.example                 # Modelo de variáveis de ambiente
├── products.txt                 # Lista de ASINs monitorados
└── requirements.txt             # Dependências Python
```

---

## ⚙️ Variáveis de Ambiente

Crie um arquivo `.env` na raiz do projeto (baseado no `.env.example`) e configure os mesmos segredos em **Settings → Secrets and variables → Actions** no GitHub:

| Variável | Obrigatória | Padrão | Descrição |
| :--- | :---: | :---: | :--- |
| `TELEGRAM_BOT_TOKEN` | Sim | — | Token do bot gerado pelo `@BotFather` |
| `TELEGRAM_CHANNEL_ID` | Sim | — | ID do canal do Telegram (ex: `-100...`) |
| `AMAZON_ASSOCIATE_TAG` | Sim | — | Tag de afiliado da Amazon Brasil |
| `MIN_DISCOUNT_PERCENT` | Não | `15` | Desconto mínimo (%) para publicar uma oferta |
| `POLL_INTERVAL_SECONDS` | Não | `3600` | Intervalo entre ciclos no modo contínuo |
| `TELEGRAM_ADMIN_CHAT_ID` | Não | — | ID do chat privado para alertas de saúde |
| `ENABLE_AUTO_DISCOVERY` | Não | `false` | Ativa (`true`) ou desativa a descoberta automática |
| `DISCOVERY_MAX_ITEMS` | Não | `10` | Limite de novos ASINs descobertos por execução |
| `DISCOVERY_MIN_PRICE` | Não | `30` | Preço mínimo (R$) para itens da descoberta automática |
| `DISCOVERY_MAX_PRICE` | Não | `200` | Preço máximo (R$) para itens da descoberta automática |
| `DISCOVERY_MIN_RATING` | Não | `4.3` | Nota mínima (de 5) para itens descobertos |
| `DISCOVERY_MIN_REVIEWS` | Não | `50` | Número mínimo de avaliações para itens descobertos |

> **Dica:** Para receber alertas de saúde, abra uma conversa privada com o seu bot no Telegram e clique em **Iniciar (`/start`)** antes de ativar o `TELEGRAM_ADMIN_CHAT_ID`.

---

## 🚀 Como Rodar Localmente

### 1. Clonar e criar ambiente virtual (Windows / PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Configurar `.env` e `products.txt`

- Copie `.env.example` para `.env` e preencha suas credenciais.
- Adicione ou remova ASINs no arquivo `products.txt` (um por linha; linhas com `#` são ignoradas).

### 3. Executar uma rodada única

```powershell
.\.venv\Scripts\python.exe -m app.main --once
```

---

## 🧪 Testes Automatizados

A suíte cobre scraping, cálculo de descontos, descoberta automática, migração/persistência SQLite, publicação/edição/exclusão no Telegram e alertas de saúde:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

---

## ☁️ Automação no GitHub Actions

O workflow em `.github/workflows/bot.yml`:
1. Restaura o banco `data/offers.sqlite3` do cache.
2. Instala as dependências Python.
3. Executa `python -m app.main --once` com os *Secrets* configurados.
4. Salva o banco atualizado novamente no cache (mesmo em caso de erro parcial).

Também é possível disparar uma execução manual a qualquer momento pela aba **Actions → Run workflow**.