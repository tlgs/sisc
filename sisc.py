import socket
import sys
from urllib.parse import urlparse

import gi
import platformdirs
import soco
from aiohttp import web
from textual import on, work
from textual.app import App
from textual.binding import Binding
from textual.containers import Vertical, Container
from textual.reactive import var
from textual.widgets import DataTable, LoadingIndicator, Static

gi.require_version("Tracker", "3.0")
from gi.repository import Tracker  # noqa: E402


SISC_PORT = 8080


def this_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(0)

    try:
        s.connect(("1.1.1.1", 53))
    except (TimeoutError, InterruptedError):
        return "127.0.0.1"

    return s.getsockname()[0]


def fetch_music():
    conn = Tracker.SparqlConnection.bus_new(
        "org.freedesktop.Tracker3.Miner.Files", None, None
    )

    stmt = """
        SELECT ?artist ?album ?trackno ?url {
          ?song a nmm:MusicPiece ;
            nie:title ?title ;
            nmm:trackNumber ?trackno ;
            nmm:musicAlbum [ nie:title ?album ; nmm:albumArtist [ nmm:artistName ?artist ] ] ;
            nie:isStoredAs ?as .
          ?as nie:url ?url .
        }
    """
    cursor = conn.query(stmt)

    records = []
    while cursor.next():
        artist, album, raw_n, url = [cursor.get_string(i)[0] for i in range(4)]
        records.append((artist, album, int(raw_n), url))

    cursor.close()
    conn.close()

    return records


class AlbumList(DataTable):
    BINDINGS = [
        Binding("k", "cursor_up", "Cursor Up", show=False),
        Binding("j", "cursor_down", "Cursor Down", show=False),
    ]


class ControllerApp(App):
    CSS = """
    Screen {
        layout: horizontal;
    }

    AlbumList {
        width: 60%;
        overflow-x: hidden;
        scrollbar-size-vertical: 1;
    }

    Container {
        align: center top;
    }

    #now-playing {
        width: 80%;
        margin-top: 2;
        padding-top: 1;
        content-align: center middle;
        border: vkey $accent;
    }

    #footer {
        height: 1;
    }

    #sonos-player {
        content-align: right bottom;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", show=False, priority=True),
    ]

    ready_check = var(3)

    def __init__(self):
        self.music_dir = platformdirs.user_music_dir()
        self.controller_ip = this_ip()
        super().__init__()

    def compose(self):
        yield LoadingIndicator()
        yield AlbumList(cursor_type="row")

        with Vertical():
            with Container():
                now_playing = Static(id="now-playing")
                now_playing.border_title = "Now Playing"
                yield now_playing

            with Container(id="footer"):
                yield Static(id="sonos-player")

    def on_mount(self):
        table = self.query_one(AlbumList)
        table.display = False

        self.load_data()
        self.find_sonos()
        self.spawn_http("0.0.0.0", SISC_PORT)

    def watch_ready_check(self, ready_check):
        if ready_check == 0:
            self.query_one(LoadingIndicator).display = False
            self.query_one(AlbumList).display = True

    @work(thread=True)
    def load_data(self):
        self.library = {}
        records = fetch_music()
        for artist, album, _, location in sorted(records):
            if (artist, album) not in self.library:
                self.library[artist, album] = []

            path = urlparse(location).path
            trimmed_path = path.removeprefix(self.music_dir)
            self.library[artist, album].append(trimmed_path)

        table = self.query_one(AlbumList)
        table.add_columns("Artist", "Album")
        table.add_rows(sorted(self.library.keys()))
        self.ready_check -= 1

    @work(thread=True)
    def find_sonos(self):
        self.sonos, *_ = soco.discover()
        self.query_one("#sonos-player").update(f"Sonos: {self.sonos.player_name}")
        self.ready_check -= 1

    @work
    async def spawn_http(self, host, port):
        app = web.Application()
        app.add_routes([web.static("/", self.music_dir)])

        self.http_runner = web.AppRunner(app)
        await self.http_runner.setup()
        site = web.TCPSite(self.http_runner, host, port)
        await site.start()

        self.ready_check -= 1

    @on(DataTable.RowSelected)
    def select_album(self, event):
        # clear previous queue
        self.sonos.clear_queue()

        # fetch tracks to dump in queue
        artist, album = event.control.get_row(event.row_key)
        for location in self.library[artist, album]:
            self.sonos.add_uri_to_queue(
                f"http://{self.controller_ip}:{SISC_PORT}{location}"
            )

        # start playing
        self.sonos.play_from_queue(0)

        # update UI
        now_playing = self.query_one("#now-playing")
        now_playing.update(f"{album},\nby {artist}")

    async def on_unmount(self):
        try:
            self.sonos.stop()
            self.sonos.clear_queue()

            await self.http_runner.cleanup()
        except AttributeError:
            pass


def main():
    app = ControllerApp()
    app.run()
    return app.return_code


if __name__ == "__main__":
    sys.exit(main())
