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
FOLGA_SALTO_H = 8            # tolerância (h) antes de considerar um salto impossível
RESET_PRECOCE_PCT = 60       # contador zerou com menos que isso do ciclo usado = suspeito
SEM_VARIACAO_DIAS = 14       # mesmo valor por mais que isso = aviso
ALERTA_DADOS_DIAS = 60       # só lista problemas de leitura dos últimos N dias
SEMANAS_ROTA = 8             # semanas exibidas no acompanhamento da rota

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
        if delta > 24 * dd + FOLGA_SALTO_H:
            # mais de 24 h/dia é impossível → leitura inconsistente, ignora o trecho
            continue
        if b["data"] >= corte:
            horas += delta
            dias += dd
    if dias >= TAXA_MIN_DIAS:
        taxa = min(horas / dias, 24.0)
    return taxa, ultima_prev


# ----------------------------------------------------------------------------
# QUALIDADE DOS DADOS — leituras suspeitas
# ----------------------------------------------------------------------------
def verificar_leituras(tag, leituras, regressivo, freq):
    """Marca cada leitura com um código de problema e devolve a lista de alertas."""
    alertas = []
    for l in leituras:
        l["flag"] = None

    def marca(l, codigo, grav, msg, ant=None):
        if l["flag"] is None or grav == "alta":
            l["flag"] = codigo
        alertas.append({
            "tag": tag, "data": l["data"].isoformat(), "codigo": codigo, "gravidade": grav,
            "msg": msg, "valor": inteiro(l["valor"]),
            "anterior": inteiro(ant["valor"]) if ant else None,
            "data_anterior": ant["data"].isoformat() if ant else None,
            "inspetor": l["inspetor"],
        })

    for a, b in zip(leituras, leituras[1:]):
        dd = (b["data"] - a["data"]).days
        if dd == 0:
            if a["valor"] != b["valor"]:
                marca(b, "DUPLICADA", "alta",
                      f"Duas leituras no mesmo dia com valores diferentes ({fmt(a['valor'])} e {fmt(b['valor'])} h)", a)
            continue
        delta = (a["valor"] - b["valor"]) if regressivo else (b["valor"] - a["valor"])
        if delta > 24 * dd + FOLGA_SALTO_H:
            marca(b, "SALTO", "alta",
                  f"Andou {fmt(delta)} h em {dd} dia(s) — o máximo possível é ~{fmt(24 * dd)} h", a)
        elif delta < 0 and freq:
            usado = (freq - a["valor"]) if regressivo else a["valor"]
            if usado / freq * 100 < RESET_PRECOCE_PCT:
                marca(b, "RESET_PRECOCE", "alta",
                      f"Contador reiniciou com só {round(usado / freq * 100)}% do ciclo usado — "
                      f"preventiva antecipada ou leitura na máquina errada?", a)
        elif delta == 0 and dd >= SEM_VARIACAO_DIAS:
            marca(b, "SEM_VARIACAO", "baixa",
                  f"Mesmo valor há {dd} dias — máquina parada ou valor repetido?", a)

    for l in leituras:
        # (total igual à frequência = campo preenchido com a frequência; ignora)
        if (not regressivo and l["total"] is not None and l["total"] < l["valor"]
                and l["total"] != freq):
            marca(l, "TOTAL_MENOR", "alta",
                  f"Horímetro total ({fmt(l['total'])} h) menor que o da preventiva ({fmt(l['valor'])} h) — campos trocados?")
    return alertas


def fmt(v):
    return f"{int(round(v)):,}".replace(",", ".")


def norm_nome(n):
    import unicodedata
    n = unicodedata.normalize("NFKD", (n or "").strip()).encode("ascii", "ignore").decode().lower()
    return " ".join(n.split())


# ----------------------------------------------------------------------------
# ROTA DE COLETA
# ----------------------------------------------------------------------------
def montar_rota(historico, tags, hoje):
    todas = [(tag, l) for tag, ls in historico.items() for l in ls]

    por_data = {}
    for tag, l in todas:
        d = por_data.setdefault(l["data"], {"tags": set(), "insp": set()})
        d["tags"].add(tag)
        if l["inspetor"]:
            d["insp"].add(l["inspetor"].strip())
    datas = [
        {"data": d.isoformat(), "maquinas": len(v["tags"]), "inspetores": sorted(v["insp"])}
        for d, v in sorted(por_data.items())
    ]

    inicio_sem = hoje - dt.timedelta(days=hoje.weekday())
    semanas = []
    for i in range(SEMANAS_ROTA - 1, -1, -1):
        ini = inicio_sem - dt.timedelta(weeks=i)
        fim = ini + dt.timedelta(days=6)
        lidas = {tag for tag, l in todas if ini <= l["data"] <= fim and tag in tags}
        semanas.append({
            "inicio": ini.isoformat(), "fim": fim.isoformat(),
            "lidas": len(lidas), "total": len(tags),
            "pct": round(len(lidas) / len(tags) * 100) if tags else 0,
            "faltaram": sorted(tags - lidas),
        })

    insp = {}
    for tag, l in todas:
        k = norm_nome(l["inspetor"]) or "(sem nome)"
        e = insp.setdefault(k, {"nome": (l["inspetor"] or "(sem nome)").strip(), "leituras": 0,
                                "ultima": l["data"], "dias": set()})
        e["leituras"] += 1
        e["dias"].add(l["data"])
        if l["data"] >= e["ultima"]:
            e["ultima"] = l["data"]
            e["nome"] = (l["inspetor"] or e["nome"]).strip()
    inspetores = sorted(
        ({"nome": e["nome"], "leituras": e["leituras"], "dias_coleta": len(e["dias"]),
          "ultima": e["ultima"].isoformat()} for e in insp.values()),
        key=lambda x: -x["leituras"])

    dts = sorted(por_data)
    intervalos = [(b - a).days for a, b in zip(dts, dts[1:])]
    return {
        "datas": datas,
        "semanas": semanas,
        "inspetores": inspetores,
        "nunca_coletadas": sorted(t for t in tags if t not in historico),
        "intervalo_medio_dias": round(sum(intervalos) / len(intervalos), 1) if intervalos else None,
        "ultima_coleta": dts[-1].isoformat() if dts else None,
    }


