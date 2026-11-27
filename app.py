"""Base de Leads: busca empresas no Google Maps (scraper gosom) e monta uma base para prospecção.

Rodar:  ./iniciar.sh   (ou: uv run uvicorn app:app --port 8090)
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import sqlite3
import subprocess
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

RAIZ = Path(__file__).parent
BIN = RAIZ / "bin" / "gmaps"
DADOS = Path(os.environ.get("BASE_LEADS_DADOS") or RAIZ / "data")
REF = RAIZ / "ref"
DB = DADOS / "leads.db"
DADOS.mkdir(exist_ok=True)

STATUS_LEAD = ["novo", "contatar", "contatado", "interessado", "cliente", "descartado", "nao_contatar"]


def sem_acento(s: str | None) -> str:
    s = unicodedata.normalize("NFD", s or "")
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower().strip()


# ---------------------------------------------------------------- municípios (IBGE)

def carregar_municipios() -> list[dict]:
    """Lista oficial do IBGE (nome, UF, região imediata) + coordenadas da sede (kelvins/municipios-brasileiros)."""
    cache = REF / "municipios.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    coords = {}
    with (REF / "municipios.csv").open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            coords[int(r["codigo_ibge"])] = (float(r["latitude"]), float(r["longitude"]), r["ddd"])
    lista = []
    for m in json.loads((REF / "ibge.json").read_text(encoding="utf-8")):
        mid = m["municipio-id"]
        lat, lon, ddd = coords.get(mid, (None, None, None))
        lista.append({
            "id": mid, "nome": m["municipio-nome"], "uf": m["UF-sigla"], "estado": m["UF-nome"],
            "lat": lat, "lon": lon, "ddd": ddd,
            "ri_id": m["regiao-imediata-id"], "ri": m["regiao-imediata-nome"],
            "rg_id": m["regiao-intermediaria-id"], "rg": m["regiao-intermediaria-nome"],
        })
    cache.write_text(json.dumps(lista, ensure_ascii=False), encoding="utf-8")
    return lista


MUNICIPIOS = carregar_municipios()
MUN_POR_ID = {m["id"]: m for m in MUNICIPIOS}
MUN_POR_NOME_UF = {(sem_acento(m["nome"]), m["uf"]): m for m in MUNICIPIOS}
UF_POR_ESTADO = {sem_acento(m["estado"]): m["uf"] for m in MUNICIPIOS}
for m in MUNICIPIOS:
    m["_busca"] = sem_acento(m["nome"])


UFS = set(UF_POR_ESTADO.values())


def uf_de(estado: str | None) -> str | None:
    if not estado:
        return None
    e = estado.strip()
    if len(e) == 2:
        return e.upper()
    return UF_POR_ESTADO.get(sem_acento(e))


# ---------------------------------------------------------------- banco

def conectar() -> sqlite3.Connection:
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.create_function("dist_km", 4, _haversine, deterministic=True)
    con.create_function("digitos", 1, fone_normal, deterministic=True)
    return con


def fone_normal(v: str | None) -> str | None:
    """Telefone só com DDD + número, para achar duplicados ("+55 31 ..." e "(31) ..." viram o mesmo)."""
    d = re.sub(r"\D", "", v or "")
    if d.startswith("55") and len(d) >= 12:
        d = d[2:]
    return d.lstrip("0") or None


def _haversine(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2):
        return None
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def colunas(c, tabela):
    return {r[1] for r in c.execute(f"PRAGMA table_info({tabela})")}


with conectar() as c:
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS buscas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            termos TEXT NOT NULL,
            modo TEXT NOT NULL,            -- 'ponto' ou 'cidade'
            cidade TEXT, lat REAL, lon REAL, raio_km REAL,
            profundidade INTEGER, emails INTEGER,
            situacao TEXT NOT NULL DEFAULT 'na_fila',  -- na_fila, rodando, pronta, erro, cancelada
            encontrados INTEGER DEFAULT 0, novos INTEGER DEFAULT 0,
            erro TEXT, criada TEXT, terminada TEXT
        );
        CREATE TABLE IF NOT EXISTS leads (
            id TEXT PRIMARY KEY,           -- place_id do Google (ou cid)
            nome TEXT, categoria TEXT, categorias TEXT,
            endereco TEXT, bairro TEXT, cidade TEXT, estado TEXT, cep TEXT,
            telefone TEXT, whatsapp TEXT, site TEXT, instagram TEXT, emails TEXT,
            nota REAL, avaliacoes INTEGER, lat REAL, lon REAL,
            link TEXT, horario TEXT, descricao TEXT, reivindicado INTEGER,
            status TEXT NOT NULL DEFAULT 'novo', observacao TEXT,
            primeira_busca INTEGER, criado TEXT, atualizado TEXT
        );
        CREATE TABLE IF NOT EXISTS busca_leads (
            busca_id INTEGER, lead_id TEXT, termo TEXT,
            PRIMARY KEY (busca_id, lead_id)
        );
        CREATE INDEX IF NOT EXISTS ix_leads_cidade ON leads(cidade);
        CREATE INDEX IF NOT EXISTS ix_leads_status ON leads(status);
        """
    )
    if "cidades" not in colunas(c, "buscas"):
        c.execute("ALTER TABLE buscas ADD COLUMN cidades TEXT")      # JSON [{id,nome,uf}]
        c.execute("ALTER TABLE buscas ADD COLUMN tentativas INTEGER DEFAULT 0")
    if "uf" not in colunas(c, "leads"):
        c.execute("ALTER TABLE leads ADD COLUMN uf TEXT")
        c.execute("ALTER TABLE leads ADD COLUMN municipio_id INTEGER")
        c.execute("ALTER TABLE leads ADD COLUMN termo TEXT")
    for r in c.execute("SELECT id, cidade, estado FROM leads WHERE uf IS NULL").fetchall():
        uf = uf_de(r["estado"])
        mun = MUN_POR_NOME_UF.get((sem_acento(r["cidade"]), uf))
        c.execute("UPDATE leads SET uf=?, municipio_id=? WHERE id=?", (uf, mun and mun["id"], r["id"]))
    c.execute("CREATE INDEX IF NOT EXISTS ix_leads_mun ON leads(municipio_id)")
    if "retorno" not in colunas(c, "leads"):
        c.execute("ALTER TABLE leads ADD COLUMN retorno TEXT")      # data do próximo contato (AAAA-MM-DD)
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS atividades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id TEXT NOT NULL,
            tipo TEXT NOT NULL,            -- status, nota, whatsapp, ligacao, retorno
            texto TEXT, criado TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_ativ_lead ON atividades(lead_id, id);
        CREATE TABLE IF NOT EXISTS config (chave TEXT PRIMARY KEY, valor TEXT);
        CREATE TABLE IF NOT EXISTS listas (
            id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT NOT NULL, params TEXT NOT NULL, criada TEXT
        );
        CREATE TABLE IF NOT EXISTS agendas (
            busca_id INTEGER PRIMARY KEY,  -- busca usada como modelo
            dias INTEGER NOT NULL,         -- repete a cada N dias
            proxima TEXT NOT NULL,         -- quando roda de novo (ISO)
            ultima_busca INTEGER, criada TEXT
        );
        """
    )
    if "tags" not in colunas(c, "leads"):
        c.execute("ALTER TABLE leads ADD COLUMN tags TEXT")           # ",tag1,tag2," (vírgulas nas pontas para LIKE)
        c.execute("ALTER TABLE leads ADD COLUMN whatsapp_ok INTEGER")  # 1/0 conferido no Zapvu, NULL = deduzido
    # buscas interrompidas quando o servidor caiu
    c.execute("UPDATE buscas SET situacao='erro', erro='O servidor foi reiniciado durante a busca. Rode de novo.' "
              "WHERE situacao IN ('rodando','na_fila')")


def agora() -> str:
    return datetime.now().isoformat(timespec="seconds")


def cfg(chave: str, padrao=None):
    with conectar() as c:
        r = c.execute("SELECT valor FROM config WHERE chave=?", (chave,)).fetchone()
    return json.loads(r["valor"]) if r else padrao


def set_cfg(chave: str, valor):
    with conectar() as c:
        if valor is None:
            c.execute("DELETE FROM config WHERE chave=?", (chave,))
        else:
            c.execute("INSERT INTO config VALUES (?, ?) ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor",
                      (chave, json.dumps(valor, ensure_ascii=False)))


# ---------------------------------------------------------------- normalização

def so_digitos(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")


def whatsapp_de(telefone: str | None) -> str | None:
    """Celular brasileiro (DDD + 9 dígitos começando com 9) vira número de WhatsApp com 55."""
    d = so_digitos(telefone)
    if d.startswith("55") and len(d) == 13:
        d = d[2:]
    d = d.lstrip("0")
    if len(d) == 11 and d[2] == "9":
        return "55" + d
    return None


def instagram_de(site: str | None) -> str | None:
    if not site:
        return None
    m = re.search(r"instagram\.com/([A-Za-z0-9_.]+)", site)
    if m and m.group(1).lower() not in {"p", "reel", "explore", "accounts"}:
        return "@" + m.group(1).rstrip(".")
    return None


EMAIL_LIXO = re.compile(r"(exemplo|example|seuemail|seu-email|email@|nome@|usuario@|user@|teste@|test@|"
                        r"@email\.com$|@dominio|@domain|sentry|wixpress|\.(png|jpe?g|gif|webp|svg)$)", re.I)


def limpar_emails(emails) -> list[str]:
    bons = set()
    for e in emails or []:
        e = e.strip().lower()
        if re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", e) and not EMAIL_LIXO.search(e):
            bons.add(e)
    return sorted(bons)


def lead_de(r: dict) -> dict | None:
    lid = r.get("place_id") or r.get("cid") or r.get("data_id")
    if not lid or not r.get("title"):
        return None
    end = r.get("complete_address") or {}
    site = (r.get("web_site") or "").strip() or None
    emails = limpar_emails(r.get("emails"))
    uf = uf_de(end.get("state"))
    mun = MUN_POR_NOME_UF.get((sem_acento(end.get("city")), uf))
    return {
        "id": lid,
        "nome": r.get("title"),
        "categoria": r.get("category") or None,
        "categorias": ", ".join(r.get("categories") or []) or None,
        "endereco": r.get("address") or None,
        "bairro": end.get("borough") or None,
        "cidade": (mun and mun["nome"]) or end.get("city") or None,
        "estado": end.get("state") or None,
        "uf": uf,
        "municipio_id": mun and mun["id"],
        "cep": end.get("postal_code") or None,
        "telefone": r.get("phone") or None,
        "whatsapp": whatsapp_de(r.get("phone")),
        "site": site,
        "instagram": instagram_de(site),
        "emails": ", ".join(emails) or None,
        "nota": r.get("review_rating") or None,
        "avaliacoes": r.get("review_count") or 0,
        "lat": r.get("latitude"),
        "lon": r.get("longitude") or r.get("longtitude"),
        "link": r.get("link"),
        "horario": json.dumps(r.get("open_hours") or {}, ensure_ascii=False),
        "descricao": r.get("description") or r.get("status") or None,
        "reivindicado": 1 if (r.get("owner") or {}).get("id") else 0,
    }


CAMPOS_MAPS = ["nome", "categoria", "categorias", "endereco", "bairro", "cidade", "estado", "uf", "municipio_id",
               "cep", "telefone", "whatsapp", "site", "instagram", "emails", "nota", "avaliacoes", "lat", "lon",
               "link", "horario", "descricao", "reivindicado"]


def mudancas(antes, depois: dict) -> str | None:
    """O que mudou no Google entre uma busca e outra, em texto curto."""
    m = []
    fmt = lambda v: f"{v:.1f}".replace(".", ",")  # noqa: E731
    if so_digitos(antes["telefone"]) != so_digitos(depois["telefone"]):
        m.append(f"telefone {antes['telefone'] or 'vazio'} → {depois['telefone'] or 'vazio'}")
    if bool(antes["site"]) != bool(depois["site"]):
        m.append("criou site" if depois["site"] else "site sumiu")
    if antes["nota"] and depois["nota"] and abs(antes["nota"] - depois["nota"]) >= 0.1:
        m.append(f"nota {fmt(antes['nota'])} → {fmt(depois['nota'])}")
    if (depois["avaliacoes"] or 0) - (antes["avaliacoes"] or 0) >= 5:
        m.append(f"+{depois['avaliacoes'] - (antes['avaliacoes'] or 0)} avaliações")
    if not antes["reivindicado"] and depois["reivindicado"]:
        m.append("o dono assumiu o perfil")
    if antes["nome"] != depois["nome"]:
        m.append(f"nome {antes['nome']} → {depois['nome']}")
    return "; ".join(m) or None


def importar(busca_id: int, arquivo: Path, termo_por_input: dict) -> tuple[int, int]:
    """Grava os resultados. Status e observação de leads que já existiam são mantidos."""
    total = novos = 0
    if not arquivo.exists():
        return 0, 0
    vistos = set()
    with conectar() as c:
        for linha in arquivo.open(encoding="utf-8"):
            linha = linha.strip()
            if not linha:
                continue
            try:
                r = json.loads(linha)
            except json.JSONDecodeError:
                continue
            l = lead_de(r)
            if not l or l["id"] in vistos:
                continue
            vistos.add(l["id"])
            total += 1
            termo = termo_por_input.get(r.get("input_id"))
            existe = c.execute("SELECT * FROM leads WHERE id=?", (l["id"],)).fetchone()
            if existe:
                if not l["emails"]:
                    l["emails"] = existe["emails"]  # busca sem e-mail não apaga o que já tinha
                mesmo_fone = so_digitos(existe["telefone"]) == so_digitos(l["telefone"])
                ok = existe["whatsapp_ok"] if mesmo_fone else None
                if ok is not None:  # conferido no Zapvu: vale mais que a dedução pelo número
                    l["whatsapp"] = existe["whatsapp"]
                if mudou := mudancas(existe, l):
                    registrar(c, l["id"], "mudanca", mudou)
                sets = ", ".join(f"{k}=:{k}" for k in CAMPOS_MAPS)
                c.execute(f"UPDATE leads SET {sets}, whatsapp_ok=:ok, atualizado=:at WHERE id=:id",
                          {**l, "ok": ok, "at": agora()})
            else:
                novos += 1
                cols = ["id", *CAMPOS_MAPS]
                c.execute(
                    f"INSERT INTO leads ({', '.join(cols)}, termo, primeira_busca, criado, atualizado) "
                    f"VALUES ({', '.join(':' + k for k in cols)}, :termo, :b, :at, :at)",
                    {**l, "termo": termo, "b": busca_id, "at": agora()},
                )
            c.execute("INSERT OR IGNORE INTO busca_leads VALUES (?,?,?)", (busca_id, l["id"], termo))
    return total, novos


# ---------------------------------------------------------------- execução do scraper

# O chrome-headless-shell que o scraper baixa (Playwright 1248 / Chromium 156) quebra com SIGTRAP ao rolar
# a lista do Maps. browsers/ tem um atalho que usa o Google Chrome instalado no Mac no lugar dele.
CHROME_MAC = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
AMBIENTE = dict(os.environ)
if CHROME_MAC.exists() and (RAIZ / "browsers").exists():
    AMBIENTE["PLAYWRIGHT_BROWSERS_PATH"] = str(RAIZ / "browsers")

PROCESSOS: dict[int, subprocess.Popen] = {}
FILA_LOCK = threading.Lock()  # uma busca por vez, para não chamar atenção do Google
INATIVIDADE_MAX = 150  # segundos sem gravar nada = terminou (o -exit-on-inactivity falha no modo grade)
MAX_TENTATIVAS = 3     # o navegador do scraper às vezes fecha sozinho; tenta de novo


def zoom_para_raio(raio_km: float) -> int:
    for z, km in [(17, 0.6), (16, 1.2), (15, 2.5), (14, 5), (13, 10), (12, 20)]:
        if raio_km <= km:
            return z
    return 11


def montar_comando(b: dict, pasta: Path) -> list[str]:
    cmd = [str(BIN), "-input", str(pasta / "termos.txt"), "-results", str(pasta / "resultado.json"),
           "-json", "-lang", "pt-BR", "-depth", str(b["profundidade"]), "-c", "4",
           "-exit-on-inactivity", "3m"]
    if b["emails"]:
        cmd.append("-email")
    if b["modo"] == "ponto":
        raio = b["raio_km"]
        if raio <= 3:
            cmd += ["-geo", f"{b['lat']},{b['lon']}", "-zoom", str(zoom_para_raio(raio)),
                    "-radius", str(int(raio * 1000))]
        else:
            # área grande: divide em quadrados para não parar nos ~120 resultados por busca
            dlat = raio / 111.0
            dlon = raio / (111.0 * math.cos(math.radians(b["lat"])))
            bbox = f"{b['lat']-dlat:.5f},{b['lon']-dlon:.5f},{b['lat']+dlat:.5f},{b['lon']+dlon:.5f}"
            celula = max(1.0, round(2 * raio / 5, 1))  # ~25 quadrados
            cmd += ["-grid-bbox", bbox, "-grid-cell", str(celula), "-grid-strict"]
    return cmd


def consultas_da_busca(b: dict) -> tuple[list[str], dict]:
    """Linhas do arquivo de entrada (com id próprio '#!#') e o termo de cada id."""
    termos = [t.strip() for t in b["termos"].splitlines() if t.strip()]
    linhas, termo_por_id = [], {}
    if b["modo"] == "cidade":
        cidades = json.loads(b["cidades"] or "[]")
        for i, t in enumerate(termos):
            for j, cid in enumerate(cidades):
                qid = f"t{i}c{j}"
                linhas.append(f"{t} em {cid['nome']}, {cid['uf']} #!#{qid}")
                termo_por_id[qid] = t
    else:
        for i, t in enumerate(termos):
            qid = f"t{i}"
            linhas.append(f"{t} #!#{qid}")
            termo_por_id[qid] = t
    return linhas, termo_por_id


def navegador_caiu(log: Path) -> bool:
    try:
        texto = log.read_text(errors="ignore")
    except FileNotFoundError:
        return False
    return "target closed" in texto or "browser has been closed" in texto


def esperar_com_vigia(p: subprocess.Popen, pasta: Path):
    arquivos = [pasta / "log.txt", pasta / "resultado.json"]
    while p.poll() is None:
        time.sleep(5)
        ultima = max((a.stat().st_mtime for a in arquivos if a.exists()), default=time.time())
        if time.time() - ultima > INATIVIDADE_MAX:
            p.terminate()
            try:
                p.wait(15)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()


def situacao_atual(busca_id: int) -> str:
    with conectar() as c:
        return c.execute("SELECT situacao FROM buscas WHERE id=?", (busca_id,)).fetchone()["situacao"]


def rodar_busca(busca_id: int):
    with FILA_LOCK:
        with conectar() as c:
            b = dict(c.execute("SELECT * FROM buscas WHERE id=?", (busca_id,)).fetchone())
            if b["situacao"] == "cancelada":
                return
            c.execute("UPDATE buscas SET situacao='rodando' WHERE id=?", (busca_id,))
        pasta = DADOS / "buscas" / str(busca_id)
        pasta.mkdir(parents=True, exist_ok=True)
        linhas, termo_por_id = consultas_da_busca(b)
        (pasta / "termos.txt").write_text("\n".join(linhas) + "\n", encoding="utf-8")
        cmd = montar_comando(b, pasta)
        (pasta / "comando.txt").write_text(" ".join(cmd), encoding="utf-8")
        erro, tentativa = None, 0
        while True:
            tentativa += 1
            erro = None
            try:
                # -resume aproveita o que já foi coletado numa tentativa anterior
                extra = ["-resume"] if tentativa > 1 and (pasta / "resultado.json").exists() else []
                with open(pasta / "log.txt", "w") as log:
                    p = subprocess.Popen(cmd + extra, stdout=log, stderr=subprocess.STDOUT, cwd=pasta,
                                         stdin=subprocess.DEVNULL, env=AMBIENTE)
                    PROCESSOS[busca_id] = p
                    esperar_com_vigia(p, pasta)
                if p.returncode not in (0, -15, -9):
                    erro = f"O scraper terminou com código {p.returncode}. Veja data/buscas/{busca_id}/log.txt"
            except Exception as e:  # noqa: BLE001
                erro = str(e)
            finally:
                PROCESSOS.pop(busca_id, None)
            if situacao_atual(busca_id) == "cancelada":
                break
            ok = contar_linhas(pasta / "resultado.json")
            if navegador_caiu(pasta / "log.txt") and tentativa < MAX_TENTATIVAS:
                with conectar() as c:
                    c.execute("UPDATE buscas SET tentativas=? WHERE id=?", (tentativa, busca_id))
                time.sleep(5)
                continue
            if ok == 0 and navegador_caiu(pasta / "log.txt"):
                erro = (f"O navegador do scraper fechou {tentativa} vezes seguidas. "
                        "Espere uns minutos e rode de novo; se repetir, o Google pode estar bloqueando.")
            break
        total, novos = importar(busca_id, pasta / "resultado.json", termo_por_id)
        with conectar() as c:
            atual = situacao_atual(busca_id)
            situacao = "cancelada" if atual == "cancelada" else ("erro" if erro and not total else "pronta")
            c.execute("UPDATE buscas SET situacao=?, encontrados=?, novos=?, erro=?, terminada=?, tentativas=? "
                      "WHERE id=?", (situacao, total, novos, erro, agora(), tentativa, busca_id))


def contar_linhas(arq: Path) -> int:
    try:
        with arq.open("rb") as f:
            return sum(1 for _ in f)
    except FileNotFoundError:
        return 0


# ---------------------------------------------------------------- API

app = FastAPI(title="Base de Leads")


class CidadeSel(BaseModel):
    id: int
    nome: str
    uf: str


class NovaBusca(BaseModel):
    termos: list[str] = Field(min_length=1)
    modo: str = Field(pattern="^(ponto|cidade)$")
    cidades: list[CidadeSel] = []
    lat: float | None = None
    lon: float | None = None
    raio_km: float = Field(default=2, gt=0, le=50)
    profundidade: int = Field(default=5, ge=1, le=20)
    emails: bool = False


@app.get("/")
def inicio():
    return FileResponse(RAIZ / "static" / "index.html")


@app.get("/api/municipios")
def buscar_municipios(q: str = ""):
    """Cidades e regiões do IBGE para o campo de seleção. Aceita 'cidade', 'cidade mg' ou 'cidade - mg'."""
    termo = sem_acento(q)
    uf = None
    m = re.match(r"^(.*?)[\s,/-]+([a-z]{2})$", termo)
    if m and m.group(2).upper() in UFS:
        termo, uf = m.group(1).strip(), m.group(2).upper()
    if len(termo) < 2:
        return {"cidades": [], "regioes": []}
    res = _procurar(termo, uf)
    if uf and not res["cidades"]:  # "xx" era parte do nome, não UF
        res = _procurar(sem_acento(q), None)
    return res


def _procurar(termo: str, uf: str | None):
    def ok(x):
        return uf is None or x["uf"] == uf
    comeca = sorted((x for x in MUNICIPIOS if x["_busca"].startswith(termo) and ok(x)),
                    key=lambda x: (x["_busca"] != termo, len(x["_busca"])))
    contem = [x for x in MUNICIPIOS if termo in x["_busca"] and not x["_busca"].startswith(termo) and ok(x)]
    cidades = [{k: x[k] for k in ("id", "nome", "uf", "ri", "ri_id", "lat", "lon")} for x in (comeca + contem)[:12]]
    regioes, vistas = [], set()
    for x in MUNICIPIOS:
        if x["ri_id"] in vistas or not ok(x):
            continue
        if sem_acento(x["ri"]).startswith(termo):
            vistas.add(x["ri_id"])
            n = sum(1 for y in MUNICIPIOS if y["ri_id"] == x["ri_id"])
            regioes.append({"id": x["ri_id"], "nome": x["ri"], "uf": x["uf"], "cidades": n})
    return {"cidades": cidades, "regioes": regioes[:5]}


@app.get("/api/regioes/{rid}")
def cidades_da_regiao(rid: int):
    lista = [{k: x[k] for k in ("id", "nome", "uf", "ri", "ri_id", "lat", "lon")} for x in MUNICIPIOS if x["ri_id"] == rid]
    if not lista:
        raise HTTPException(404, "Região não encontrada")
    return lista


def _get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "base-leads/1.0"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode())


@app.get("/api/cep/{cep}")
def consultar_cep(cep: str):
    d = so_digitos(cep)
    if len(d) != 8:
        raise HTTPException(400, "CEP precisa ter 8 números.")
    res = None
    try:
        j = _get_json(f"https://brasilapi.com.br/api/cep/v2/{d}")
        coord = (j.get("location") or {}).get("coordinates") or {}
        res = {"cep": d, "rua": j.get("street"), "bairro": j.get("neighborhood"),
               "ibge": int((j.get("ibge") or {}).get("city") or 0) or None,
               "cidade": j.get("city"), "uf": j.get("state"),
               "lat": float(coord["latitude"]) if coord.get("latitude") else None,
               "lon": float(coord["longitude"]) if coord.get("longitude") else None}
    except Exception:  # noqa: BLE001
        pass
    if not res:
        try:
            j = _get_json(f"https://viacep.com.br/ws/{d}/json/")
            if not j.get("erro"):
                res = {"cep": d, "rua": j.get("logradouro"), "bairro": j.get("bairro"), "ibge": int(j["ibge"]),
                       "cidade": j.get("localidade"), "uf": j.get("uf"), "lat": None, "lon": None}
        except Exception:  # noqa: BLE001
            pass
    if not res:
        raise HTTPException(404, "Não achei esse CEP. Confira os números.")
    mun = MUN_POR_ID.get(res["ibge"]) or MUN_POR_NOME_UF.get((sem_acento(res["cidade"]), res["uf"]))
    if mun:
        res["municipio"] = {k: mun[k] for k in ("id", "nome", "uf", "ri", "ri_id", "lat", "lon")}
        if res["lat"] is None:
            res["lat"], res["lon"], res["precisao"] = mun["lat"], mun["lon"], "cidade"
    res.setdefault("precisao", "cep")
    return res


@app.post("/api/buscas")
def criar_busca(nb: NovaBusca):
    termos = [t.strip() for t in nb.termos if t.strip()]
    if not termos:
        raise HTTPException(400, "Escreva pelo menos um segmento (ex.: padaria).")
    if nb.modo == "cidade" and not nb.cidades:
        raise HTTPException(400, "Escolha pelo menos uma cidade.")
    if nb.modo == "ponto" and (nb.lat is None or nb.lon is None):
        raise HTTPException(400, "Marque o ponto central no mapa, por CEP ou pela sua localização.")
    cidades = [c.model_dump() for c in nb.cidades] if nb.modo == "cidade" else []
    rotulo = ", ".join(f"{c['nome']} - {c['uf']}" for c in cidades) or None
    with conectar() as c:
        cur = c.execute(
            "INSERT INTO buscas (termos, modo, cidade, cidades, lat, lon, raio_km, profundidade, emails, criada) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("\n".join(termos), nb.modo, rotulo, json.dumps(cidades, ensure_ascii=False), nb.lat, nb.lon,
             nb.raio_km if nb.modo == "ponto" else None, nb.profundidade, int(nb.emails), agora()))
        bid = cur.lastrowid
    threading.Thread(target=rodar_busca, args=(bid,), daemon=True).start()
    return {"id": bid}


@app.get("/api/buscas")
def listar_buscas():
    with conectar() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM buscas ORDER BY id DESC LIMIT 100")]
        agendas = {r["busca_id"]: dict(r) for r in c.execute("SELECT * FROM agendas")}
    for r in rows:
        r["cidades"] = json.loads(r["cidades"] or "[]")
        n_termos = len([t for t in r["termos"].splitlines() if t.strip()])
        r["consultas"] = n_termos * max(1, len(r["cidades"]))
        if r["situacao"] == "rodando":
            r["parcial"] = contar_linhas(DADOS / "buscas" / str(r["id"]) / "resultado.json")
        r["agenda"] = agendas.get(r["id"])
    return rows


@app.post("/api/buscas/{bid}/cancelar")
def cancelar_busca(bid: int):
    with conectar() as c:
        c.execute("UPDATE buscas SET situacao='cancelada' WHERE id=? AND situacao IN ('na_fila','rodando')", (bid,))
    p = PROCESSOS.get(bid)
    if p:
        p.terminate()
    return {"ok": True}


@app.delete("/api/buscas/{bid}")
def apagar_busca(bid: int):
    """Tira do histórico uma busca que não trouxe nada. Buscas com empresas ficam, para não perder a origem."""
    with conectar() as c:
        b = c.execute("SELECT situacao, encontrados FROM buscas WHERE id=?", (bid,)).fetchone()
        if not b:
            raise HTTPException(404, "Busca não encontrada")
        if b["situacao"] in ("rodando", "na_fila") or b["encontrados"]:
            raise HTTPException(400, "Só dá para tirar da lista buscas paradas que não trouxeram empresas.")
        c.execute("DELETE FROM buscas WHERE id=?", (bid,))
    return {"ok": True}


@app.post("/api/buscas/{bid}/repetir")
def repetir_busca(bid: int):
    with conectar() as c:
        b = c.execute("SELECT * FROM buscas WHERE id=?", (bid,)).fetchone()
    if not b:
        raise HTTPException(404, "Busca não encontrada")
    nb = NovaBusca(termos=b["termos"].splitlines(), modo=b["modo"], cidades=json.loads(b["cidades"] or "[]"),
                   lat=b["lat"], lon=b["lon"], raio_km=b["raio_km"] or 2, profundidade=b["profundidade"],
                   emails=bool(b["emails"]))
    return criar_busca(nb)


# Score de oportunidade (0-100): quanto mais alto, mais a empresa precisa de ajuda com presença digital
# e mais fácil é falar com ela. Os pesos podem ser mudados na tela Configurações; a soma vira 0-100.
CRITERIOS_SCORE = [
    ("whats", "whatsapp IS NOT NULL", 30, "Tem WhatsApp"),
    ("sem_site", "site IS NULL", 25, "Não tem site"),
    ("sem_dono", "COALESCE(reivindicado, 0) = 0", 15, "Perfil do Google sem dono"),
    ("poucas_aval", "COALESCE(avaliacoes, 0) < 20", 10, "Menos de 20 avaliações"),
    ("nota_baixa", "nota IS NOT NULL AND nota < 4.2", 10, "Nota abaixo de 4,2"),
    ("contato_extra", "emails IS NOT NULL OR instagram IS NOT NULL", 10, "Tem e-mail ou Instagram"),
]


def pesos_score() -> dict:
    salvos = cfg("pesos", {}) or {}
    return {k: int(salvos.get(k, padrao)) for k, _, padrao, _ in CRITERIOS_SCORE}


def score_sql() -> str:
    pesos = pesos_score()
    total = sum(pesos.values()) or 1
    soma = " + ".join(f"(CASE WHEN {cond} THEN {pesos[k]} ELSE 0 END)" for k, cond, _, _ in CRITERIOS_SCORE)
    return f"CAST(ROUND(({soma}) * 100.0 / {total}) AS INTEGER)"


ORDENS = {
    "score": "score DESC, avaliacoes DESC",
    "retorno": "retorno IS NULL, retorno",
    "distancia": "distancia IS NULL, distancia",
    "nota_asc": "nota IS NULL, nota",
    "nota_desc": "nota DESC",
    "avaliacoes_asc": "avaliacoes",
    "avaliacoes_desc": "avaliacoes DESC",
    "nome": "nome COLLATE NOCASE",
    "recentes": "criado DESC",
}


def lista_param(p: dict, chave: str) -> list[str]:
    return [v for v in (p.get(chave) or "").split(",") if v]


def filtrar(p: dict) -> tuple[str, list]:
    where, args = ["1=1"], []
    lat, lon = p.get("lat"), p.get("lon")
    dist = "NULL"
    if lat and lon:
        dist = "dist_km(lat, lon, ?, ?)"
        args += [float(lat), float(lon)]
    if q := p.get("q"):
        where.append("(nome LIKE ? OR categorias LIKE ? OR endereco LIKE ? OR observacao LIKE ? OR tags LIKE ? "
                     "OR digitos(telefone) LIKE ?)")
        args += [f"%{q}%"] * 5 + [f"%{so_digitos(q) or q}%"]
    if mids := [int(x) for x in lista_param(p, "municipios") if x.isdigit()]:
        where.append(f"municipio_id IN ({','.join('?' * len(mids))})")
        args += mids
    if cats := lista_param(p, "categorias"):
        where.append(f"categoria IN ({','.join('?' * len(cats))})")
        args += cats
    if v := p.get("bairro"):
        where.append("bairro LIKE ?")
        args.append(f"%{v}%")
    sts = [s for s in lista_param(p, "status") if s in STATUS_LEAD]
    if sts:
        where.append(f"status IN ({','.join('?' * len(sts))})")
        args += sts
    else:
        where.append("status <> 'nao_contatar'")
    for chave, sql, conv in (("nota_min", "nota >= ?", float), ("nota_max", "nota <= ?", float),
                             ("aval_min", "avaliacoes >= ?", int), ("aval_max", "avaliacoes <= ?", int)):
        if v := p.get(chave):
            where.append(sql)
            args.append(conv(v))
    tem = {"sim": "IS NOT NULL", "nao": "IS NULL"}
    for chave, coluna in (("site", "site"), ("whats", "whatsapp"), ("email", "emails"), ("insta", "instagram"),
                          ("telefone", "telefone")):
        if p.get(chave) in tem:
            where.append(f"{coluna} {tem[p[chave]]}")
    if p.get("reivindicado") in ("sim", "nao"):
        where.append("reivindicado = ?")
        args.append(1 if p["reivindicado"] == "sim" else 0)
    if v := p.get("busca"):
        where.append("id IN (SELECT lead_id FROM busca_leads WHERE busca_id=?)")
        args.append(int(v))
    score = score_sql()
    if v := p.get("score_min"):
        where.append(f"{score} >= ?")
        args.append(int(v))
    if tags := lista_param(p, "tags"):
        where.append("(" + " OR ".join("tags LIKE ?" for _ in tags) + ")")
        args += [f"%,{t},%" for t in tags]
    if p.get("duplicado") == "sim":
        where.append("digitos(telefone) IN (SELECT digitos(telefone) FROM leads WHERE telefone IS NOT NULL "
                     "GROUP BY digitos(telefone) HAVING COUNT(*) > 1)")
    if p.get("mudou") == "7d":
        where.append("id IN (SELECT lead_id FROM atividades WHERE tipo='mudanca' "
                     "AND criado >= date('now','localtime','-6 days'))")
    if p.get("zap_conferido") in ("sim", "nao", "pendente"):
        where.append({"sim": "whatsapp_ok = 1", "nao": "whatsapp_ok = 0",
                      "pendente": "whatsapp_ok IS NULL AND telefone IS NOT NULL"}[p["zap_conferido"]])
    if p.get("retorno") == "pendente":
        where.append("retorno IS NOT NULL AND retorno <= date('now', 'localtime')")
    elif p.get("retorno") == "agendado":
        where.append("retorno IS NOT NULL")
    if ids := lista_param(p, "ids"):
        where.append(f"id IN ({','.join('?' * len(ids))})")
        args += ids
    sql_where = " AND ".join(where)
    if (raio := p.get("raio_km")) and lat and lon:
        sql_where += " AND distancia <= ?"
        args.append(float(raio))
    ordem = ORDENS.get(p.get("ordem") or "", "distancia IS NULL, distancia" if lat else "criado DESC")
    return f"SELECT *, {dist} AS distancia, {score} AS score FROM leads WHERE {sql_where} ORDER BY {ordem}", args


@app.get("/api/leads")
def listar_leads(request: Request):
    p = dict(request.query_params)
    sql, args = filtrar(p)
    limite = min(int(p.get("limite") or 500), 5000)
    with conectar() as c:
        total = c.execute(f"SELECT COUNT(*) FROM ({sql})", args).fetchone()[0]
        rows = [dict(r) for r in c.execute(sql + " LIMIT ?", [*args, limite])]
        resumo = dict(c.execute("SELECT COUNT(*) AS total, SUM(whatsapp IS NOT NULL) AS whats, "
                                "SUM(site IS NULL) AS sem_site FROM leads").fetchone())
    return {"total": total, "leads": rows, "base": resumo}


@app.get("/api/leads.csv")
def exportar(request: Request):
    sql, args = filtrar(dict(request.query_params))
    cols = ["nome", "categoria", "termo", "score", "telefone", "whatsapp", "emails", "site", "instagram", "nota",
            "avaliacoes", "endereco", "bairro", "cidade", "uf", "cep", "distancia", "status", "retorno", "tags",
            "observacao", "link", "lat", "lon"]
    buf = io.StringIO()
    buf.write("﻿")  # Excel reconhece acentos
    w = csv.writer(buf, delimiter=";")
    w.writerow(cols)
    with conectar() as c:
        for r in c.execute(sql, args):
            linha = []
            for k in cols:
                v = r[k]
                if k == "tags" and v:
                    v = ", ".join(t for t in v.split(",") if t)
                if isinstance(v, float):
                    casas = {"distancia": 2, "lat": 6, "lon": 6}.get(k, 1)
                    v = f"{v:.{casas}f}".replace(".", ",")
                linha.append("" if v is None else v)
            w.writerow(linha)
    nome = f"leads-{datetime.now():%Y-%m-%d-%H%M}.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{nome}"'})


class Edicao(BaseModel):
    status: str | None = None
    observacao: str | None = None
    retorno: str | None = None   # "AAAA-MM-DD" marca, "" tira
    tags: list[str] | None = None


class Lote(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=5000)
    status: str | None = None
    tag: str | None = None       # acrescenta esta tag


def limpar_tag(t: str) -> str:
    return re.sub(r"\s+", " ", t.replace(",", " ")).strip().lower()[:40]


def tags_texto(tags: list[str]) -> str | None:
    lista = list(dict.fromkeys(t for t in (limpar_tag(x) for x in tags) if t))
    return ("," + ",".join(lista) + ",") if lista else None


class NovaAtividade(BaseModel):
    tipo: str = Field(pattern="^(nota|whatsapp|ligacao|email|visita)$")
    texto: str | None = None


NOMES_STATUS = {"novo": "Novo", "contatar": "Contatar", "contatado": "Contatado", "interessado": "Interessado",
                "cliente": "Cliente", "descartado": "Descartado", "nao_contatar": "Não contatar"}


def registrar(c, lead_id: str, tipo: str, texto: str | None):
    c.execute("INSERT INTO atividades (lead_id, tipo, texto, criado) VALUES (?,?,?,?)", (lead_id, tipo, texto, agora()))


def mudar_status(c, lid: str, novo: str):
    r = c.execute("SELECT status FROM leads WHERE id=?", (lid,)).fetchone()
    if r and r["status"] != novo:
        c.execute("UPDATE leads SET status=?, atualizado=? WHERE id=?", (novo, agora(), lid))
        registrar(c, lid, "status", f"{NOMES_STATUS.get(r['status'], r['status'])} → {NOMES_STATUS[novo]}")


def lead_completo(c, lid: str):
    r = c.execute(f"SELECT *, NULL AS distancia, {score_sql()} AS score FROM leads WHERE id=?", (lid,)).fetchone()
    if not r:
        raise HTTPException(404, "Lead não encontrado")
    return dict(r)


@app.post("/api/leads/lote")
def editar_lote(e: Lote):
    if e.status is not None and e.status not in STATUS_LEAD:
        raise HTTPException(400, "Status inválido")
    tag = limpar_tag(e.tag or "")
    with conectar() as c:
        for lid in e.ids:
            if e.status:
                mudar_status(c, lid, e.status)
            if tag:
                c.execute("UPDATE leads SET tags = COALESCE(tags, ',') || ? || ',' WHERE id=? "
                          "AND COALESCE(tags, '') NOT LIKE ?", (tag, lid, f"%,{tag},%"))
    return {"ok": True, "n": len(e.ids)}


@app.get("/api/leads/{lid}")
def ver_lead(lid: str):
    with conectar() as c:
        return lead_completo(c, lid)


@app.patch("/api/leads/{lid}")
def editar_lead(lid: str, e: Edicao):
    with conectar() as c:
        lead_completo(c, lid)
        if e.status is not None:
            if e.status not in STATUS_LEAD:
                raise HTTPException(400, "Status inválido")
            mudar_status(c, lid, e.status)
        if e.observacao is not None:
            c.execute("UPDATE leads SET observacao=?, atualizado=? WHERE id=?", (e.observacao, agora(), lid))
        if e.tags is not None:
            c.execute("UPDATE leads SET tags=?, atualizado=? WHERE id=?", (tags_texto(e.tags), agora(), lid))
        if e.retorno is not None:
            data = e.retorno.strip() or None
            if data and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", data):
                raise HTTPException(400, "Data de retorno inválida")
            c.execute("UPDATE leads SET retorno=?, atualizado=? WHERE id=?", (data, agora(), lid))
            registrar(c, lid, "retorno", f"Retorno marcado para {data[8:]}/{data[5:7]}/{data[:4]}" if data else "Retorno tirado")
        return lead_completo(c, lid)


@app.get("/api/leads/{lid}/atividades")
def atividades_do_lead(lid: str):
    with conectar() as c:
        return [dict(r) for r in c.execute("SELECT * FROM atividades WHERE lead_id=? ORDER BY id DESC LIMIT 200", (lid,))]


@app.post("/api/leads/{lid}/atividades")
def nova_atividade(lid: str, a: NovaAtividade):
    with conectar() as c:
        lead_completo(c, lid)
        registrar(c, lid, a.tipo, (a.texto or "").strip() or None)
    return {"ok": True}


@app.get("/api/painel")
def painel():
    """Números para a tela inicial."""
    SCORE_SQL = score_sql()
    with conectar() as c:
        um = lambda sql, *a: c.execute(sql, a).fetchone()[0] or 0  # noqa: E731
        ativos = "status NOT IN ('descartado','nao_contatar')"
        k = {
            "total": um("SELECT COUNT(*) FROM leads"),
            "whats": um("SELECT COUNT(*) FROM leads WHERE whatsapp IS NOT NULL"),
            "sem_site": um("SELECT COUNT(*) FROM leads WHERE site IS NULL"),
            "sem_dono": um("SELECT COUNT(*) FROM leads WHERE COALESCE(reivindicado,0)=0"),
            "quentes": um(f"SELECT COUNT(*) FROM leads WHERE {SCORE_SQL} >= 70 AND status IN ('novo','contatar')"),
            "trabalhados": um("SELECT COUNT(*) FROM leads WHERE status IN ('contatado','interessado','cliente')"),
            "clientes": um("SELECT COUNT(*) FROM leads WHERE status='cliente'"),
            "novos_7d": um("SELECT COUNT(*) FROM leads WHERE criado >= date('now','localtime','-6 days')"),
            "duplicados": um("SELECT COUNT(*) FROM leads WHERE digitos(telefone) IN (SELECT digitos(telefone) FROM leads "
                             "WHERE telefone IS NOT NULL GROUP BY 1 HAVING COUNT(*) > 1)"),
            "mudancas_7d": um("SELECT COUNT(DISTINCT lead_id) FROM atividades WHERE tipo='mudanca' "
                              "AND criado >= date('now','localtime','-6 days')"),
            "zap_pendente": um("SELECT COUNT(*) FROM leads WHERE whatsapp_ok IS NULL AND telefone IS NOT NULL"),
            "retornos_pendentes": um(f"SELECT COUNT(*) FROM leads WHERE retorno <= date('now','localtime') AND {ativos} "
                                     "AND status <> 'cliente'"),
        }
        status = {r[0]: r[1] for r in c.execute("SELECT status, COUNT(*) FROM leads GROUP BY status")}
        dias = {r[0]: r[1] for r in c.execute(
            "SELECT substr(criado,1,10), COUNT(*) FROM leads WHERE criado >= date('now','localtime','-29 days') GROUP BY 1")}
        hoje = datetime.now().date()
        por_dia = [{"dia": (d := (hoje - timedelta(days=29 - i)).isoformat()), "n": dias.get(d, 0)} for i in range(30)]
        categorias = [dict(r) for r in c.execute(
            f"SELECT categoria AS nome, COUNT(*) AS n, ROUND(AVG({SCORE_SQL})) AS score FROM leads "
            "WHERE categoria IS NOT NULL GROUP BY categoria ORDER BY n DESC LIMIT 8")]
        cidades = [dict(r) for r in c.execute(
            "SELECT cidade AS nome, uf, COUNT(*) AS n FROM leads WHERE cidade IS NOT NULL "
            "GROUP BY cidade, uf ORDER BY n DESC LIMIT 8")]
        faixas = [dict(r) for r in c.execute(
            f"SELECT MIN(({SCORE_SQL}) / 20, 4) AS faixa, COUNT(*) AS n FROM leads GROUP BY 1 ORDER BY 1")]
        retornos = [dict(r) for r in c.execute(
            f"SELECT id, nome, categoria, cidade, uf, retorno, status, whatsapp, telefone, {SCORE_SQL} AS score "
            f"FROM leads WHERE retorno IS NOT NULL AND {ativos} AND status <> 'cliente' ORDER BY retorno LIMIT 8")]
        quentes = [dict(r) for r in c.execute(
            f"SELECT id, nome, categoria, cidade, uf, status, whatsapp, {SCORE_SQL} AS score FROM leads "
            f"WHERE status IN ('novo','contatar') ORDER BY score DESC, avaliacoes DESC LIMIT 8")]
        recentes = [dict(r) for r in c.execute(
            "SELECT a.*, l.nome FROM atividades a JOIN leads l ON l.id=a.lead_id ORDER BY a.id DESC LIMIT 10")]
    return {"kpis": k, "funil": [{"id": s, "n": status.get(s, 0)} for s in STATUS_LEAD], "por_dia": por_dia,
            "categorias": categorias, "cidades": cidades, "faixas": faixas, "retornos": retornos,
            "quentes": quentes, "atividades": recentes}


@app.get("/api/opcoes")
def opcoes():
    """O que existe na base, com contagem, para montar os filtros."""
    with conectar() as c:
        cidades = [dict(r) for r in c.execute(
            "SELECT municipio_id AS id, cidade AS nome, uf, COUNT(*) AS n FROM leads "
            "WHERE municipio_id IS NOT NULL GROUP BY municipio_id ORDER BY n DESC")]
        categorias = [dict(r) for r in c.execute(
            "SELECT categoria AS nome, COUNT(*) AS n FROM leads WHERE categoria IS NOT NULL "
            "GROUP BY categoria ORDER BY n DESC")]
        status = {r[0]: r[1] for r in c.execute("SELECT status, COUNT(*) FROM leads GROUP BY status")}
        tags: dict[str, int] = {}
        for (t,) in c.execute("SELECT tags FROM leads WHERE tags IS NOT NULL"):
            for x in t.split(","):
                if x:
                    tags[x] = tags.get(x, 0) + 1
    return {"cidades": cidades, "categorias": categorias,
            "status": [{"id": s, "n": status.get(s, 0)} for s in STATUS_LEAD],
            "tags": [{"nome": k, "n": n} for k, n in sorted(tags.items(), key=lambda x: (-x[1], x[0]))]}


# ---------------------------------------------------------------- listas salvas

class NovaLista(BaseModel):
    nome: str = Field(min_length=1, max_length=80)
    params: str = Field(max_length=4000)  # query string dos filtros


@app.get("/api/listas")
def listar_listas():
    with conectar() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM listas ORDER BY nome COLLATE NOCASE")]
        for r in rows:
            p = dict(urllib.parse.parse_qsl(r["params"]))
            p.pop("ordem", None)
            sql, args = filtrar(p)
            r["n"] = c.execute(f"SELECT COUNT(*) FROM ({sql})", args).fetchone()[0]
    return rows


@app.post("/api/listas")
def criar_lista(l: NovaLista):
    with conectar() as c:
        cur = c.execute("INSERT INTO listas (nome, params, criada) VALUES (?,?,?)", (l.nome.strip(), l.params, agora()))
    return {"id": cur.lastrowid}


@app.delete("/api/listas/{lid}")
def apagar_lista(lid: int):
    with conectar() as c:
        c.execute("DELETE FROM listas WHERE id=?", (lid,))
    return {"ok": True}


# ---------------------------------------------------------------- buscas agendadas

class Agenda(BaseModel):
    dias: int = Field(ge=0, le=90)  # 0 desliga


@app.put("/api/buscas/{bid}/agenda")
def agendar_busca(bid: int, a: Agenda):
    with conectar() as c:
        if not c.execute("SELECT 1 FROM buscas WHERE id=?", (bid,)).fetchone():
            raise HTTPException(404, "Busca não encontrada")
        c.execute("DELETE FROM agendas WHERE busca_id=?", (bid,))
        if a.dias:
            proxima = (datetime.now() + timedelta(days=a.dias)).replace(hour=7, minute=0, second=0)
            c.execute("INSERT INTO agendas (busca_id, dias, proxima, criada) VALUES (?,?,?,?)",
                      (bid, a.dias, proxima.isoformat(timespec="seconds"), agora()))
    return {"ok": True}


def vigia_agendas():
    """Roda as buscas agendadas na hora marcada. A fila (FILA_LOCK) garante uma busca por vez."""
    while True:
        time.sleep(60)
        try:
            with conectar() as c:
                vencidas = [dict(r) for r in c.execute("SELECT * FROM agendas WHERE proxima <= ?", (agora(),))]
            for a in vencidas:
                nova = repetir_busca(a["busca_id"])["id"]
                proxima = datetime.fromisoformat(a["proxima"])
                while proxima <= datetime.now():
                    proxima += timedelta(days=a["dias"])
                with conectar() as c:
                    c.execute("UPDATE agendas SET proxima=?, ultima_busca=? WHERE busca_id=?",
                              (proxima.isoformat(timespec="seconds"), nova, a["busca_id"]))
        except Exception as e:  # noqa: BLE001
            print("agenda:", e)


threading.Thread(target=vigia_agendas, daemon=True).start()


# ---------------------------------------------------------------- configurações

class ConfigZapvu(BaseModel):
    url: str | None = None
    instancia: str | None = None
    chave: str | None = None   # None mantém; "" apaga


class NovaConfig(BaseModel):
    pesos: dict[str, int] | None = None
    zapvu: ConfigZapvu | None = None


def zapvu_cfg() -> dict:
    z = cfg("zapvu", {}) or {}
    z.setdefault("url", "https://api.zapvu.com.br/v1")
    return z


@app.get("/api/config")
def ver_config():
    pesos = pesos_score()
    z = zapvu_cfg()
    chave = z.get("chave") or ""
    return {
        "pesos": [{"chave": k, "texto": t, "peso": pesos[k], "padrao": p} for k, _, p, t in CRITERIOS_SCORE],
        "zapvu": {"url": z["url"], "instancia": z.get("instancia") or "",
                  "chave_fim": ("…" + chave[-4:]) if chave else "", "ativo": bool(chave and z.get("instancia"))},
    }


@app.put("/api/config")
def salvar_config(n: NovaConfig):
    if n.pesos is not None:
        validos = {k for k, *_ in CRITERIOS_SCORE}
        pesos = {k: max(0, min(100, int(v))) for k, v in n.pesos.items() if k in validos}
        if not sum(pesos.values()):
            raise HTTPException(400, "Pelo menos um critério precisa ter peso maior que zero.")
        set_cfg("pesos", pesos)
    if n.zapvu is not None:
        z = zapvu_cfg()
        if n.zapvu.url is not None:
            url = n.zapvu.url.strip().rstrip("/")
            if url and not re.match(r"^https?://", url):
                raise HTTPException(400, "O endereço da API precisa começar com https://")
            z["url"] = url or "https://api.zapvu.com.br/v1"
        if n.zapvu.instancia is not None:
            z["instancia"] = n.zapvu.instancia.strip()
        if n.zapvu.chave is not None:
            z["chave"] = n.zapvu.chave.strip()
        set_cfg("zapvu", z)
    return ver_config()


# ---------------------------------------------------------------- Zapvu (WhatsApp de verdade)

def zapvu(metodo: str, caminho: str, corpo: dict | None = None, idem: str | None = None):
    z = zapvu_cfg()
    if not (z.get("chave") and z.get("instancia")):
        raise HTTPException(400, "Configure o Zapvu em Configurações (instância e chave).")
    url = f"{z['url'].rstrip('/')}/instances/{urllib.parse.quote(z['instancia'])}{caminho}"
    cab = {"Authorization": f"Bearer {z['chave']}", "Content-Type": "application/json", "User-Agent": "base-leads/1.0"}
    if idem:
        cab["Idempotency-Key"] = idem
    req = urllib.request.Request(url, method=metodo, headers=cab,
                                 data=json.dumps(corpo).encode() if corpo is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode()).get("error") or e.reason
        except Exception:  # noqa: BLE001
            msg = e.reason
        if e.code in (401, 403):
            msg = "chave recusada. Confira a chave zv_ e se ela é desta instância."
        elif e.code == 404:
            msg = "instância não encontrada. Confira o ID."
        raise HTTPException(502, f"Zapvu: {msg}")
    except (urllib.error.URLError, TimeoutError) as e:
        raise HTTPException(502, f"Zapvu fora do ar ou endereço errado ({getattr(e, 'reason', e)}).")


@app.post("/api/zapvu/testar")
def testar_zapvu():
    j = zapvu("GET", "")
    return {"nome": j.get("name"), "telefone": j.get("phone"), "status": j.get("status"),
            "conectado": bool(j.get("connected"))}


class Envio(BaseModel):
    texto: str = Field(min_length=1, max_length=4000)
    modelo: str | None = None


@app.post("/api/leads/{lid}/enviar")
def enviar_whatsapp(lid: str, e: Envio):
    """Uma mensagem por vez, disparada pela pessoa. Sem envio em massa, para não arriscar o número."""
    with conectar() as c:
        l = lead_completo(c, lid)
    numero = l["whatsapp"] or ("55" + so_digitos(l["telefone"])[-11:] if l["telefone"] else None)
    if not numero:
        raise HTTPException(400, "Essa empresa não tem telefone.")
    r = zapvu("POST", "/messages/text", {"to": numero, "text": e.texto.strip()}, idem=f"base-leads-{uuid.uuid4()}")
    with conectar() as c:
        registrar(c, lid, "whatsapp", f"Enviado pelo Zapvu{f' (modelo {e.modelo})' if e.modelo else ''}: {e.texto.strip()[:300]}")
        if l["status"] in ("novo", "contatar"):
            mudar_status(c, lid, "contatado")
        return {"fila": r.get("id"), "lead": lead_completo(c, lid)}


class Conferir(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=50)


@app.post("/api/leads/conferir-whatsapp")
def conferir_whatsapp(cf: Conferir):
    """Pergunta ao WhatsApp (via Zapvu) quais números têm conta. Até 50 por vez; use com moderação."""
    with conectar() as c:
        marcas = ",".join("?" * len(cf.ids))
        leads = [dict(r) for r in c.execute(f"SELECT id, telefone FROM leads WHERE id IN ({marcas}) "
                                            "AND telefone IS NOT NULL", cf.ids)]
    por_num = {}
    for l in leads:
        d = so_digitos(l["telefone"])
        d = d[2:] if d.startswith("55") and len(d) > 11 else d
        if len(d) in (10, 11):
            por_num.setdefault("55" + d, []).append(l["id"])
    if not por_num:
        return {"conferidos": 0, "com_whatsapp": 0}
    res = zapvu("POST", "/contacts/check", {"phones": list(por_num)}).get("results", [])
    com = 0
    with conectar() as c:
        for r in res:
            num = so_digitos(r.get("query"))
            for lid in por_num.get(num, []):
                existe = bool(r.get("exists"))
                com += existe
                zap = so_digitos(r.get("jid", "").split("@")[0]) or num
                c.execute("UPDATE leads SET whatsapp=?, whatsapp_ok=?, atualizado=? WHERE id=?",
                          (zap if existe else None, int(existe), agora(), lid))
                registrar(c, lid, "conferido", "WhatsApp confirmado" if existe else "Número sem WhatsApp")
    return {"conferidos": len(res), "com_whatsapp": com}
