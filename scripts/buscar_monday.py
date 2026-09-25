"""
Busca os dados de horímetro das codificadoras no Monday.com e gera data/dados.json
para o painel HTML (GitHub Pages).

Boards usados:
  - 📋 Controle de Horímetro Codificadoras  (status atual de cada máquina)
  - 📋 Registro de Coleta de Horímetros      (histórico de leituras)

Uso:
  MONDAY_API_TOKEN=xxxx python scripts/buscar_monday.py
  python scripts/buscar_monday.py --snapshot caminho/raw.json   (teste offline)
"""

import datetime as dt
import json
import os
import sys
import urllib.request
from pathlib import Path

# ----------------------------------------------------------------------------
# CONFIGURAÇÃO
# ----------------------------------------------------------------------------
API_URL = "https://api.monday.com/v2"
BOARD_STATUS = 18420324650   # Controle de Horímetro Codificadoras
BOARD_HIST = 18420370319     # Registro de Coleta de Horímetros

LIMITE_PENDENTE_H = 200      # mesma regra do Monday: <=200 h restantes = PENDENTE
COLETA_VENCIDA_DIAS = 14     # sem leitura há mais que isso = coleta vencida
JANELA_TAXA_DIAS = 90        # janela usada para calcular a média de uso (h/dia)
TAXA_MIN_DIAS = 7            # mínimo de dias de histórico para confiar na taxa

# IDs das colunas — board de status
C_EQUIP = "text_mm4wzj7b"
C_SERIE = "text_mm4w81xk"
C_TAG = "text_mm4w6va"
C_FREQ = "numeric_mm4w3rfd"
C_TIPO = "color_mm4wc4wg"
C_PREV = "numeric_mm4wgaw9"
C_TOTAL = "numeric_mm4wr9en"
C_DATA = "date_mm4w5qgr"

# IDs das colunas — board de histórico
H_MAQ = "dropdown_mm4w31hw"
H_DATA = "date4"
H_PREV = "numeric_mm4w4nmb"
H_TOTAL = "numeric_mm4wjjd3"
H_INSP = "text_mm4wjf9s"
H_OBS = "text_mm4weyyq"

RAIZ = Path(__file__).resolve().parent.parent
SAIDA = RAIZ / "data" / "dados.json"
FUSO_BR = dt.timezone(dt.timedelta(hours=-3))


