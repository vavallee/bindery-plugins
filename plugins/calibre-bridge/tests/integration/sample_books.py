"""Book files for the real Calibre checks, generated on the fly.

No binary fixture is committed. ``write_epub`` builds a small EPUB 2 with
zipfile, which is enough for Calibre to read the metadata and copy the file
into a library. Standard library only: this module is imported both by the
host interpreter (``run.py``) and by Calibre's embedded one (``model_check.py``).
"""

import zipfile
from xml.sax.saxutils import escape

_CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

_OPF = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="BookId">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
    <dc:title>{title}</dc:title>
    <dc:creator opf:role="aut">{author}</dc:creator>
    <dc:language>en</dc:language>
    <dc:identifier id="BookId" opf:scheme="UUID">urn:uuid:{uuid}</dc:identifier>
  </metadata>
  <manifest>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="chapter1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine toc="ncx">
    <itemref idref="chapter1"/>
  </spine>
</package>
"""

_NCX = """<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head>
    <meta name="dtb:uid" content="urn:uuid:{uuid}"/>
    <meta name="dtb:depth" content="1"/>
    <meta name="dtb:totalPageCount" content="0"/>
    <meta name="dtb:maxPageNumber" content="0"/>
  </head>
  <docTitle><text>{title}</text></docTitle>
  <navMap>
    <navPoint id="chapter1" playOrder="1">
      <navLabel><text>Chapter 1</text></navLabel>
      <content src="chapter1.xhtml"/>
    </navPoint>
  </navMap>
</ncx>
"""

_CHAPTER = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
  <head><title>{title}</title></head>
  <body>
    <h1>Chapter 1</h1>
    <p>Generated for the Calibre Bridge integration check.</p>
  </body>
</html>
"""


def write_epub(path: str, title: str, author: str, uuid: str) -> str:
    """Write a minimal valid EPUB to ``path`` and return ``path``."""
    fields = {"title": escape(title), "author": escape(author), "uuid": escape(uuid)}
    with zipfile.ZipFile(path, "w") as zf:
        # The mimetype entry has to come first and be stored uncompressed.
        zf.writestr(
            zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED
        )
        for name, body in (
            ("META-INF/container.xml", _CONTAINER),
            ("OEBPS/content.opf", _OPF.format(**fields)),
            ("OEBPS/toc.ncx", _NCX.format(**fields)),
            ("OEBPS/chapter1.xhtml", _CHAPTER.format(**fields)),
        ):
            zf.writestr(name, body, compress_type=zipfile.ZIP_DEFLATED)
    return path


def write_text_book(path: str, title: str) -> str:
    """Write a plain text book, used as a second format of an existing row."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"{title}\n\nGenerated for the Calibre Bridge integration check.\n")
    return path
