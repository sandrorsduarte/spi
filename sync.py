import time
import os
import re
import sys
import sqlite3
import logging
from pathlib import Path
from abc import ABC, abstractmethod
from typing import List, Dict, Optional
from dotenv import load_dotenv

# Carregar variáveis de ambiente
load_dotenv()

# Configuração de Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(levelname)s] - %(message)s'
)

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 60))
DB_FILE = "synced_tracks.db"
TIDAL_SESSION_FILE = Path("tidal_session.json")

# ==============================================================================
# 1. GESTOR DE BASE DE DADOS CENTRAL (SQLite)
# ==============================================================================
class DatabaseManager:
    """
    Controlador SQLite para a fonte central de verdade (Hub-and-Spoke).
    Garante o mapeamento de IDs entre plataformas e previne loops infinitos.
    """
    def __init__(self, db_path: str = DB_FILE):
        self.db_path = db_path
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS tracks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint TEXT UNIQUE NOT NULL,
                    title TEXT NOT NULL,
                    artist TEXT NOT NULL,
                    isrc TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS service_mappings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    track_id INTEGER NOT NULL,
                    service_name TEXT NOT NULL,
                    service_track_id TEXT NOT NULL,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE,
                    UNIQUE(service_name, service_track_id)
                );
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_mappings_lookup 
                ON service_mappings(service_name, service_track_id);
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_mappings_track 
                ON service_mappings(track_id, service_name);
            """)
            conn.commit()

    @staticmethod
    def normalize_string(text: str) -> str:
        if not text:
            return ""
        text = text.lower()
        text = re.sub(r"[\(\[\{].*?[\)\]\}]", "", text)  # remove parêntesis/colchetes
        text = re.sub(r"[^\w\s]", "", text)  # remove pontuação
        return " ".join(text.split()).strip()

    def make_fingerprint(self, title: str, artist: str) -> str:
        norm_title = self.normalize_string(title)
        norm_artist = self.normalize_string(artist)
        return f"{norm_artist}:::{norm_title}"

    def is_service_track_known(self, service_name: str, service_track_id: str) -> bool:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM service_mappings WHERE service_name = ? AND service_track_id = ?",
                (service_name, str(service_track_id))
            )
            return cursor.fetchone() is not None

    def get_or_create_track(self, title: str, artist: str, isrc: Optional[str] = None) -> int:
        fingerprint = self.make_fingerprint(title, artist)
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM tracks WHERE fingerprint = ?", (fingerprint,))
            row = cursor.fetchone()
            if row:
                return row["id"]

            cursor.execute(
                "INSERT INTO tracks (fingerprint, title, artist, isrc) VALUES (?, ?, ?, ?)",
                (fingerprint, title.strip(), artist.strip(), isrc)
            )
            conn.commit()
            return cursor.lastrowid

    def record_mapping(self, track_id: int, service_name: str, service_track_id: str):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR IGNORE INTO service_mappings (track_id, service_name, service_track_id)
                VALUES (?, ?, ?)
                """,
                (track_id, service_name, str(service_track_id))
            )
            conn.commit()

    def has_service_mapping(self, track_id: int, service_name: str) -> bool:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM service_mappings WHERE track_id = ? AND service_name = ?",
                (track_id, service_name)
            )
            return cursor.fetchone() is not None


