/* Painel do vigia-fogo — desenha as fazendas sobre satélite e mostra os focos.
   Lê /config/fazendas.json (estático) uma vez e /dados/painel-estado.json (o que
   o robô grava) a cada 30 s. Nada de servidor pesado — é 1 tela, 1 PC. */

"use strict";

var LIMITE_PADRAO_MIN = 25;             // usado se o estado não trouxer o limite
var INTERVALO_ATUALIZA_MS = 30000;      // relê o estado a cada 30 s
// Cores da legenda (gravidade por proximidade):
var COR = { dentro: "#ff3b1f", urgente: "#ff8c1a", atencao: "#ffd21a", verde: "#aef06f",
            observacao: "#7fb3ff" };  // azul = so observacao, terra que nao e sua
var COR_DIVISA = "#ff3b1f";     // dentro da divisa = VERMELHO
var COR_ANEL_INT = "#ff8c1a";   // anel de 5 km = LARANJA
var COR_ANEL_EXT = "#ffd21a";   // anel de 10 km = AMARELO

// Escapa texto antes de ir pro HTML (nomes/contato vêm de arquivo — defesa simples).
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
  });
}
function num(v) { return Number(v) || 0; }  // coordenadas/distâncias: sempre número

// Idade em minutos de um instante ISO. Ilegível/ausente => Infinito (= "velho").
function idadeMin(iso) {
  var t = iso ? new Date(iso).getTime() : NaN;
  return isNaN(t) ? Infinity : (Date.now() - t) / 60000;
}

// Anéis de vigilância descobertos genericamente (funciona com 5/10 km ou outros).
function anelBboxes(f) {
  var out = [];
  Object.keys(f).forEach(function (k) {
    var m = k.match(/^bbox_vigilancia_anel_(\d+)km$/);
    if (m) { out.push({ km: parseInt(m[1], 10), bbox: f[k] }); }
  });
  out.sort(function (a, b) { return a.km - b.km; });
  return out;
}

var mapa = L.map("mapa", { zoomControl: true, attributionControl: true });

// --- fundos de satélite (Esri principal, NASA de reserva) ---
var esri = L.tileLayer(
  "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
  { maxZoom: 19, attribution: "Imagens © Esri, Maxar, Earthstar Geographics" }
);
function novaNasa() {  // recria com a data de ontem (não congela ao virar o dia)
  var ontem = new Date(Date.now() - 86400000).toISOString().slice(0, 10);
  return L.tileLayer(
    "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/MODIS_Terra_CorrectedReflectance_TrueColor/default/" +
    ontem + "/GoogleMapsCompatible_Level9/{z}/{y}/{x}.jpg",
    { maxZoom: 19, maxNativeZoom: 8, attribution: "Imagens NASA GIBS (domínio público)" }
  );
}

var bannerOffline = document.getElementById("banner-offline");
function mostrarOffline() { bannerOffline.classList.remove("oculto"); }
function esconderOffline() { bannerOffline.classList.add("oculto"); }

var fundoAtual = null;
function usarFundo(camada) {
  if (fundoAtual) { mapa.removeLayer(fundoAtual); }
  fundoAtual = camada;
  camada.on("tileerror", mostrarOffline);
  camada.on("tileload", esconderOffline);   // tiles voltaram => tira o aviso
  camada.addTo(mapa);
}
usarFundo(esri);

document.getElementById("btn-fundo").addEventListener("click", function () {
  esconderOffline();
  usarFundo(fundoAtual === esri ? novaNasa() : esri);
});

// --- estado em memória: camadas por fazenda ---
var fazendasCfg = [];
var camadas = {};          // nome -> { poligono, item }
var focosLayer = L.layerGroup().addTo(mapa);

