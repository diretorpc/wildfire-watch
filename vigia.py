"""Vigia-fogo M1 — vigia mínimo de queimadas via satélite.

A cada 10 minutos baixa o CSV de focos de calor do INPE (satélite GOES-19),
confere se algum foco caiu no retângulo de vigilância (divisa + anel de 5 km)
de alguma fazenda de config/fazendas.json e, se caiu, manda UM e-mail por
ciclo com fazenda(s), distância aproximada da divisa e link do Google Maps.

Uso:
  python vigia.py                # fica vigiando (ciclo de 10 min)
  python vigia.py --uma-vez      # roda 1 ciclo e sai
  python vigia.py --teste-email  # manda e-mail de teste e sai

Regras herdadas do dossiê (docs/contexto-projeto.md):
- valores do CSV vêm com espaço à esquerda → trim antes de converter;
- a coluna `data` vem SEM hora (verificado 19/07/2026) → hora sai do nome do arquivo;
- horários dos arquivos em UTC; atraso real do dado ~20 min;
- pixel do GOES ~2 km → mensagem diz "~X km", nunca finge precisão;
- anti-spam mínimo: 1 e-mail por fazenda por 60 min (dedupe de verdade é M3);
- "de pé" ≠ "enxergando": todo ciclo devolve 'ok' ou 'cego', e o resumo diário
  nunca diz "tudo em ordem" sem ter conseguido dado (auditoria Gaia, 21/07/2026 —
  docs/analise-gaia-pre-seca-2026.md).
"""

import argparse
import json
import math
import os
import re
import smtplib
import ssl
import sys
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from http.client import HTTPException
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

RAIZ = Path(__file__).resolve().parent
CONFIG = RAIZ / "config" / "fazendas.json"
ARQUIVO_ENV = RAIZ / ".env"
ESTADO = RAIZ / "dados" / "estado-vigia.json"
PAINEL_ESTADO = RAIZ / "dados" / "painel-estado.json"
LOCK = RAIZ / "dados" / "vigia.lock"

CICLO_SEGUNDOS = 600
COOLDOWN_MINUTOS = 60
# Se o robô ficar parado por horas, NÃO reprocessa o dia inteiro: vigia só a
# última ~1h de arquivos (6 x 10 min). Foco de horas atrás não é alerta, é história.
MAX_ARQUIVOS_POR_CICLO = 6
TIMEOUT_HTTP = 30
# Brasília não tem mais horário de verão desde 2019 — offset fixo -3 é seguro.
FUSO_BRASILIA = timezone(timedelta(hours=-3))
KM_POR_GRAU_LAT = 110.574

# Se o robô não "bate o ponto" por mais que isto, a tela avisa que ele parou.
HEARTBEAT_LIMITE_MIN = 25
# "Vivo" não é "enxergando": o robô pode rodar liso e não conseguir dado nenhum do
# INPE. Sem contar isso, o resumo diário diria "tudo em ordem" sem ter olhado —
# falso conforto é o pior modo de falha num sistema de vigilância.
CICLOS_CEGO_PARA_ALERTAR = 6   # 6 x 10 min = ~1h cego antes de incomodar
COOLDOWN_CEGO_HORAS = 6        # cego o dia todo = ~2-3 e-mails, não um por ciclo
# O INPE publica a cada 10 min com ~20 min de atraso, então o arquivo mais novo
# fica normalmente ~30 min atrás. Além disto, o servidor parou de publicar.
DADO_VELHO_LIMITE_MIN = 60
# Gravidade por proximidade (maior número = mais grave). Guia e-mail e cor da tela.
ORDEM_GRAVIDADE = {None: 0, "atencao": 1, "urgente": 2, "dentro": 3}
CARDEAIS = ["Norte", "Nordeste", "Leste", "Sudeste", "Sul", "Sudoeste", "Oeste", "Noroeste"]
# Um e-mail que diz "corra" pode mandar alguém sozinho, de moto, contra uma frente
# de fogo com vento — e fogo em capim mata gente todo ano. O aviso anda GRUDADO no
# pedido de urgência: quem manda correr é quem tem que dizer como não morrer.
AVISO_SEGURANCA = [
    "⚠️ SEGURANÇA — antes de sair:",
    "  • Nunca vá sozinho, e avise alguém para onde você está indo.",
    "  • Nunca ataque a frente do fogo com vento — combata pelas laterais.",
    "  • Tenha sempre rota de fuga (estrada, aceiro, área já queimada).",
    "  • Fogo em capim seco anda mais rápido que gente correndo. Na dúvida, recue.",
    "",
]
# Focos ligados por até esta distância (≤) são o MESMO incêndio (~pixel do GOES).
RAIO_AGRUPAMENTO_KM = 2.0

CHAVES_ENV_OBRIGATORIAS = [
    "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD",
    "ALERTA_EMAIL_PARA", "INPE_10MIN_URL",
]
PADRAO_NOME_CSV = re.compile(r"focos_10min_\d{8}_\d{4}\.csv")
# HTTPException cobre resposta cortada no meio (IncompleteRead etc.), que não
# herda de OSError; ValueError cobre URL malformada no .env.
ERROS_REDE = (URLError, HTTPException, OSError, TimeoutError, ValueError)


def log(msg: str) -> None:
    agora = datetime.now(FUSO_BRASILIA).strftime("%d/%m %H:%M:%S")
    print(f"[{agora}] {msg}", flush=True)


# ---------------------------------------------------------------- .env


def carregar_env(caminho: Path) -> dict:
    """Lê KEY=VALOR linha a linha. Ignora comentários e linhas vazias.

    Tolera o Bloco de Notas (BOM no início do arquivo) e valor entre aspas.
    """
    env = {}
    if not caminho.exists():
        return env
    for linha in caminho.read_text(encoding="utf-8-sig").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        valor = valor.strip()
        if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in "\"'":
            valor = valor[1:-1]
        env[chave.strip()] = valor
    return env


def chaves_faltando(env: dict) -> list:
    """Faltando = ausente, vazia ou ainda com o texto de exemplo 'preencher-...'."""
    faltando = []
    for chave in CHAVES_ENV_OBRIGATORIAS:
        valor = env.get(chave, "")
        if not valor or valor.startswith("preencher"):
            faltando.append(chave)
    return faltando