# ==============================================================================
# 2. ADAPTERS MODULARES PARA CADA SERVIÇO
# ==============================================================================
class BaseMusicAdapter(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @abstractmethod
    def is_configured(self) -> bool:
        pass

    @abstractmethod
    def fetch_playlist_tracks(self) -> List[Dict]:
        """Retorna lista de dicionários: [{'id': str, 'title': str, 'artist': str, 'isrc': Optional[str]}]"""
        pass

    @abstractmethod
    def search_track(self, title: str, artist: str) -> Optional[str]:
        """Pesquisa uma música e retorna o ID nativo da faixa no serviço."""
        pass

    @abstractmethod
    def add_track_to_playlist(self, service_track_id: str) -> bool:
        """Adiciona a faixa à playlist da respetiva plataforma."""
        pass

    @staticmethod
    def clean_title(title: str) -> str:
        """Remove termos comuns de vídeo/áudio do YouTube/outros serviços."""
        terms = [
            r"\(official.*?\)", r"\[official.*?\]",
            r"\(music video\)", r"\[music video\]",
            r"\(lyric video\)", r"\[lyric video\]",
            r"\(audio\)", r"\[audio\]",
            r"\(visualizer\)", r"\[visualizer\]",
            r"\(feat\..*?\)", r"\[feat\..*?\]",
            r"\(ft\..*?\)", r"\[ft\..*?\]",
            r"\(prod\..*?\)", r"\[prod\..*?\]"
        ]
        clean = title
        for pattern in terms:
            clean = re.sub(pattern, "", clean, flags=re.IGNORECASE)
        return clean.strip()


class YTMusicAdapter(BaseMusicAdapter):
    def __init__(self, playlist_id: Optional[str]):
        self.playlist_id = playlist_id
        self._yt = None
        self._can_write = False
        self._initialize()

    @property
    def name(self) -> str:
        return "YouTube Music"

    def _initialize(self):
        try:
            from ytmusicapi import YTMusic
            
            # Suporte para carregar credenciais a partir de variável de ambiente na Cloud
            browser_json_env = os.getenv("YTMUSIC_BROWSER_JSON")
            if browser_json_env and not os.path.exists("browser.json"):
                with open("browser.json", "w") as f:
                    f.write(browser_json_env)

            if os.path.exists("browser.json"):
                self._yt = YTMusic("browser.json")
                self._can_write = True
                logging.info("[YouTube Music] Autenticado com browser.json (Leitura e Escrita ativas).")
            elif os.path.exists("oauth.json"):
                self._yt = YTMusic("oauth.json")
                self._can_write = True
                logging.info("[YouTube Music] Autenticado com oauth.json (Leitura e Escrita ativas).")
            else:
                self._yt = YTMusic()
                self._can_write = False
                logging.info("[YouTube Music] Modo anónimo ativo (Leitura ativa; para escrita crie browser.json).")
        except Exception as e:
            logging.error(f"[YouTube Music] Erro ao inicializar cliente: {e}")
            self._yt = None

    def is_configured(self) -> bool:
        return self._yt is not None and bool(self.playlist_id)

    def fetch_playlist_tracks(self) -> List[Dict]:
        if not self.is_configured():
            return []
        try:
            data = self._yt.get_playlist(self.playlist_id)
            tracks = []
            for item in data.get("tracks", []):
                vid = item.get("videoId")
                title = item.get("title")
                artists = item.get("artists", [])
                artist = artists[0].get("name", "") if artists else ""
                if vid and title:
                    tracks.append({
                        "id": str(vid),
                        "title": title,
                        "artist": artist,
                        "isrc": None
                    })
            return tracks
        except Exception as e:
            logging.error(f"[YouTube Music] Erro ao obter playlist: {e}")
            return []

    def search_track(self, title: str, artist: str) -> Optional[str]:
        if not self.is_configured():
            return None
        clean_t = self.clean_title(title)
        query = f"{artist} {clean_t}".strip()
        try:
            results = self._yt.search(query, filter="songs")
            if results:
                return results[0].get("videoId")
            # Fallback apenas com título
            fallback = self._yt.search(clean_t, filter="songs")
            if fallback:
                return fallback[0].get("videoId")
        except Exception as e:
            logging.warning(f"[YouTube Music] Erro na pesquisa ('{query}'): {e}")
        return None

    def add_track_to_playlist(self, service_track_id: str) -> bool:
        if not self.is_configured():
            return False
        if not self._can_write:
            logging.warning(
                f"[YouTube Music] Não foi possível adicionar a faixa ({service_track_id}) porque o cliente está em modo leitura. "
                "Configure 'browser.json' via `ytmusicapi setup` para ativar escrita no YouTube Music."
            )
            return False
        try:
            self._yt.add_playlist_items(self.playlist_id, [service_track_id])
            return True
        except Exception as e:
            logging.error(f"[YouTube Music] Erro ao adicionar faixa {service_track_id}: {e}")
            return False


class TidalAdapter(BaseMusicAdapter):
    def __init__(self, playlist_id: Optional[str], session_file: Path = TIDAL_SESSION_FILE):
        self.playlist_id = playlist_id
        self.session_file = session_file
        self._session = None
        self._playlist = None
        self._initialize()

    @property
    def name(self) -> str:
        return "TIDAL"

    def _initialize(self):
        try:
            import tidalapi
            
            # Suporte para carregar sessão do TIDAL a partir de segredo de ambiente na Cloud
            tidal_session_env = os.getenv("TIDAL_SESSION_JSON")
            if tidal_session_env and not self.session_file.exists():
                with open(self.session_file, "w") as f:
                    f.write(tidal_session_env)

            self._session = tidalapi.Session()
            logging.info("[TIDAL] A autenticar sessão...")
            self._session.login_session_file(self.session_file)

            if not self._session.check_login():
                logging.warning("[TIDAL] Falha na autenticação do TIDAL.")
                return

            logging.info(f"[TIDAL] Sessão iniciada (Utilizador ID: {self._session.user.id}).")
            self._playlist = self._get_or_create_playlist()
        except Exception as e:
            logging.error(f"[TIDAL] Erro de inicialização: {e}")

    def _get_or_create_playlist(self):
        import tidalapi
        if self.playlist_id:
            try:
                pl = self._session.playlist(self.playlist_id)
                logging.info(f"[TIDAL] Playlist carregada: '{pl.name}' (ID: {pl.id})")
                return pl
            except Exception as e:
                logging.warning(f"[TIDAL] Não foi possível obter a playlist ID '{self.playlist_id}': {e}. A procurar por nome...")

        default_name = "YouTube Music Sync"
        try:
            for pl in self._session.user.playlists():
                if pl.name == default_name:
                    logging.info(f"[TIDAL] Playlist encontrada na conta: '{pl.name}' (ID: {pl.id})")
                    return pl
            logging.info(f"[TIDAL] A criar nova playlist '{default_name}'...")
            new_pl = self._session.user.create_playlist(default_name, "Playlist sincronizada multidirecionalmente pelo SPI")
            logging.info(f"[TIDAL] Nova playlist criada com sucesso (ID: {new_pl.id})")
            return new_pl
        except Exception as e:
            logging.error(f"[TIDAL] Erro ao obter/criar playlist: {e}")
            return None

    def is_configured(self) -> bool:
        return self._session is not None and self._playlist is not None

    def fetch_playlist_tracks(self) -> List[Dict]:
        if not self.is_configured():
            return []
        try:
            tracks = []
            for item in self._playlist.tracks():
                track_id = getattr(item, "id", None)
                title = getattr(item, "name", None)
                artist_obj = getattr(item, "artist", None)
                artist = getattr(artist_obj, "name", "") if artist_obj else ""
                isrc = getattr(item, "isrc", None)

                if track_id and title:
                    tracks.append({
                        "id": str(track_id),
                        "title": title,
                        "artist": artist,
                        "isrc": isrc
                    })
            return tracks
        except Exception as e:
            logging.error(f"[TIDAL] Erro ao obter faixas da playlist: {e}")
            return []

    def search_track(self, title: str, artist: str) -> Optional[str]:
        if not self.is_configured():
            return None
        import tidalapi
        clean_t = self.clean_title(title)
        query = f"{artist} {clean_t}".strip()
        try:
            results = self._session.search(query, models=[tidalapi.media.Track], limit=5)
            tracks = results.get("tracks", [])
            if tracks:
                return str(tracks[0].id)
            
            # Fallback
            fallback = self._session.search(clean_t, models=[tidalapi.media.Track], limit=5)
            fallback_tracks = fallback.get("tracks", [])
            if fallback_tracks:
                return str(fallback_tracks[0].id)
        except Exception as e:
            logging.warning(f"[TIDAL] Erro na pesquisa ('{query}'): {e}")
        return None

    def add_track_to_playlist(self, service_track_id: str) -> bool:
        if not self.is_configured():
            return False
        try:
            self._playlist.add([int(service_track_id) if service_track_id.isdigit() else service_track_id])
            return True
        except Exception as e:
            logging.error(f"[TIDAL] Erro ao adicionar faixa {service_track_id}: {e}")
            return False


class SpotifyAdapter(BaseMusicAdapter):
    def __init__(self, playlist_id: Optional[str]):
        self.playlist_id = playlist_id
        self._sp = None
        self._initialize()

    @property
    def name(self) -> str:
        return "Spotify"

    def _initialize(self):
        client_id = os.getenv("SPOTIFY_CLIENT_ID")
        client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
        redirect_uri = os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")

        if not (client_id and client_secret and self.playlist_id):
            logging.info("[Spotify] Não configurado no .env (Módulo desativado).")
            return

        try:
            import spotipy
            from spotipy.oauth2 import SpotifyOAuth
            self._sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=redirect_uri,
                scope="playlist-modify-private playlist-modify-public"
            ))
            logging.info("[Spotify] Cliente inicializado com sucesso.")
        except Exception as e:
            logging.warning(f"[Spotify] Não foi possível autenticar o Spotify: {e}")
            self._sp = None

    def is_configured(self) -> bool:
        return self._sp is not None and bool(self.playlist_id)

    def fetch_playlist_tracks(self) -> List[Dict]:
        if not self.is_configured():
            return []
        try:
            results = self._sp.playlist_items(self.playlist_id)
            tracks = []
            for item in results.get("items", []):
                track = item.get("track")
                if not track:
                    continue
                track_id = track.get("uri") or track.get("id")
                title = track.get("name")
                artists = track.get("artists", [])
                artist = artists[0].get("name", "") if artists else ""
                isrc = track.get("external_ids", {}).get("isrc")
                if track_id and title:
                    tracks.append({
                        "id": str(track_id),
                        "title": title,
                        "artist": artist,
                        "isrc": isrc
                    })
            return tracks
        except Exception as e:
            logging.warning(f"[Spotify] Falha ao ler playlist: {e}")
            return []

    def search_track(self, title: str, artist: str) -> Optional[str]:
        if not self.is_configured():
            return None
        clean_t = self.clean_title(title)
        query = f"track:{clean_t} artist:{artist}"
        try:
            res = self._sp.search(q=query, type="track", limit=1)
            tracks = res.get("tracks", {}).get("items", [])
            if tracks:
                return tracks[0]["uri"]
            # Fallback
            fallback = self._sp.search(q=clean_t, type="track", limit=1)
            fallback_tracks = fallback.get("tracks", {}).get("items", [])
            if fallback_tracks:
                return fallback_tracks[0]["uri"]
        except Exception as e:
            logging.warning(f"[Spotify] Falha na pesquisa ('{query}'): {e}")
        return None

    def add_track_to_playlist(self, service_track_id: str) -> bool:
        if not self.is_configured():
            return False
        try:
            self._sp.playlist_add_items(self.playlist_id, [service_track_id])
            return True
        except Exception as e:
            logging.warning(f"[Spotify] Falha ao adicionar faixa: {e}")
            return False