// Anel "redondo": um retângulo do bbox com os 4 cantos arredondados (fica
// com cara de oval/pílula em vez de caixa quadrada).
function anelRedondo(bbox, opcoes) {
  var o = bbox.oeste, e = bbox.leste, s = bbox.sul, n = bbox.norte;
  var r = Math.min(e - o, n - s) * 0.4;  // raio dos cantos
  var cantos = [
    { cx: e - r, cy: n - r, a0: 0 },     // NE
    { cx: o + r, cy: n - r, a0: 90 },    // NO
    { cx: o + r, cy: s + r, a0: 180 },   // SO
    { cx: e - r, cy: s + r, a0: 270 },   // SE
  ];
  var pts = [];
  cantos.forEach(function (c) {
    for (var a = 0; a <= 90; a += 9) {
      var ang = (c.a0 + a) * Math.PI / 180;
      pts.push([c.cy + r * Math.sin(ang), c.cx + r * Math.cos(ang)]);  // [lat, lng]
    }
  });
  return L.polygon(pts, opcoes);
}

function desenharFazendas() {
  var todos = [];
  fazendasCfg.forEach(function (f) {
    var aneis = anelBboxes(f);
    if (f.apenas_observacao) {
      // Zona de observacao: linha azul discreta. Nao usa vermelho/laranja porque
      // aqui nao ha o que fazer — e informacao de contexto, nao chamado de acao.
      aneis.forEach(function (a) {
        anelRedondo(a.bbox, { color: COR.observacao, weight: 2, opacity: 0.75, dashArray: "2 8",
                              fill: true, fillColor: COR.observacao, fillOpacity: 0.04 }).addTo(mapa);
      });
      if (f.centro) {
        L.marker([f.centro.lat, f.centro.lon], { opacity: 0, interactive: false })
          .bindTooltip(f.nome, { permanent: true, direction: "center", className: "rotulo-faz rotulo-obs", opacity: 1 })
          .addTo(mapa);
      }
      var ez = aneis.length ? aneis[aneis.length - 1].bbox : null;
      if (ez) { todos.push([ez.sul, ez.oeste]); todos.push([ez.norte, ez.leste]); }
      camadas[f.nome] = { poligono: null };
      return;   // sem divisa vermelha: a zona nao tem "dentro da divisa"
    }
    aneis.forEach(function (a, i) {
      var externo = (i === aneis.length - 1);
      if (externo) {  // 10 km = AMARELO, zona levemente tingida
        anelRedondo(a.bbox, { color: COR_ANEL_EXT, weight: 2, opacity: 0.7, dashArray: "3 7",
                              fill: true, fillColor: COR_ANEL_EXT, fillOpacity: 0.05 }).addTo(mapa);
      } else {         // 5 km = LARANJA, linha tracejada
        anelRedondo(a.bbox, { color: COR_ANEL_INT, weight: 2.5, opacity: 0.9, dashArray: "10 8",
                              fill: false }).addTo(mapa);
      }
    });
    if (aneis.length) {
      var ext = aneis[aneis.length - 1].bbox;  // maior anel enquadra o mapa
      todos.push([ext.sul, ext.oeste]); todos.push([ext.norte, ext.leste]);
    }
    // divisa = VERMELHO (zona mais crítica)
    var poligono = L.geoJSON(null, { style: { color: COR_DIVISA, weight: 3, fillColor: COR_DIVISA, fillOpacity: 0.22 } }).addTo(mapa);
    camadas[f.nome] = { poligono: poligono };
    if (f.centro) {  // nome da fazenda escrito no mapa, no centro dela
      L.marker([f.centro.lat, f.centro.lon], { opacity: 0, interactive: false })
        .bindTooltip(f.nome, { permanent: true, direction: "center", className: "rotulo-faz", opacity: 1 })
        .addTo(mapa);
    }
    if (f.poligono_geojson) {
      fetch("/" + f.poligono_geojson, { cache: "no-store" })
        .then(function (r) { return r.json(); })
        .then(function (geo) { poligono.addData(geo); })
        .catch(function () { /* sem polígono: o anel já mostra a área */ });
    }
  });
  if (todos.length) { mapa.fitBounds(todos, { padding: [30, 30] }); }
  else { mapa.setView([-15.80, -47.90], 5); }
}

