"""Testes das regras do vigia (funções puras + fluxo do ciclo com fakes).

Rodar: python -m unittest discover -s tests -v
"""

import json
import smtplib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import vigia  # noqa: E402


BBOX = {"oeste": -48.0, "sul": -16.6, "leste": -47.8, "norte": -16.4}

# Fazenda de teste com os dois anéis (interno 5 km, externo 10 km).
FARM = {
    "nome": "A",
    "centro": {"lat": -16.50, "lon": -47.90},
    "bbox_fazenda": {"oeste": -47.92, "sul": -16.52, "leste": -47.88, "norte": -16.48},
    "bbox_vigilancia_anel_5km": {"oeste": -47.97, "sul": -16.565, "leste": -47.83, "norte": -16.435},
    "bbox_vigilancia_anel_10km": {"oeste": -48.01, "sul": -16.61, "leste": -47.79, "norte": -16.39},
    "contato": {"nome": "Zé", "telefone": "(34) 99999-0000"},
    "ponto_de_agua": "represa atrás do curral",
}


def foco(lat, lon, data="2026-07-19 22:30:00"):
    return {"lat": lat, "lon": lon, "satelite": "GOES-19", "data": data}


class TestParseFocos(unittest.TestCase):
    def test_valores_com_espaco_a_esquerda(self):
        csv = "lat,lon,satelite,data\n -16.5, -47.9,GOES-19, 2026/07/19 22:30:00"
        focos, puladas = vigia.parse_focos(csv)
        self.assertEqual(len(focos), 1)
        self.assertEqual(puladas, 0)
        self.assertAlmostEqual(focos[0]["lat"], -16.5)

    def test_linha_malformada_e_pulada_sem_derrubar(self):
        csv = ("lat,lon,satelite,data\n-16.5,-47.9,GOES-19,x\nbanana,-47.9,G,x\n-16.5,-47.9\n")
        focos, puladas = vigia.parse_focos(csv)
        self.assertEqual((len(focos), puladas), (1, 2))

    def test_cabecalho_nao_conta_como_pulada_mesmo_com_espaco(self):
        self.assertEqual(vigia.parse_focos(" lat, lon, satelite, data\n"), ([], 0))


class TestSelecaoDeArquivos(unittest.TestCase):
    NOMES = [f"focos_10min_20260719_{h:02d}00.csv" for h in range(10)]

    def test_primeira_execucao_pega_so_o_mais_recente(self):
        self.assertEqual(vigia.selecionar_novos(self.NOMES, ""), ([self.NOMES[-1]], 0))

    def test_execucao_normal_pega_so_os_mais_novos(self):
        novos, _ = vigia.selecionar_novos(self.NOMES, self.NOMES[6])
        self.assertEqual(novos, self.NOMES[7:])

    def test_backlog_longo_e_limitado(self):
        novos, descartados = vigia.selecionar_novos(self.NOMES, self.NOMES[0])
        self.assertEqual(len(novos), vigia.MAX_ARQUIVOS_POR_CICLO)
        self.assertEqual(descartados, 9 - vigia.MAX_ARQUIVOS_POR_CICLO)


class TestHoraDoArquivo(unittest.TestCase):
    def test_hora_extraida_do_nome(self):
        self.assertEqual(vigia.hora_do_nome_csv("focos_10min_20260720_0020.csv"), "2026-07-20 00:20:00")
        self.assertEqual(vigia.hora_do_nome_csv("outro.csv"), "")

    def test_data_sem_hora_completada_e_com_hora_preservada(self):
        fs = [foco(0, 0, "2026-07-20"), foco(0, 0, "2026-07-20 03:10:00")]
        vigia.aplicar_hora_do_arquivo(fs, "focos_10min_20260720_0020.csv")
        self.assertEqual(fs[0]["data"], "2026-07-20 00:20:00")
        self.assertEqual(fs[1]["data"], "2026-07-20 03:10:00")


class TestGeometria(unittest.TestCase):
    def test_dentro_fora_borda(self):
        self.assertTrue(vigia.dentro_bbox(-16.5, -47.9, BBOX))
        self.assertFalse(vigia.dentro_bbox(-16.5, -47.7, BBOX))
        self.assertTrue(vigia.dentro_bbox(-16.4, -48.0, BBOX))  # borda conta

    def test_distancia(self):
        self.assertEqual(vigia.distancia_km_ate_bbox(-16.5, -47.9, BBOX), 0.0)
        d = vigia.distancia_km_ate_bbox(-16.3, -47.9, BBOX)
        self.assertAlmostEqual(d, 0.1 * vigia.KM_POR_GRAU_LAT, delta=0.2)

    def test_aneis_ordenados_e_externo(self):
        aneis = vigia.aneis_vigilancia(FARM)
        self.assertEqual([km for km, _ in aneis], [5, 10])
        self.assertEqual(vigia.bbox_vigilancia(FARM), FARM["bbox_vigilancia_anel_10km"])
        with self.assertRaises(KeyError):
            vigia.aneis_vigilancia({"nome": "sem anel"})


