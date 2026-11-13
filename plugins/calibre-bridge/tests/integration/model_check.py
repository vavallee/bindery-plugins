"""Model level check of the Calibre Bridge against a real Calibre.

Runs inside Calibre's own interpreter, with the plugin zip installed:

    QT_QPA_PLATFORM=offscreen calibre-debug -e model_check.py

``run.py`` in this directory does the install and calls this for you.

The unit tests replace Calibre with fakes, so they cannot tell whether the
plugin's idea of Calibre is right. This script uses the real thing: a real
library (``LibraryDatabase``), the real ``BooksModel`` the library list is
drawn from, and the plugin imported from the installed zip under the
``calibre_plugins.bindery_bridge`` namespace, exactly as Calibre imports it.

What is real and what is a stand in:

* Real: the database, its ``View`` (``db.data``), the ``BooksModel``, the
  QApplication, the plugin's ``BinderyBridgeAction`` object, the Dispatchers
  it builds to hop onto the GUI thread, and ``adder.add_book_detailed``, which
  is called from a worker thread as the HTTP handler calls it.
* Stand in: the main window. ``GuiStandIn`` is a bare QObject that carries
  ``library_view.model()`` (the real model) and ``current_db``. It has no
  ``iactions`` and no ``tags_view``, so the adder takes its documented
  fallback, ``model.books_added`` and a skipped tag recount, instead of the
  Add Books action's ``refresh_gui``. ``genesis`` is not run here either. The
  boot check in ``run.py`` covers both with a full Calibre.

Exits non zero with a message on the first failed expectation.
"""

import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
from typing import Any

from calibre.constants import numeric_version
from calibre.customize.ui import find_plugin
from calibre.db.legacy import LibraryDatabase
from calibre.ebooks.metadata.book.base import Metadata
from calibre.gui2 import Application
from calibre.gui2.library.models import BooksModel
from qt.core import QModelIndex, QObject, Qt

# calibre-debug puts this directory on sys.path already; do not depend on it.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sample_books  # noqa: E402

TITLE = "Bindery Integration Probe"
AUTHOR = "Ada Tester"
BINDERY_ID = "101"
ISBN = "9780306406157"
# Spaces and parentheses force the quoted form of the identifier search.
HARDCOVER_ID = "integration probe (2nd ed)"

SEED_TITLE = "Hand Curated Book"
SEED_AUTHOR = "Existing Author"
SEED_ISBN = "9780441172719"


class CheckFailed(Exception):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


def step(text: str) -> None:
    print(f"  ok: {text}", flush=True)


class LibraryViewStandIn:
    """Stand in for ``gui.library_view``. Only ``model()``, which is real."""

    def __init__(self, model: BooksModel) -> None:
        self._model = model

    def model(self) -> BooksModel:
        return self._model


class GuiStandIn(QObject):
    """Stand in for Calibre's main window. See the module docstring."""

    def __init__(self, model: BooksModel, db: LibraryDatabase) -> None:
        QObject.__init__(self)
        self.library_view = LibraryViewStandIn(model)
        self.current_db = db