def problemas_de_partida(env: dict, fazendas: list) -> list:
    """Valida .env e config das fazendas ANTES de começar a vigiar.

    Melhor abortar agora com mensagem clara do que explodir horas depois,
    no meio do ciclo em que um fogo aparecer.
    """
    problemas = [f"falta preencher {c} no .env" for c in chaves_faltando(env)]
    if env.get("SMTP_PORT", "").strip() and not env["SMTP_PORT"].strip().isdigit():
        problemas.append(f"SMTP_PORT no .env deve ser só número (veio '{env['SMTP_PORT']}')")
    for fazenda in fazendas:
        nome = fazenda.get("nome") or "(fazenda sem nome no config)"
        if not fazenda.get("nome"):
            problemas.append("fazenda sem campo 'nome' em config/fazendas.json")
        if not isinstance(fazenda.get("bbox_fazenda"), dict):
            problemas.append(f"'{nome}' sem 'bbox_fazenda' válido no config")
        try:
            bbox_vigilancia(fazenda)
        except KeyError:
            problemas.append(f"'{nome}' sem retângulo de vigilância (bbox_vigilancia_anel_*) no config")
    return problemas


# ---------------------------------------------------------------- INPE


def listar_csvs(url_base: str) -> list:
    """Lista os nomes de arquivo CSV disponíveis no índice do INPE, ordenados."""
    if not url_base.endswith("/"):
        url_base += "/"
    with urlopen(url_base, timeout=TIMEOUT_HTTP) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    return sorted(set(PADRAO_NOME_CSV.findall(html)))


def baixar_csv(url_base: str, nome: str) -> str:
    if not url_base.endswith("/"):
        url_base += "/"
    with urlopen(url_base + nome, timeout=TIMEOUT_HTTP) as resp:
        return resp.read().decode("utf-8", errors="replace")


def selecionar_novos(disponiveis: list, ultimo_csv: str) -> tuple:
    """Escolhe quais arquivos processar. Retorna (novos, atrasados_descartados).

    Primeira execução: só o mais recente (não varre o dia inteiro).
    Demais: só os mais novos que o último processado, limitados à última ~1h —
    depois de um desligamento longo, foco velho não vira alerta falso de "agora".
    """
    novos = sorted(n for n in disponiveis if n > ultimo_csv)
    if not ultimo_csv:
        return novos[-1:], 0
    descartados = max(0, len(novos) - MAX_ARQUIVOS_POR_CICLO)
    return novos[-MAX_ARQUIVOS_POR_CICLO:], descartados


def hora_do_nome_csv(nome: str) -> str:
    """Extrai a hora UTC do nome do arquivo (focos_10min_YYYYMMDD_HHMM.csv).

    Necessário porque a coluna `data` do CSV real vem sem hora (só '2026-07-20')
    — verificado ao vivo em 19/07/2026.
    """
    m = re.match(r"focos_10min_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})\.csv$", nome)
    if not m:
        return ""
    a, mes, d, h, minuto = m.groups()
    return f"{a}-{mes}-{d} {h}:{minuto}:00"