class TestGravidade(unittest.TestCase):
    def test_dentro(self):
        self.assertEqual(vigia.classificar_foco(foco(-16.50, -47.90), FARM), "dentro")

    def test_urgente_anel_5km(self):
        self.assertEqual(vigia.classificar_foco(foco(-16.50, -47.95), FARM), "urgente")

    def test_atencao_anel_10km(self):
        self.assertEqual(vigia.classificar_foco(foco(-16.50, -47.99), FARM), "atencao")

    def test_fora_de_tudo(self):
        self.assertIsNone(vigia.classificar_foco(foco(-16.50, -48.05), FARM))

    def test_verificar_focos_traz_gravidade_rumo_distancia(self):
        atingidas = vigia.verificar_focos([foco(-16.50, -47.99)], [FARM])
        self.assertEqual(list(atingidas), ["A"])
        hit = atingidas["A"][0]
        self.assertEqual(hit["gravidade"], "atencao")
        self.assertEqual(hit["rumo"], "Oeste")
        self.assertGreater(hit["dist_km"], 0)

    def test_pior_gravidade(self):
        self.assertEqual(vigia.pior_gravidade(["atencao", "urgente"]), "urgente")
        self.assertEqual(vigia.pior_gravidade(["atencao", "dentro", "urgente"]), "dentro")


class TestAgrupamento(unittest.TestCase):
    def _hit(self, lat, lon, grav="urgente", dist=3.0):
        return {"foco": foco(lat, lon), "gravidade": grav, "dist_km": dist, "rumo": "Norte"}

    def test_distancia_km_pontos(self):
        # 0.1 grau de latitude ≈ 11.06 km
        d = vigia.distancia_km_pontos(-16.50, -47.90, -16.40, -47.90)
        self.assertAlmostEqual(d, 0.1 * vigia.KM_POR_GRAU_LAT, delta=0.2)
        self.assertEqual(vigia.distancia_km_pontos(-16.5, -47.9, -16.5, -47.9), 0.0)

    def test_tres_focos_colados_viram_um(self):
        hits = [self._hit(-16.50, -47.90), self._hit(-16.505, -47.905), self._hit(-16.51, -47.90)]
        reps = vigia.agrupar_hits(hits, raio_km=2.0)
        self.assertEqual(len(reps), 1)
        self.assertEqual(reps[0]["n_focos"], 3)

    def test_dois_focos_distantes_ficam_separados(self):
        hits = [self._hit(-16.50, -47.90), self._hit(-16.50, -47.80)]  # ~10 km
        reps = vigia.agrupar_hits(hits, raio_km=2.0)
        self.assertEqual(len(reps), 2)
        self.assertTrue(all(r["n_focos"] == 1 for r in reps))

    def test_representante_e_a_pior_gravidade(self):
        hits = [self._hit(-16.50, -47.90, "atencao", 8.0), self._hit(-16.505, -47.905, "urgente", 3.0)]
        reps = vigia.agrupar_hits(hits, raio_km=2.0)
        self.assertEqual(len(reps), 1)
        self.assertEqual(reps[0]["gravidade"], "urgente")
        self.assertEqual(reps[0]["n_focos"], 2)

    def test_empate_gravidade_vence_o_mais_perto(self):
        hits = [self._hit(-16.50, -47.90, "urgente", 4.5), self._hit(-16.505, -47.905, "urgente", 1.2)]
        reps = vigia.agrupar_hits(hits, raio_km=2.0)
        self.assertEqual(reps[0]["dist_km"], 1.2)

    def test_foco_unico_passa_com_n1(self):
        reps = vigia.agrupar_hits([self._hit(-16.5, -47.9)])
        self.assertEqual((len(reps), reps[0]["n_focos"]), (1, 1))

    def test_fila_de_focos_vira_um_incendio_independente_da_ordem(self):
        # A–B–C em linha, ~1,3 km entre vizinhos (A–C ~2,6 km > raio): fogo comprido.
        import itertools
        base = [self._hit(-16.50, -47.90), self._hit(-16.512, -47.90), self._hit(-16.524, -47.90)]
        contagens = set()
        for ordem in itertools.permutations(base):
            reps = vigia.agrupar_hits(list(ordem), raio_km=2.0)
            contagens.add((len(reps), reps[0]["n_focos"]))
        # single-linkage: sempre 1 incêndio com 3 detecções, em qualquer ordem
        self.assertEqual(contagens, {(1, 3)})

    def test_dois_incendios_ligados_por_ponte_nao_super_fundem_alem_do_par(self):
        # dois focos a 3 km NÃO se juntam sem um foco-ponte no meio
        reps = vigia.agrupar_hits([self._hit(-16.50, -47.90), self._hit(-16.527, -47.90)], raio_km=2.0)
        self.assertEqual(len(reps), 2)

    def test_nao_muta_os_hits_originais(self):
        hits = [self._hit(-16.5, -47.9)]
        vigia.agrupar_hits(hits)
        self.assertNotIn("n_focos", hits[0])  # o original fica intacto


class TestRumoCardeal(unittest.TestCase):
    C = (-16.50, -47.90)

    def test_oito_direcoes(self):
        r = lambda la, lo: vigia.rumo_cardeal(*self.C, la, lo)
        self.assertEqual(r(-16.40, -47.90), "Norte")
        self.assertEqual(r(-16.60, -47.90), "Sul")
        self.assertEqual(r(-16.50, -47.80), "Leste")
        self.assertEqual(r(-16.50, -48.00), "Oeste")
        self.assertEqual(r(-16.45, -47.85), "Nordeste")
        self.assertEqual(r(-16.55, -47.85), "Sudeste")
        self.assertEqual(r(-16.55, -47.95), "Sudoeste")
        self.assertEqual(r(-16.45, -47.95), "Noroeste")

    def test_centro(self):
        self.assertEqual(vigia.rumo_cardeal(*self.C, *self.C), "no centro")


