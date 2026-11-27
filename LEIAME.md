# Base de Leads

Busca empresas no Google Maps por segmento e região, guarda tudo num banco local e ajuda a organizar a prospecção.

## Como abrir

Precisa de macOS (Apple Silicon), [uv](https://docs.astral.sh/uv/) e Google Chrome.

    ./instalar.sh   # só na primeira vez: baixa o scraper e prepara o Chrome
    ./iniciar.sh

Abre em http://localhost:8090. Para fechar, Ctrl+C no terminal.

## Como usar

**Tela "Nova busca"**
1. Digite os segmentos e aperte Enter (ou clique nas sugestões).
2. Onde:
   - **Cidades**: digite o nome, a região ou o CEP. Pode escolher várias cidades; escolher uma
     região (regiões imediatas do IBGE) adiciona todas as cidades dela.
   - **Raio em volta de um ponto**: CEP, cidade, clique no mapa ou "Minha localização", e o raio.
     Até 3 km é uma busca só. Acima disso a área é dividida em quadrados e pega bem mais empresas.
3. "Buscar empresas". Cada segmento × cidade vira uma consulta. Roda em segundo plano.

**Painel**: números da prospecção, empresas novas por dia, funil, as melhores para abordar,
próximos retornos, telefones repetidos, quem mudou no Google e atividade recente.

**Leads**: mapa + lista. Filtros por busca, score, cidades, categorias, tags, celular, site,
Instagram, e-mail, perfil sem dono, telefone repetido, mudou no Google, WhatsApp conferido,
nota, avaliações, status, retorno, bairro e distância de um ponto que você clica no mapa.
Marque várias para mudar o status, pôr uma tag, conferir o WhatsApp ou baixar só elas em CSV.
"Salvar lista" guarda os filtros com um nome no menu.

**Detalhe da empresa**: score com os motivos, tags, status, data de retorno, mensagem pronta
(modelos com {nome}, {cidade}, {bairro}, {categoria}, {nota}), anotações e o histórico do contato
(status, mensagens, ligações, visitas, o que mudou no Google).

**Funil**: quadro por status; arraste o cartão para mudar.

**Histórico**: todas as buscas. "Repetir sozinha" roda a busca de novo toda semana, a cada 15 dias
ou todo mês, às 7h (o Base de Leads precisa estar aberto). Empresas que mudaram no Google
(nota, site, telefone, dono) ficam marcadas.

**Configurações**:
- Zapvu: com a instância e a chave (zv_...), a mensagem sai pelo seu número e dá para conferir
  quem tem WhatsApp de verdade (até 50 números por vez). O envio é sempre um por vez, pelo botão.
- Pesos do score de oportunidade. A soma é convertida para 0 a 100.

Atalhos: ⌘K (ou Ctrl K) procura empresa ou ação; `/` vai para a busca da lista; Esc fecha.

Status "Não contatar" esconde a empresa de todas as listas, a não ser que você filtre por ele.
Rodar a mesma busca de novo atualiza os dados do Maps e mantém seu status, tags e anotações.

## Onde ficam as coisas

- `data/leads.db`: o banco (SQLite), com leads, histórico, listas, agendas e configurações
  (inclusive a chave do Zapvu). Faça cópia dele de vez em quando e não o compartilhe.
- `data/buscas/<n>/`: resultado bruto e log de cada busca.
- `ref/`: lista de municípios do IBGE com coordenadas (para o seletor de cidades).
- `browsers/`: faz o scraper usar o Google Chrome do Mac. O navegador que vem com ele
  (Chromium 156) trava ao rolar a lista do Maps. Se tirar o Chrome do Mac, as buscas voltam a falhar
  em "Normal" e "Máximo".
- `bin/gmaps`: scraper gosom/google-maps-scraper v1.21.0 (MIT).

## Cuidados

- Uma busca roda por vez, de propósito. Buscas muito grandes seguidas podem fazer o Google
  bloquear seu IP por um tempo (aparece como busca com 0 resultados).
- "WhatsApp" é deduzido do número (celular com 9 dígitos começando com 9) até ser conferido no
  Zapvu. Conferir muitos números seguidos pode bloquear o seu; vá aos poucos.
- Raspar o Maps vai contra os termos do Google. Para uso pessoal e em volume baixo é o risco aceito;
  para volume grande, troque pela Places API oficial.
