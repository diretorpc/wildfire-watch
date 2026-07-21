"""Servidor local do painel do vigia-fogo.

Abre a tela de monitoramento no seu navegador. NÃO é o robô — é só a "janela"
que mostra o que o robô (vigia.py) já descobriu. Deixe o vigia.py rodando em
outra janela pra ter dado novo.

Uso:
  python painel.py            # abre em http://127.0.0.1:8000/painel/
  python painel.py --porta 8080
  python painel.py --sem-navegador   # não abre o navegador sozinho

Só biblioteca padrão. Escuta SÓ em 127.0.0.1 (só este computador enxerga) —
nunca na rede, por segurança.
"""

import argparse
import re
import sys
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

RAIZ = Path(__file__).resolve().parent
HOST = "127.0.0.1"

# Só estes caminhos podem sair pela porta. Tudo o resto (incl. .env, .git,
# estado-vigia.json) é bloqueado — o servidor NÃO expõe a pasta inteira.
GEOJSON = re.compile(r"^/dados/[\w.\-]+\.geojson$")
HOSTS_OK = {"127.0.0.1", "localhost"}


def caminho_liberado(path: str) -> bool:
    p = unquote(urlparse(path).path)
    if p in ("/", "/painel", "/painel/"):
        return True
    if p.startswith("/painel/") and ".." not in p:
        return True
    if p in ("/config/fazendas.json", "/dados/painel-estado.json"):
        return True
    return bool(GEOJSON.match(p))


class Handler(SimpleHTTPRequestHandler):
    """Serve SÓ os arquivos da tela (allowlist), só para o próprio PC."""

    def log_message(self, *args):
        pass  # silêncio: não poluir o terminal com cada requisição

    def _seguro(self) -> bool:
        # Defesa contra "DNS rebinding": só aceita se o Host for local.
        host = (self.headers.get("Host") or "").split(":")[0]
        if host and host not in HOSTS_OK:
            self.send_error(403, "Apenas acesso local (127.0.0.1)")
            return False
        if not caminho_liberado(self.path):
            self.send_error(404, "Arquivo não disponível pelo painel")
            return False
        return True

    def do_GET(self):
        if self.path in ("/", "/painel"):
            self.send_response(302)
            self.send_header("Location", "/painel/")
            self.end_headers()
            return
        if self._seguro():
            super().do_GET()

    def do_HEAD(self):
        if self._seguro():
            super().do_HEAD()

    def end_headers(self):
        # a tela relê o estado toda hora; garantir que nunca venha de cache
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def achar_porta_livre(inicial: int) -> ThreadingHTTPServer:
    """Tenta a porta pedida e as seguintes até achar uma livre."""
    handler = partial(Handler, directory=str(RAIZ))
    for porta in range(inicial, inicial + 20):
        try:
            return ThreadingHTTPServer((HOST, porta), handler)
        except OSError:
            continue
    raise SystemExit(f"ERRO: nenhuma porta livre entre {inicial} e {inicial + 19}.")


def main() -> int:
    for saida in (sys.stdout, sys.stderr):
        if hasattr(saida, "reconfigure"):
            saida.reconfigure(encoding="utf-8", errors="replace")

    p = argparse.ArgumentParser(description="Painel local do vigia-fogo")
    p.add_argument("--porta", type=int, default=8000, help="porta inicial (padrão 8000)")
    p.add_argument("--sem-navegador", action="store_true", help="não abrir o navegador sozinho")
    args = p.parse_args()

    servidor = achar_porta_livre(args.porta)
    url = f"http://{HOST}:{servidor.server_address[1]}/painel/"
    print(f"Painel no ar: {url}")
    print("Deixe o 'python vigia.py' rodando em outra janela para ter dado novo.")
    print("Para encerrar: Ctrl+C.")
    if not args.sem_navegador:
        webbrowser.open(url)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nPainel encerrado.")
    finally:
        servidor.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
