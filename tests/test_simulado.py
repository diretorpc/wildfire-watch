"""Testes do tools/simulado.py — a lógica que escolhe onde fazer o teste.

Por que existe: se o simulado escolher o alvo errado ou montar a divisa torta, ele
"passa" sem testar nada — e aí a gente entra em agosto confiando num teste que
nunca provou coisa alguma. Teste que mente é pior que teste que falta.

Rodar: python -m unittest discover -s tests -v
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "tools"))

import simulado  # noqa: E402
import vigia  # noqa: E402


def foco(lat, lon, data="2026-07-21 17:00:00"):
    return {"lat": lat, "lon": lon, "satelite": "GOES-19", "data": data}


class TestEscolhaDoAlvo(unittest.TestCase):
    def test_pega_o_maior_aglomerado(self):
        focos = [foco(-3.0, -60.0)] + [foco(-11.5 + i * 0.01, -50.5) for i in range(9)]
        lat, lon, grupo = simulado.melhor_alvo(focos)
        self.assertEqual(len(grupo), 9)
        self.assertAlmostEqual(lat, -11.46, delta=0.1)

    def test_sem_focos_devolve_nada(self):
        self.assertIsNone(simulado.melhor_alvo([]))

    def test_perto_de_filtra_por_raio(self):
        longe = [foco(-11.5, -50.5) for _ in range(20)]   # aglomerado grande, longe
        perto = [foco(-16.61, -47.64), foco(-16.62, -47.64)]
        lat, lon, grupo = simulado.melhor_alvo(longe + perto, perto_de=(-16.75, -47.93), raio_km=100)
        self.assertEqual(len(grupo), 2)          # ignorou o aglomerado grande de fora
        self.assertAlmostEqual(lat, -16.615, delta=0.02)

    def test_sem_fogo_no_raio_devolve_nada(self):
        """Não achar fogo perto NÃO é falha do robô — e o script precisa saber a diferença."""
        self.assertIsNone(simulado.melhor_alvo([foco(-11.5, -50.5)],
                                               perto_de=(-16.75, -47.93), raio_km=50))

    def test_resultado_nao_depende_da_ordem_de_leitura(self):
        focos = [foco(-11.5 + i * 0.01, -50.5) for i in range(5)] + [foco(-3.0, -60.0)]
        a = simulado.melhor_alvo(focos)
        b = simulado.melhor_alvo(list(reversed(focos)))
        self.assertAlmostEqual(a[0], b[0], places=6)
        self.assertAlmostEqual(a[1], b[1], places=6)


class TestFazendaSimulada(unittest.TestCase):
    def test_o_fogo_cai_DENTRO_da_divisa(self):
        """Se a divisa não cobrir o foco, o simulado testaria o caminho errado."""
        f = simulado.fazenda_simulada(-11.5, -50.5)
        self.assertEqual(vigia.classificar_foco(foco(-11.5, -50.5), f), "dentro")

    def test_aneis_ficam_na_ordem_certa(self):
        f = simulado.fazenda_simulada(-11.5, -50.5)
        aneis = vigia.aneis_vigilancia(f)
        self.assertEqual([km for km, _ in aneis], [5, 10])
        interno, externo = aneis[0][1], aneis[1][1]
        self.assertGreater(interno["oeste"], externo["oeste"])   # 5 km cabe dentro do 10 km
        self.assertLess(interno["leste"], externo["leste"])

    def test_passa_pela_validacao_de_partida_do_vigia(self):
        f = simulado.fazenda_simulada(-11.5, -50.5)
        env = {c: "ok" for c in vigia.CHAVES_ENV_OBRIGATORIAS}
        env["SMTP_PORT"] = "587"
        self.assertEqual(vigia.problemas_de_partida(env, [f]), [])

    def test_gera_alerta_urgente_com_bloco_de_seguranca(self):
        f = simulado.fazenda_simulada(-11.5, -50.5)
        atingidas = vigia.verificar_focos([foco(-11.5, -50.5)], [f])
        assunto, corpo = vigia.montar_email(atingidas, [f])
        self.assertIn("🔥 FOGO", assunto)
        self.assertIn("Nunca vá sozinho", corpo)
        self.assertIn("193", corpo)


class TestArgumentoDeCoordenada(unittest.TestCase):
    """Coordenada no Brasil começa com '-', e o leitor de argumentos tropeça nisso."""

    def test_junta_a_coordenada_negativa_na_opcao(self):
        self.assertEqual(simulado.consertar_argv(["--perto-de", "-16.75,-47.93"]),
                         ["--perto-de=-16.75,-47.93"])

    def test_nao_mexe_no_que_ja_esta_certo(self):
        argv = ["--perto-de=-16.75,-47.93", "--sem-email"]
        self.assertEqual(simulado.consertar_argv(argv), argv)

    def test_nao_engole_a_opcao_seguinte(self):
        """'--perto-de --sem-email' é erro do usuário e tem que continuar sendo erro."""
        argv = ["--perto-de", "--sem-email"]
        self.assertEqual(simulado.consertar_argv(argv), argv)

    def test_preserva_as_outras_opcoes(self):
        self.assertEqual(
            simulado.consertar_argv(["--sem-email", "--perto-de", "-16.7,-47.9", "--raio-km", "50"]),
            ["--sem-email", "--perto-de=-16.7,-47.9", "--raio-km", "50"])


class TestAtrasoDoDado(unittest.TestCase):
    AGORA = datetime(2026, 7, 21, 20, 0, tzinfo=timezone.utc)

    def test_calcula_a_idade_da_deteccao(self):
        m = simulado.atraso_minutos(foco(0, 0, "2026-07-21 17:30:00"), self.AGORA)
        self.assertAlmostEqual(m, 150, delta=1)   # 2h30 — o lag dos satélites finos

    def test_aceita_formato_iso(self):
        m = simulado.atraso_minutos(foco(0, 0, "2026-07-21T19:00:00"), self.AGORA)
        self.assertAlmostEqual(m, 60, delta=1)

    def test_hora_ilegivel_nao_estoura(self):
        self.assertIsNone(simulado.atraso_minutos(foco(0, 0, "???"), self.AGORA))


if __name__ == "__main__":
    unittest.main()