class Harness:
    """A real library with a real model on it, and the plugin wired to both."""

    def __init__(self, app: Application, workdir: str, bridge: Any, adder: Any) -> None:
        self.app = app
        self.workdir = workdir
        # The plugin, imported from the installed zip: the package that holds
        # BinderyBridgeAction, and its adder module.
        self.bridge = bridge
        self.adder = adder
        library = os.path.join(workdir, "library")
        os.makedirs(library)
        self.db = LibraryDatabase(library)
        self.api = self.db.new_api

        # A book the user put there by hand, before the bridge ever ran. It
        # goes in through the cache, so the view is rebuilt afterwards, which
        # is all Ctrl+R does.
        seed = Metadata(SEED_TITLE, [SEED_AUTHOR])
        seed.set_identifiers({"isbn": SEED_ISBN})
        seed_path = sample_books.write_epub(
            os.path.join(workdir, "seed.epub"),
            SEED_TITLE,
            SEED_AUTHOR,
            "00000000-0000-4000-8000-000000000001",
        )
        ids, _ = self.api.add_books([(seed, {"EPUB": seed_path})], run_hooks=False)
        self.seed_id = int(ids[0])
        self.db.data.refresh()

        self.model = BooksModel()
        self.model.set_database(self.db)
        self.inserted: list[tuple[int, int, int]] = []
        self.changed_rows: list[tuple[int, int]] = []
        self.model.rowsInserted.connect(self._on_rows_inserted)
        self.model.dataChanged.connect(self._on_data_changed)

        self.gui = GuiStandIn(self.model, self.db)
        # The plugin's own action class, built the way Calibre builds it
        # (parent is the main window). do_genesis is not called: it would
        # start the HTTP server and needs the window's keyboard manager.
        self.action = bridge.BinderyBridgeAction(self.gui, None)
        # The same two lines genesis runs to get onto the GUI thread.
        self.on_added = bridge._gui_thread_dispatcher(self.action._refresh_gui)
        self.on_updated = bridge._gui_thread_dispatcher(self.action._refresh_gui_row)
        check(self.on_added is not None, "calibre.gui2.Dispatcher is not importable")
        check(self.on_updated is not None, "calibre.gui2.Dispatcher is not importable")

    def _on_rows_inserted(self, _parent: QModelIndex, first: int, last: int) -> None:
        self.inserted.append((first, last, self.rows()))

    def _on_data_changed(
        self, top_left: QModelIndex, bottom_right: QModelIndex, *_: object
    ) -> None:
        self.changed_rows.append((top_left.row(), bottom_right.row()))

    def close(self) -> None:
        self.model.stop_metadata_backup()
        self.db.close()

    def rows(self) -> int:
        return int(self.model.rowCount(QModelIndex()))

    def view_search(self, query: str) -> list[int]:
        """What the search box would find: only ids the view knows about."""
        return [int(i) for i in self.db.data.search_getting_ids(query, "")]

    def cell(self, book_id: int, column: str) -> str:
        """The text the library list shows for ``book_id`` in ``column``."""
        row = self.db.data.id_to_index(book_id)
        index = self.model.index(row, self.model.column_map.index(column))
        return str(self.model.data(index, Qt.ItemDataRole.DisplayRole))

    def formats(self, book_id: int) -> set[str]:
        return {str(f).upper() for f in self.api.formats(book_id)}

    def deliver(self, path: str, metadata: dict, add_format: bool = False) -> Any:
        """One delivery, made the way the HTTP handler makes it.

        ``add_book_detailed`` runs on a worker thread, as it does under the
        bridge's HTTP server, and hands the refresh to the Dispatchers. This
        thread is the GUI thread, so the queued refresh runs when it next
        processes events.
        """
        box: dict = {}

        def work() -> None:
            try:
                box["result"] = self.adder.add_book_detailed(
                    self.db,
                    path,
                    gui=self.gui,
                    metadata=metadata,
                    ingest_root="",
                    on_added=self.on_added,
                    on_updated=self.on_updated,
                    add_format=add_format,
                )
            except BaseException as exc:
                box["error"] = exc

        worker = threading.Thread(target=work, name="bridge-delivery", daemon=True)
        worker.start()
        deadline = time.monotonic() + 60
        while worker.is_alive():
            check(time.monotonic() < deadline, "add_book_detailed did not return in 60 seconds")
            self.app.processEvents()
            worker.join(0.01)
        for _ in range(3):
            self.app.processEvents()
        if "error" in box:
            raise box["error"]
        return box["result"]


