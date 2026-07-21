"""Cadastra uma fazenda no config/fazendas.json — pelo número do CAR ou por KMZ.

Dois caminhos:
- `--car`: busca o polígono oficial no geoserver público do SICAR e valida contra
  os dados do recibo (área, município, centroide). É o caminho preferido, porque
  o desenho vem da fonte oficial e dá pra conferir com papel.
- `--kmz`: lê o polígono de um arquivo KMZ/KML (Google Earth), para fazenda que
  ainda não tem CAR em mãos. Sem recibo não há o que validar, então a ferramenta
  MOSTRA a área e o local calculados para você conferir antes de confiar.

Uso:
  python -X utf8 tools/cadastrar_fazenda.py --car "UF-9999999-XXXX.XXXX...." \
      --nome "Fazenda Tal" [--area-doc 590.6] [--centro-doc "-16.340,-49.178"] \
      [--municipio-doc "Example County"]
  python -X utf8 tools/cadastrar_fazenda.py --kmz "C:/caminho/fazenda.kmz" \
      --nome "Fazenda Tal" [--municipio-doc "Example County/GO"] [--fazenda-longe]

Regras herdadas da pesquisa (docs/contexto-projeto.md):
- código no banco é SEM pontos;
- camada por estado: sicar:sicar_imoveis_<uf>;
- validação erra pro lado seguro: divergência = não cadastra e avisa.
"""

import argparse
import json
import math
import re
import subprocess
import sys
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
CONFIG = RAIZ / "config" / "fazendas.json"
DADOS = RAIZ / "dados"
WFS = "https://geoserver.car.gov.br/geoserver/sicar/ows"
TOLERANCIA_AREA_PCT = 5.0
TOLERANCIA_CENTRO_KM = 3.0
KM_POR_GRAU_LAT = 110.574
# Fazenda muito longe das já cadastradas quase sempre é arquivo errado ou
# latitude/longitude trocadas — erros que não dão mensagem nenhuma, só põem o
# robô vigiando terra de estranho. Acima disto a ferramenta para e pergunta.
LONGE_DEMAIS_KM = 150.0
# Divisa de fazenda cabe em poucos KB. Arquivo maior que isto é outra coisa.
LIMITE_KML_MB = 50
# Quanto o retângulo pode ser maior que a área desenhada. Nas 6 fazendas atuais
# fica entre 1,5x e 2,8x (consequência normal de usar retângulo). Muito acima
# disso = talhões separados no mesmo arquivo, e o retângulo engole terra alheia.
INCHACO_MAXIMO_BBOX = 4.0


def buscar_poligono(car_sem_pontos: str, uf: str) -> dict:
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeName": f"sicar:sicar_imoveis_{uf.lower()}",
        "outputFormat": "application/json",
        "CQL_FILTER": f"cod_imovel='{car_sem_pontos}'",
    }
    url = WFS + "?" + urllib.parse.urlencode(params)
    # curl em vez de urllib: o geoserver do SICAR usa TLS antigo que o Python
    # recusa (SSLV3_ALERT_HANDSHAKE_FAILURE); o curl do Windows negocia normal.
    res = subprocess.run(
        ["curl", "-s", "-m", "40", url], capture_output=True, text=True, encoding="utf-8"
    )
    if res.returncode != 0 or not res.stdout.strip():
        raise RuntimeError(f"curl falhou (codigo {res.returncode}) ao consultar o SICAR")
    return json.loads(res.stdout)


def _sem_namespace(tag: str) -> str:
    """'{http://...}Polygon' -> 'Polygon'. KML vem com endereço colado na etiqueta."""
    return tag.rsplit("}", 1)[-1]


