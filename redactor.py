"""
PDF Redactor — A macOS application for true PDF redaction.

Supports visual rectangle selection and text search to permanently
remove sensitive content from PDF documents.

Usage:
    source venv/bin/activate && python redactor.py
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import uuid
import os
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import fitz  # PyMuPDF
from PIL import Image, ImageTk

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RENDER_DPI = 150
DEFAULT_WINDOW_SIZE = "1200x800"
MIN_WINDOW_WIDTH = 900
MIN_WINDOW_HEIGHT = 600
SIDEBAR_WIDTH = 280
MIN_RECT_SIZE = 5  # Minimum pixel size to count as intentional draw
CACHE_LIMIT = 5

# Visual style for redaction overlays
REDACT_FILL = "red"
REDACT_STIPPLE = "gray25"
REDACT_OUTLINE = "#cc0000"
REDACT_OUTLINE_WIDTH = 2
SELECTED_OUTLINE = "#0066ff"
SELECTED_OUTLINE_WIDTH = 3
TEMP_OUTLINE = "red"
TEMP_DASH = (4, 4)

# Applied redaction color (what PyMuPDF draws)
APPLIED_FILL_RGB = (0, 0, 0)  # Black


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class RedactionRect:
    """A pending redaction area on a specific PDF page."""

    id: str
    page_num: int
    pdf_rect: Tuple[float, float, float, float]  # (x0, y0, x1, y1) PDF points
    source: str  # 'manual' or 'search'
    search_term: str = ""

    @staticmethod
    def create(page_num: int, pdf_rect: Tuple[float, float, float, float],
               source: str, search_term: str = "") -> "RedactionRect":
        return RedactionRect(
            id=uuid.uuid4().hex[:8],
            page_num=page_num,
            pdf_rect=pdf_rect,
            source=source,
            search_term=search_term,
        )

    @property
    def description(self) -> str:
        if self.source == "search":
            return f'"{self.search_term}"'
        x0, y0, x1, y1 = self.pdf_rect
        return f"({x0:.0f},{y0:.0f})-({x1:.0f},{y1:.0f})"


# ---------------------------------------------------------------------------
# RedactionModel — document state + pending redactions
# ---------------------------------------------------------------------------

class RedactionModel:
    """Manages the PDF document and tracks pending redactions."""

    def __init__(self):
        self.doc: Optional[fitz.Document] = None
        self.file_path: Optional[str] = None
        self.current_page: int = 0
        self.pending: Dict[int, List[RedactionRect]] = {}  # page_num -> [rects]
        self.is_applied: bool = False

    # -- Document lifecycle ---------------------------------------------------

    def open_document(self, path: str) -> None:
        self.close_document()
        self.doc = fitz.open(path)
        self.file_path = path
        self.current_page = 0
        self.pending = {}
        self.is_applied = False

    def close_document(self) -> None:
        if self.doc:
            self.doc.close()
        self.doc = None
        self.file_path = None
        self.current_page = 0
        self.pending = {}
        self.is_applied = False

    @property
    def page_count(self) -> int:
        return len(self.doc) if self.doc else 0

    def get_page(self, page_num: int) -> fitz.Page:
        return self.doc[page_num]

    # -- Redaction management -------------------------------------------------

    def add_redaction(self, redaction: RedactionRect) -> None:
        page_list = self.pending.setdefault(redaction.page_num, [])
        page_list.append(redaction)

    def remove_redaction(self, redaction_id: str) -> Optional[RedactionRect]:
        for page_num, rects in self.pending.items():
            for r in rects:
                if r.id == redaction_id:
                    rects.remove(r)
                    if not rects:
                        del self.pending[page_num]
                    return r
        return None

    def get_page_redactions(self, page_num: int) -> List[RedactionRect]:
        return self.pending.get(page_num, [])

    def all_redactions(self) -> List[RedactionRect]:
        result = []
        for page_num in sorted(self.pending.keys()):
            result.extend(self.pending[page_num])
        return result

    def redaction_count(self) -> int:
        return sum(len(v) for v in self.pending.values())

    def has_pending(self) -> bool:
        return self.redaction_count() > 0

    def clear_page_redactions(self, page_num: int) -> int:
        removed = len(self.pending.get(page_num, []))
        self.pending.pop(page_num, None)
        return removed

    def clear_all_redactions(self) -> int:
        count = self.redaction_count()
        self.pending.clear()
        return count

    # -- Search ---------------------------------------------------------------

    def search_text(self, text: str) -> Dict[int, list]:
        """Search all pages for text. Returns {page_num: [fitz.Rect]}."""
        fitz.TOOLS.set_small_glyph_heights(True)
        results: Dict[int, list] = {}
        for i in range(self.page_count):
            page = self.doc[i]
            hits = page.search_for(text, quads=True)
            if hits:
                results[i] = hits
        return results

    # -- Apply & Save ---------------------------------------------------------

    def apply_redactions(self) -> int:
        """Apply all pending redactions. Returns count applied. IRREVERSIBLE."""
        count = 0
        for page_num, rects in self.pending.items():
            page = self.doc[page_num]
            for r in rects:
                fitz_rect = fitz.Rect(r.pdf_rect)
                page.add_redact_annot(fitz_rect, fill=APPLIED_FILL_RGB)
                count += 1
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS,
                                  graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED)
        self.pending.clear()
        self.is_applied = True
        return count

    def save_document(self, path: str) -> None:
        """Save with scrub + garbage collection for true data removal."""
        self.doc.scrub()
        self.doc.save(path, garbage=3, deflate=True)


# ---------------------------------------------------------------------------
# PDFRenderer — converts PDF pages to display images
# ---------------------------------------------------------------------------

class PDFRenderer:
    """Renders PDF pages to PIL Images with an LRU cache."""

    def __init__(self):
        self._cache: OrderedDict[int, Image.Image] = OrderedDict()

    def render_page(self, page: fitz.Page, page_num: int) -> Image.Image:
        """Render a page at RENDER_DPI. Returns a PIL Image."""
        if page_num in self._cache:
            self._cache.move_to_end(page_num)
            return self._cache[page_num]

        pix = page.get_pixmap(dpi=RENDER_DPI)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

        self._cache[page_num] = img
        if len(self._cache) > CACHE_LIMIT:
            self._cache.popitem(last=False)

        return img

    def invalidate(self, page_num: Optional[int] = None) -> None:
        if page_num is not None:
            self._cache.pop(page_num, None)
        else:
            self._cache.clear()

    @staticmethod
    def render_scale() -> float:
        return RENDER_DPI / 72.0


# ---------------------------------------------------------------------------
# CanvasController — mouse interaction + coordinate mapping
# ---------------------------------------------------------------------------

class CanvasController:
    """Handles canvas drawing, mouse events, and coordinate mapping."""

    def __init__(self, canvas: tk.Canvas, model: RedactionModel,
                 renderer: PDFRenderer, on_change: callable):
        self.canvas = canvas
        self.model = model
        self.renderer = renderer
        self.on_change = on_change  # callback when redactions change

        self._total_scale: float = 1.0
        self._drawing: bool = False
        self._draw_start: Optional[Tuple[float, float]] = None
        self._temp_rect_id: Optional[int] = None
        self._photo: Optional[ImageTk.PhotoImage] = None  # prevent GC
        self._pil_image: Optional[Image.Image] = None

        # Maps canvas item id -> redaction id
        self._canvas_to_rid: Dict[int, str] = {}
        self._rid_to_canvas: Dict[str, int] = {}
        self._selected_rid: Optional[str] = None

        self._bind_events()

    def _bind_events(self) -> None:
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        # Right-click / Control-click for context menu
        self.canvas.bind("<Button-2>", self._on_right_click)
        self.canvas.bind("<Control-Button-1>", self._on_right_click)

    # -- Coordinate helpers ---------------------------------------------------

    def _event_coords(self, event) -> Tuple[float, float]:
        """Get scroll-aware canvas coordinates from an event."""
        return self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)

    def canvas_to_pdf(self, cx: float, cy: float) -> Tuple[float, float]:
        if self._total_scale == 0:
            return 0.0, 0.0
        return cx / self._total_scale, cy / self._total_scale

    def pdf_to_canvas(self, px: float, py: float) -> Tuple[float, float]:
        return px * self._total_scale, py * self._total_scale

    # -- Drawing events -------------------------------------------------------

    def _on_press(self, event) -> None:
        cx, cy = self._event_coords(event)
        self._drawing = True
        self._draw_start = (cx, cy)
        self._temp_rect_id = self.canvas.create_rectangle(
            cx, cy, cx, cy,
            outline=TEMP_OUTLINE, width=2, dash=TEMP_DASH,
            tags=("temp_drawing",)
        )

    def _on_drag(self, event) -> None:
        if not self._drawing or self._temp_rect_id is None:
            return
        cx, cy = self._event_coords(event)
        x0, y0 = self._draw_start
        self.canvas.coords(self._temp_rect_id, x0, y0, cx, cy)

    def _on_release(self, event) -> None:
        if not self._drawing:
            return
        self._drawing = False

        cx, cy = self._event_coords(event)
        x0, y0 = self._draw_start

        # Clean up temp rectangle
        if self._temp_rect_id is not None:
            self.canvas.delete(self._temp_rect_id)
            self._temp_rect_id = None

        # Normalize coordinates
        rx0, rx1 = min(x0, cx), max(x0, cx)
        ry0, ry1 = min(y0, cy), max(y0, cy)

        # Ignore tiny rectangles (accidental clicks)
        if (rx1 - rx0) < MIN_RECT_SIZE or (ry1 - ry0) < MIN_RECT_SIZE:
            return

        # Convert to PDF coordinates
        pdf_x0, pdf_y0 = self.canvas_to_pdf(rx0, ry0)
        pdf_x1, pdf_y1 = self.canvas_to_pdf(rx1, ry1)

        # Create redaction
        redaction = RedactionRect.create(
            page_num=self.model.current_page,
            pdf_rect=(pdf_x0, pdf_y0, pdf_x1, pdf_y1),
            source="manual",
        )
        self.model.add_redaction(redaction)

        # Draw permanent overlay
        self._draw_overlay(redaction)
        self.on_change()

    def _on_right_click(self, event) -> None:
        """Right-click to remove a redaction rectangle."""
        cx, cy = self._event_coords(event)
        items = self.canvas.find_overlapping(cx - 3, cy - 3, cx + 3, cy + 3)
        for item in items:
            if item in self._canvas_to_rid:
                rid = self._canvas_to_rid[item]
                self._remove_overlay(rid)
                self.model.remove_redaction(rid)
                self.on_change()
                return

    # -- Overlay drawing ------------------------------------------------------

    def _draw_overlay(self, redaction: RedactionRect) -> int:
        """Draw a semi-transparent red rectangle for a pending redaction."""
        x0, y0 = self.pdf_to_canvas(redaction.pdf_rect[0], redaction.pdf_rect[1])
        x1, y1 = self.pdf_to_canvas(redaction.pdf_rect[2], redaction.pdf_rect[3])

        item_id = self.canvas.create_rectangle(
            x0, y0, x1, y1,
            fill=REDACT_FILL, stipple=REDACT_STIPPLE,
            outline=REDACT_OUTLINE, width=REDACT_OUTLINE_WIDTH,
            tags=("redaction", f"rid_{redaction.id}"),
        )
        self._canvas_to_rid[item_id] = redaction.id
        self._rid_to_canvas[redaction.id] = item_id
        return item_id

    def _remove_overlay(self, redaction_id: str) -> None:
        item_id = self._rid_to_canvas.pop(redaction_id, None)
        if item_id is not None:
            self._canvas_to_rid.pop(item_id, None)
            self.canvas.delete(item_id)

    def draw_all_overlays(self, page_num: int) -> None:
        """Draw overlays for all pending redactions on the given page."""
        for r in self.model.get_page_redactions(page_num):
            self._draw_overlay(r)

    def clear_overlays(self) -> None:
        """Remove all overlay rectangles from the canvas."""
        self.canvas.delete("redaction")
        self._canvas_to_rid.clear()
        self._rid_to_canvas.clear()
        self._selected_rid = None

    def select_redaction(self, redaction_id: str) -> None:
        """Highlight a specific redaction on the canvas."""
        self.deselect_all()
        item_id = self._rid_to_canvas.get(redaction_id)
        if item_id:
            self.canvas.itemconfigure(item_id, outline=SELECTED_OUTLINE,
                                      width=SELECTED_OUTLINE_WIDTH)
            self._selected_rid = redaction_id

    def deselect_all(self) -> None:
        if self._selected_rid:
            item_id = self._rid_to_canvas.get(self._selected_rid)
            if item_id:
                self.canvas.itemconfigure(item_id, outline=REDACT_OUTLINE,
                                          width=REDACT_OUTLINE_WIDTH)
        self._selected_rid = None

    # -- Page display ---------------------------------------------------------

    def display_page(self, page_num: int, fit_width: Optional[int] = None) -> None:
        """Render and display a page, including all redaction overlays."""
        if not self.model.doc:
            return

        page = self.model.get_page(page_num)
        pil_image = self.renderer.render_page(page, page_num)
        self._pil_image = pil_image

        # Calculate scale to fit canvas width
        if fit_width is None:
            fit_width = max(self.canvas.winfo_width() - 20, 400)

        display_scale = fit_width / pil_image.width
        render_scale = PDFRenderer.render_scale()
        self._total_scale = render_scale * display_scale

        # Scale image for display
        new_w = int(pil_image.width * display_scale)
        new_h = int(pil_image.height * display_scale)
        display_img = pil_image.resize((new_w, new_h), Image.LANCZOS)

        # Keep reference to prevent garbage collection
        self._photo = ImageTk.PhotoImage(display_img)

        # Update canvas
        self.canvas.delete("all")
        self._canvas_to_rid.clear()
        self._rid_to_canvas.clear()
        self._selected_rid = None

        self.canvas.create_image(0, 0, anchor="nw", image=self._photo,
                                 tags=("page_image",))
        self.canvas.config(scrollregion=(0, 0, new_w, new_h))

        # Draw redaction overlays for this page
        self.draw_all_overlays(page_num)


# ---------------------------------------------------------------------------
# RedactorApp — main application window
# ---------------------------------------------------------------------------

class RedactorApp:
    """Main PDF Redactor application."""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("PDF Redactor")
        self.root.geometry(DEFAULT_WINDOW_SIZE)
        self.root.minsize(MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)

        # Core components
        self.model = RedactionModel()
        self.renderer = PDFRenderer()

        # Build UI
        self._build_menu()
        self._build_toolbar()
        self._build_main_area()
        self._build_status_bar()

        # Canvas controller (after canvas is created)
        self.controller = CanvasController(
            self.canvas, self.model, self.renderer,
            on_change=self._on_redaction_change,
        )

        # Resize debounce
        self._resize_job = None
        self.canvas.bind("<Configure>", self._on_canvas_configure)

        # Keyboard shortcuts
        self.root.bind("<Command-o>", lambda e: self._on_open())
        self.root.bind("<Command-s>", lambda e: self._on_save())
        self.root.bind("<Command-w>", lambda e: self._on_close_doc())

        self._update_ui_state()

    # -- Menu bar -------------------------------------------------------------

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)
        self.root.config(menu=menubar)

        # File menu
        file_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="File", menu=file_menu)
        file_menu.add_command(label="Open...", command=self._on_open,
                              accelerator="Cmd+O")
        file_menu.add_command(label="Save Redacted As...", command=self._on_save,
                              accelerator="Cmd+S")
        file_menu.add_separator()
        file_menu.add_command(label="Close Document", command=self._on_close_doc,
                              accelerator="Cmd+W")

        # Edit menu
        edit_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Edit", menu=edit_menu)
        edit_menu.add_command(label="Clear Page Redactions",
                              command=self._on_clear_page)
        edit_menu.add_command(label="Clear All Redactions",
                              command=self._on_clear_all)

    # -- Toolbar --------------------------------------------------------------

    def _build_toolbar(self) -> None:
        toolbar = ttk.Frame(self.root, padding=(5, 3))
        toolbar.pack(side="top", fill="x")

        # Open / Save
        ttk.Button(toolbar, text="Open", command=self._on_open,
                    width=6).pack(side="left", padx=(0, 2))
        self.save_btn = ttk.Button(toolbar, text="Save", command=self._on_save,
                                   width=6)
        self.save_btn.pack(side="left", padx=(0, 8))

        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y",
                                                        padx=5, pady=2)

        # Page navigation
        self.prev_btn = ttk.Button(toolbar, text="\u25C0", command=self._prev_page,
                                   width=3)
        self.prev_btn.pack(side="left", padx=(0, 3))

        self.page_label = ttk.Label(toolbar, text="No document", width=16,
                                    anchor="center")
        self.page_label.pack(side="left", padx=3)

        self.next_btn = ttk.Button(toolbar, text="\u25B6", command=self._next_page,
                                   width=3)
        self.next_btn.pack(side="left", padx=(3, 0))

        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y",
                                                        padx=8, pady=2)

        # Page jump
        ttk.Label(toolbar, text="Go to:").pack(side="left", padx=(0, 3))
        self.page_entry = ttk.Entry(toolbar, width=5)
        self.page_entry.pack(side="left", padx=(0, 3))
        self.page_entry.bind("<Return>", self._on_page_jump)

    # -- Main area (canvas + sidebar) -----------------------------------------

    def _build_main_area(self) -> None:
        main = ttk.PanedWindow(self.root, orient="horizontal")
        main.pack(fill="both", expand=True, padx=2, pady=2)

        # --- Canvas frame (left) ---
        canvas_frame = ttk.Frame(main)
        main.add(canvas_frame, weight=3)

        self.canvas = tk.Canvas(canvas_frame, bg="#e0e0e0",
                                highlightthickness=0, cursor="crosshair")
        v_scroll = ttk.Scrollbar(canvas_frame, orient="vertical",
                                 command=self.canvas.yview)
        h_scroll = ttk.Scrollbar(canvas_frame, orient="horizontal",
                                 command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=v_scroll.set,
                              xscrollcommand=h_scroll.set)

        # Grid layout for canvas + scrollbars
        self.canvas.grid(row=0, column=0, sticky="nsew")
        v_scroll.grid(row=0, column=1, sticky="ns")
        h_scroll.grid(row=1, column=0, sticky="ew")
        canvas_frame.grid_rowconfigure(0, weight=1)
        canvas_frame.grid_columnconfigure(0, weight=1)

        # Mouse wheel scrolling
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)

        # --- Sidebar (right) ---
        sidebar = ttk.Frame(main, width=SIDEBAR_WIDTH)
        main.add(sidebar, weight=0)

        # -- Search section --
        search_frame = ttk.LabelFrame(sidebar, text="Search Text", padding=8)
        search_frame.pack(fill="x", padx=5, pady=(5, 3))

        search_row = ttk.Frame(search_frame)
        search_row.pack(fill="x")

        self.search_entry = ttk.Entry(search_row)
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self.search_entry.bind("<Return>", lambda e: self._on_search())

        ttk.Button(search_row, text="Find All",
                    command=self._on_search, width=8).pack(side="right")

        self.search_status = ttk.Label(search_frame, text="", foreground="gray")
        self.search_status.pack(fill="x", pady=(4, 0))

        # -- Pending redactions list --
        redact_frame = ttk.LabelFrame(sidebar, text="Pending Redactions",
                                       padding=8)
        redact_frame.pack(fill="both", expand=True, padx=5, pady=3)

        # Treeview
        columns = ("page", "type", "detail")
        self.tree = ttk.Treeview(redact_frame, columns=columns,
                                  show="headings", height=12,
                                  selectmode="browse")
        self.tree.heading("page", text="Page")
        self.tree.heading("type", text="Type")
        self.tree.heading("detail", text="Detail")
        self.tree.column("page", width=45, minwidth=40, anchor="center")
        self.tree.column("type", width=60, minwidth=50)
        self.tree.column("detail", width=130, minwidth=80)

        tree_scroll = ttk.Scrollbar(redact_frame, orient="vertical",
                                    command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)

        self.tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")

        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        # Buttons under the treeview
        btn_frame = ttk.Frame(sidebar)
        btn_frame.pack(fill="x", padx=5, pady=3)

        self.remove_btn = ttk.Button(btn_frame, text="Remove Selected",
                                     command=self._on_remove_selected)
        self.remove_btn.pack(side="left", padx=(0, 5))

        self.clear_page_btn = ttk.Button(btn_frame, text="Clear Page",
                                         command=self._on_clear_page)
        self.clear_page_btn.pack(side="left", padx=(0, 5))

        self.clear_all_btn = ttk.Button(btn_frame, text="Clear All",
                                        command=self._on_clear_all)
        self.clear_all_btn.pack(side="left")

        # -- Apply section --
        action_frame = ttk.LabelFrame(sidebar, text="Actions", padding=8)
        action_frame.pack(fill="x", padx=5, pady=(3, 5))

        self.apply_btn = ttk.Button(action_frame, text="APPLY REDACTIONS",
                                    command=self._on_apply)
        self.apply_btn.pack(fill="x", pady=2)

        ttk.Label(action_frame,
                  text="Warning: applying is irreversible.\nContent will be permanently removed.",
                  foreground="gray", font=("TkDefaultFont", 10),
                  wraplength=240, justify="center").pack(pady=(2, 0))

    # -- Status bar -----------------------------------------------------------

    def _build_status_bar(self) -> None:
        status_frame = ttk.Frame(self.root, relief="sunken", padding=(5, 2))
        status_frame.pack(side="bottom", fill="x")
        self.status_label = ttk.Label(status_frame, text="Ready — open a PDF to begin")
        self.status_label.pack(side="left")

    # -- UI state management --------------------------------------------------

    def _update_ui_state(self) -> None:
        """Enable/disable controls based on document state."""
        has_doc = self.model.doc is not None
        has_pending = self.model.has_pending()

        state_doc = "normal" if has_doc else "disabled"
        state_pending = "normal" if (has_doc and has_pending) else "disabled"

        self.save_btn.config(state=state_doc)
        self.prev_btn.config(state=state_doc)
        self.next_btn.config(state=state_doc)
        self.search_entry.config(state=state_doc)
        self.apply_btn.config(state=state_pending)
        self.remove_btn.config(state=state_pending)
        self.clear_page_btn.config(state=state_pending)
        self.clear_all_btn.config(state=state_pending)

        if has_doc:
            p = self.model.current_page
            n = self.model.page_count
            self.page_label.config(text=f"Page {p + 1} / {n}")
            count = self.model.redaction_count()
            fname = os.path.basename(self.model.file_path)
            status = f"{fname} — Page {p + 1}/{n}"
            if count:
                status += f" — {count} pending redaction{'s' if count != 1 else ''}"
            self.status_label.config(text=status)
        else:
            self.page_label.config(text="No document")
            self.status_label.config(text="Ready — open a PDF to begin")

    def _refresh_page(self) -> None:
        """Re-render and display the current page."""
        if not self.model.doc:
            return
        self.controller.display_page(self.model.current_page)
        self._update_ui_state()

    def _update_redaction_list(self) -> None:
        """Rebuild the treeview with all pending redactions."""
        self.tree.delete(*self.tree.get_children())
        for r in self.model.all_redactions():
            self.tree.insert("", "end", iid=r.id,
                             values=(r.page_num + 1, r.source.capitalize(),
                                     r.description))
        self._update_ui_state()

    def _on_redaction_change(self) -> None:
        """Called whenever redactions are added or removed."""
        self._update_redaction_list()

    # -- File operations ------------------------------------------------------

    def _on_open(self) -> None:
        path = filedialog.askopenfilename(
            title="Open PDF",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            self.model.open_document(path)
            self.renderer.invalidate()
            self._update_redaction_list()
            self._refresh_page()
            self.root.title(f"PDF Redactor — {os.path.basename(path)}")
        except Exception as e:
            messagebox.showerror("Error", f"Could not open PDF:\n{e}")

    def _on_save(self) -> None:
        if not self.model.doc:
            return

        # Warn about unapplied redactions
        if self.model.has_pending():
            answer = messagebox.askyesnocancel(
                "Unapplied Redactions",
                "You have pending redactions that have not been applied.\n\n"
                "Apply them before saving?\n\n"
                "Yes = Apply & Save\n"
                "No = Save without applying\n"
                "Cancel = Go back",
            )
            if answer is None:
                return
            if answer:
                self._do_apply()

        # Determine default filename
        base = os.path.splitext(os.path.basename(self.model.file_path))[0]
        default_name = f"{base}_redacted.pdf"
        default_dir = os.path.dirname(self.model.file_path)

        path = filedialog.asksaveasfilename(
            title="Save Redacted PDF",
            defaultextension=".pdf",
            initialfile=default_name,
            initialdir=default_dir,
            filetypes=[("PDF files", "*.pdf")],
        )
        if not path:
            return

        try:
            self.model.save_document(path)
            messagebox.showinfo("Saved", f"Redacted PDF saved to:\n{path}")
            self.status_label.config(text=f"Saved to {os.path.basename(path)}")
        except Exception as e:
            messagebox.showerror("Error", f"Could not save PDF:\n{e}")

    def _on_close_doc(self) -> None:
        if self.model.has_pending():
            if not messagebox.askyesno("Close",
                                        "Discard pending redactions?"):
                return
        self.model.close_document()
        self.renderer.invalidate()
        self.canvas.delete("all")
        self.controller.clear_overlays()
        self._update_redaction_list()
        self.root.title("PDF Redactor")
        self._update_ui_state()

    # -- Page navigation ------------------------------------------------------

    def _prev_page(self) -> None:
        if self.model.current_page > 0:
            self.model.current_page -= 1
            self._refresh_page()

    def _next_page(self) -> None:
        if self.model.current_page < self.model.page_count - 1:
            self.model.current_page += 1
            self._refresh_page()

    def _on_page_jump(self, event=None) -> None:
        try:
            page = int(self.page_entry.get()) - 1  # 1-indexed input
            if 0 <= page < self.model.page_count:
                self.model.current_page = page
                self._refresh_page()
                self.page_entry.delete(0, "end")
            else:
                messagebox.showwarning("Invalid Page",
                                       f"Page must be 1-{self.model.page_count}")
        except ValueError:
            messagebox.showwarning("Invalid Input", "Enter a page number.")

    def _on_mousewheel(self, event) -> None:
        """Handle mouse wheel scrolling on the canvas."""
        self.canvas.yview_scroll(-1 * event.delta, "units")

    # -- Canvas resize --------------------------------------------------------

    def _on_canvas_configure(self, event) -> None:
        """Debounced handler for canvas resize."""
        if self._resize_job:
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(200, self._do_resize)

    def _do_resize(self) -> None:
        self._resize_job = None
        if self.model.doc:
            self._refresh_page()

    # -- Search ---------------------------------------------------------------

    def _on_search(self) -> None:
        if not self.model.doc:
            return
        text = self.search_entry.get().strip()
        if not text:
            self.search_status.config(text="Enter text to search for")
            return

        results = self.model.search_text(text)
        if not results:
            self.search_status.config(text="No matches found")
            return

        total_matches = 0
        pages_with_matches = len(results)

        for page_num, quads in results.items():
            for q in quads:
                rect = q.rect  # bounding rect of the quad
                redaction = RedactionRect.create(
                    page_num=page_num,
                    pdf_rect=(rect.x0, rect.y0, rect.x1, rect.y1),
                    source="search",
                    search_term=text,
                )
                self.model.add_redaction(redaction)
                total_matches += 1

        self.search_status.config(
            text=f"Found {total_matches} match{'es' if total_matches != 1 else ''} "
                 f"on {pages_with_matches} page{'s' if pages_with_matches != 1 else ''}"
        )

        # Navigate to first match
        first_page = min(results.keys())
        self.model.current_page = first_page

        self._update_redaction_list()
        self._refresh_page()

    # -- Redaction management -------------------------------------------------

    def _on_tree_select(self, event=None) -> None:
        """When user clicks a redaction in the treeview."""
        selection = self.tree.selection()
        if not selection:
            return

        rid = selection[0]
        # Find the redaction to get its page
        for r in self.model.all_redactions():
            if r.id == rid:
                if r.page_num != self.model.current_page:
                    self.model.current_page = r.page_num
                    self._refresh_page()
                self.controller.select_redaction(rid)
                break

    def _on_remove_selected(self) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        rid = selection[0]
        self.model.remove_redaction(rid)
        self.controller._remove_overlay(rid)
        self._on_redaction_change()

    def _on_clear_page(self) -> None:
        removed = self.model.clear_page_redactions(self.model.current_page)
        if removed:
            self._refresh_page()
            self._on_redaction_change()

    def _on_clear_all(self) -> None:
        if not self.model.has_pending():
            return
        if messagebox.askyesno("Clear All",
                                "Remove all pending redactions?"):
            self.model.clear_all_redactions()
            self._refresh_page()
            self._on_redaction_change()

    # -- Apply redactions -----------------------------------------------------

    def _on_apply(self) -> None:
        if not self.model.has_pending():
            messagebox.showinfo("Nothing to Apply",
                                "No pending redactions to apply.")
            return

        count = self.model.redaction_count()
        pages = len(self.model.pending)

        answer = messagebox.askyesno(
            "Apply Redactions",
            f"Apply {count} redaction{'s' if count != 1 else ''} "
            f"across {pages} page{'s' if pages != 1 else ''}?\n\n"
            "This will PERMANENTLY remove the content under\n"
            "the marked areas. This cannot be undone.\n\n"
            "The original file will not be modified until you Save.",
        )
        if not answer:
            return

        self._do_apply()

    def _do_apply(self) -> None:
        """Actually apply the redactions."""
        try:
            count = self.model.apply_redactions()
            self.renderer.invalidate()  # force re-render all pages
            self._refresh_page()
            self._update_redaction_list()
            self.search_status.config(text="")
            self.status_label.config(
                text=f"Applied {count} redaction{'s' if count != 1 else ''}. "
                     "Save to write to file."
            )
        except Exception as e:
            messagebox.showerror("Error", f"Failed to apply redactions:\n{e}")

    # -- Run ------------------------------------------------------------------

    def run(self) -> None:
        self.root.mainloop()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Enable tight text bounding boxes for accurate redaction
    fitz.TOOLS.set_small_glyph_heights(True)
    app = RedactorApp()
    app.run()
