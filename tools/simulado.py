"""Simulado do vigia: prova a corrente inteira com fogo REAL, sem tocar em nada.

Por que existe: testar "o robô está rodando?" não é a mesma coisa que testar
"o alerta chega em quem precisa agir?". Este script exercita download → leitura →
geometria → agrupamento → montagem do e-mail → envio → chegada, usando um foco de
verdade que está queimando agora em algum lugar do Brasil.

O que ele NÃO faz, de propósito:
- não mexe em config/fazendas.json (nada de "fazenda de teste" esquecida em agosto);
- não mexe no estado do robô (nem no ponteiro, nem no cooldown, nem no resumo diário);
- não briga com o vigia que está rodando (não pega a trava, não escreve arquivo).
Nada para limpar depois. É o motivo de ele existir separado do vigia.

Uso:
  python -X utf8 tools/simulado.py                 # acha fogo ativo e manda o alerta
  python -X utf8 tools/simulado.py --sem-email     # só mostra o que sairia
  python -X utf8 tools/simulado.py --perto-de -16.75,-47.93 --raio-km 100

Faça pelo menos um antes de agosto, e CRONOMETRE: do e-mail chegar até alguém
estar no local com equipamento. Se a resposta for "não sei", o sistema não está
pronto — a auditoria da Gaia (docs/analise-gaia-pre-seca-2026.md) é clara nisso.
"""

import argparse
import math
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import vigia  # noqa: E402

# Quantos arquivos de 10 min olhar para achar fogo ativo (6 = última ~1 hora).
ARQUIVOS_PARA_VARRER = 6
# Tamanho da célula da grade ao procurar aglomerado de fogo, em graus (~55 km).
CELULA_GRAUS = 0.5
# Metade do lado da "divisa" simulada, em graus (~1,1 km) — os focos caem DENTRO.
MEIO_LADO_GRAUS = 0.01


def focos_recentes(env: dict, quantos: int = ARQUIVOS_PARA_VARRER) -> list:
    """Baixa os últimos arquivos do INPE e devolve todos os focos, com hora."""
    disponiveis = vigia.listar_csvs(env["INPE_10MIN_URL"])
    focos = []
    for nome in disponiveis[-quantos:]:
        try:
            texto = vigia.baixar_csv(env["INPE_10MIN_URL"], nome)
        except vigia.ERROS_REDE as e:
            print(f"  aviso: não baixei {nome} ({e})")
            continue
        lote, _ = vigia.parse_focos(texto)
        vigia.aplicar_hora_do_arquivo(lote, nome)
        focos.extend(lote)
    return focos


def melhor_alvo(focos: list, perto_de=None, raio_km: float = None):
    """Escolhe onde fazer o simulado: o maior aglomerado de fogo disponível.

    Sem `perto_de`, pega a maior concentração do Brasil (garante que o teste
    dispara). Com `perto_de`, só considera focos dentro do raio pedido — útil
    para testar na sua própria região, com a ressalva de que pode não haver fogo
    perto agora. Devolve (lat, lon, focos_do_grupo) ou None.
    """
    candidatos = focos
    if perto_de is not None:
        lat0, lon0 = perto_de
        candidatos = [f for f in focos
                      if vigia.distancia_km_pontos(f["lat"], f["lon"], lat0, lon0) <= raio_km]
    if not candidatos:
        return None

    grade = defaultdict(list)
    for f in candidatos:
        chave = (round(f["lat"] / CELULA_GRAUS), round(f["lon"] / CELULA_GRAUS))
        grade[chave].append(f)
    # empate resolvido pela chave, para o resultado não depender da ordem de leitura
    grupo = max(grade.values(), key=lambda g: (len(g), -g[0]["lat"], -g[0]["lon"]))
    lat = sum(f["lat"] for f in grupo) / len(grupo)
    lon = sum(f["lon"] for f in grupo) / len(grupo)
    return lat, lon, grupo


def fazenda_simulada(lat: float, lon: float, nome: str = "SIMULADO") -> dict:
    """Monta em memória uma 'fazenda' centrada no fogo, com os anéis de sempre."""
    m = MEIO_LADO_GRAUS
    km_por_grau_lon = max(111.320 * math.cos(math.radians(lat)), 1.0)  # nunca dividir por ~0
    def anel(km):
        dlat, dlon = km / vigia.KM_POR_GRAU_LAT, km / km_por_grau_lon
        return {"oeste": lon - m - dlon, "sul": lat - m - dlat,
                "leste": lon + m + dlon, "norte": lat + m + dlat}
    return {
        "nome": nome,
        "centro": {"lat": lat, "lon": lon},
        "bbox_fazenda": {"oeste": lon - m, "sul": lat - m, "leste": lon + m, "norte": lat + m},
        "bbox_vigilancia_anel_5km": anel(5),
        "bbox_vigilancia_anel_10km": anel(10),
        "contato": {"nome": "SIMULADO — não é fogo na sua terra", "telefone": "+55 00 00000-0000"},
        "ponto_de_agua": "simulado",
        "ativa": True,
    }


PADRAO_COORDENADA = re.compile(r"^-?\d+(\.\d+)?\s*,\s*-?\d+(\.\d+)?$")