def _pontos(texto: str) -> list:
    """Converte o texto de <coordinates> do KML em [[lon, lat], ...].

    KML escreve 'lon,lat,altura' separados por espaço/quebra de linha — a MESMA
    ordem do GeoJSON (longitude primeiro). A altura é descartada.
    """
    pontos = []
    for item in texto.replace("\n", " ").replace("\t", " ").split():
        partes = item.split(",")
        if len(partes) >= 2:
            pontos.append([float(partes[0]), float(partes[1])])
    return pontos


def _recusar_xml_perigoso(texto: str, nome: str) -> None:
    """Recusa arquivo com "bomba de entidades" — o truque do arquivo que incha.

    Existe um golpe conhecido em que um XML de poucos KB manda o programa repetir
    um pedaço milhões de vezes e engole toda a memória do computador. KMZ de
    verdade (Google Earth, topógrafo) NUNCA declara entidade, então basta recusar
    quem declara. Resolve sem instalar biblioteca de terceiro — o projeto roda só
    com o Python padrão, e é assim que fica.
    """
    if len(texto) > LIMITE_KML_MB * 1024 * 1024:
        raise ValueError(f"'{nome}' passa de {LIMITE_KML_MB} MB — grande demais para "
                         "ser desenho de divisa. Recusei por segurança.")
    if re.search(r"<!\s*(DOCTYPE|ENTITY)", texto, re.IGNORECASE):
        raise ValueError(f"'{nome}' traz declaração de entidade XML, coisa que KMZ de "
                         "mapa não tem. Recusei por segurança — confira a origem do arquivo.")


def ler_kmz(caminho: Path) -> dict:
    """Lê o(s) polígono(s) de um KMZ/KML e devolve a geometria em GeoJSON.

    KMZ é só um ZIP com um KML dentro (formato do Google Earth). Usa só a
    biblioteca padrão do Python — nada para instalar.
    """
    caminho = Path(caminho)
    if zipfile.is_zipfile(caminho):
        with zipfile.ZipFile(caminho) as z:
            kmls = [n for n in z.namelist() if n.lower().endswith(".kml")]
            if not kmls:
                raise ValueError(f"'{caminho.name}' é um ZIP mas não tem KML dentro.")
            info = z.getinfo(kmls[0])
            if info.file_size > LIMITE_KML_MB * 1024 * 1024:  # antes de descompactar
                raise ValueError(f"o KML dentro de '{caminho.name}' passa de "
                                 f"{LIMITE_KML_MB} MB descompactado. Recusei por segurança.")
            texto = z.read(kmls[0]).decode("utf-8", errors="replace")
    else:
        texto = caminho.read_text(encoding="utf-8", errors="replace")

    _recusar_xml_perigoso(texto, caminho.name)
    raiz = ET.fromstring(texto)
    poligonos = []
    for elem in raiz.iter():
        if _sem_namespace(elem.tag) != "Polygon":
            continue
        aneis = []
        for lado in elem.iter():
            nome = _sem_namespace(lado.tag)
            if nome not in ("outerBoundaryIs", "innerBoundaryIs"):
                continue
            for coord in lado.iter():
                if _sem_namespace(coord.tag) == "coordinates" and coord.text:
                    pontos = _pontos(coord.text)
                    if len(pontos) >= 4:  # anel fechado tem no mínimo 4 pontos
                        aneis.append(pontos)
        if aneis:
            poligonos.append(aneis)

    if not poligonos:
        raise ValueError(
            f"não achei nenhum polígono em '{caminho.name}'. "
            "O arquivo precisa ter a ÁREA da fazenda desenhada — um pino ou uma "
            "linha solta não serve para calcular a divisa."
        )
    if len(poligonos) == 1:
        return {"type": "Polygon", "coordinates": poligonos[0]}
    return {"type": "MultiPolygon", "coordinates": poligonos}