function montarLista() {
  var lista = document.getElementById("lista");
  lista.innerHTML = "";
  fazendasCfg.forEach(function (f) {
    var el = document.createElement("div");
    el.className = "faz";
    el.dataset.nome = f.nome;
    el.innerHTML =
      '<span class="pin"></span>' +
      '<span class="nm"><b>' + esc(f.nome) + "</b><span>" +
        (f.apenas_observacao ? "fogo de terceiros" : esc(f.area_ha || "?") + " ha") + "</span></span>" +
      '<span class="tag">' + (f.apenas_observacao ? "OBSERVANDO" : "VIGIANDO") + '</span>';
    el.addEventListener("mouseenter", function () {
      var c = camadas[f.nome];
      if (c && c.poligono && c.poligono.getBounds().isValid()) {
        mapa.fitBounds(c.poligono.getBounds(), { maxZoom: 14, padding: [40, 40] });
      } else {   // zona de observacao nao tem divisa: enquadra pelo anel
        var a = anelBboxes(f);
        if (a.length) {
          var b = a[a.length - 1].bbox;
          mapa.fitBounds([[b.sul, b.oeste], [b.norte, b.leste]], { padding: [40, 40] });
        }
      }
    });
    lista.appendChild(el);
    camadas[f.nome].item = el;
  });
}

function pinFoco(nomeFaz, contato, foco) {
  var c = COR[foco.gravidade] || COR.urgente;
  var lat = num(foco.lat), lon = num(foco.lon);
  var tel = (contato && contato.telefone) ? "<br>📞 " + esc(contato.nome) + " " + esc(contato.telefone) : "";
  var onde = foco.gravidade === "observacao"
    ? "fogo de terceiros — fora das suas terras"
    : (foco.gravidade === "dentro" ? "DENTRO da divisa" : ("~" + esc(foco.dist_km) + " km a " + esc(foco.rumo)));
  var agrup = (foco.n_focos && foco.n_focos > 1) ? "<br>🛰️ " + esc(foco.n_focos) + " detecções agrupadas" : "";
  L.circleMarker([lat, lon], { radius: 16, color: c, weight: 0, fillColor: c, fillOpacity: 0.22 }).addTo(focosLayer);  // brilho
  L.circleMarker([lat, lon], { radius: 8, color: "#fff", weight: 2, fillColor: c, fillOpacity: 1 })
    .bindPopup("<b>" + esc(nomeFaz) + "</b><br>" + onde + agrup + "<br>🕐 " + esc(foco.hora_local) + tel +
               '<br><a href="https://maps.google.com/?q=' + lat + "," + lon + '" target="_blank" rel="noopener">Abrir no Google Maps</a>')
    .addTo(focosLayer);
}