def run_checks(h: Harness) -> None:
    adder = h.adder
    metadata = {
        "title": TITLE,
        "authors": [AUTHOR],
        "identifiers": {"bindery": BINDERY_ID, "isbn": ISBN, "hardcover": HARDCOVER_ID},
    }
    epub = sample_books.write_epub(
        os.path.join(h.workdir, "probe.epub"), TITLE, AUTHOR, "00000000-0000-4000-8000-000000000002"
    )
    text = sample_books.write_text_book(os.path.join(h.workdir, "probe.txt"), TITLE)

    check(h.rows() == 1, f"the seeded library should list 1 book, the model has {h.rows()}")
    step("a real library with one hand made book is listed by a real BooksModel")

    # 1. A new book shows up in the list without a manual refresh (#30).
    first = h.deliver(epub, metadata)
    check(not first.duplicate, "the first delivery was reported as a duplicate")
    check(first.book_id > 0, f"the first delivery returned book id {first.book_id}")
    check(first.book_id != h.seed_id, "the first delivery landed on the hand made book")
    book_id = first.book_id
    check(
        h.rows() == 2,
        f"the library list did not grow: BooksModel.rowCount is {h.rows()} after a delivery, "
        "expected 2. The book is in the database but not in the view, so the user would "
        "have to press Ctrl+R to see it (issue #30)",
    )
    check(
        h.inserted == [(0, 0, 2)],
        "expected one rowsInserted for one row with the model already at 2 rows, got "
        f"(first, last, rowCount) = {h.inserted}",
    )
    found = h.view_search(f'title:"={TITLE}"')
    check(
        found == [book_id],
        f"a title search through the view found {found}, expected [{book_id}]",
    )
    check(h.cell(book_id, "title") == TITLE, f"the list shows title {h.cell(book_id, 'title')!r}")
    step("a delivery adds one row to the model and a view search finds it by title")

    check(h.formats(book_id) == {"EPUB"}, f"formats on the new row: {h.formats(book_id)}")
    stored = h.api.format_abspath(book_id, "EPUB")
    check(bool(stored) and os.path.isfile(stored), f"no EPUB file in the library at {stored!r}")
    check(
        os.path.getsize(stored) == os.path.getsize(epub),
        "the EPUB in the library is not the size of the one delivered",
    )
    check(
        "EPUB" in h.cell(book_id, "formats"),
        f"the list shows formats {h.cell(book_id, 'formats')!r} for the new row",
    )
    step("the format is attached and the list shows it")

    # 2. The identifier searches the dedupe ladder runs, on the real search
    # grammar. The quoted form is the one with spaces and parentheses.
    for typ, value, expected in (
        ("bindery", BINDERY_ID, book_id),
        ("isbn", ISBN, book_id),
        ("hardcover", HARDCOVER_ID, book_id),
        ("isbn", SEED_ISBN, h.seed_id),
        ("bindery", "no-such-delivery", 0),
    ):
        got = adder._book_id_for_identifier(h.api, typ, value)
        check(
            got == expected, f"identifier search {typ}:{value!r} found {got}, expected {expected}"
        )
    query = adder._identifier_search_query("bindery", BINDERY_ID)
    found = h.view_search(query)
    check(found == [book_id], f"view search {query!r} found {found}, expected [{book_id}]")
    step("identifier searches find the right rows, through the cache and through the view")

    # 3. The same book delivered again is a duplicate, not a second row.
    again = h.deliver(epub, metadata)
    check(again.duplicate, "a second delivery of the same book was not reported as a duplicate")
    check(again.book_id == book_id, f"the duplicate points at {again.book_id}, not {book_id}")
    check(h.rows() == 2, f"a duplicate delivery changed the row count to {h.rows()}")
    check(len(h.api.all_book_ids()) == 2, "a duplicate delivery added a book to the database")
    check(h.formats(book_id) == {"EPUB"}, "a duplicate delivery changed the formats")
    step("a second delivery of the same book is a duplicate and adds no row")

    # 4. A different Bindery book that shares an ISBN with the hand made row
    # matches it, and never puts a file on it, even with addFormat.
    other = {"title": SEED_TITLE, "identifiers": {"bindery": "202", "isbn": SEED_ISBN}}
    clash = h.deliver(text, other, add_format=True)
    check(clash.duplicate, "an ISBN match on a hand made row was not reported as a duplicate")
    check(clash.book_id == h.seed_id, f"the ISBN match points at {clash.book_id}, not the seed")
    check(h.formats(h.seed_id) == {"EPUB"}, "a file was added to a hand made row")
    check(h.rows() == 2, f"an ISBN duplicate changed the row count to {h.rows()}")
    step("an ISBN match on a hand made row is a duplicate and the row is left alone")

    # 5. A second format of the same Bindery book joins the existing row, and
    # the row is repainted rather than inserted again.
    del h.changed_rows[:]
    second = h.deliver(text, metadata, add_format=True)
    check(not second.duplicate, "addFormat with a new format was reported as a duplicate")
    check(second.format_added, "addFormat did not report format_added")
    check(second.book_id == book_id, f"addFormat went to {second.book_id}, not {book_id}")
    check(h.formats(book_id) == {"EPUB", "TXT"}, f"formats after addFormat: {h.formats(book_id)}")
    check(h.rows() == 2, f"addFormat changed the row count to {h.rows()}")
    row = h.db.data.id_to_index(book_id)
    check(
        any(top <= row <= bottom for top, bottom in h.changed_rows),
        f"the row of the changed book ({row}) was not repainted, dataChanged saw {h.changed_rows}",
    )
    shown = h.cell(book_id, "formats")
    check("TXT" in shown and "EPUB" in shown, f"the list shows formats {shown!r} after addFormat")
    step("addFormat puts a second format on the row and the row is repainted")

    # 6. Both refreshes are safe to run again: no phantom row, no id twice.
    adder.show_new_books(h.gui, 1, book_id)
    adder.show_new_books(h.gui, 1)
    adder.redraw_book(h.gui, book_id)
    h.app.processEvents()
    listed = h.view_search("")
    check(h.rows() == 2, f"running the refreshes again changed the row count to {h.rows()}")
    check(
        sorted(listed) == sorted({h.seed_id, book_id}),
        f"the view lists {listed}, expected the two books once each",
    )
    check(len(h.inserted) == 1, f"rowsInserted fired {len(h.inserted)} times for one new book")
    step("show_new_books and redraw_book can run again without adding rows")