def consertar_argv(argv: list) -> list:
    """Faz `--perto-de -16.75,-47.93` funcionar igual a `--perto-de=-16.75,-47.93`.

    O leitor de argumentos do Python acha que o '-' da latitude começa outra opção
    e falha com uma mensagem que não explica nada. Coordenada no Brasil é quase
    sempre negativa, então esse tropeço seria a regra, não a exceção.
    """
    saida, i = [], 0
    while i < len(argv):
        if (argv[i] == "--perto-de" and i + 1 < len(argv)
                and PADRAO_COORDENADA.match(argv[i + 1])):
            saida.append(f"--perto-de={argv[i + 1]}")
            i += 2
            continue
        saida.append(argv[i])
        i += 1
    return saida


def atraso_minutos(foco: dict, agora: datetime):
    """Quanto tempo faz que o satélite viu este foco. None se a hora não der pra ler."""
    limpo = foco.get("data", "").strip().replace("/", "-")
    for formato in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            visto = datetime.strptime(limpo[:19], formato).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        return (agora - visto).total_seconds() / 60
    return None


def main() -> int:
    for saida in (sys.stdout, sys.stderr):
        if hasattr(saida, "reconfigure"):
            saida.reconfigure(encoding="utf-8", errors="replace")

    p = argparse.ArgumentParser(description="Simulado do vigia-fogo com fogo real")
    p.add_argument("--sem-email", action="store_true", help="mostra o alerta, não envia")
    p.add_argument("--perto-de", help="testar perto de 'lat,lon' (ex.: sua região)")
    p.add_argument("--raio-km", type=float, default=100.0, help="raio de busca do --perto-de")
    args = p.parse_args(consertar_argv(sys.argv[1:]))

    env = vigia.carregar_env(vigia.ARQUIVO_ENV)
    faltando = vigia.chaves_faltando(env)
    if faltando and not args.sem_email:
        print("ERRO: .env incompleto para enviar e-mail:", ", ".join(faltando))
        return 3

    perto = None
    if args.perto_de:
        try:
            perto = tuple(float(x) for x in args.perto_de.split(","))
        except ValueError:
            print("ERRO: --perto-de precisa ser 'lat,lon' (ex.: \"-16.75,-47.93\")")
            return 3

    inicio = time.monotonic()
    print("Procurando fogo ativo no Brasil (última ~1 hora)...")
    focos = focos_recentes(env)
    if not focos:
        print("ERRO: não consegui dado do INPE agora. Isso já é um resultado: "
              "se acontecesse valendo, o robô estaria CEGO — confira a internet.")
        return 1
    print(f"  {len(focos)} detecção(ões) no Brasil.")

    alvo = melhor_alvo(focos, perto, args.raio_km)
    if alvo is None:
        print(f"Nenhum foco num raio de {args.raio_km:.0f} km de {args.perto_de}.")
        print("Isso NÃO quer dizer que o robô falhou — quer dizer que não há fogo lá agora.")
        print("Rode sem --perto-de para testar com o maior incêndio ativo do país.")
        return 2

    lat, lon, grupo = alvo
    agora = datetime.now(timezone.utc)
    atrasos = [a for a in (atraso_minutos(f, agora) for f in grupo) if a is not None]
    print(f"  Alvo: {len(grupo)} detecção(ões) em {lat:.4f}, {lon:.4f}")
    print(f"  Mapa: https://maps.google.com/?q={lat:.4f},{lon:.4f}")
    if atrasos:
        print(f"  Idade do dado: {min(atrasos):.0f} a {max(atrasos):.0f} min desde o satélite ver.")

    fazenda = fazenda_simulada(lat, lon)
    atingidas = vigia.verificar_focos(focos, [fazenda])
    if not atingidas:
        print("ERRO INESPERADO: montei a divisa em cima do fogo e a regra não pegou. "
              "Isso é bug de verdade — vale investigar antes de agosto.")
        return 1
    atingidas = {n: vigia.agrupar_hits(h) for n, h in atingidas.items()}

    assunto, corpo = vigia.montar_email(atingidas, [fazenda])
    assunto = "🧪 SIMULADO — " + assunto
    corpo = ("🧪 ISTO É UM SIMULADO. NÃO há fogo nas suas fazendas.\n"
             "Fogo real de outro lugar do Brasil, usado só para testar o alerta.\n"
             + "-" * 60 + "\n\n" + corpo)

    if args.sem_email:
        print("\n" + "=" * 60)
        print("ASSUNTO:", assunto)
        print("=" * 60)
        print(corpo)
        print("\n(--sem-email: nada foi enviado)")
        return 0

    try:
        vigia.enviar_email(env, assunto, corpo)
    except Exception as e:
        print(f"\nFALHOU no envio: {e}")
        print("Este é o tipo de falha que o simulado existe para achar. Confira "
              "SMTP_USER e SMTP_PASSWORD no .env (precisa ser senha de APLICATIVO).")
        return 1

    print(f"\n✅ E-mail enviado para {env['ALERTA_EMAIL_PARA']} "
          f"em {time.monotonic() - inicio:.1f}s.")
    print("\nAGORA A PARTE QUE IMPORTA — cronometre:")
    print("  1. Quanto tempo até você VER o e-mail no celular?")
    print("  2. Quanto tempo até alguém estar no local com equipamento?")
    print("  Se a resposta de (2) for 'não sei', o sistema ainda não está pronto.")
    print("\nNada foi alterado: nem config, nem estado do robô. Nada para limpar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
