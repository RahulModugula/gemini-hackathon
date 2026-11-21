"""One process-wide lock for PyMuPDF. MuPDF is not thread-safe, and requests (/api/check, the SSE worker threads, the
/api/video claims in the threadpool) all open, render and read drawings from PDFs. Hold LOCK around every document
open/render/get_drawings/text extraction on the request path. Re-entrant, so a locked caller can call a locked helper."""
import threading

LOCK = threading.RLock()