class TestCooldownEEscalada(unittest.TestCase):
    AGORA = datetime(2026, 7, 19, 18, 0, tzinfo=vigia.FUSO_BRASILIA)

    def _iso(self, min_atras):
        return (self.AGORA - timedelta(minutes=min_atras)).isoformat()

    def test_cooldown_basico(self):
        self.assertFalse(vigia.cooldown_ativo("", self.AGORA))
        self.assertTrue(vigia.cooldown_ativo(self._iso(30), self.AGORA))
        self.assertFalse(vigia.cooldown_ativo(self._iso(61), self.AGORA))
        self.assertFalse(vigia.cooldown_ativo("nao-e-data", self.AGORA))

    def test_timestamp_sem_fuso_e_futuro(self):
        self.assertTrue(vigia.cooldown_ativo("2026-07-19T17:45:00", self.AGORA))
        self.assertFalse(vigia.cooldown_ativo(self._iso(-120), self.AGORA))

    def test_sem_registro_alerta(self):
        self.assertTrue(vigia.deve_alertar(None, "atencao", self.AGORA))

    def test_registro_antigo_str_fora_do_cooldown(self):
        self.assertTrue(vigia.deve_alertar(self._iso(61), "urgente", self.AGORA))

    def test_registro_str_no_cooldown_nao_re_alerta(self):
        self.assertFalse(vigia.deve_alertar(self._iso(30), "urgente", self.AGORA))

    def test_escalada_re_alerta_no_cooldown(self):
        reg = {"em": self._iso(30), "grav": "atencao"}
        self.assertTrue(vigia.deve_alertar(reg, "urgente", self.AGORA))  # fogo chegou perto

    def test_mesma_gravidade_no_cooldown_nao_re_alerta(self):
        reg = {"em": self._iso(30), "grav": "urgente"}
        self.assertFalse(vigia.deve_alertar(reg, "urgente", self.AGORA))

    def test_gravidade_menor_no_cooldown_nao_re_alerta(self):
        reg = {"em": self._iso(30), "grav": "urgente"}
        self.assertFalse(vigia.deve_alertar(reg, "atencao", self.AGORA))

    def test_cooldown_expirado_re_alerta_mesma_gravidade(self):
        reg = {"em": self._iso(61), "grav": "atencao"}
        self.assertTrue(vigia.deve_alertar(reg, "atencao", self.AGORA))

    def test_gravidade_corrompida_no_estado_nao_estoura(self):
        # grav desconhecida (estado editado à mão) não pode levantar KeyError
        reg = {"em": self._iso(30), "grav": "xpto"}
        self.assertIsInstance(vigia.deve_alertar(reg, "urgente", self.AGORA), bool)
        reg2 = {"em": self._iso(30), "grav": "urgente"}
        self.assertIsInstance(vigia.deve_alertar(reg2, "xpto", self.AGORA), bool)


class TestEnv(unittest.TestCase):
    def test_parser_comentario_trim_bom_aspas(self):
        with tempfile.TemporaryDirectory() as tmp:
            arq = Path(tmp) / ".env"
            arq.write_bytes('# c\nSMTP_HOST = "smtp.x.com" \nVAZIA=\n'.encode("utf-8-sig"))
            env = vigia.carregar_env(arq)
        self.assertEqual(env["SMTP_HOST"], "smtp.x.com")
        self.assertEqual(env["VAZIA"], "")

    def test_env_inexistente(self):
        self.assertEqual(vigia.carregar_env(Path("nao/existe/.env")), {})

    def test_chaves_faltando(self):
        env = {c: "ok" for c in vigia.CHAVES_ENV_OBRIGATORIAS}
        self.assertEqual(vigia.chaves_faltando(env), [])
        env["SMTP_PASSWORD"] = "preencher-x"
        self.assertEqual(vigia.chaves_faltando(env), ["SMTP_PASSWORD"])

    def test_problemas_de_partida(self):
        env = {c: "ok" for c in vigia.CHAVES_ENV_OBRIGATORIAS}
        env["SMTP_PORT"] = "587 # porta"
        problemas = vigia.problemas_de_partida(env, [{"nome": "Sem tudo"}])
        self.assertTrue(any("SMTP_PORT" in p for p in problemas))
        self.assertTrue(any("bbox_fazenda" in p for p in problemas))
        self.assertTrue(any("vigil" in p.lower() for p in problemas))
        env["SMTP_PORT"] = "587"
        self.assertEqual(vigia.problemas_de_partida(env, [FARM]), [])


class TestEstado(unittest.TestCase):
    def test_carregar_estado_tolerante(self):
        padrao = {"ultimo_csv": "", "ultimo_alerta": {},
                  "resumo_data": "", "alertas_desde_resumo": 0,
                  "ciclos_ok_desde_resumo": 0, "ciclos_falha_desde_resumo": 0,
                  "falhas_seguidas": 0, "alerta_cego_em": "",
                  "observacao_desde_resumo": 0}
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.assertEqual(vigia.carregar_estado(base / "nada.json"), padrao)
            (base / "lixo.json").write_text("{quebrado", encoding="utf-8")
            self.assertEqual(vigia.carregar_estado(base / "lixo.json"), padrao)
            (base / "lista.json").write_text("[1,2]", encoding="utf-8")
            self.assertEqual(vigia.carregar_estado(base / "lista.json"), padrao)

    def test_salvar_recarregar(self):
        with tempfile.TemporaryDirectory() as tmp:
            caminho = Path(tmp) / "sub" / "estado.json"
            estado = {"ultimo_csv": "a.csv", "ultimo_alerta": {"A": {"em": "x", "grav": "urgente"}},
                      "resumo_data": "2026-07-20", "alertas_desde_resumo": 2,
                      "ciclos_ok_desde_resumo": 41, "ciclos_falha_desde_resumo": 3,
                      "falhas_seguidas": 0, "alerta_cego_em": "2026-07-20T10:00:00-03:00",
                      "observacao_desde_resumo": 5}
            vigia.salvar_estado(estado, caminho)
            self.assertEqual(vigia.carregar_estado(caminho), estado)


