"""Testes da leitura de KMZ/KML do tools/cadastrar_fazenda.py.

Por que existe: trocar latitude por longitude aqui não dá erro nenhum — só põe a
fazenda no lugar errado do mapa, e o robô passa a vigiar terra de estranho.

Rodar: python -m unittest discover -s tests -v
"""

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import cadastrar_fazenda as cad  # noqa: E402


# Quadrado de ~1,1 km de lado no cerrado. KML guarda "lon,lat,altura".
KML_QUADRADO = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark><Polygon>
<outerBoundaryIs><LinearRing><coordinates>
-47.90,-16.50,0 -47.89,-16.50,0 -47.89,-16.49,0 -47.90,-16.49,0 -47.90,-16.50,0
</coordinates></LinearRing></outerBoundaryIs>
</Polygon></Placemark></Document></kml>"""

KML_SEM_POLIGONO = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark>
<Point><coordinates>-47.90,-16.50,0</coordinates></Point>
</Placemark></Document></kml>"""


class TestLeituraKml(unittest.TestCase):
    def _arquivo(self, nome, conteudo, zipar=False):
        pasta = Path(tempfile.mkdtemp())
        if zipar:
            caminho = pasta / nome
            with zipfile.ZipFile(caminho, "w") as z:
                z.writestr("doc.kml", conteudo)
        else:
            caminho = pasta / nome
            caminho.write_text(conteudo, encoding="utf-8")
        return caminho

    def test_le_kml_solto(self):
        geom = cad.ler_kmz(self._arquivo("a.kml", KML_QUADRADO))
        self.assertEqual(geom["type"], "Polygon")
        self.assertEqual(len(geom["coordinates"][0]), 5)

    def test_le_kmz_zipado(self):
        geom = cad.ler_kmz(self._arquivo("a.kmz", KML_QUADRADO, zipar=True))
        self.assertEqual(geom["type"], "Polygon")

    def test_ordem_lon_lat_preservada(self):
        """O erro clássico: KML é lon,lat e GeoJSON também — não pode inverter."""
        geom = cad.ler_kmz(self._arquivo("a.kml", KML_QUADRADO))
        lon, lat = geom["coordinates"][0][0]
        self.assertAlmostEqual(lon, -47.90, places=4)   # longitude ~ -47
        self.assertAlmostEqual(lat, -16.50, places=4)   # latitude ~ -19

    def test_altura_e_descartada(self):
        geom = cad.ler_kmz(self._arquivo("a.kml", KML_QUADRADO))
        self.assertTrue(all(len(p) == 2 for p in geom["coordinates"][0]))

    def test_arquivo_sem_poligono_da_erro_claro(self):
        with self.assertRaises(ValueError) as ctx:
            cad.ler_kmz(self._arquivo("a.kml", KML_SEM_POLIGONO))
        self.assertIn("polígono", str(ctx.exception).lower())

    def test_area_bate_com_o_esperado(self):
        """Quadrado de ~0,01 grau de lado no cerrado ≈ 116 ha."""
        geom = cad.ler_kmz(self._arquivo("a.kml", KML_QUADRADO))
        self.assertAlmostEqual(cad.area_ha_do_poligono(geom), 116, delta=6)

    def test_bbox_do_geojson_bate(self):
        geom = cad.ler_kmz(self._arquivo("a.kml", KML_QUADRADO))
        w, s, e, n = cad.bbox_do_geojson(geom)
        self.assertAlmostEqual(w, -47.90, places=4)
        self.assertAlmostEqual(e, -47.89, places=4)
        self.assertAlmostEqual(s, -16.50, places=4)
        self.assertAlmostEqual(n, -16.49, places=4)


class TestDistanciaDasOutras(unittest.TestCase):
    OUTRAS = [{"centro": {"lat": -16.48, "lon": -47.93}}]

    def test_vizinha_e_aceita(self):
        self.assertLess(cad.km_da_fazenda_mais_perto(-16.45, -47.90, self.OUTRAS), 10)

    def test_fazenda_muito_longe_e_flagrada(self):
        """uma fazenda a centenas de km das outras — tem que aparecer, não passar batido."""
        self.assertGreater(cad.km_da_fazenda_mais_perto(-21.34, -47.73, self.OUTRAS), 150)

    def test_sem_outras_fazendas_nao_estoura(self):
        self.assertIsNone(cad.km_da_fazenda_mais_perto(-16.5, -47.9, []))


if __name__ == "__main__":
    unittest.main()