function aplicarEstado(estado) {
  var porNome = {};
  (estado.fazendas || []).forEach(function (f) { porNome[f.nome] = f; });
  focosLayer.clearLayers();

  fazendasCfg.forEach(function (fcfg) {
    var f = porNome[fcfg.nome] || { gravidade_atual: null, focos: [] };
    var grav = f.gravidade_atual;
    var cam = camadas[fcfg.nome];
    if (cam) {
      // divisa sempre vermelha; com fogo, ACENDE (borda branca + mais forte)
      if (cam.poligono) cam.poligono.setStyle({ color: grav ? "#fff" : COR_DIVISA, fillColor: COR_DIVISA,
                              weight: grav ? 4 : 3, fillOpacity: grav ? 0.55 : 0.22 });
      if (cam.item) {
        cam.item.className = "faz" + (grav ? " " + grav : "");
        cam.item.querySelector(".tag").textContent =
          grav === "observacao" ? "👁️ OBSERVANDO"
          : grav ? (grav === "atencao" ? "⚠️ ATENÇÃO" : "🔥 FOGO")
          : (fcfg.apenas_observacao ? "OBSERVANDO" : "VIGIANDO");
      }
    }
    (f.focos || []).forEach(function (foco) { pinFoco(fcfg.nome, f.contato, foco); });
  });

  // focos de fazenda que está no estado mas não no config: mostrar mesmo assim
  (estado.fazendas || []).forEach(function (f) {
    if (!camadas[f.nome]) { (f.focos || []).forEach(function (foco) { pinFoco(f.nome + " (?)", f.contato, foco); }); }
  });

  document.getElementById("ultima-checagem").textContent = estado.ultima_checagem_local || "aguardando 1ª checagem…";
  var tf = estado.total_focos_brasil;
  document.getElementById("focos-brasil").textContent = (tf === null || tf === undefined) ? "—" : tf;

  atualizarSaude(estado);
}

// Duas checagens independentes: robô vivo? (batimento) e dado fresco? (checagem)
function atualizarSaude(estado) {
  var limite = estado.heartbeat_limite_min || LIMITE_PADRAO_MIN;
  var pulso = document.getElementById("pulso");
  var stTxt = document.getElementById("status-txt");
  var banner = document.getElementById("banner-robo");
  var hbMin = idadeMin(estado.heartbeat_local);
  var dadoMin = idadeMin(estado.gerado_em);

  if (hbMin > limite) {                       // robô não bate o ponto = parado/morto
    pulso.classList.add("morto");
    stTxt.textContent = "robô parado";
    banner.textContent = "⚠️ Robô parado — a vigilância NÃO está ativa. Rode 'python vigia.py'.";
    banner.className = "banner robo-parado";
  } else if (dadoMin > limite) {              // robô vivo, mas sem dado novo do INPE
    pulso.classList.remove("morto");
    stTxt.textContent = "sem dado novo";
    banner.textContent = "⚠️ Dados do INPE atrasados há " + Math.round(dadoMin) +
      " min — o que aparece pode não ser o de agora.";
    banner.className = "banner aviso";
  } else {                                    // tudo em dia
    pulso.classList.remove("morto");
    stTxt.textContent = "vigiando";
    banner.className = "banner oculto";
  }
}

function atualizar() {
  fetch("/dados/painel-estado.json?t=" + Date.now(), { cache: "no-store" })
    .then(function (r) { if (!r.ok) throw new Error("sem estado"); return r.json(); })
    .then(aplicarEstado)
    .catch(function () { document.getElementById("status-txt").textContent = "aguardando robô…"; });
}

function tique() { document.getElementById("relogio").textContent = new Date().toLocaleTimeString("pt-BR"); }
setInterval(tique, 1000); tique();

fetch("/config/fazendas.json", { cache: "no-store" })
  .then(function (r) { return r.json(); })
  .then(function (cfg) {
    fazendasCfg = (cfg.fazendas || []).filter(function (f) { return f.ativa; });
    // Contagem sai do dado, nunca escrita à mão: cadastrar fazenda nova não pode
    // deixar o cabeçalho mentindo (dizia "5 fazendas" com 6 cadastradas).
    var sub = document.getElementById("marca-sub");
    if (sub) {
      var nFaz = fazendasCfg.filter(function (f) { return !f.apenas_observacao; }).length;
      var nObs = fazendasCfg.length - nFaz;
      sub.textContent = sub.textContent + " · " + nFaz + " fazendas" +
        (nObs ? " + " + nObs + " zona" + (nObs > 1 ? "s" : "") + " de observação" : "");
    }
    desenharFazendas();
    montarLista();
    atualizar();
    setInterval(atualizar, INTERVALO_ATUALIZA_MS);
  })
  .catch(function () { document.getElementById("status-txt").textContent = "erro ao ler as fazendas"; });