class TestPainelEstado(unittest.TestCase):
    AGORA = datetime(2026, 7, 20, 15, 0, tzinfo=vigia.FUSO_BRASILIA)

    def test_grava_todas_fazendas_com_gravidade_e_hora(self):
        atingidas = {"A": [{
            "foco": foco(-16.50, -47.99, "2026-07-20 15:00:00"),
            "gravidade": "atencao", "dist_km": 8.3, "rumo": "Oeste",
        }]}
        outra = dict(FARM, nome="B")
        with tempfile.TemporaryDirectory() as tmp:
            caminho = Path(tmp) / "painel.json"
            vigia.salvar_painel_estado([FARM, outra], atingidas, self.AGORA, 7, caminho)
            d = json.loads(caminho.read_text(encoding="utf-8"))
        self.assertEqual(d["total_focos_brasil"], 7)
        self.assertTrue(d["heartbeat_local"].startswith("2026-07-20"))
        porname = {f["nome"]: f for f in d["fazendas"]}
        self.assertEqual(porname["A"]["gravidade_atual"], "atencao")
        self.assertEqual(porname["A"]["focos"][0]["dist_km"], 8.3)
        self.assertIn("Brasília", porname["A"]["focos"][0]["hora_local"])
        self.assertIsNone(porname["B"]["gravidade_atual"])  # sem fogo = verde
        self.assertEqual(porname["B"]["focos"], [])

    def test_heartbeat_cria_e_preserva(self):
        with tempfile.TemporaryDirectory() as tmp:
            caminho = Path(tmp) / "painel.json"
            vigia.carimbar_heartbeat(caminho)  # arquivo não existia
            d = json.loads(caminho.read_text(encoding="utf-8"))
            self.assertIn("heartbeat_local", d)
            self.assertEqual(d["fazendas"], [])
            # agora com conteúdo: heartbeat atualiza, resto preserva
            caminho.write_text(json.dumps({"heartbeat_local": "velho", "fazendas": [1],
                                           "total_focos_brasil": 3}), encoding="utf-8")
            vigia.carimbar_heartbeat(caminho)
            d = json.loads(caminho.read_text(encoding="utf-8"))
            self.assertNotEqual(d["heartbeat_local"], "velho")
            self.assertEqual(d["fazendas"], [1])
            self.assertEqual(d["total_focos_brasil"], 3)


class TestResumoDiario(unittest.TestCase):
    def _agora(self, hora):
        return datetime(2026, 7, 20, hora, 0, tzinfo=vigia.FUSO_BRASILIA)

    def test_manda_apos_horario_se_ainda_nao_hoje(self):
        self.assertTrue(vigia.deve_enviar_resumo(self._agora(18), 18, ""))
        self.assertTrue(vigia.deve_enviar_resumo(self._agora(20), 18, "2026-07-19"))

    def test_nao_manda_antes_do_horario(self):
        self.assertFalse(vigia.deve_enviar_resumo(self._agora(17), 18, ""))

    def test_nao_repete_no_mesmo_dia(self):
        self.assertFalse(vigia.deve_enviar_resumo(self._agora(19), 18, "2026-07-20"))

    def test_corpo_sem_alertas_vs_com_alertas(self):
        _, corpo0 = vigia.montar_resumo_diario([FARM, FARM, FARM], 0, self._agora(18), 100, 0)
        self.assertIn("Nenhum alerta", corpo0)
        self.assertIn("NÃO chegar", corpo0)
        assunto, corpo2 = vigia.montar_resumo_diario([FARM, FARM, FARM], 2, self._agora(18), 100, 0)
        self.assertIn("2 alerta(s)", corpo2)
        self.assertIn("resumo do dia", assunto)

    def test_dia_limpo_diz_quantas_vezes_olhou(self):
        _, corpo = vigia.montar_resumo_diario([FARM], 0, self._agora(18), 143, 0)
        self.assertIn("143", corpo)
        self.assertNotIn("cego", corpo.lower())

    def test_com_falhas_avisa_que_ficou_cego_parte_do_tempo(self):
        assunto, corpo = vigia.montar_resumo_diario([FARM], 0, self._agora(18), 90, 53)
        self.assertIn("⚠️", assunto + corpo)
        self.assertIn("90", corpo)
        self.assertIn("53", corpo)
        self.assertIn("NÃO estava vigiando", corpo)

    def test_sem_nenhum_ciclo_bom_nunca_diz_que_esta_tudo_certo(self):
        """O pior modo de falha: robô cego o dia inteiro dizendo 'tudo em ordem'."""
        assunto, corpo = vigia.montar_resumo_diario([FARM], 0, self._agora(18), 0, 143)
        self.assertNotIn("tudo em ordem", corpo)
        self.assertIn("🚨", assunto + corpo)
        self.assertIn("NÃO consegui dado do INPE", corpo)
        self.assertIn("não enxerguei", corpo.lower())

    def test_assunto_acompanha_a_saude(self):
        """O assunto é o que se lê de relance no celular — não pode disfarçar dia cego."""
        limpo, _ = vigia.montar_resumo_diario([FARM], 0, self._agora(18), 143, 0)
        parcial, _ = vigia.montar_resumo_diario([FARM], 0, self._agora(18), 90, 53)
        cego, _ = vigia.montar_resumo_diario([FARM], 0, self._agora(18), 0, 143)
        self.assertTrue(limpo.startswith("🌙"))
        self.assertTrue(parcial.startswith("⚠️"))
        self.assertTrue(cego.startswith("🚨"))
        self.assertIn("NÃO CONSEGUI VIGIAR", cego)


