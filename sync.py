import time
import json
import os
import re
import logging
from pathlib import Path
from dotenv import load_dotenv
import tidalapi
from ytmusicapi import YTMusic

# Configuração de Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Carregar variáveis de ambiente
load_dotenv()

# Configurações do sistema
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 60))
STATE_FILE = "synced_tracks.json"
SESSION_FILE = Path("tidal_session.json")
YT_PLAYLIST_ID = os.getenv("YT_PLAYLIST_ID")
TIDAL_PLAYLIST_ID = os.getenv("TIDAL_PLAYLIST_ID")

class PlaylistSyncer:
    def __init__(self):
        logging.info("A inicializar os clientes das APIs...")
        
        # 1. YouTube Music (não necessita de autenticação para playlists públicas/não-listadas)
        self.yt = YTMusic()
        
        # 2. TIDAL
        self.tidal = tidalapi.Session()
        logging.info("A autenticar com o TIDAL...")
        self.tidal.login_session_file(SESSION_FILE)
        
        if not self.tidal.check_login():
            raise RuntimeError("Não foi possível autenticar no TIDAL.")
            
        logging.info(f"Sessão TIDAL iniciada com sucesso (Utilizador: {self.tidal.user.id}).")
        
        # Obter ou criar a Playlist no TIDAL
        self.playlist = self._get_or_create_playlist()
        
        # Carregar estado de faixas já sincronizadas
        self.processed_ids = self._load_state()

    def _get_or_create_playlist(self):
        if TIDAL_PLAYLIST_ID:
            try:
                playlist = self.tidal.playlist(TIDAL_PLAYLIST_ID)
                logging.info(f"Playlist TIDAL carregada: '{playlist.name}' (ID: {playlist.id})")
                return playlist
            except Exception as e:
                logging.warning(f"Não foi possível carregar a playlist com ID '{TIDAL_PLAYLIST_ID}': {e}. A procurar por nome...")

        # Se não tiver ID definido no .env, procura por uma playlist com nome padrão ou cria uma nova
        default_name = "YouTube Music Sync"
        try:
            for pl in self.tidal.user.playlists():
                if pl.name == default_name:
                    logging.info(f"Playlist TIDAL encontrada na sua conta: '{pl.name}' (ID: {pl.id})")
                    return pl
            
            logging.info(f"A criar nova playlist no TIDAL: '{default_name}'...")
            new_pl = self.tidal.user.create_playlist(default_name, "Playlist sincronizada automaticamente a partir do YouTube Music")
            logging.info(f"Nova playlist TIDAL criada com sucesso: ID {new_pl.id}")
            return new_pl
        except Exception as e:
            logging.error(f"Erro ao obter/criar playlist no TIDAL: {e}")
            raise

    def _load_state(self) -> set:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                return set(json.load(f))
        return set()

    def _save_state(self):
        with open(STATE_FILE, "w") as f:
            json.dump(list(self.processed_ids), f, indent=4)

    def _clean_title(self, title: str) -> str:
        """Remove termos comuns do YouTube que prejudicam a pesquisa no TIDAL."""
        terms_to_remove = [
            r"\(official.*?\)", r"\[official.*?\]", 
            r"\(music video\)", r"\[music video\]",
            r"\(lyric video\)", r"\[lyric video\]",
            r"\(audio\)", r"\[audio\]",
            r"\(visualizer\)", r"\[visualizer\]",
            r"\(feat\..*?\)", r"\[feat\..*?\]",
            r"\(ft\..*?\)", r"\[ft\..*?\]",
            r"\(prod\..*?\)", r"\[prod\..*?\]"
        ]
        clean_title = title
        for term in terms_to_remove:
            clean_title = re.sub(term, "", clean_title, flags=re.IGNORECASE)
        return clean_title.strip()

    def _search_tidal_track(self, title: str, artist: str) -> str | None:
        clean_title = self._clean_title(title)
        query = f"{artist} {clean_title}".strip()
        
        # Tentativa 1: Pesquisa combinada com Artista + Título
        try:
            results = self.tidal.search(query, models=[tidalapi.media.Track], limit=5)
            tracks = results.get("tracks", [])
            if tracks:
                return str(tracks[0].id)
        except Exception as e:
            logging.warning(f"Erro na pesquisa TIDAL ('{query}'): {e}")

        # Tentativa 2: Fallback apenas pelo título limpo
        logging.warning(f"Correspondência falhou para '{query}'. A tentar fallback apenas com título...")
        try:
            fallback = self.tidal.search(clean_title, models=[tidalapi.media.Track], limit=5)
            fallback_tracks = fallback.get("tracks", [])
            if fallback_tracks:
                return str(fallback_tracks[0].id)
        except Exception as e:
            logging.warning(f"Erro no fallback TIDAL ('{clean_title}'): {e}")

        return None

    def check_and_sync(self):
        logging.info("A verificar a playlist do YouTube Music...")
        try:
            yt_data = self.yt.get_playlist(YT_PLAYLIST_ID)
            yt_tracks = yt_data.get("tracks", [])
        except Exception as e:
            logging.error(f"Falha ao obter playlist do YouTube: {e}")
            return

        new_tracks_found = False

        for track in yt_tracks:
            yt_id = track.get("videoId")
            
            if yt_id and yt_id not in self.processed_ids:
                title = track.get("title")
                artist = track.get("artists", [{}])[0].get("name", "")

                logging.info(f"Nova música detetada: {artist} - {title}")
                
                tidal_track_id = self._search_tidal_track(title, artist)

                if tidal_track_id:
                    try:
                        self.playlist.add([tidal_track_id])
                        logging.info(f"Sucesso: Faixa adicionada ao TIDAL (Track ID: {tidal_track_id}).")
                    except Exception as e:
                        logging.error(f"Erro ao adicionar ao TIDAL: {e}")
                else:
                    logging.error(f"Não foi possível encontrar a faixa no TIDAL: {title}")

                self.processed_ids.add(yt_id)
                new_tracks_found = True

        if new_tracks_found:
            self._save_state()

    def run(self):
        logging.info(f"Serviço de sincronização iniciado. Polling a cada {POLL_INTERVAL} segundos.")
        try:
            while True:
                self.check_and_sync()
                time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            logging.info("Serviço interrompido pelo utilizador. A encerrar de forma segura...")

if __name__ == "__main__":
    syncer = PlaylistSyncer()
    syncer.run()