# ==============================================================================
# 3. MOTOR DE SINCRONIZAÇÃO MULTIDIRECIONAL (Hub-and-Spoke Sync Engine)
# ==============================================================================
class MultidirectionalSyncEngine:
    def __init__(self):
        self.db = DatabaseManager()
        self.adapters: List[BaseMusicAdapter] = []
        self._setup_adapters()

    def _setup_adapters(self):
        yt_id = os.getenv("YT_PLAYLIST_ID")
        tidal_id = os.getenv("TIDAL_PLAYLIST_ID")
        spotify_id = os.getenv("SPOTIFY_PLAYLIST_ID")

        yt_adapter = YTMusicAdapter(yt_id)
        tidal_adapter = TidalAdapter(tidal_id)
        spotify_adapter = SpotifyAdapter(spotify_id)

        for adapter in [yt_adapter, tidal_adapter, spotify_adapter]:
            if adapter.is_configured():
                self.adapters.append(adapter)
                logging.info(f"-> Adaptador ativo: [{adapter.name}]")

        if len(self.adapters) < 2:
            logging.warning("Atenção: Menos de 2 adaptadores estão ativos. A sincronização requer pelo menos 2 serviços.")

    def sync_cycle(self):
        logging.info("--- A iniciar ciclo de sincronização multidirecional ---")
        
        for source_adapter in self.adapters:
            tracks = source_adapter.fetch_playlist_tracks()
            
            for item in tracks:
                track_id = item["id"]
                title = item["title"]
                artist = item["artist"]
                isrc = item.get("isrc")

                # Se a faixa já foi processada neste serviço, ignorar
                if self.db.is_service_track_known(source_adapter.name, track_id):
                    continue

                # Faixa nova detetada nesta plataforma!
                central_track_id = self.db.get_or_create_track(title, artist, isrc)
                self.db.record_mapping(central_track_id, source_adapter.name, track_id)

                logging.info(f"✨ Nova música detetada no [{source_adapter.name}]: '{artist} - {title}'")

                # Propagar para todas as outras plataformas ativas
                for target_adapter in self.adapters:
                    if target_adapter.name == source_adapter.name:
                        continue

                    if self.db.has_service_mapping(central_track_id, target_adapter.name):
                        continue

                    logging.info(f"A procurar '{artist} - {title}' no [{target_adapter.name}]...")
                    target_track_id = target_adapter.search_track(title, artist)

                    if target_track_id:
                        success = target_adapter.add_track_to_playlist(target_track_id)
                        if success:
                            self.db.record_mapping(central_track_id, target_adapter.name, target_track_id)
                            logging.info(f"✅ Sucesso: '{title}' adicionada ao [{target_adapter.name}].")
                        else:
                            logging.warning(f"❌ Falha ao adicionar '{title}' ao [{target_adapter.name}].")
                    else:
                        logging.warning(f"⚠️ Faixa não encontrada no [{target_adapter.name}]: '{title}' ({artist})")

    def run(self, once: bool = False):
        if once:
            logging.info("A executar ciclo único de sincronização...")
            self.sync_cycle()
            logging.info("Ciclo concluído com sucesso.")
            return

        logging.info(f"Serviço SPI iniciado. A verificar a cada {POLL_INTERVAL} segundos...")
        try:
            while True:
                self.sync_cycle()
                time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            logging.info("Serviço interrompido pelo utilizador. A encerrar de forma segura...")


if __name__ == "__main__":
    run_once = "--once" in sys.argv
    engine = MultidirectionalSyncEngine()
    engine.run(once=run_once)