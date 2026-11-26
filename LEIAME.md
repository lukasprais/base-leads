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

**Tela "Resultados"**: mapa + lista. Filtros por busca, cidades, categorias (marque várias),
celular, site, Instagram, e-mail, perfil sem dono, nota, avaliações, status, bairro e distância
de um ponto que você clica no mapa. Marque o status, anote, e baixe o CSV (abre no Excel).

Status "Não contatar" esconde a empresa de todas as listas, a não ser que você filtre por ele.
Rodar a mesma busca de novo atualiza os dados do Maps e mantém seu status e anotações.

## Onde ficam as coisas

- `data/leads.db`: o banco (SQLite). Faça cópia dele de vez em quando.
- `data/buscas/<n>/`: resultado bruto e log de cada busca.
- `ref/`: lista de municípios do IBGE com coordenadas (para o seletor de cidades).
- `browsers/`: faz o scraper usar o Google Chrome do Mac. O navegador que vem com ele
  (Chromium 156) trava ao rolar a lista do Maps. Se tirar o Chrome do Mac, as buscas voltam a falhar
  em "Normal" e "Máximo".
- `bin/gmaps`: scraper gosom/google-maps-scraper v1.21.0 (MIT).

## Cuidados

- Uma busca roda por vez, de propósito. Buscas muito grandes seguidas podem fazer o Google
  bloquear seu IP por um tempo (aparece como busca com 0 resultados).
- "WhatsApp" é deduzido: telefone celular (9 dígitos começando com 9). Não garante que tem WhatsApp.
- Raspar o Maps vai contra os termos do Google. Para uso pessoal e em volume baixo é o risco aceito;
  para volume grande, troque pela Places API oficial.