class TestSaudeDoVigia(unittest.TestCase):
    """A1 — 'vivo' não é a mesma coisa que 'enxergando'."""

    AGORA = datetime(2026, 7, 21, 15, 0, tzinfo=vigia.FUSO_BRASILIA)

    def test_dado_fresco_nao_e_velho(self):
        # 15:00 Brasília = 18:00 UTC; arquivo das 17:40 UTC = 20 min de atraso (normal)
        self.assertFalse(vigia.dado_esta_velho("focos_10min_20260721_1740.csv", self.AGORA))

    def test_dado_parado_ha_horas_e_velho(self):
        """INPE no ar mas sem publicar: sem isto o robô diria 'olhei' sem ter dado novo."""
        self.assertTrue(vigia.dado_esta_velho("focos_10min_20260721_1200.csv", self.AGORA))

    def test_nome_fora_do_padrao_nao_acusa_cegueira(self):
        self.assertFalse(vigia.dado_esta_velho("qualquer-coisa.csv", self.AGORA))

    def test_data_impossivel_no_nome_nao_derruba_o_ciclo(self):
        """Mês 13 casa com o padrão \\d{2} mas não é data — não pode estourar."""
        self.assertFalse(vigia.dado_esta_velho("focos_10min_20261332_2599.csv", self.AGORA))

    def test_status_desconhecido_conta_como_cego(self):
        """Erra pro lado seguro: o que não sabemos que enxergou, não enxergou."""
        estado = self._estado()
        vigia.registrar_saude(estado, "???", self.AGORA)
        self.assertEqual(estado["ciclos_falha_desde_resumo"], 1)
        self.assertEqual(estado["ciclos_ok_desde_resumo"], 0)

    def _estado(self):
        return dict(vigia.carregar_estado(Path("nao/existe.json")))

    def test_ciclo_bom_conta_e_zera_falhas_seguidas(self):
        estado = self._estado()
        estado["falhas_seguidas"] = 3
        self.assertFalse(vigia.registrar_saude(estado, "ok", self.AGORA))
        self.assertEqual(estado["ciclos_ok_desde_resumo"], 1)
        self.assertEqual(estado["falhas_seguidas"], 0)

    def test_poucas_falhas_ainda_nao_incomodam(self):
        estado = self._estado()
        for _ in range(vigia.CICLOS_CEGO_PARA_ALERTAR - 1):
            self.assertFalse(vigia.registrar_saude(estado, "cego", self.AGORA))
        self.assertEqual(estado["ciclos_falha_desde_resumo"], vigia.CICLOS_CEGO_PARA_ALERTAR - 1)

    def test_falhas_seguidas_demais_pedem_alerta(self):
        estado = self._estado()
        pedidos = [vigia.registrar_saude(estado, "cego", self.AGORA)
                   for _ in range(vigia.CICLOS_CEGO_PARA_ALERTAR)]
        self.assertTrue(pedidos[-1])
        self.assertEqual(sum(pedidos), 1)  # só o ciclo que cruzou o limite pediu

    def test_nao_repete_o_aviso_dentro_do_cooldown(self):
        estado = self._estado()
        estado["falhas_seguidas"] = vigia.CICLOS_CEGO_PARA_ALERTAR
        estado["alerta_cego_em"] = self.AGORA.isoformat()
        self.assertFalse(vigia.registrar_saude(estado, "cego", self.AGORA))

    def test_avisa_de_novo_quando_o_cooldown_vence(self):
        estado = self._estado()
        estado["falhas_seguidas"] = vigia.CICLOS_CEGO_PARA_ALERTAR
        velho = self.AGORA - timedelta(hours=vigia.COOLDOWN_CEGO_HORAS, minutes=1)
        estado["alerta_cego_em"] = velho.isoformat()
        self.assertTrue(vigia.registrar_saude(estado, "cego", self.AGORA))

    def test_email_de_cego_e_explicito_sobre_o_silencio(self):
        _, corpo = vigia.montar_email_cego(12, self.AGORA)
        self.assertIn("não estão sendo vigiadas", corpo.lower())
        self.assertIn("não estou enxergando", corpo.lower())
        self.assertIn("120", corpo)  # 12 ciclos x 10 min

    def test_avisar_se_cego_manda_email_e_marca_o_horario(self):
        estado = self._estado()
        estado["falhas_seguidas"] = vigia.CICLOS_CEGO_PARA_ALERTAR - 1
        enviados, salvos = [], []
        vigia.avisar_se_cego({}, estado, "cego",
                             enviar=lambda env, a, c: enviados.append(a),
                             salvar=lambda e, caminho=None: salvos.append(e))
        self.assertEqual(len(enviados), 1)
        self.assertTrue(estado["alerta_cego_em"])
        self.assertEqual(len(salvos), 1)  # saúde é gravada mesmo em ciclo que falhou

    def test_email_de_cego_que_nao_sai_nao_marca_horario(self):
        """Se nem o aviso saiu, ele precisa ser tentado de novo — não pode 'contar como dado'."""
        estado = self._estado()
        estado["falhas_seguidas"] = vigia.CICLOS_CEGO_PARA_ALERTAR - 1
        def quebra(env, a, c):
            raise smtplib.SMTPAuthenticationError(535, b"x")
        vigia.avisar_se_cego({}, estado, "cego", enviar=quebra,
                             salvar=lambda e, caminho=None: None)
        self.assertEqual(estado["alerta_cego_em"], "")