def main() -> int:
    plugin = find_plugin("Bindery Bridge")
    if plugin is None:
        print(
            "FAIL: the Bindery Bridge plugin is not installed in this Calibre. "
            "Install the zip first: calibre-customize -a <zip>",
            file=sys.stderr,
        )
        return 1
    print(
        "Calibre {} with Bindery Bridge {} from {}".format(
            ".".join(str(p) for p in numeric_version[:3]),
            ".".join(str(p) for p in plugin.version),
            getattr(plugin, "plugin_path", "?"),
        ),
        flush=True,
    )
    app = Application([])
    workdir = tempfile.mkdtemp(prefix="bridge-model-check-")
    harness = None
    try:
        # Imported here, after the installed check above, and by the name
        # Calibre itself uses for a plugin loaded from its zip.
        import calibre_plugins.bindery_bridge.plugin as bridge
        from calibre_plugins.bindery_bridge.plugin import adder

        harness = Harness(app, workdir, bridge, adder)
        run_checks(harness)
    except CheckFailed as exc:
        print(f"FAIL: {exc}", file=sys.stderr, flush=True)
        return 1
    except Exception:
        traceback.print_exc()
        print("FAIL: unexpected error, see the traceback above", file=sys.stderr, flush=True)
        return 1
    finally:
        if harness is not None:
            harness.close()
        shutil.rmtree(workdir, ignore_errors=True)
    print("PASS: model check", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