def area_ha_do_poligono(geom: dict) -> float:
    """Área aproximada em hectares, na mesma projeção plana do resto do projeto.

    Método do agrimensor (shoelace). Buracos no polígono são descontados.
    Serve para CONFERIR se o desenho faz sentido, não para valer como medição.
    """
    partes = ([geom["coordinates"]] if geom["type"] == "Polygon"
              else geom["coordinates"])
    lat_ref = bbox_do_geojson(geom)[1]
    km_lon = 111.320 * math.cos(math.radians(lat_ref))
    total_km2 = 0.0
    for aneis in partes:
        for i, anel in enumerate(aneis):
            soma = 0.0
            for (x1, y1), (x2, y2) in zip(anel, anel[1:] + anel[:1]):
                soma += (x1 * km_lon) * (y2 * KM_POR_GRAU_LAT) - \
                        (x2 * km_lon) * (y1 * KM_POR_GRAU_LAT)
            area = abs(soma) / 2
            total_km2 += area if i == 0 else -area  # anel 0 é a borda; resto é buraco
    return total_km2 * 100  # 1 km² = 100 ha


def km_da_fazenda_mais_perto(lat: float, lon: float, fazendas: list):
    """Distância até a fazenda já cadastrada mais próxima. None se não houver nenhuma."""
    km_lon = 111.320 * math.cos(math.radians(lat))
    distancias = [
        math.hypot((lon - f["centro"]["lon"]) * km_lon,
                   (lat - f["centro"]["lat"]) * KM_POR_GRAU_LAT)
        for f in fazendas if f.get("centro")
    ]
    return min(distancias) if distancias else None


def bbox_do_geojson(geom: dict):
    xs, ys = [], []

    def walk(c):
        if isinstance(c[0], (int, float)):
            xs.append(c[0])
            ys.append(c[1])
        else:
            for i in c:
                walk(i)

    walk(geom["coordinates"])
    return min(xs), min(ys), max(xs), max(ys)  # w, s, e, n