def processar(itens_status, itens_hist, hoje):
    historico = montar_historico(itens_hist)
    maquinas = []
    alertas = []

    for it in itens_status:
        c = it["cols"]
        tag = (c.get(C_TAG) or "").strip().upper()
        freq = num(c.get(C_FREQ)) or 0
        tipo = c.get(C_TIPO) or "Crescente"
        regressivo = tipo.lower().startswith("regress")
        leituras = historico.get(tag, [])
        alertas += verificar_leituras(tag, leituras, regressivo, freq)

        # Horímetro atual: o valor do board de status, a menos que exista uma coleta
        # mais recente no histórico (a automação do Monday nem sempre atualiza o status).
        prev = num(c.get(C_PREV))
        data_status = data(c.get(C_DATA))
        fonte_valor = "status"
        ult = leituras[-1] if leituras else None
        if ult and (prev is None or data_status is None or ult["data"] > data_status):
            if prev is None or data_status is None:
                # sem data no status: só troca se a coleta for recente
                if prev is None or (hoje - ult["data"]).days <= COLETA_VENCIDA_DIAS:
                    prev, fonte_valor = ult["valor"], "coleta"
            else:
                prev, fonte_valor = ult["valor"], "coleta"
        tem_leitura = prev is not None
        valor = prev or 0

        # mesma fórmula do Monday
        restantes = valor if regressivo else freq - valor
        status = classificar(restantes, tem_leitura)
        consumido = (freq - valor) if regressivo else valor
        pct = max(0.0, consumido / freq * 100) if freq else 0.0

        taxa, ultima_prev = analisar_historico(leituras, regressivo, hoje)

        datas = [d for d in [data_status] + [l["data"] for l in leituras] if d]
        ultima_coleta = max(datas) if datas else None
        dias_sem_coleta = (hoje - ultima_coleta).days if ultima_coleta else None

        previsao = dias_prev = None
        if tem_leitura and restantes > 0 and taxa and taxa > 0:
            dias_prev = restantes / taxa
            previsao = hoje + dt.timedelta(days=round(dias_prev))

        obs = next((l["obs"] for l in reversed(leituras) if l["obs"]), None)
        suspeita_atual = bool(fonte_valor == "coleta" and ult and ult["flag"]
                              and ult["flag"] != "SEM_VARIACAO")

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
                "horimetro_fonte": fonte_valor,
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
                "leitura_suspeita": suspeita_atual,
                # [data, horímetro prev., horímetro total, inspetor, código do problema]
                "historico": [[l["data"].isoformat(), inteiro(l["valor"]), inteiro(l["total"]),
                               l["inspetor"], l["flag"]] for l in leituras],
            }
        )

    ordem = {"ATRASADO": 0, "PENDENTE": 1, "EM_DIA": 2, "SEM_LEITURA": 3}
    maquinas.sort(key=lambda m: (ordem[m["status"]], m["restantes"] if m["restantes"] is not None else 1e9, m["tag"]))

    resumo = {k.lower(): sum(1 for m in maquinas if m["status"] == k) for k in ordem}
    resumo["total"] = len(maquinas)
    resumo["coleta_vencida"] = sum(1 for m in maquinas if m["coleta_vencida"])

    corte = hoje - dt.timedelta(days=ALERTA_DADOS_DIAS)
    alertas = [a for a in alertas if dt.date.fromisoformat(a["data"]) >= corte]
    alertas.sort(key=lambda a: (a["gravidade"] != "alta", -dt.date.fromisoformat(a["data"]).toordinal(), a["tag"]))
    resumo["leituras_suspeitas"] = sum(1 for a in alertas if a["gravidade"] == "alta")

    tags = {m["tag"] for m in maquinas}
    return {
        "parametros": {
            "limite_pendente_h": LIMITE_PENDENTE_H,
            "coleta_vencida_dias": COLETA_VENCIDA_DIAS,
            "janela_taxa_dias": JANELA_TAXA_DIAS,
            "alerta_dados_dias": ALERTA_DADOS_DIAS,
        },
        "fonte": {
            "board_status": BOARD_STATUS,
            "board_historico": BOARD_HIST,
            "leituras_historico": sum(len(v) for v in historico.values()),
        },
        "resumo": resumo,
        "maquinas": maquinas,
        "alertas_dados": alertas,
        "rota": montar_rota(historico, tags, hoje),
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