class TestTravaInstancia(unittest.TestCase):
    def test_segunda_copia_recusada_e_reaproveita_apos_liberar(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "vigia.lock"
            self.assertTrue(vigia.tentar_travar(lock))       # 1ª cópia pega
            try:
                self.assertFalse(vigia.tentar_travar(lock))  # 2ª cópia recusada
            finally:
                vigia.liberar_lock(lock)
            self.assertTrue(vigia.tentar_travar(lock))       # após liberar, pega de novo
            vigia.liberar_lock(lock)

    def test_liberar_sem_travar_nao_estoura(self):
        with tempfile.TemporaryDirectory() as tmp:
            vigia.liberar_lock(Path(tmp) / "inexistente.lock")  # não deve levantar


class TestEmail(unittest.TestCase):
    def test_hora_arquivo_para_brasilia(self):
        self.assertEqual(vigia.formatar_hora_local("2026-07-20 00:20:00"), "19/07/2026 21:20 (Brasília)")

    def test_hora_ilegivel(self):
        self.assertEqual(vigia.formatar_hora_local("???"), "??? (UTC)")

    def test_email_urgente(self):
        atingidas = {"A": [{"foco": foco(-16.51, -47.89), "gravidade": "urgente",
                            "dist_km": 1.2, "rumo": "Nordeste"}]}
        assunto, corpo = vigia.montar_email(atingidas, [FARM])
        self.assertEqual(assunto, "🔥 FOGO: A")
        self.assertIn("~1.2 km a Nordeste (URGENTE", corpo)
        self.assertIn("https://maps.google.com/?q=-16.51,-47.89", corpo)

    def test_email_atencao(self):
        atingidas = {"A": [{"foco": foco(-16.5, -47.99), "gravidade": "atencao",
                            "dist_km": 8.0, "rumo": "Oeste"}]}
        assunto, corpo = vigia.montar_email(atingidas, [FARM])
        self.assertTrue(assunto.startswith("⚠️ ATENÇÃO"))
        self.assertIn("chegando", corpo)

    def test_email_mostra_deteccoes_agrupadas(self):
        atingidas = {"A": [{"foco": foco(-16.5, -47.95), "gravidade": "urgente",
                            "dist_km": 3.0, "rumo": "Oeste", "n_focos": 4}]}
        _, corpo = vigia.montar_email(atingidas, [FARM])
        self.assertIn("4 detecções agrupadas", corpo)

    def test_email_dentro(self):
        atingidas = {"A": [{"foco": foco(-16.5, -47.9), "gravidade": "dentro",
                            "dist_km": 0.0, "rumo": "no centro"}]}
        _, corpo = vigia.montar_email(atingidas, [FARM])
        self.assertIn("DENTRO da divisa", corpo)

    def test_email_mistura_so_urgentes_no_assunto(self):
        atingidas = {
            "A": [{"foco": foco(-16.5, -47.95), "gravidade": "urgente", "dist_km": 3.0, "rumo": "Oeste"}],
            "B": [{"foco": foco(-16.5, -47.99), "gravidade": "atencao", "dist_km": 8.0, "rumo": "Oeste"}],
        }
        assunto, corpo = vigia.montar_email(atingidas, [FARM, dict(FARM, nome="B")])
        self.assertEqual(assunto, "🔥 FOGO: A")  # só A é urgente
        self.assertIn("■ B:", corpo)             # mas B aparece no corpo

    # --- A2: quem ligar, onde tem água, e como não morrer indo ver ---

    def test_email_traz_telefone_e_agua_da_fazenda(self):
        """Em emergência ninguém abre agenda nem procura arquivo de configuração."""
        atingidas = {"A": [{"foco": foco(-16.5, -47.9), "gravidade": "dentro",
                            "dist_km": 0.0, "rumo": "no centro"}]}
        _, corpo = vigia.montar_email(atingidas, [FARM])
        self.assertIn("(34) 99999-0000", corpo)
        self.assertIn("Zé", corpo)
        self.assertIn("represa atrás do curral", corpo)

    def test_email_cobra_telefone_que_falta_em_vez_de_calar(self):
        sem_contato = dict(FARM)
        del sem_contato["contato"]
        atingidas = {"A": [{"foco": foco(-16.5, -47.9), "gravidade": "dentro",
                            "dist_km": 0.0, "rumo": "no centro"}]}
        _, corpo = vigia.montar_email(atingidas, [sem_contato])
        self.assertIn("sem telefone cadastrado", corpo)

    def test_email_cobra_ponto_de_agua_que_falta(self):
        """Água é a 1ª coisa que o carro-pipa pergunta — faltar calado é pior."""
        sem_agua = dict(FARM, ponto_de_agua="")
        atingidas = {"A": [{"foco": foco(-16.5, -47.9), "gravidade": "dentro",
                            "dist_km": 0.0, "rumo": "no centro"}]}
        _, corpo = vigia.montar_email(atingidas, [sem_agua])
        self.assertIn("sem ponto de água cadastrado", corpo)

    def test_alerta_urgente_leva_aviso_de_seguranca(self):
        """O e-mail que manda correr é quem manda a pessoa pro perigo."""
        atingidas = {"A": [{"foco": foco(-16.5, -47.9), "gravidade": "dentro",
                            "dist_km": 0.0, "rumo": "no centro"}]}
        _, corpo = vigia.montar_email(atingidas, [FARM])
        self.assertIn("Nunca vá sozinho", corpo)
        self.assertIn("rota de fuga", corpo)
        self.assertIn("193", corpo)

    def test_alerta_de_atencao_nao_carrega_o_bloco_de_seguranca(self):
        """Foco a 8 km é aviso, não é ordem de sair de casa — não polui o e-mail."""
        atingidas = {"A": [{"foco": foco(-16.5, -47.99), "gravidade": "atencao",
                            "dist_km": 8.0, "rumo": "Oeste"}]}
        _, corpo = vigia.montar_email(atingidas, [FARM])
        self.assertNotIn("Nunca vá sozinho", corpo)
        self.assertIn("193", corpo)  # o telefone de emergência fica sempre


class TestZonaDeObservacao(unittest.TestCase):
    """Área que se OLHA mas não é nossa: aparece na tela, nunca manda e-mail."""

    ZONA = dict(FARM, nome="Cidade (observação)", apenas_observacao=True)

    def test_reconhece_a_zona(self):
        self.assertTrue(vigia.eh_zona_de_observacao(self.ZONA))
        self.assertFalse(vigia.eh_zona_de_observacao(FARM))

    def test_fazenda_sem_o_campo_continua_alertando(self):
        """Ausência do campo NUNCA pode calar uma fazenda de verdade."""
        self.assertFalse(vigia.eh_zona_de_observacao({"nome": "X"}))

    def test_painel_marca_gravidade_propria(self):
        """Na tela ela não pode acender vermelho de emergência — não é a sua terra."""
        atingidas = {self.ZONA["nome"]: [{
            "foco": foco(-16.50, -47.90), "gravidade": "dentro",
            "dist_km": 0.0, "rumo": "no centro"}]}
        with tempfile.TemporaryDirectory() as tmp:
            caminho = Path(tmp) / "painel.json"
            vigia.salvar_painel_estado([self.ZONA], atingidas,
                                       datetime(2026, 7, 21, 15, tzinfo=vigia.FUSO_BRASILIA),
                                       9, caminho)
            d = json.loads(caminho.read_text(encoding="utf-8"))
        self.assertEqual(d["fazendas"][0]["gravidade_atual"], "observacao")
        self.assertTrue(d["fazendas"][0]["apenas_observacao"])

    def test_resumo_diario_conta_os_avistamentos(self):
        agora = datetime(2026, 7, 21, 18, tzinfo=vigia.FUSO_BRASILIA)
        _, corpo = vigia.montar_resumo_diario([FARM], 0, agora, 100, 0, observacao=7)
        self.assertIn("7", corpo)
        self.assertIn("observação", corpo.lower())

    def test_resumo_sem_avistamento_nao_polui(self):
        agora = datetime(2026, 7, 21, 18, tzinfo=vigia.FUSO_BRASILIA)
        _, corpo = vigia.montar_resumo_diario([FARM], 0, agora, 100, 0, observacao=0)
        self.assertNotIn("observação", corpo.lower())


class TestCiclo(unittest.TestCase):
    CSV = "lat,lon,satelite,data\n -16.50, -47.90,GOES-19,2026-07-19\n"  # foco DENTRO de FARM
    ARQ1 = "focos_10min_20260719_1200.csv"
    ARQ2 = "focos_10min_20260719_1210.csv"

    def _env(self):
        env = {c: "ok" for c in vigia.CHAVES_ENV_OBRIGATORIAS}
        env["SMTP_PORT"] = "587"
        return env

    def _ciclo(self, estado, listar, baixar, enviar):
        return vigia.ciclo(self._env(), [FARM], estado, listar=listar, baixar=baixar,
                           enviar=enviar, salvar=lambda e, caminho=None: None,
                           salvar_painel=lambda *a, **k: None)

    def _agora_csv(self):
        """Nome de arquivo do INPE com hora de agora — dado 'fresco' para o teste."""
        return datetime.now(vigia.timezone.utc).strftime("focos_10min_%Y%m%d_%H%M.csv")

    def test_smtp_falhou_nao_avanca_estado(self):
        estado = {"ultimo_csv": "", "ultimo_alerta": {}}
        def quebra(env, a, c):
            raise smtplib.SMTPAuthenticationError(535, b"x")
        self._ciclo(estado, lambda u: [self.ARQ1], lambda u, n: self.CSV, quebra)
        self.assertEqual(estado["ultimo_csv"], "")
        self.assertEqual(estado["ultimo_alerta"], {})

    def test_sucesso_avanca_estado_e_guarda_gravidade(self):
        estado = {"ultimo_csv": self.ARQ1, "ultimo_alerta": {}}
        enviados = []
        self._ciclo(estado, lambda u: [self.ARQ1, self.ARQ2, self.ARQ2],
                    lambda u, n: self.CSV, lambda env, a, c: enviados.append((a, c)))
        self.assertEqual(len(enviados), 1)
        self.assertEqual(enviados[0][1].count("Mapa:"), 1)  # foco repetido = 1 linha
        self.assertEqual(estado["ultimo_csv"], self.ARQ2)
        self.assertEqual(estado["ultimo_alerta"]["A"]["grav"], "dentro")

    def test_cooldown_suprime_mas_avanca_estado(self):
        agora = vigia.datetime.now(vigia.FUSO_BRASILIA)
        estado = {"ultimo_csv": self.ARQ1,
                  "ultimo_alerta": {"A": {"em": agora.isoformat(), "grav": "dentro"}}}
        enviados = []
        self._ciclo(estado, lambda u: [self.ARQ2], lambda u, n: self.CSV,
                    lambda env, a, c: enviados.append(a))
        self.assertEqual(enviados, [])  # mesma gravidade dentro do cooldown
        self.assertEqual(estado["ultimo_csv"], self.ARQ2)

    def test_download_quebrado_pula_arquivo(self):
        estado = {"ultimo_csv": self.ARQ1, "ultimo_alerta": {}}
        def baixar(u, n):
            if n == self.ARQ2:
                return self.CSV
            raise OSError("404")
        self._ciclo(estado, lambda u: [self.ARQ2, "focos_10min_20260719_1220.csv"],
                    baixar, lambda env, a, c: None)
        self.assertEqual(estado["ultimo_csv"], "focos_10min_20260719_1220.csv")

    # --- A1: o ciclo precisa CONTAR se conseguiu olhar, não só logar num vazio ---

    def test_inpe_fora_do_ar_devolve_cego(self):
        def cai(u):
            raise OSError("connection refused")
        self.assertEqual(self._ciclo({"ultimo_csv": "", "ultimo_alerta": {}},
                                     cai, lambda u, n: self.CSV, lambda *a: None), "cego")

    def test_indice_vazio_devolve_cego(self):
        self.assertEqual(self._ciclo({"ultimo_csv": "", "ultimo_alerta": {}},
                                     lambda u: [], lambda u, n: self.CSV, lambda *a: None), "cego")

    def test_nenhum_download_deu_certo_devolve_cego(self):
        def quebra(u, n):
            raise OSError("404")
        estado = {"ultimo_csv": self.ARQ1, "ultimo_alerta": {}}
        self.assertEqual(self._ciclo(estado, lambda u: [self.ARQ2], quebra, lambda *a: None), "cego")

    def test_inpe_no_ar_mas_parado_devolve_cego(self):
        """Caso traiçoeiro: servidor responde, arquivo é de horas atrás."""
        estado = {"ultimo_csv": self.ARQ1, "ultimo_alerta": {}}
        self.assertEqual(self._ciclo(estado, lambda u: [self.ARQ1, self.ARQ2],
                                     lambda u, n: self.CSV, lambda *a: None), "cego")

    def test_dado_fresco_devolve_ok(self):
        arq = self._agora_csv()
        estado = {"ultimo_csv": "", "ultimo_alerta": {}}
        self.assertEqual(self._ciclo(estado, lambda u: [arq], lambda u, n: self.CSV,
                                     lambda *a: None), "ok")

    def test_zona_de_observacao_nunca_manda_email(self):
        """O teste que mais importa: terra que não é sua não pode gastar sua atenção."""
        zona = dict(FARM, nome="Cidade (observação)", apenas_observacao=True)
        estado = {"ultimo_csv": self.ARQ1, "ultimo_alerta": {}}
        enviados = []
        vigia.ciclo(self._env(), [zona], estado,
                    listar=lambda u: [self.ARQ2], baixar=lambda u, n: self.CSV,
                    enviar=lambda env, a, c: enviados.append(a),
                    salvar=lambda e, caminho=None: None, salvar_painel=lambda *a, **k: None)
        self.assertEqual(enviados, [])                       # nenhum e-mail
        self.assertEqual(estado["ultimo_csv"], self.ARQ2)    # mas o ciclo andou
        self.assertGreaterEqual(estado.get("observacao_desde_resumo", 0), 1)

    def test_fazenda_de_verdade_alerta_mesmo_com_zona_junto(self):
        """A zona não pode contaminar as fazendas reais no mesmo ciclo."""
        zona = dict(FARM, nome="Cidade (observação)", apenas_observacao=True)
        estado = {"ultimo_csv": self.ARQ1, "ultimo_alerta": {}}
        enviados = []
        vigia.ciclo(self._env(), [FARM, zona], estado,
                    listar=lambda u: [self.ARQ2], baixar=lambda u, n: self.CSV,
                    enviar=lambda env, a, c: enviados.append((a, c)),
                    salvar=lambda e, caminho=None: None, salvar_painel=lambda *a, **k: None)
        self.assertEqual(len(enviados), 1)
        self.assertIn("FOGO: A", enviados[0][0])              # só a fazenda real
        self.assertNotIn("observação", enviados[0][1])

    def test_sem_arquivo_novo_mas_dado_fresco_e_ok(self):
        """Não ter arquivo novo é normal (o INPE publica a cada 10 min) — não é cegueira."""
        arq = self._agora_csv()
        estado = {"ultimo_csv": arq, "ultimo_alerta": {}}
        self.assertEqual(self._ciclo(estado, lambda u: [arq], lambda u, n: self.CSV,
                                     lambda *a: None), "ok")


if __name__ == "__main__":
    unittest.main()