def main():
    p = argparse.ArgumentParser()
    fonte = p.add_mutually_exclusive_group(required=True)
    fonte.add_argument("--car", help="Registro no CAR (com ou sem pontos)")
    fonte.add_argument("--kmz", help="Arquivo KMZ/KML com a divisa desenhada")
    p.add_argument("--nome", required=True, help="Nome da fazenda para os alertas")
    p.add_argument("--area-doc", type=float, help="Área total (ha) impressa no recibo")
    p.add_argument("--centro-doc", help="Centroide do recibo em 'lat,lon' decimal")
    # Sem padrão cravado: qual município é depende da fazenda, não da ferramenta.
    p.add_argument("--municipio-doc", default=None,
                   help="município do recibo, para conferir contra o SICAR (opcional)")
    p.add_argument("--fazenda-longe", action="store_true",
                   help="confirma que a fazenda fica mesmo longe das outras (só --kmz)")
    p.add_argument("--anel-urgente-km", type=float, default=None, help="anel interno 'urgente' (padrao 5)")
    p.add_argument("--anel-atencao-km", type=float, default=None, help="anel externo 'atencao' (padrao 10)")
    args = p.parse_args()

    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    problemas = []

    if args.kmz:
        car_formatado = car_limpo = ""
        try:
            geometria = ler_kmz(Path(args.kmz))
        except (OSError, ValueError, ET.ParseError) as erro:
            print(f"ERRO ao ler '{args.kmz}': {erro}")
            return 1
        w, s, e, n = bbox_do_geojson(geometria)
        clat, clon = (s + n) / 2, (w + e) / 2
        area_ha = area_ha_do_poligono(geometria)
        municipio = args.municipio_doc or "(não informado)"
        data = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"origem": "KMZ", "nome": args.nome},
             "geometry": geometria}]}
        sufixo = re.sub(r"[^a-z0-9]+", "-", args.nome.lower()).strip("-")[:24]

        # Sem recibo não há o que conferir com papel — então a ferramenta confere
        # o que dá sozinha: se a fazenda caiu perto das outras e se o tamanho faz
        # sentido. Lat/lon trocada ou arquivo errado aparece justamente aqui.
        longe = km_da_fazenda_mais_perto(clat, clon, cfg["fazendas"])
        if longe is not None and longe > LONGE_DEMAIS_KM and not args.fazenda_longe:
            problemas.append(
                f"fica a {longe:.0f} km da fazenda cadastrada mais próxima. Isso costuma "
                f"ser arquivo errado ou latitude/longitude trocada. Se ela fica mesmo lá, "
                f"rode de novo com --fazenda-longe")
        if not 1 <= area_ha <= 100_000:
            problemas.append(f"área calculada de {area_ha:.1f} ha não parece divisa de fazenda")
        # Talhões separados no mesmo arquivo: o retângulo abraça todos e engole a
        # terra alheia no meio, que passa a valer como "DENTRO da divisa" — a
        # categoria que manda correr. Já vi inflar 95x sem nenhum aviso.
        area_retangulo = ((n - s) * KM_POR_GRAU_LAT) * ((e - w) * 111.320 *
                          math.cos(math.radians(clat))) * 100  # km² -> ha
        if area_ha > 0 and area_retangulo / area_ha > INCHACO_MAXIMO_BBOX:
            problemas.append(
                f"o retângulo da divisa ({area_retangulo:.0f} ha) ficou "
                f"{area_retangulo / area_ha:.0f}x maior que a área desenhada ({area_ha:.0f} ha). "
                f"O arquivo provavelmente tem talhões separados — cadastre um por vez, "
                f"senão o robô vai tratar a terra do vizinho no meio como sua")
        if args.area_doc and abs(area_ha - args.area_doc) / args.area_doc * 100 > TOLERANCIA_AREA_PCT:
            problemas.append(f"área do KMZ {area_ha:.2f} ha difere mais de "
                             f"{TOLERANCIA_AREA_PCT}% do informado {args.area_doc:.2f} ha")
    else:
        car_formatado = args.car.strip().upper()
        car_limpo = car_formatado.replace(".", "")
        uf = car_limpo.split("-")[0]

        data = buscar_poligono(car_limpo, uf)
        feats = data.get("features", [])
        if not feats:
            print(f"ERRO: nenhum imovel encontrado no SICAR para {car_limpo}")
            return 1
        if len(feats) > 1:
            print(f"ERRO: {len(feats)} imoveis para o mesmo codigo (inesperado) — verificar manualmente:")
            for f in feats:
                pr = f["properties"]
                print(f"  {pr['cod_imovel']} | {pr['municipio']} | {pr['area']} ha")
            return 2

        feat = feats[0]
        pr = feat["properties"]
        geometria = feat["geometry"]
        area_ha = pr["area"]
        municipio = f"{pr['municipio']}/{pr['uf']}"
        sufixo = f"{uf}-{car_limpo[-4:]}"  # mantém o padrão dos arquivos já existentes

        if args.municipio_doc and pr["municipio"].strip().lower() != args.municipio_doc.strip().lower():
            problemas.append(f"municipio do banco '{pr['municipio']}' != recibo '{args.municipio_doc}'")

        if args.area_doc:
            diff_pct = abs(pr["area"] - args.area_doc) / args.area_doc * 100
            if diff_pct > TOLERANCIA_AREA_PCT:
                problemas.append(
                    f"area do banco {pr['area']:.2f} ha difere {diff_pct:.1f}% do recibo {args.area_doc:.2f} ha"
                )

        w, s, e, n = bbox_do_geojson(geometria)
        clat, clon = (s + n) / 2, (w + e) / 2

        if args.centro_doc:
            dlat_doc, dlon_doc = (float(x) for x in args.centro_doc.split(","))
            km_lon_doc = 111.320 * math.cos(math.radians(clat))
            dist_km = math.hypot((clat - dlat_doc) * KM_POR_GRAU_LAT, (clon - dlon_doc) * km_lon_doc)
            if dist_km > TOLERANCIA_CENTRO_KM:
                problemas.append(f"centro calculado a {dist_km:.1f} km do centroide do recibo (>{TOLERANCIA_CENTRO_KM} km)")

    if problemas:
        print(f"NAO CADASTRADA — '{args.nome}'. Divergencias:")
        for x in problemas:
            print(f"  - {x}")
        return 3

    km_lat = KM_POR_GRAU_LAT
    km_lon = 111.320 * math.cos(math.radians(clat))
    r4 = lambda v: round(v, 4)
    r5 = lambda v: round(v, 5)

    def anel_bbox(km):
        dlat, dlon = km / km_lat, km / km_lon
        return {"oeste": r4(w - dlon), "sul": r4(s - dlat), "leste": r4(e + dlon), "norte": r4(n + dlat)}

    km_urgente = (args.anel_urgente_km if args.anel_urgente_km is not None
                  else cfg.get("anel_urgente_km", cfg.get("anel_padrao_km", 5)))
    km_atencao = (args.anel_atencao_km if args.anel_atencao_km is not None
                  else cfg.get("anel_atencao_km", 10))

    arq_geo = DADOS / f"fazenda-{sufixo}.geojson"
    arq_geo.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    entrada = {
        "nome": args.nome,
        "car": car_formatado,
        "car_sem_pontos": car_limpo,
        "municipio": municipio,
        "area_ha": round(area_ha, 2),
        "centro": {"lat": r5(clat), "lon": r5(clon)},
        "bbox_fazenda": {"oeste": r5(w), "sul": r5(s), "leste": r5(e), "norte": r5(n)},
        f"bbox_vigilancia_anel_{int(km_urgente)}km": anel_bbox(km_urgente),
        f"bbox_vigilancia_anel_{int(km_atencao)}km": anel_bbox(km_atencao),
        "poligono_geojson": f"dados/{arq_geo.name}",
        "contato": {"nome": "", "telefone": ""},
        "ponto_de_agua": "",
        "ativa": True,
    }

    # Fazenda vinda de KMZ não tem CAR, então o que identifica ela é o nome.
    lista = cfg["fazendas"]
    if car_limpo:
        idx = next((i for i, f in enumerate(lista) if f.get("car_sem_pontos") == car_limpo), None)
    else:
        idx = next((i for i, f in enumerate(lista) if f.get("nome") == args.nome), None)
    acao = "atualizada" if idx is not None else "cadastrada"
    if idx is not None:
        entrada["contato"] = lista[idx].get("contato", entrada["contato"])
        entrada["ponto_de_agua"] = lista[idx].get("ponto_de_agua", "")
        lista[idx] = entrada
    else:
        lista.append(entrada)
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"OK ({acao}): {args.nome} | {municipio} | {area_ha:.1f} ha | centro {r5(clat)},{r5(clon)}")
    if not car_limpo:
        print(f"  Origem: KMZ (sem CAR). Confira no mapa do painel se a divisa bateu.")
        print(f"  Anéis: {int(km_urgente)} km (urgente) e {int(km_atencao)} km (atenção).")
        print(f"  FALTA preencher contato e ponto de água desta fazenda em config/fazendas.json")
    # O vigia lê o config UMA vez, na partida. Sem reiniciar, esta fazenda aparece
    # no painel como se estivesse vigiada e NÃO está — a pior mentira possível.
    print()
    print("  " + "!" * 66)
    print("  !!  REINICIE O VIGIA AGORA, senão esta fazenda NÃO está sendo vigiada.")
    print("  !!  Ele só lê este arquivo quando sobe. Até reiniciar, o painel mostra")
    print("  !!  a fazenda mas o robô não olha para ela.")
    print("  !!  Windows: schtasks /end /tn VigiaFogo  &&  schtasks /run /tn VigiaFogo")
    print("  " + "!" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