def dado_esta_velho(nome_csv: str, agora: datetime, limite_min: int = DADO_VELHO_LIMITE_MIN) -> bool:
    """True se o arquivo mais novo do INPE já está velho demais para servir de vigia.

    Cobre o caso traiçoeiro: o servidor RESPONDE, mas parou de publicar. Sem isto o
    robô diria "olhei, tudo em ordem" olhando para dado de horas atrás.
    Nome fora do padrão não acusa cegueira — não dá para julgar o que não se lê.
    """
    hora = hora_do_nome_csv(nome_csv)
    if not hora:
        return False
    try:  # nome com data impossível (mês 13) não pode derrubar o ciclo inteiro
        quando = datetime.strptime(hora, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return (agora - quando) > timedelta(minutes=limite_min)


def aplicar_hora_do_arquivo(focos: list, nome_csv: str) -> None:
    """Completa a hora dos focos cuja coluna `data` veio só com a data."""
    hora = hora_do_nome_csv(nome_csv)
    if not hora:
        return
    for foco in focos:
        if len(foco["data"]) <= 10:
            foco["data"] = hora


def parse_focos(texto_csv: str) -> tuple:
    """Extrai focos (lat, lon, satelite, data) do CSV. Retorna (focos, puladas).

    Tolerante: faz trim em cada campo (valores vêm com espaço à esquerda),
    pula cabeçalho e qualquer linha que não tenha lat/lon numéricos.
    """
    focos, puladas = [], 0
    for linha in texto_csv.splitlines():
        partes = [p.strip() for p in linha.split(",")]
        if partes and partes[0].lower() == "lat":
            continue  # cabeçalho (mesmo com espaço à esquerda)
        if len(partes) < 4:
            if linha.strip():
                puladas += 1
            continue
        try:
            lat, lon = float(partes[0]), float(partes[1])
        except ValueError:
            puladas += 1
            continue
        focos.append({"lat": lat, "lon": lon, "satelite": partes[2], "data": partes[3]})
    return focos, puladas


# ---------------------------------------------------------------- geometria


def dentro_bbox(lat: float, lon: float, bbox: dict) -> bool:
    return bbox["oeste"] <= lon <= bbox["leste"] and bbox["sul"] <= lat <= bbox["norte"]


def distancia_km_ate_bbox(lat: float, lon: float, bbox: dict) -> float:
    """Distância aproximada (km) do ponto até o retângulo. 0 se estiver dentro.

    Fórmula plana — suficiente para os ~10 km da vigilância (mesma abordagem
    do tools/cadastrar_fazenda.py).
    """
    km_por_grau_lon = 111.320 * math.cos(math.radians(lat))
    dlon = max(bbox["oeste"] - lon, 0.0, lon - bbox["leste"]) * km_por_grau_lon
    dlat = max(bbox["sul"] - lat, 0.0, lat - bbox["norte"]) * KM_POR_GRAU_LAT
    return math.hypot(dlon, dlat)


def aneis_vigilancia(fazenda: dict) -> list:
    """Anéis da fazenda como [(km, bbox), ...] ordenados do interno ao externo."""
    aneis = []
    for chave, valor in fazenda.items():
        m = re.match(r"bbox_vigilancia_anel_(\d+)km$", chave)
        if m:
            aneis.append((int(m.group(1)), valor))
    if not aneis:
        raise KeyError(f"fazenda '{fazenda.get('nome')}' sem anel de vigilância no config")
    aneis.sort(key=lambda a: a[0])
    return aneis


def bbox_vigilancia(fazenda: dict) -> dict:
    """Maior anel (externo) — o limite geral do que está sendo vigiado."""
    return aneis_vigilancia(fazenda)[-1][1]


def distancia_km_pontos(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distância aproximada (km) entre dois pontos (fórmula plana, como o resto)."""
    km_lon = 111.320 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot((lon1 - lon2) * km_lon, (lat1 - lat2) * KM_POR_GRAU_LAT)


def rumo_cardeal(centro_lat: float, centro_lon: float, foco_lat: float, foco_lon: float) -> str:
    """Palavra do rumo do foco visto do centro da fazenda (Norte, Nordeste, ...)."""
    dlon = (foco_lon - centro_lon) * math.cos(math.radians(centro_lat))
    dlat = foco_lat - centro_lat
    if abs(dlat) < 1e-9 and abs(dlon) < 1e-9:
        return "no centro"
    ang = math.degrees(math.atan2(dlon, dlat)) % 360  # 0 = Norte, 90 = Leste
    return CARDEAIS[int((ang + 22.5) // 45) % 8]


def classificar_foco(foco: dict, fazenda: dict, aneis: list = None):
    """Gravidade do foco p/ esta fazenda: 'dentro' | 'urgente' | 'atencao' | None.

    Anéis são retângulos concêntricos (o de 5 km cabe dentro do de 10 km), então
    interno = urgente e externo = atenção classifica qualquer faixa corretamente.
    `aneis` pode vir pronto (evita recalcular por foco).
    """
    lat, lon = foco["lat"], foco["lon"]
    if dentro_bbox(lat, lon, fazenda["bbox_fazenda"]):
        return "dentro"
    if aneis is None:
        aneis = aneis_vigilancia(fazenda)
    if dentro_bbox(lat, lon, aneis[0][1]):   # anel interno (5 km) = urgente
        return "urgente"
    if dentro_bbox(lat, lon, aneis[-1][1]):  # anel externo (10 km) = atenção
        return "atencao"
    return None


def pior_gravidade(gravidades) -> str:
    """A mais grave de uma lista de gravidades."""
    return max(gravidades, key=lambda g: ORDEM_GRAVIDADE[g])


def verificar_focos(focos: list, fazendas: list) -> dict:
    """Retorna {nome: [{'foco','gravidade','dist_km','rumo'}, ...]}."""
    atingidas = {}
    for fazenda in fazendas:
        aneis = aneis_vigilancia(fazenda)  # calcula 1x por fazenda, não por foco
        clat, clon = fazenda["centro"]["lat"], fazenda["centro"]["lon"]
        for foco in focos:
            grav = classificar_foco(foco, fazenda, aneis)
            if grav is None:
                continue
            atingidas.setdefault(fazenda["nome"], []).append({
                "foco": foco,
                "gravidade": grav,
                "dist_km": distancia_km_ate_bbox(foco["lat"], foco["lon"], fazenda["bbox_fazenda"]),
                "rumo": rumo_cardeal(clat, clon, foco["lat"], foco["lon"]),
            })
    return atingidas


def agrupar_hits(hits: list, raio_km: float = RAIO_AGRUPAMENTO_KM) -> list:
    """Junta focos do mesmo incêndio num representante por grupo.

    "Componentes conectados" (single-linkage): dois focos a ≤ raio se ligam, e o
    grupo é todo o conjunto ligado — resultado NÃO depende da ordem de chegada,
    e nenhum par dentro do grupo fica além do raio por transitividade indireta.
    Representante = pior gravidade (empate: mais perto da divisa; depois lat/lon,
    para ser determinístico), com `n_focos` = quantas detecções foram agrupadas.
    """
    def perto(a, b):
        return distancia_km_pontos(a["lat"], a["lon"], b["lat"], b["lon"]) <= raio_km

    grupos = []
    for h in hits:
        f = h["foco"]
        vizinhos = [g for g in grupos if any(perto(f, m["foco"]) for m in g)]
        if not vizinhos:
            grupos.append([h])
        else:  # funde este foco com TODOS os grupos que ele conecta
            fundido = [h]
            for g in vizinhos:
                fundido.extend(g)
                grupos.remove(g)
            grupos.append(fundido)

    representantes = []
    for g in grupos:
        melhor = max(g, key=lambda h: (
            ORDEM_GRAVIDADE.get(h["gravidade"], 0), -h["dist_km"],
            h["foco"]["lat"], h["foco"]["lon"]))
        rep = dict(melhor)
        rep["foco"] = dict(melhor["foco"])  # não compartilhar o foco aninhado
        rep["n_focos"] = len(g)
        representantes.append(rep)
    return representantes


# ---------------------------------------------------------------- estado


def carregar_estado(caminho: Path = ESTADO) -> dict:
    """Carrega o estado tolerando arquivo ausente, ilegível ou de formato errado."""
    padrao = {"ultimo_csv": "", "ultimo_alerta": {},
              "resumo_data": "", "alertas_desde_resumo": 0,
              "ciclos_ok_desde_resumo": 0, "ciclos_falha_desde_resumo": 0,
              "falhas_seguidas": 0, "alerta_cego_em": ""}
    if not caminho.exists():
        return dict(padrao)
    try:
        lido = json.loads(caminho.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        log("AVISO: estado-vigia.json ilegível — recomeçando estado do zero.")
        return dict(padrao)
    if not isinstance(lido, dict):
        log("AVISO: estado-vigia.json com formato inesperado — recomeçando do zero.")
        return dict(padrao)
    estado = dict(padrao)
    for chave in ("ultimo_csv", "resumo_data", "alerta_cego_em"):
        if isinstance(lido.get(chave), str):
            estado[chave] = lido[chave]
    if isinstance(lido.get("ultimo_alerta"), dict):
        estado["ultimo_alerta"] = lido["ultimo_alerta"]
    for chave in ("alertas_desde_resumo", "ciclos_ok_desde_resumo",
                  "ciclos_falha_desde_resumo", "falhas_seguidas"):
        if isinstance(lido.get(chave), int):
            estado[chave] = lido[chave]
    return estado


def _gravar_json_atomico(dados: dict, caminho: Path) -> bool:
    """Grava JSON de forma atômica (temporário + troca). True se conseguiu.

    O arquivo-temporário-e-troca evita deixar um JSON pela metade se faltar
    energia no meio da escrita.
    """
    # No Windows, trocar um arquivo que o painel está lendo pode falhar 1x
    # (PermissionError). Tenta de novo algumas vezes antes de desistir.
    for tentativa in range(3):
        try:
            caminho.parent.mkdir(parents=True, exist_ok=True)
            temporario = caminho.with_suffix(caminho.suffix + ".tmp")
            temporario.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporario, caminho)
            return True
        except OSError as e:
            if tentativa == 2:
                log(f"AVISO: não consegui salvar {caminho.name} ({e}) — sigo vigiando mesmo assim.")
                return False
            time.sleep(0.2)
    return False


def salvar_estado(estado: dict, caminho: Path = ESTADO) -> None:
    """Memória operacional do robô (último CSV + cooldown de e-mail)."""
    _gravar_json_atomico(estado, caminho)


def salvar_painel_estado(fazendas: list, atingidas: dict, agora: datetime,
                         total_focos_brasil: int, caminho: Path = PAINEL_ESTADO) -> None:
    """Grava o que a TELA precisa mostrar (separado da memória de e-mail).

    Inclui TODAS as fazendas (a maioria "verde"); as atingidas ganham gravidade
    atual e a lista de focos com hora já em Brasília. `heartbeat_local` = agora.
    """
    lista = []
    for fazenda in fazendas:
        hits = atingidas.get(fazenda["nome"], [])
        lista.append({
            "nome": fazenda["nome"],
            "gravidade_atual": pior_gravidade([h["gravidade"] for h in hits]) if hits else None,
            "contato": fazenda.get("contato", {"nome": "", "telefone": ""}),
            "focos": [{
                "lat": h["foco"]["lat"], "lon": h["foco"]["lon"],
                "hora_local": formatar_hora_local(h["foco"]["data"]),
                "dist_km": round(h["dist_km"], 1), "rumo": h["rumo"],
                "gravidade": h["gravidade"], "n_focos": h.get("n_focos", 1),
            } for h in hits],
        })
    _gravar_json_atomico({
        "gerado_em": agora.isoformat(),           # última checagem REAL (dado do INPE)
        "ultima_checagem_local": agora.strftime("%d/%m/%Y %H:%M"),
        "heartbeat_local": agora.isoformat(),     # última "batida" do robô (vivo?)
        "heartbeat_limite_min": HEARTBEAT_LIMITE_MIN,
        "total_focos_brasil": total_focos_brasil,
        "fazendas": lista,
    }, caminho)


def carimbar_heartbeat(caminho: Path = PAINEL_ESTADO) -> None:
    """Atualiza só o 'batimento' no painel-estado, a cada volta do laço.

    Assim a tela sabe que o robô está vivo mesmo nos ciclos sem CSV novo. Se o
    arquivo ainda não existe (robô acabou de subir), cria um mínimo "aguardando".
    """
    agora_iso = datetime.now(FUSO_BRASILIA).isoformat()
    try:
        painel = json.loads(caminho.read_text(encoding="utf-8")) if caminho.exists() else {}
        if not isinstance(painel, dict):
            painel = {}
    except (ValueError, OSError):
        painel = {}
    painel["heartbeat_local"] = agora_iso
    painel["heartbeat_limite_min"] = HEARTBEAT_LIMITE_MIN
    painel.setdefault("fazendas", [])
    painel.setdefault("total_focos_brasil", None)
    painel.setdefault("ultima_checagem_local", None)
    painel.setdefault("gerado_em", None)  # preserva a hora da última checagem real
    _gravar_json_atomico(painel, caminho)


def cooldown_ativo(ultimo_alerta_iso: str, agora: datetime, minutos: int = COOLDOWN_MINUTOS) -> bool:
    """True se o último alerta desta fazenda foi há menos de `minutos`."""
    if not ultimo_alerta_iso:
        return False
    try:
        ultimo = datetime.fromisoformat(ultimo_alerta_iso)
        if ultimo.tzinfo is None:  # timestamp editado à mão, sem fuso
            ultimo = ultimo.replace(tzinfo=FUSO_BRASILIA)
        delta = agora - ultimo
    except (ValueError, TypeError):
        return False
    # delta negativo = relógio andou pra trás; nunca bloquear alerta por isso
    return timedelta(0) <= delta < timedelta(minutes=minutos)


def deve_alertar(registro, gravidade_atual: str, agora: datetime,
                 minutos: int = COOLDOWN_MINUTOS) -> bool:
    """Decide se manda e-mail agora, considerando cooldown E escalada de gravidade.

    Fora do cooldown: sempre alerta. Dentro do cooldown: só se o fogo se
    aproximou (gravidade subiu desde o último alerta) — fogo chegando nunca cala.
    `registro` pode ser None, uma string ISO antiga, ou {'em':..., 'grav':...}.
    """
    if not registro:
        return True
    if isinstance(registro, str):
        em, grav_ant = registro, None
    else:
        em, grav_ant = registro.get("em", ""), registro.get("grav")
    if not cooldown_ativo(em, agora, minutos):
        return True
    # cooldown ativo: só re-alerta se subiu de gravidade (e sabemos a anterior).
    # .get tolera gravidade corrompida/desconhecida no estado (como cooldown_ativo
    # já tolera timestamp corrompido) — nunca deixa o ciclo estourar por isso.
    return grav_ant is not None and \
        ORDEM_GRAVIDADE.get(gravidade_atual, 0) > ORDEM_GRAVIDADE.get(grav_ant, 0)


# ---------------------------------------------------------------- e-mail


def formatar_hora_local(data_str: str) -> str:
    """Converte a hora UTC do CSV ('2026/07/19 22:30:00' ou ISO) para Brasília."""
    limpo = data_str.strip().replace("/", "-")
    for formato in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            utc = datetime.strptime(limpo[:19], formato).replace(tzinfo=timezone.utc)
            return utc.astimezone(FUSO_BRASILIA).strftime("%d/%m/%Y %H:%M") + " (Brasília)"
        except ValueError:
            continue
    return f"{data_str} (UTC)"


def _onde_foco(hit: dict) -> str:
    """Frase de localização de um foco conforme a gravidade."""
    grav, dist, rumo = hit["gravidade"], hit["dist_km"], hit["rumo"]
    if grav == "dentro" or dist < 0.05:
        return "DENTRO da divisa (aprox.)"
    if grav == "urgente":
        return f"a ~{dist:.1f} km a {rumo} (URGENTE, ≤5 km)"
    return f"a ~{dist:.1f} km a {rumo} (atenção — chegando)"


def _bloco_acao(fazenda: dict) -> list:
    """Linhas de 'o que fazer': quem ligar e onde tem água, por fazenda.

    Em emergência ninguém abre agenda nem vai procurar arquivo de configuração —
    tem que estar dentro do e-mail. Telefone que falta é COBRADO em vez de omitido:
    buraco silencioso na cadeia de resposta é pior que buraco visível.
    """
    contato = fazenda.get("contato") or {}
    nome, telefone = contato.get("nome", ""), contato.get("telefone", "")
    if telefone:
        linhas = [f"  ☎ {nome + ' — ' if nome else ''}{telefone}"]
    else:
        linhas = ["  ☎ (sem telefone cadastrado — preencha em config/fazendas.json)"]
    if fazenda.get("ponto_de_agua"):
        linhas.append(f"  💧 Água: {fazenda['ponto_de_agua']}")
    else:  # é a PRIMEIRA coisa que o carro-pipa pergunta — não pode faltar calado
        linhas.append("  💧 (sem ponto de água cadastrado — preencha em config/fazendas.json)")
    return linhas


def montar_email(atingidas: dict, fazendas: list) -> tuple:
    """Monta (assunto, corpo) do alerta. `atingidas`: {nome: [hit, ...]}.

    `fazendas` entra para o alerta carregar telefone e ponto de água — detectar
    sem dizer a quem ligar não protege ninguém.
    """
    por_nome = {f["nome"]: f for f in fazendas}
    nomes = list(atingidas)
    piores = {n: pior_gravidade([h["gravidade"] for h in hits]) for n, hits in atingidas.items()}
    urgentes = [n for n in nomes if ORDEM_GRAVIDADE[piores[n]] >= ORDEM_GRAVIDADE["urgente"]]

    if urgentes:  # tem fogo perto/dentro em pelo menos uma fazenda
        alvo = urgentes
        if len(alvo) == 1:
            assunto = f"🔥 FOGO: {alvo[0]}"
        else:
            assunto = f"🔥 FOGO: {len(alvo)} fazendas ({', '.join(alvo)})"
        titulo = "🔥 ALERTA DE FOGO — vigia-fogo"
    else:  # só focos no anel externo (5–10 km): atenção, chegando
        if len(nomes) == 1:
            assunto = f"⚠️ ATENÇÃO: foco chegando — {nomes[0]}"
        else:
            assunto = f"⚠️ ATENÇÃO: foco chegando — {len(nomes)} fazendas ({', '.join(nomes)})"
        titulo = "⚠️ ATENÇÃO — foco de calor se aproximando (vigia-fogo)"

    linhas = [titulo, ""]
    for nome in nomes:
        linhas.append(f"■ {nome}:")
        for hit in atingidas[nome]:
            foco = hit["foco"]
            extra = f" · {hit['n_focos']} detecções agrupadas" if hit.get("n_focos", 1) > 1 else ""
            linhas.append(f"  • Foco {_onde_foco(hit)} — satélite {foco['satelite']}, "
                          f"{formatar_hora_local(foco['data'])}{extra}")
            linhas.append(f"    Mapa: https://maps.google.com/?q={foco['lat']},{foco['lon']}")
        linhas.extend(_bloco_acao(por_nome.get(nome, {})))
        linhas.append("")
    if urgentes:  # só quando alguém pode de fato sair de casa por causa deste e-mail
        linhas.extend(AVISO_SEGURANCA)
    linhas.append("Emergência: Bombeiros 193 · Defesa Civil 199")
    linhas.append("Detecção por satélite: pixel de ~2 km, dado com ~20 min de atraso.")
    linhas.append("Na dúvida, confirme olhando o horizonte ou ligando pra alguém na fazenda.")
    return assunto, "\n".join(linhas)


def enviar_email(env: dict, assunto: str, corpo: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = assunto
    msg["From"] = env["SMTP_USER"]
    msg["To"] = env["ALERTA_EMAIL_PARA"]
    msg.set_content(corpo)
    contexto = ssl.create_default_context()
    with smtplib.SMTP(env["SMTP_HOST"], int(env["SMTP_PORT"]), timeout=TIMEOUT_HTTP) as smtp:
        smtp.starttls(context=contexto)
        smtp.login(env["SMTP_USER"], env["SMTP_PASSWORD"])
        smtp.send_message(msg)


# ---------------------------------------------------------------- ciclo


def carregar_fazendas() -> list:
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    return [f for f in cfg["fazendas"] if f.get("ativa")]


def ciclo(env: dict, fazendas: list, estado: dict,
          listar=listar_csvs, baixar=baixar_csv, enviar=enviar_email,
          salvar=salvar_estado, salvar_painel=salvar_painel_estado) -> str:
    """Um ciclo completo: baixar novos CSVs, checar fazendas, alertar.

    Devolve 'ok' (enxerguei) ou 'cego' (rodei, mas não consegui dado do INPE) —
    quem cuida disso é `avisar_se_cego`. Devolver o status é o que impede o robô
    de bater o ponto em silêncio enquanto está sem enxergar nada.
    `listar`/`baixar`/`enviar`/`salvar`/`salvar_painel` são injetáveis para teste.
    """
    try:
        disponiveis = listar(env["INPE_10MIN_URL"])
    except ERROS_REDE as e:
        log(f"AVISO: não consegui listar os arquivos do INPE ({e}). Tento no próximo ciclo.")
        return "cego"
    if not disponiveis:
        log("AVISO: índice do INPE veio vazio — nada a processar neste ciclo.")
        return "cego"

    # Servidor no ar mas parado de publicar: seguimos processando o que há (melhor
    # que nada), só não deixamos isso passar por "vigilância normal".
    status = "ok"
    if dado_esta_velho(disponiveis[-1], datetime.now(FUSO_BRASILIA)):
        log(f"AVISO: o arquivo mais novo do INPE ({disponiveis[-1]}) está velho — "
            f"o servidor parou de publicar. Estou vigiando com dado atrasado.")
        status = "cego"

    novos, descartados = selecionar_novos(disponiveis, estado["ultimo_csv"])
    if descartados:
        log(f"AVISO: robô ficou parado — {descartados} arquivo(s) antigo(s) ignorados; "
            f"vigiando só a última ~1h.")
    if not novos:
        log("Nenhum arquivo novo do INPE ainda.")
        return status  # normal: o INPE publica a cada 10 min, igual ao nosso ciclo

    total_focos, baixados, atingidas, ja_vistos = 0, 0, {}, set()
    for nome in novos:
        try:
            texto = baixar(env["INPE_10MIN_URL"], nome)
        except ERROS_REDE as e:
            log(f"AVISO: falha ao baixar {nome} ({e}) — pulando este arquivo.")
            continue
        baixados += 1
        focos, puladas = parse_focos(texto)
        aplicar_hora_do_arquivo(focos, nome)
        total_focos += len(focos)
        if puladas:
            log(f"AVISO: {puladas} linha(s) malformada(s) puladas em {nome}.")
        for fazenda_nome, hits in verificar_focos(focos, fazendas).items():
            for hit in hits:
                foco = hit["foco"]
                chave = (fazenda_nome, foco["lat"], foco["lon"])
                if chave not in ja_vistos:  # mesmo foco repetido em 2+ arquivos
                    ja_vistos.add(chave)
                    atingidas.setdefault(fazenda_nome, []).append(hit)

    if baixados == 0:
        log("AVISO: nenhum arquivo baixou neste ciclo — tento tudo de novo no próximo.")
        return "cego"

    # M3: junta focos vizinhos do mesmo incêndio — 1 fogo = 1 ponto/1 linha.
    atingidas = {nome: agrupar_hits(hits) for nome, hits in atingidas.items()}

    log(f"{baixados}/{len(novos)} arquivo(s) processado(s), {total_focos} foco(s) no Brasil, "
        f"{sum(len(v) for v in atingidas.values())} incêndio(s) na(s) fazenda(s).")

    agora = datetime.now(FUSO_BRASILIA)
    # A tela reflete os focos AGORA, independente do e-mail/cooldown.
    salvar_painel(fazendas, atingidas, agora, total_focos)

    para_alertar = {}
    for nome, hits in atingidas.items():
        grav = pior_gravidade([h["gravidade"] for h in hits])
        if deve_alertar(estado["ultimo_alerta"].get(nome), grav, agora):
            para_alertar[nome] = hits
        else:
            log(f"Cooldown ativo para '{nome}' — e-mail suprimido (foco continua lá).")

    if para_alertar:
        assunto, corpo = montar_email(para_alertar, fazendas)
        try:
            enviar(env, assunto, corpo)
        except (smtplib.SMTPException, OSError, ValueError) as e:
            log(f"ERRO: e-mail de alerta NÃO saiu ({e}). Vou tentar de novo no próximo ciclo.")
            return status  # não avança estado nem cooldown: próximo ciclo re-tenta
        log(f"E-mail de alerta enviado: {assunto}")
        estado["alertas_desde_resumo"] = estado.get("alertas_desde_resumo", 0) + 1
        for nome, hits in para_alertar.items():
            estado["ultimo_alerta"][nome] = {
                "em": agora.isoformat(),
                "grav": pior_gravidade([h["gravidade"] for h in hits]),
            }

    estado["ultimo_csv"] = novos[-1]
    salvar(estado)
    return status


def modo_teste_email(env: dict) -> int:
    agora = datetime.now(FUSO_BRASILIA).strftime("%d/%m/%Y %H:%M")
    corpo = (
        "Este é um e-mail de TESTE do vigia-fogo.\n\n"
        f"Se você recebeu isto ({agora}), o envio de alertas está funcionando.\n"
        "Se caiu em spam/lixo eletrônico, marque como confiável para não perder alerta real."
    )
    try:
        enviar_email(env, "✅ vigia-fogo: teste de e-mail", corpo)
    except (smtplib.SMTPException, OSError, ValueError) as e:
        log(f"ERRO no teste de e-mail: {e}")
        log("Confira no .env: SMTP_HOST/SMTP_PORT/SMTP_USER e principalmente a "
            "SMTP_PASSWORD (precisa ser a senha de APLICATIVO, não a senha normal).")
        return 1
    log(f"E-mail de teste enviado para {env['ALERTA_EMAIL_PARA']} — confira a caixa de entrada.")
    return 0


# ---------------------------------------------------------------- trava (1 cópia só)


_lock_fd = None  # mantido aberto enquanto o robô vive = mantém o lock do S.O.


def tentar_travar(caminho: Path = LOCK) -> bool:
    """Reserva a vez com um lock do PRÓPRIO sistema operacional. False se outra
    cópia já o segura.

    O S.O. solta o lock sozinho quando o processo morre (até se travar) — sem PID
    frágil, sem lock órfão, sem corrida entre "checar" e "gravar". No Windows usa
    `msvcrt.locking`; no resto, `fcntl.flock`.
    """
    global _lock_fd
    try:
        caminho.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(caminho), os.O_CREAT | os.O_RDWR)
    except OSError:
        return True  # não deu pra abrir o arquivo: vigiar é mais importante, segue
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False  # outra cópia já segura o lock
    _lock_fd = fd
    return True


def liberar_lock(caminho: Path = LOCK) -> None:
    """Solta o lock (fechar o arquivo já solta no S.O.) e limpa o arquivo."""
    global _lock_fd
    if _lock_fd is not None:
        try:
            os.close(_lock_fd)  # fechar o descritor solta o lock do S.O.
        except OSError:
            pass
        _lock_fd = None
    try:
        caminho.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------- saúde (estou enxergando?)


def registrar_saude(estado: dict, status: str, agora: datetime) -> bool:
    """Contabiliza o ciclo e diz se é hora de avisar que o robô está cego.

    O robô roda invisível: sem esta contagem, "não consegui dado do INPE" só ia
    para um log que ninguém lê, e o resumo das 18h diria "tudo em ordem" do mesmo
    jeito. Aqui `status` é 'ok' (conseguiu dado fresco) ou 'cego' (não conseguiu).
    """
    if status == "ok":
        estado["ciclos_ok_desde_resumo"] = estado.get("ciclos_ok_desde_resumo", 0) + 1
        estado["falhas_seguidas"] = 0
        return False
    estado["ciclos_falha_desde_resumo"] = estado.get("ciclos_falha_desde_resumo", 0) + 1
    estado["falhas_seguidas"] = estado.get("falhas_seguidas", 0) + 1
    if estado["falhas_seguidas"] < CICLOS_CEGO_PARA_ALERTAR:
        return False  # queda curta do INPE é rotina; não vale acordar ninguém
    return not cooldown_ativo(estado.get("alerta_cego_em", ""), agora, COOLDOWN_CEGO_HORAS * 60)


def montar_email_cego(falhas_seguidas: int, agora: datetime) -> tuple:
    """Monta o aviso de que o robô está de pé mas não está enxergando nada."""
    minutos = falhas_seguidas * CICLO_SEGUNDOS // 60
    corpo = (
        f"O vigia está rodando, mas há ~{minutos} minuto(s) ele NÃO consegue dado do "
        f"INPE ({falhas_seguidas} tentativas seguidas sem sucesso).\n\n"
        "ENQUANTO ISSO DURAR, SUAS FAZENDAS NÃO ESTÃO SENDO VIGIADAS.\n"
        "Silêncio agora não quer dizer \"sem fogo\" — quer dizer \"não estou enxergando\".\n"
        "Se o tempo estiver seco, vale olhar o horizonte e avisar o pessoal na fazenda.\n\n"
        "O que costuma ser:\n"
        "  • a internet deste computador caiu; ou\n"
        "  • o site do INPE saiu do ar (acontece, ainda mais na temporada de queimadas).\n\n"
        "Não precisa fazer nada no robô: ele continua tentando sozinho, volta a vigiar "
        f"assim que o dado voltar, e só reavisa se continuar cego por mais "
        f"{COOLDOWN_CEGO_HORAS} horas."
    )
    return f"🚨 vigia-fogo CEGO — sem dado do INPE ({agora.strftime('%d/%m %H:%M')})", corpo


def avisar_se_cego(env: dict, estado: dict, status: str,
                   enviar=enviar_email, salvar=salvar_estado) -> None:
    """Registra a saúde do ciclo e manda e-mail se o robô estiver cego há tempo demais.

    Grava o estado SEMPRE — inclusive em ciclo que falhou, que é justamente quando
    `ciclo()` sai cedo sem salvar nada.
    """
    agora = datetime.now(FUSO_BRASILIA)
    if registrar_saude(estado, status, agora):
        assunto, corpo = montar_email_cego(estado["falhas_seguidas"], agora)
        try:
            enviar(env, assunto, corpo)
        except (smtplib.SMTPException, OSError, ValueError) as e:
            log(f"AVISO: não consegui avisar que estou cego ({e}) — tento no próximo ciclo.")
        else:
            # só marca depois de sair: aviso que não saiu precisa ser tentado de novo
            estado["alerta_cego_em"] = agora.isoformat()
            log(f"Aviso de 'robô cego' enviado ({estado['falhas_seguidas']} ciclos sem dado).")
    salvar(estado)


# ---------------------------------------------------------------- resumo diário


def deve_enviar_resumo(agora: datetime, hora_alvo: int, data_ultimo: str) -> bool:
    """True se passou do horário-alvo e ainda não houve resumo hoje."""
    return agora.hour >= hora_alvo and data_ultimo != agora.date().isoformat()


def montar_resumo_diario(fazendas: list, alertas: int, agora: datetime,
                         ciclos_ok: int, ciclos_falha: int) -> tuple:
    """Monta o e-mail 'estou vivo'. Contagens = desde o resumo anterior.

    Regra que dá sentido ao resumo inteiro: ele NUNCA diz "tudo em ordem" sem ter
    olhado. "Estar de pé" e "estar enxergando" são coisas diferentes, e confundir
    as duas é o que transforma este e-mail em falso conforto.
    """
    dia, hora = agora.strftime("%d/%m/%Y"), agora.strftime("%H:%M")
    rodape = ("\n\nSe um dia este resumo NÃO chegar, é sinal de que o robô parou "
              "de rodar — vale conferir.")

    # O assunto acompanha a saúde: é ele que o dono lê de relance no celular,
    # e um "resumo do dia" tranquilo escondendo um dia cego seria o mesmo engano.
    if ciclos_ok == 0:  # rodou o tempo todo e não enxergou nada: o pior caso
        assunto = f"🚨 vigia-fogo: NÃO CONSEGUI VIGIAR ({dia})"
        titulo = f"🚨 vigia-fogo NÃO CONSEGUIU VIGIAR ({dia}, {hora})."
        saude = (f"Desde o resumo anterior eu NÃO consegui dado do INPE nenhuma vez "
                 f"({ciclos_falha} tentativa(s)).\n"
                 f"Atenção: aqui \"nenhum alerta de fogo\" NÃO quer dizer que não houve "
                 f"fogo — quer dizer que eu não enxerguei nada.\n"
                 f"Confira a internet deste computador e se o site do INPE está no ar.")
    elif ciclos_falha:
        assunto = f"⚠️ vigia-fogo: resumo do dia ({dia}) — fiquei cego parte do tempo"
        titulo = f"⚠️ vigia-fogo de pé em {dia}, às {hora} — mas fiquei cego parte do tempo."
        saude = (f"Olhei o satélite {ciclos_ok} vez(es) e falhei {ciclos_falha} vez(es) "
                 f"(sem dado do INPE). Nessas {ciclos_falha} falhas eu NÃO estava vigiando.")
    else:
        assunto = f"🌙 vigia-fogo: resumo do dia ({dia})"
        titulo = f"✅ vigia-fogo de pé em {dia}, às {hora}."
        saude = f"Olhei o satélite {ciclos_ok} vez(es) desde o resumo anterior, sem falha."

    if alertas == 0:
        noticia = f"Nenhum alerta de fogo nas suas {len(fazendas)} fazendas."
    else:
        noticia = f"Enviei {alertas} alerta(s) de fogo nas suas {len(fazendas)} fazendas."
    return assunto, f"{titulo}\n\n{saude}\n\n{noticia}{rodape}"


def main() -> int:
    # Console do Windows sem UTF-8 não pode derrubar o vigia por causa de um emoji.
    for saida in (sys.stdout, sys.stderr):
        if hasattr(saida, "reconfigure"):
            saida.reconfigure(encoding="utf-8", errors="replace")

    p = argparse.ArgumentParser(description="Vigia de queimadas via satélite (M1)")
    p.add_argument("--uma-vez", action="store_true", help="roda 1 ciclo e sai")
    p.add_argument("--teste-email", action="store_true", help="manda e-mail de teste e sai")
    args = p.parse_args()

    env = carregar_env(ARQUIVO_ENV)

    # Código 3 = "erro de partida, NÃO adianta reiniciar" (o iniciar-vigia.bat
    # para o laço nesse caso, em vez de tentar pra sempre).
    try:
        fazendas = carregar_fazendas()
    except (OSError, ValueError, KeyError) as e:
        print(f"ERRO: não consegui ler config/fazendas.json ({e}).")
        return 3

    problemas = problemas_de_partida(env, fazendas)
    if problemas:
        print("ERRO: não dá pra começar a vigiar. Corrija antes:")
        for item in problemas:
            print(f"  - {item}")
        print("(.env: copie o .env.example e preencha — instruções lá dentro.)")
        return 3

    if args.teste_email:
        return modo_teste_email(env)

    if not fazendas:
        print("ERRO: nenhuma fazenda ativa em config/fazendas.json.")
        return 3

    if not tentar_travar():
        print("ERRO: já tem um vigia rodando neste computador (trava em dados/vigia.lock).")
        print("Feche a outra janela do vigia antes de abrir esta.")
        return 3

    try:
        hora_resumo = int(env.get("RESUMO_DIARIO_HORA") or 18)
    except ValueError:
        hora_resumo = 18
    if not 0 <= hora_resumo <= 23:  # typo tipo "24" não pode desligar o resumo
        log(f"AVISO: RESUMO_DIARIO_HORA='{env.get('RESUMO_DIARIO_HORA')}' fora de 0-23; usando 18.")
        hora_resumo = 18
    log(f"Vigiando {len(fazendas)} fazenda(s): {', '.join(f['nome'] for f in fazendas)}")
    log(f"Painel: rode 'python painel.py' noutra janela para ver o mapa.")

    estado = carregar_estado()
    carimbar_heartbeat()  # a tela já abre mostrando "robô vivo, aguardando 1ª checagem"
    try:
        while True:
            # Ciclo que ESTOURA também é cegueira: se ele morresse aqui sem ser
            # contado, o robô voltaria a bater o ponto como se estivesse enxergando.
            status, erro = "cego", None
            try:  # rede/estado/config: vigia NUNCA morre em silêncio
                status = ciclo(env, fazendas, estado)
            except Exception as e:
                erro = e
                log(f"ERRO inesperado no ciclo ({e!r}) — continuo vigiando.")
            try:
                avisar_se_cego(env, estado, status)
                enviar_resumo_se_hora(env, fazendas, estado, hora_resumo)
            except Exception as e:
                erro = erro or e
                log(f"ERRO inesperado ao cuidar da saúde/resumo ({e!r}) — continuo vigiando.")
            carimbar_heartbeat()  # bate o ponto a cada volta, mesmo sem CSV novo
            if args.uma_vez:
                return 1 if erro else 0
            time.sleep(CICLO_SEGUNDOS)
    except KeyboardInterrupt:
        log("Vigia encerrado pelo usuário. Até a próxima.")
        return 0
    finally:
        liberar_lock()


def enviar_resumo_se_hora(env: dict, fazendas: list, estado: dict, hora_resumo: int) -> None:
    """Manda o resumo diário 'estou vivo' se passou do horário e ainda não foi hoje."""
    agora = datetime.now(FUSO_BRASILIA)
    if not deve_enviar_resumo(agora, hora_resumo, estado.get("resumo_data", "")):
        return
    assunto, corpo = montar_resumo_diario(
        fazendas, estado.get("alertas_desde_resumo", 0), agora,
        estado.get("ciclos_ok_desde_resumo", 0), estado.get("ciclos_falha_desde_resumo", 0))
    try:
        enviar_email(env, assunto, corpo)
    except (smtplib.SMTPException, OSError, ValueError) as e:
        log(f"AVISO: resumo diário não saiu ({e}); tento no próximo ciclo.")
        return
    # Duplicar o resumo (raro, só se travar entre enviar e salvar) é melhor que
    # perdê-lo e disparar falso alarme de "robô morreu". Aceito "pelo menos 1x".
    estado["resumo_data"] = agora.date().isoformat()
    estado["alertas_desde_resumo"] = 0
    estado["ciclos_ok_desde_resumo"] = 0
    estado["ciclos_falha_desde_resumo"] = 0
    salvar_estado(estado)
    log("Resumo diário enviado.")


if __name__ == "__main__":
    sys.exit(main())