# ----------------------------------------------------------------------------
# MONDAY API
# ----------------------------------------------------------------------------
def gql(query, variables, token):
    corpo = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        API_URL,
        data=corpo,
        headers={
            "Authorization": token,
            "Content-Type": "application/json",
            "API-Version": "2024-10",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.loads(r.read().decode())
    if resp.get("errors"):
        raise RuntimeError(f"Erro da API do Monday: {resp['errors']}")
    return resp["data"]


Q_PRIMEIRA = """
query ($board: [ID!]) {
  boards(ids: $board) {
    items_page(limit: 500) {
      cursor
      items { id name column_values { id text } }
    }
  }
}"""

Q_PROXIMA = """
query ($cursor: String!) {
  next_items_page(limit: 500, cursor: $cursor) {
    cursor
    items { id name column_values { id text } }
  }
}"""


def buscar_itens(board_id, token):
    """Retorna lista de {id, name, cols: {col_id: texto}} com paginação."""
    dados = gql(Q_PRIMEIRA, {"board": [str(board_id)]}, token)
    pagina = dados["boards"][0]["items_page"]
    itens = list(pagina["items"])
    cursor = pagina["cursor"]
    while cursor:
        pagina = gql(Q_PROXIMA, {"cursor": cursor}, token)["next_items_page"]
        itens.extend(pagina["items"])
        cursor = pagina["cursor"]
    return [
        {
            "id": i["id"],
            "name": i["name"],
            "cols": {c["id"]: (c["text"] or None) for c in i["column_values"]},
        }
        for i in itens
    ]


# ----------------------------------------------------------------------------
# UTILITÁRIOS
# ----------------------------------------------------------------------------
def num(v):
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", "."))
    except ValueError:
        return None


def data(v):
    if not v:
        return None
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def inteiro(v):
    return None if v is None else int(round(v))


def familia(equip):
    e = (equip or "").upper()
    if "HITACHI" in e:
        return "Hitachi"
    if "VIDEOJET" in e:
        return "Videojet"
    if "CODELINE" in e:
        return "Codeline"
    return "Outros"


def classificar(restantes, tem_leitura):
    if not tem_leitura:
        return "SEM_LEITURA"
    if restantes <= 0:
        return "ATRASADO"
    if restantes <= LIMITE_PENDENTE_H:
        return "PENDENTE"
    return "EM_DIA"


# ----------------------------------------------------------------------------
# PROCESSAMENTO
# ----------------------------------------------------------------------------
def montar_historico(itens_hist):
    """Agrupa as leituras por TAG (primeira palavra do dropdown 'Maquina')."""
    por_tag = {}
    for it in itens_hist:
        c = it["cols"]
        maq = (c.get(H_MAQ) or "").strip()
        d = data(c.get(H_DATA))
        v = num(c.get(H_PREV))
        if not maq or d is None or v is None:
            continue
        tag = maq.split()[0].upper()
        por_tag.setdefault(tag, []).append(
            {
                "data": d,
                "valor": v,
                "total": num(c.get(H_TOTAL)),
                "inspetor": c.get(H_INSP),
                "obs": c.get(H_OBS),
            }
        )
    for lst in por_tag.values():
        lst.sort(key=lambda x: x["data"])
    return por_tag


def analisar_historico(leituras, regressivo, hoje):
    """Calcula taxa média de uso (h/dia) e detecta a última preventiva (reset)."""
    taxa = None
    ultima_prev = None
    horas = dias = 0.0
    corte = hoje - dt.timedelta(days=JANELA_TAXA_DIAS)
    for a, b in zip(leituras, leituras[1:]):
        dd = (b["data"] - a["data"]).days
        if dd <= 0:
            continue
        delta = (a["valor"] - b["valor"]) if regressivo else (b["valor"] - a["valor"])
        if delta < 0:
            # contador voltou → preventiva feita entre a e b
            ultima_prev = b["data"]
            continue
        if delta / dd > 24:
            # mais de 24 h/dia é impossível → leitura inconsistente, ignora o trecho
            continue
        if b["data"] >= corte:
            horas += delta
            dias += dd
    if dias >= TAXA_MIN_DIAS:
        taxa = min(horas / dias, 24.0)
    return taxa, ultima_prev


def processar(itens_status, itens_hist, hoje):
    historico = montar_historico(itens_hist)
    maquinas = []

    for it in itens_status:
        c = it["cols"]
        tag = (c.get(C_TAG) or "").strip().upper()
        freq = num(c.get(C_FREQ)) or 0
        tipo = c.get(C_TIPO) or "Crescente"
        regressivo = tipo.lower().startswith("regress")
        prev = num(c.get(C_PREV))
        tem_leitura = prev is not None
        valor = prev or 0

        # mesma fórmula do Monday
        restantes = valor if regressivo else freq - valor
        status = classificar(restantes, tem_leitura)
        consumido = (freq - valor) if regressivo else valor
        pct = max(0.0, consumido / freq * 100) if freq else 0.0

        leituras = historico.get(tag, [])
        taxa, ultima_prev = analisar_historico(leituras, regressivo, hoje)

        datas = [d for d in [data(c.get(C_DATA))] + [l["data"] for l in leituras] if d]
        ultima_coleta = max(datas) if datas else None
        dias_sem_coleta = (hoje - ultima_coleta).days if ultima_coleta else None

        previsao = dias_prev = None
        if tem_leitura and restantes > 0 and taxa and taxa > 0:
            dias_prev = restantes / taxa
            previsao = hoje + dt.timedelta(days=round(dias_prev))

        obs = next((l["obs"] for l in reversed(leituras) if l["obs"]), None)

        maquinas.append(
            {
                "id": it["id"],
                "tag": tag,
                "local": it["name"],
                "equip": c.get(C_EQUIP),
                "familia": familia(c.get(C_EQUIP)),
                "serie": c.get(C_SERIE),
                "tipo": "Regressivo" if regressivo else "Crescente",
                "freq": inteiro(freq),
                "horimetro": inteiro(prev),
                "horimetro_total": inteiro(num(c.get(C_TOTAL))),
                "restantes": inteiro(restantes) if tem_leitura else None,
                "pct_consumido": round(pct, 1) if tem_leitura else None,
                "status": status,
                "dias_24h": inteiro(restantes / 24) if tem_leitura and restantes > 0 else None,
                "taxa_h_dia": round(taxa, 1) if taxa is not None else None,
                "dias_previstos": inteiro(dias_prev),
                "previsao": previsao.isoformat() if previsao else None,
                "ultima_coleta": ultima_coleta.isoformat() if ultima_coleta else None,
                "dias_sem_coleta": dias_sem_coleta,
                "coleta_vencida": dias_sem_coleta is None or dias_sem_coleta > COLETA_VENCIDA_DIAS,
                "ultima_preventiva": ultima_prev.isoformat() if ultima_prev else None,
                "obs_recente": obs,
                "historico": [[l["data"].isoformat(), inteiro(l["valor"])] for l in leituras],
            }
        )

    ordem = {"ATRASADO": 0, "PENDENTE": 1, "EM_DIA": 2, "SEM_LEITURA": 3}
    maquinas.sort(key=lambda m: (ordem[m["status"]], m["restantes"] if m["restantes"] is not None else 1e9, m["tag"]))

    resumo = {k.lower(): sum(1 for m in maquinas if m["status"] == k) for k in ordem}
    resumo["total"] = len(maquinas)
    resumo["coleta_vencida"] = sum(1 for m in maquinas if m["coleta_vencida"])

    return {
        "parametros": {
            "limite_pendente_h": LIMITE_PENDENTE_H,
            "coleta_vencida_dias": COLETA_VENCIDA_DIAS,
            "janela_taxa_dias": JANELA_TAXA_DIAS,
        },
        "fonte": {
            "board_status": BOARD_STATUS,
            "board_historico": BOARD_HIST,
            "leituras_historico": sum(len(v) for v in historico.values()),
        },
        "resumo": resumo,
        "maquinas": maquinas,
    }


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------
def main():
    hoje = dt.datetime.now(FUSO_BR).date()

    if "--snapshot" in sys.argv:
        raw = json.loads(Path(sys.argv[sys.argv.index("--snapshot") + 1]).read_text(encoding="utf-8"))
        itens_status, itens_hist = raw["status"], raw["historico"]
        if raw.get("hoje"):
            hoje = dt.date.fromisoformat(raw["hoje"])
    else:
        token = os.environ.get("MONDAY_API_TOKEN")
        if not token:
            sys.exit("ERRO: defina o secret MONDAY_API_TOKEN.")
        itens_status = buscar_itens(BOARD_STATUS, token)
        itens_hist = buscar_itens(BOARD_HIST, token)

    novo = processar(itens_status, itens_hist, hoje)

    # Só regrava se algo mudou (evita commit a cada execução do Actions)
    if SAIDA.exists():
        antigo = json.loads(SAIDA.read_text(encoding="utf-8"))
        antigo.pop("atualizado_em", None)
        if antigo == json.loads(json.dumps(novo)):
            print("Sem alterações nos dados.")
            return

    novo = {"atualizado_em": dt.datetime.now(FUSO_BR).isoformat(timespec="minutes"), **novo}
    SAIDA.parent.mkdir(parents=True, exist_ok=True)
    SAIDA.write_text(json.dumps(novo, ensure_ascii=False, indent=1), encoding="utf-8")
    r = novo["resumo"]
    print(f"OK: {r['total']} máquinas | atrasado {r['atrasado']} | pendente {r['pendente']} | "
          f"em dia {r['em_dia']} | sem leitura {r['sem_leitura']}")


if __name__ == "__main__":
    main()
