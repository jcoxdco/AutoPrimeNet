#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Optional Tk launcher for AutoPrimeNet: builds CLI arguments and runs autoprimenet in a subprocess."""

from __future__ import division, print_function, unicode_literals

import io
import json
import os
import platform
from email.utils import parseaddr
import queue
import subprocess
import sys
import tempfile
import threading
import webbrowser

# PrimeNet work-type codes for the GUI (mirrors autoprimenet.py SUPPORTED).
MAX_PRIMENET_EXP = 1000000000


class _WP(object):
	FACTOR = 2
	PFACTOR = 4
	ECM_SMALL = 5
	ECM_COFACTOR = 8
	GPU_FACTOR = 12
	LL_FIRST = 100
	LL_DBLCHK = 101
	LL_WORLD_RECORD = 102
	LL_100M = 104
	PRP_FIRST = 150
	PRP_DBLCHK = 151
	PRP_WORLD_RECORD = 152
	PRP_100M = 153
	PRP_NO_PMINUS1 = 154
	PRP_DC_PROOF = 155
	PRP_COFACTOR = 160
	PRP_COFACTOR_DBLCHK = 161


_LL_ZERO_SHIFT = 106

_WORKPREF_LABELS = {
	_WP.FACTOR: "Trial factoring",
	_WP.PFACTOR: "P-1 factoring",
	_WP.ECM_SMALL: "ECM factoring",
	_WP.ECM_COFACTOR: "ECM on Mersenne cofactors",
	_WP.GPU_FACTOR: "Trial factoring GPU",
	_WP.LL_FIRST: "First time LL tests",
	_WP.LL_DBLCHK: "Double-check LL tests",
	_WP.LL_WORLD_RECORD: "World record LL tests",
	_WP.LL_100M: "100 million digit LL tests",
	_LL_ZERO_SHIFT: "Double-check LL (zero shift)",
	_WP.PRP_FIRST: "First time PRP tests",
	_WP.PRP_DBLCHK: "Double-check PRP tests",
	_WP.PRP_WORLD_RECORD: "World record PRP tests",
	_WP.PRP_100M: "100 million digit PRP tests",
	_WP.PRP_NO_PMINUS1: "First time PRP needing P-1",
	_WP.PRP_DC_PROOF: "Double-check PRP with proof",
	_WP.PRP_COFACTOR: "First time PRP on Mersenne cofactors",
	_WP.PRP_COFACTOR_DBLCHK: "Double-check PRP on cofactors",
}


def supported_workprefs(program):
	"""Return sorted work preference integer codes valid for the given program key."""
	p = (program or "mlucas").strip()
	mfaktc = p == "mfaktc"
	mfakto = p == "mfakto"
	if mfaktc or mfakto:
		return sorted({_WP.FACTOR, _WP.GPU_FACTOR})
	mlucas = p == "mlucas"
	gpuowl = p == "gpuowl"
	prpll = p == "prpll"
	prmers = p == "prmers"
	cudalucas = p == "cudalucas"
	s = (
		[_WP.LL_FIRST, _WP.LL_DBLCHK, _WP.LL_WORLD_RECORD, _WP.LL_100M]
		+ ([] if cudalucas else [_WP.PRP_FIRST, _WP.PRP_DBLCHK, _WP.PRP_WORLD_RECORD, _WP.PRP_100M])
		+ ([_LL_ZERO_SHIFT, _WP.PRP_DC_PROOF] if gpuowl or prpll or prmers else [])
		+ ([_WP.PRP_NO_PMINUS1] if gpuowl or mlucas else [])
		+ ([] if prpll else [_WP.PFACTOR])
		+ ([_WP.ECM_SMALL, _WP.ECM_COFACTOR] if prmers else [])
		+ ([_WP.PRP_COFACTOR, _WP.PRP_COFACTOR_DBLCHK] if mlucas or prmers else [])
	)
	return sorted(set(s))


def workpref_is_trial_factoring(workpref_code):
	"""True for PrimeNet trial factoring work types (CPU or GPU)."""
	try:
		c = int(workpref_code)
	except (TypeError, ValueError):
		return False
	return c in (_WP.FACTOR, _WP.GPU_FACTOR)


def workpref_requires_gpu_picker(program, workpref_code):
	"""True when PrimeNet registration should target a GPU for the chosen program/work type."""
	p = (program or "mlucas").strip()
	try:
		code = int(workpref_code)
	except (TypeError, ValueError):
		return False
	if p in ("mfaktc", "mfakto"):
		return code == _WP.GPU_FACTOR
	if p in ("gpuowl", "prpll", "prmers", "cudalucas"):
		return True
	return False


def default_workpref(program):
	"""Default work preference code for a program (matches setup() defaults)."""
	p = (program or "mlucas").strip()
	if p in ("mfaktc", "mfakto"):
		return _WP.GPU_FACTOR
	if p == "cudalucas":
		return _WP.LL_DBLCHK
	return _WP.PRP_FIRST


def format_workpref_line(code):
	"""Single line for combobox display."""
	c = int(code)
	label = _WORKPREF_LABELS.get(c, "Worktype {}".format(c))
	return "{} - {}".format(c, label)


try:
	from configparser import ConfigParser
	from configparser import Error as ConfigParserError
except ImportError:
	from ConfigParser import SafeConfigParser as ConfigParser
	from ConfigParser import Error as ConfigParserError

# Match autoprimenet.py SEC.* and prime.ini layout (do not import autoprimenet).
SEC_PRIMENET = "PrimeNet"
SEC_INTERNALS = "Internals"
SEC_EMAIL = "Email"
LOCALFILE_DEFAULT = "prime.ini"
PROGRAM_KEYS = ("mlucas", "gpuowl", "prpll", "prmers", "cudalucas", "mfaktc", "mfakto")
GIMPS_CREATE_ACCOUNT_URL = "https://www.mersenne.org/update/"

# (work_type_int, combobox label) — same codes as ``autoprimenet --register-exponents``.
REGISTER_EXPONENT_WORKTYPES = (
	(2, "2 — Trial factoring (mfaktc/mfakto)"),
	(3, "3 — P-1 Pminus1= (Mlucas)"),
	(4, "4 — P-1 Pfactor= (GpuOwl)"),
	(5, "5 — ECM (PrMers)"),
	(100, "100 — First time LL (Test=)"),
	(101, "101 — Double-check LL (DoubleCheck=)"),
	(150, "150 — First time PRP"),
	(151, "151 — Double-check PRP (PRPDC)"),
)

_REGISTER_EXPONENT_FIELDS = (
	("sieve_depth", "TF / LL / PRP: sieve depth (bits)"),
	("factor_to", "TF: factor-to bit level (end)"),
	("B1", "P-1 / ECM: B1 bound"),
	("B2", "P-1 / ECM: B2 bound"),
	("B2_start", "Pminus1: B2_start (optional)"),
	("tests_saved", "PRP / Pfactor: primality tests saved if factor found"),
	("prp_base", "PRP DC: PRP base (default 3)"),
	("prp_residue_type", "PRP DC: residue type 1–5 (default 1)"),
	("curves_to_do", "ECM: curves to test"),
	("known_factors", "Known factors (comma-separated, optional)"),
)

_REGISTER_EXPONENT_VISIBLE = {
	2: {"sieve_depth", "factor_to"},
	3: {"B1", "B2", "sieve_depth", "B2_start", "known_factors"},
	4: {"B1", "B2", "sieve_depth", "tests_saved", "known_factors"},
	5: {"B1", "B2", "curves_to_do", "known_factors"},
	100: {"sieve_depth"},
	101: {"sieve_depth"},
	150: {"sieve_depth", "tests_saved", "B1", "B2", "known_factors"},
	151: {"sieve_depth", "tests_saved", "B1", "B2", "prp_base", "prp_residue_type", "known_factors"},
}
INI_GUI_WORKDIR = "GUIWorkdir"
INI_GUI_GPU_INDEX = "GUIGpuDeviceIndex"
TF_ONLY_INI_OPTS = ("min_bit", "max_bit", "force_target_bits")

try:
	import tkinter as tk
	from tkinter import filedialog, messagebox, scrolledtext

	try:
		import ttkbootstrap as ttk
		from ttkbootstrap import Window as _GuiRoot

		_GUI_USE_TTKBOOTSTRAP = True
	except ImportError:
		from tkinter import ttk

		_GuiRoot = tk.Tk
		_GUI_USE_TTKBOOTSTRAP = False
except ImportError:
	try:
		import Tkinter as tk
		import tkFileDialog as filedialog
		import tkMessageBox as messagebox
		import ttk
		from ScrolledText import ScrolledText as _LegacyScrolledText
	except ImportError as e:
		sys.stderr.write(
			"AutoPrimeNet GUI requires Tcl/Tk and the tkinter library (Python 3) or Tkinter (Python 2).\n"
			"Install python3-tk (Linux) or use a Python build that includes Tk.\n"
			"Error: {}\n".format(e)
		)
		sys.exit(1)

	_GuiRoot = tk.Tk
	_GUI_USE_TTKBOOTSTRAP = False

	# Thin wrapper so both Pythons use .ScrolledText(master, ...)
	class scrolledtext(object):
		"""Namespace exposing legacy ``ScrolledText`` as ``scrolledtext.ScrolledText``."""

		ScrolledText = _LegacyScrolledText


def _repo_dir():
	"""Directory containing this script or the GUI executable (for sibling autoprimenet)."""
	if getattr(sys, "frozen", False):
		return os.path.dirname(os.path.abspath(sys.executable))
	return os.path.dirname(os.path.abspath(__file__))


def _gui_icon_paths():
	"""Candidates for the title-bar icon (same favicon as the PyInstaller --icon .exe)."""
	name = "favicon.ico"
	paths = []
	if getattr(sys, "frozen", False):
		meipass = getattr(sys, "_MEIPASS", None)
		if meipass:
			paths.append(os.path.join(meipass, "assets", name))
		paths.append(os.path.join(_repo_dir(), "assets", name))
	else:
		paths.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", name))
	return paths


def _apply_window_icon(window):
	"""Set title-bar icon to favicon.ico for the main window or any ``Toplevel`` (where supported, e.g. Windows)."""
	for path in _gui_icon_paths():
		path = os.path.normpath(os.path.abspath(path))
		if not os.path.isfile(path):
			continue
		try:
			window.iconbitmap(path)
			return
		except tk.TclError:
			continue


def _autoprimenet_command():
	"""Return argv prefix: [exe] or [python, autoprimenet.py]."""
	if getattr(sys, "frozen", False):
		sibling = os.path.join(_repo_dir(), "autoprimenet.exe" if sys.platform == "win32" else "autoprimenet")
		if not os.path.isfile(sibling):
			sibling = os.path.join(_repo_dir(), "autoprimenet")
		if not os.path.isfile(sibling):
			raise RuntimeError(
				"Could not find autoprimenet next to this program. "
				"Place autoprimenet_gui.exe and autoprimenet.exe in the same folder."
			)
		return [sibling]
	script = os.path.join(_repo_dir(), "autoprimenet.py")
	if not os.path.isfile(script):
		raise RuntimeError("Could not find autoprimenet.py next to autoprimenet_gui.py.")
	return [sys.executable, script]


class AutoPrimeNetGUI(object):
	def __init__(self, root):
		"""Lay out tabs and widgets, bind persistence, and load ``prime.ini`` for the work directory."""
		self.root = root
		self.root.title("AutoPrimeNet")
		self.proc = None
		self.reader_thread = None
		self.out_queue = queue.Queue()
		self._register_after_id = None
		self._primeuserid_for_register = "ANONYMOUS"
		self._poll_out()

		shell = ttk.Frame(root, padding=8)
		shell.grid(row=0, column=0, sticky="nsew")
		root.columnconfigure(0, weight=1)
		root.rowconfigure(0, weight=1)
		shell.columnconfigure(0, weight=1)
		shell.rowconfigure(0, weight=1)

		self.notebook = ttk.Notebook(shell)
		self.notebook.grid(row=0, column=0, sticky="nsew")

		setup_tab = ttk.Frame(self.notebook, padding=8)
		work_settings_tab = ttk.Frame(self.notebook, padding=8)
		notifications_tab = ttk.Frame(self.notebook, padding=8)
		out_tab = ttk.Frame(self.notebook, padding=0)
		self.notebook.add(setup_tab, text="Setup")
		self.notebook.add(work_settings_tab, text="Work Settings")
		self.notebook.add(notifications_tab, text="Notifications")
		self.notebook.add(out_tab, text="Output")
		self._tab_output = out_tab
		self._gui_init_done = False

		setup_tab.columnconfigure(1, weight=1)
		work_settings_tab.columnconfigure(1, weight=1)
		notifications_tab.columnconfigure(1, weight=1)

		setup_entries = self._build_setup_tab(setup_tab)
		work_entries = self._build_work_settings_tab(work_settings_tab)
		notif_entries = self._build_notifications_tab(notifications_tab)
		self._build_output_tab(out_tab)

		self._suppress_ini_write = False

		self._load_ui_from_ini()
		self._bind_ini_persistence(*(setup_entries + work_entries + notif_entries))
		self.workpref_combo.bind("<<ComboboxSelected>>", self._on_workpref_selected)
		self.workpref_combo.bind("<FocusOut>", lambda e: self._on_blur_workpref(), add="+")
		self.gpu_device_combo.bind("<<ComboboxSelected>>", self._on_gpu_device_selected)
		self._refresh_workpref_choices()
		self._update_mfak_and_gpu_visibility()
		self._gui_init_done = True
		self._update_tf_options_visibility()
		self._update_get_exp_visibility()
		self.root.after(200, self._deferred_sync_ini)

	def _build_setup_tab(self, setup_tab):
		"""Create Setup tab widgets; return entries for ``_bind_ini_persistence`` (first 8)."""
		r = 0
		ttk.Label(setup_tab, text="Work directory").grid(row=r, column=0, sticky="nw")
		self.workdir = tk.StringVar(value=os.getcwd())
		entry_wd = ttk.Entry(setup_tab, textvariable=self.workdir, width=48)
		entry_wd.grid(row=r, column=1, sticky="ew", padx=(4, 4))
		ttk.Button(setup_tab, text="Browse…", command=self._browse_workdir).grid(row=r, column=2, sticky="ne")
		r += 1

		ttk.Label(setup_tab, text="PrimeNet user ID").grid(row=r, column=0, sticky="w")
		self.user_id = tk.StringVar(value="ANONYMOUS")
		user_row = ttk.Frame(setup_tab)
		user_row.grid(row=r, column=1, columnspan=2, sticky="ew", padx=(4, 0))
		user_row.columnconfigure(0, weight=1)
		entry_user = ttk.Entry(user_row, textvariable=self.user_id, width=28)
		entry_user.grid(row=0, column=0, sticky="ew", padx=(0, 8))
		account_link = tk.Label(
			user_row,
			text="Create account",
			fg="#0066cc",
			cursor="hand2",
			font=("TkDefaultFont", 9, "underline"),
		)
		account_link.grid(row=0, column=1, sticky="e")
		account_link.bind("<Button-1>", lambda _e: webbrowser.open(GIMPS_CREATE_ACCOUNT_URL))
		self._create_account_link = account_link
		if hasattr(self.user_id, "trace_add"):
			self.user_id.trace_add("write", lambda *_a: self._sync_create_account_link_visibility())
		else:
			self.user_id.trace("w", lambda *_a: self._sync_create_account_link_visibility())
		self._sync_create_account_link_visibility()
		r += 1

		ttk.Label(setup_tab, text="GIMPS program").grid(row=r, column=0, sticky="nw")
		prog = ttk.Frame(setup_tab)
		prog.grid(row=r, column=1, columnspan=2, sticky="w", padx=(4, 0))
		self.program = tk.StringVar(value="")
		programs = (
			("mlucas", "Mlucas (-m)"),
			("gpuowl", "GpuOwl (-g)"),
			("prpll", "PRPLL (--prpll)"),
			("prmers", "PrMers (--prmers)"),
			("cudalucas", "CUDALucas (--cudalucas)"),
			("mfaktc", "mfaktc (--mfaktc)"),
			("mfakto", "mfakto (--mfakto)"),
		)
		for i, (val, label) in enumerate(programs):
			ttk.Radiobutton(prog, text=label, value=val, variable=self.program).grid(row=i // 2, column=i % 2, sticky="w")
		r += 1

		ttk.Label(setup_tab, text="Work preference").grid(row=r, column=0, sticky="w")
		self.workpref_combo = ttk.Combobox(setup_tab, width=52, state="readonly")
		self.workpref_combo.grid(row=r, column=1, columnspan=2, sticky="ew", padx=(4, 0))
		r += 1

		self.gpu_pick_frame = ttk.Frame(setup_tab)
		self.gpu_pick_frame.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(4, 0))
		self.gpu_pick_frame.columnconfigure(1, weight=1)
		ttk.Label(self.gpu_pick_frame, text="GPU for PrimeNet").grid(row=0, column=0, sticky="w")
		self.gpu_device_combo = ttk.Combobox(self.gpu_pick_frame, width=48, state="readonly")
		self.gpu_device_combo.grid(row=0, column=1, sticky="ew", padx=(4, 4))
		ttk.Button(self.gpu_pick_frame, text="Refresh", command=self._on_refresh_gpu_list).grid(row=0, column=2, sticky="e")
		self._gpu_devices_json = []
		self._gpu_list_fetched = False
		self._gpu_pick_pending = None
		self.gpu_pick_frame.grid_remove()
		r += 1

		self.tf1g_frame = ttk.Frame(setup_tab)
		self.tf1g_frame.grid(row=r, column=0, columnspan=3, sticky="w", pady=(2, 0))
		self.tf1g = tk.IntVar(value=0)
		ttk.Checkbutton(
			self.tf1g_frame,
			text="TF1G on mersenne.ca (min exponent 1e9; mfaktc/mfakto only)",
			variable=self.tf1g,
			command=self._on_tf1g_toggle,
		).pack(side="left")
		r += 1

		self.gpu_hint = ttk.Label(
			setup_tab,
			text="GpuOwl/PRPLL/PrMers/CUDALucas and GPU trial factoring (mfak*): use the GPU picker when it appears so PrimeNet gets the correct CpuBrand.",
			font=("TkDefaultFont", 8),
			wraplength=520,
			justify="left",
		)
		self.gpu_hint.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(2, 2))
		r += 1

		ttk.Label(setup_tab, text="Computer name").grid(row=r, column=0, sticky="w")
		self.computer_id = tk.StringVar(value=platform.node()[:20])
		entry_computer = ttk.Entry(setup_tab, textvariable=self.computer_id, width=24)
		entry_computer.grid(row=r, column=1, sticky="w", padx=(4, 0))
		ttk.Label(setup_tab, text="Optional (PrimeNet)").grid(row=r, column=2, sticky="w")
		r += 1

		ttk.Separator(setup_tab, orient="horizontal").grid(row=r, column=0, columnspan=3, sticky="ew", pady=(10, 6))
		r += 1
		ttk.Label(setup_tab, text="Files (names relative to work directory)", font=("TkDefaultFont", 9, "bold")).grid(
			row=r, column=0, columnspan=3, sticky="w"
		)
		r += 1

		ttk.Label(setup_tab, text="Work file").grid(row=r, column=0, sticky="w")
		self.work_file = tk.StringVar(value="worktodo.txt")
		entry_work_file = ttk.Entry(setup_tab, textvariable=self.work_file, width=44)
		entry_work_file.grid(row=r, column=1, columnspan=2, sticky="ew", padx=(4, 0))
		r += 1

		ttk.Label(setup_tab, text="Results file").grid(row=r, column=0, sticky="w")
		self.results_file = tk.StringVar(value="results.txt")
		entry_results_file = ttk.Entry(setup_tab, textvariable=self.results_file, width=44)
		entry_results_file.grid(row=r, column=1, columnspan=2, sticky="ew", padx=(4, 0))
		r += 1

		ttk.Label(setup_tab, text="Log file").grid(row=r, column=0, sticky="w")
		self.log_filename = tk.StringVar(value="autoprimenet.log")
		entry_log_filename = ttk.Entry(setup_tab, textvariable=self.log_filename, width=44)
		entry_log_filename.grid(row=r, column=1, columnspan=2, sticky="ew", padx=(4, 0))
		r += 1

		ttk.Label(setup_tab, text="Worker disk space (GiB)").grid(row=r, column=0, sticky="w")
		self.worker_disk_space = tk.StringVar(value="0.0")
		entry_worker_disk = ttk.Entry(setup_tab, textvariable=self.worker_disk_space, width=12)
		entry_worker_disk.grid(row=r, column=1, sticky="w", padx=(4, 0))
		ttk.Label(setup_tab, text="0 = do not report to PrimeNet").grid(row=r, column=2, sticky="w")
		r += 1

		ttk.Label(setup_tab, text="Proof archive directory").grid(row=r, column=0, sticky="w")
		self.archive_dir = tk.StringVar(value="")
		entry_archive = ttk.Entry(setup_tab, textvariable=self.archive_dir, width=44)
		entry_archive.grid(row=r, column=1, columnspan=2, sticky="ew", padx=(4, 0))
		ttk.Label(setup_tab, text="Optional; PRP proof uploads (relative path ok)").grid(row=r + 1, column=1, columnspan=2, sticky="w", padx=(4, 0))
		r += 2
		return (
			entry_wd,
			entry_user,
			entry_computer,
			entry_work_file,
			entry_results_file,
			entry_log_filename,
			entry_worker_disk,
			entry_archive,
		)

	def _build_work_settings_tab(self, work_settings_tab):
		"""Create Work Settings tab; return entries for ``_bind_ini_persistence`` (next 8)."""
		wr = 0
		ttk.Label(work_settings_tab, text="Max exponents").grid(row=wr, column=0, sticky="w")
		self.max_exponents = tk.StringVar(value="")
		entry_max_exp = ttk.Entry(work_settings_tab, textvariable=self.max_exponents, width=12)
		entry_max_exp.grid(row=wr, column=1, sticky="w", padx=(4, 0))
		ttk.Label(work_settings_tab, text="PrimeNet MaxExponents (blank = default on sync)").grid(row=wr, column=2, sticky="w")
		wr += 1

		ttk.Label(work_settings_tab, text="Check-in (hours)").grid(row=wr, column=0, sticky="w")
		self.checkin = tk.StringVar(value="1")
		entry_checkin = ttk.Entry(work_settings_tab, textvariable=self.checkin, width=8)
		entry_checkin.grid(row=wr, column=1, sticky="w", padx=(4, 0))
		ttk.Label(work_settings_tab, text="1–168").grid(row=wr, column=2, sticky="w")
		wr += 1

		ttk.Label(work_settings_tab, text="Days of work").grid(row=wr, column=0, sticky="w")
		self.days_work = tk.StringVar(value="3")
		entry_days_work = ttk.Entry(work_settings_tab, textvariable=self.days_work, width=8)
		entry_days_work.grid(row=wr, column=1, sticky="w", padx=(4, 0))
		ttk.Label(work_settings_tab, text="0–180 (PrimeNet queue horizon)").grid(row=wr, column=2, sticky="w")
		wr += 1

		self.cert_work_frame = ttk.Frame(work_settings_tab)
		self.cert_work_frame.grid(row=wr, column=0, columnspan=3, sticky="ew")
		self.cert_work_var = tk.IntVar(value=0)
		self.cb_cert_work = ttk.Checkbutton(
			self.cert_work_frame,
			text="Get PRP proof certification work (--cert-work; PRPLL only)",
			variable=self.cert_work_var,
			command=self._on_cert_work_toggle,
		)
		self.cb_cert_work.grid(row=0, column=0, columnspan=3, sticky="w")
		self.cert_limit_row = ttk.Frame(self.cert_work_frame)
		self.cert_limit_row.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))
		ttk.Label(self.cert_limit_row, text="Certification time limit (%)").grid(row=0, column=0, sticky="w")
		self.cert_cpu_limit = tk.StringVar(value="10")
		entry_cert_limit = ttk.Entry(self.cert_limit_row, textvariable=self.cert_cpu_limit, width=8)
		entry_cert_limit.grid(row=0, column=1, sticky="w", padx=(4, 8))
		ttk.Label(self.cert_limit_row, text="CertDailyCPULimit (--cert-work-limit), default 10%").grid(row=0, column=2, sticky="w")
		self._cert_work_widgets = (self.cb_cert_work, entry_cert_limit)
		wr += 1
		self._update_cert_limit_row_visibility()
		self._update_cert_work_controls_for_program()

		self.get_exp_frame = ttk.Frame(work_settings_tab)
		self.get_exp_frame.grid(row=wr, column=0, columnspan=3, sticky="ew", pady=(4, 0))
		self.get_exp_frame.columnconfigure(1, weight=1)
		ttk.Label(self.get_exp_frame, text="Get min exponent").grid(row=0, column=0, sticky="w")
		self.get_min_exp = tk.StringVar(value="")
		entry_get_min = ttk.Entry(self.get_exp_frame, textvariable=self.get_min_exp, width=16)
		entry_get_min.grid(row=0, column=1, sticky="w", padx=(4, 8))
		ttk.Label(self.get_exp_frame, text="Get max exponent").grid(row=0, column=2, sticky="w")
		self.get_max_exp = tk.StringVar(value="")
		entry_get_max = ttk.Entry(self.get_exp_frame, textvariable=self.get_max_exp, width=16)
		entry_get_max.grid(row=0, column=3, sticky="w", padx=(4, 0))
		wr += 1

		self.tf1g_exp_note = ttk.Label(
			work_settings_tab,
			text="TF1G is on: minimum exponent is fixed at 1e9 (GetMinExponent set by TF1G checkbox on Setup).",
			font=("TkDefaultFont", 8),
			wraplength=520,
			justify="left",
		)
		self.tf1g_exp_note.grid(row=wr, column=0, columnspan=3, sticky="w", pady=(4, 0))
		self.tf1g_exp_note.grid_remove()
		wr += 1

		ttk.Label(
			work_settings_tab,
			text="PrimeNet reporting",
			font=("TkDefaultFont", 9, "bold"),
		).grid(row=wr, column=0, columnspan=3, sticky="w", pady=(8, 0))
		wr += 1
		self.report_100m_var = tk.IntVar(value=1)
		ttk.Checkbutton(
			work_settings_tab,
			text="Report prime results for exponents ≥ 100 million digits (--report-100m / --no-report-100m)",
			variable=self.report_100m_var,
			command=self._on_report_100m_toggle,
		).grid(row=wr, column=0, columnspan=3, sticky="w")
		wr += 1

		ttk.Separator(work_settings_tab, orient="horizontal").grid(row=wr, column=0, columnspan=3, sticky="ew", pady=(12, 6))
		wr += 1
		ttk.Label(work_settings_tab, text="Trial factoring (work types 2 and 12)", font=("TkDefaultFont", 9, "bold")).grid(
			row=wr, column=0, columnspan=3, sticky="w"
		)
		wr += 1

		self.tf_opts_frame = ttk.Frame(work_settings_tab)
		self.tf_opts_frame.grid(row=wr, column=0, columnspan=3, sticky="ew", pady=(4, 0))
		self.force_target_bits = tk.IntVar(value=0)
		self.cb_force_target_bits = ttk.Checkbutton(
			self.tf_opts_frame,
			text="Force target bits (depth-first TF to mersenne.ca target level)",
			variable=self.force_target_bits,
			command=self._on_force_target_bits_toggle,
		)
		self.cb_force_target_bits.grid(row=0, column=0, columnspan=3, sticky="w")
		ttk.Label(self.tf_opts_frame, text="Min bit level").grid(row=1, column=0, sticky="w", pady=(6, 0))
		self.min_bit = tk.StringVar(value="")
		entry_min_bit = ttk.Entry(self.tf_opts_frame, textvariable=self.min_bit, width=12)
		entry_min_bit.grid(row=1, column=1, sticky="w", padx=(4, 8), pady=(6, 0))
		ttk.Label(self.tf_opts_frame, text="Max bit level").grid(row=1, column=2, sticky="w", pady=(6, 0))
		self.max_bit = tk.StringVar(value="")
		entry_max_bit = ttk.Entry(self.tf_opts_frame, textvariable=self.max_bit, width=12)
		entry_max_bit.grid(row=1, column=3, sticky="w", padx=(4, 0), pady=(6, 0))
		self._tf_opts_widgets = (self.cb_force_target_bits, entry_min_bit, entry_max_bit)
		wr += 1

		ttk.Separator(work_settings_tab, orient="horizontal").grid(row=wr, column=0, columnspan=3, sticky="ew", pady=(14, 8))
		wr += 1
		reg_row = ttk.Frame(work_settings_tab)
		reg_row.grid(row=wr, column=0, columnspan=3, sticky="w", pady=(0, 4))
		wr += 1
		ttk.Button(
			reg_row,
			text="Register Exponent",
			command=self._on_register_exponent_dialog,
		).pack(side="left", padx=(0, 8))
		ttk.Button(
			reg_row,
			text="Unreserve exponent",
			command=self._on_unreserve_exponent_dialog,
		).pack(side="left", padx=(0, 8))
		ttk.Button(reg_row, text="Unreserve all", command=self._on_unreserve_all).pack(side="left")
		ttk.Label(
			work_settings_tab,
			text=(
				"Register: add a line and call PrimeNet register-assignment.\n"
				"Unreserve exponent: pick from your work todo (--unreserve).\n"
				"Unreserve all: report results and drop every assignment (--unreserve-all); you will be asked to confirm."
			),
			font=("TkDefaultFont", 8),
			wraplength=520,
			justify="left",
		).grid(row=wr, column=0, columnspan=3, sticky="w")
		return (
			entry_max_exp,
			entry_checkin,
			entry_days_work,
			entry_cert_limit,
			entry_get_min,
			entry_get_max,
			entry_min_bit,
			entry_max_bit,
		)

	def _build_notifications_tab(self, notifications_tab):
		"""Create Notifications tab; return last 5 persistence entries."""
		nr = 0
		ttk.Label(
			notifications_tab,
			text="E-mail notifications",
			font=("TkDefaultFont", 9, "bold"),
		).grid(row=nr, column=0, columnspan=3, sticky="w")
		nr += 1
		ttk.Label(notifications_tab, text="From address (-f / --from-email)").grid(row=nr, column=0, sticky="w")
		self.email_from = tk.StringVar(value="")
		entry_notif_from = ttk.Entry(notifications_tab, textvariable=self.email_from, width=52)
		entry_notif_from.grid(row=nr, column=1, columnspan=2, sticky="ew", padx=(4, 0))
		nr += 1
		ttk.Label(notifications_tab, text="SMTP server (-S / --smtp-server)").grid(row=nr, column=0, sticky="w")
		self.email_smtp = tk.StringVar(value="")
		entry_notif_smtp = ttk.Entry(notifications_tab, textvariable=self.email_smtp, width=52)
		entry_notif_smtp.grid(row=nr, column=1, columnspan=2, sticky="ew", padx=(4, 0))
		nr += 1
		ttk.Label(notifications_tab, text="To address(es)").grid(row=nr, column=0, sticky="nw", pady=(2, 0))
		self.email_to = tk.StringVar(value="")
		entry_notif_to = ttk.Entry(notifications_tab, textvariable=self.email_to, width=52)
		entry_notif_to.grid(row=nr, column=1, columnspan=2, sticky="ew", padx=(4, 0), pady=(2, 0))
		nr += 1
		ttk.Label(notifications_tab, text="").grid(row=nr, column=0, sticky="w")
		ttk.Label(
			notifications_tab,
			text="Optional; comma-separated. Defaults to From if omitted (--to-email).",
			font=("TkDefaultFont", 8),
			wraplength=480,
			justify="left",
		).grid(row=nr, column=1, columnspan=2, sticky="w", padx=(4, 0))
		nr += 1
		self.email_tls = tk.IntVar(value=0)
		ttk.Checkbutton(
			notifications_tab,
			text="Use SSL/TLS (SMTP_SSL, e.g. port 465) (--tls)",
			variable=self.email_tls,
			command=self._on_notif_tls_toggle,
		).grid(row=nr, column=0, columnspan=3, sticky="w", pady=(6, 0))
		nr += 1
		self.email_starttls = tk.IntVar(value=0)
		ttk.Checkbutton(
			notifications_tab,
			text="Use STARTTLS (--starttls)",
			variable=self.email_starttls,
			command=self._on_notif_starttls_toggle,
		).grid(row=nr, column=0, columnspan=3, sticky="w")
		nr += 1
		ttk.Label(notifications_tab, text="SMTP username (-U / --email-username)").grid(row=nr, column=0, sticky="w", pady=(6, 0))
		self.email_username = tk.StringVar(value="")
		entry_notif_user = ttk.Entry(notifications_tab, textvariable=self.email_username, width=52)
		entry_notif_user.grid(row=nr, column=1, columnspan=2, sticky="ew", padx=(4, 0), pady=(6, 0))
		nr += 1
		ttk.Label(notifications_tab, text="SMTP password (-P / --email-password)").grid(row=nr, column=0, sticky="w")
		self.email_password = tk.StringVar(value="")
		entry_notif_pass = ttk.Entry(notifications_tab, textvariable=self.email_password, width=52, show="*")
		entry_notif_pass.grid(row=nr, column=1, columnspan=2, sticky="ew", padx=(4, 0))
		nr += 1
		self.btn_test_email = ttk.Button(notifications_tab, text="Send test e-mail (--test-email)", command=self._on_test_email)
		self.btn_test_email.grid(row=nr, column=0, columnspan=3, sticky="w", pady=(12, 4))
		nr += 1
		ttk.Label(
			notifications_tab,
			text="Requires From and SMTP server. Password is saved to prime.ini only (not passed on the command line).",
			font=("TkDefaultFont", 8),
			wraplength=520,
			justify="left",
		).grid(row=nr, column=0, columnspan=3, sticky="w")

		def _sync_test_email_btn(*_):
			"""Enable the test e-mail button only when From and SMTP are non-empty."""
			ok = bool(self.email_from.get().strip() and self.email_smtp.get().strip())
			try:
				self.btn_test_email.configure(state=("normal" if ok else "disabled"))
			except tk.TclError:
				pass

		for _ev in (self.email_from, self.email_smtp):
			if hasattr(_ev, "trace_add"):
				_ev.trace_add("write", _sync_test_email_btn)
			else:
				_ev.trace("w", _sync_test_email_btn)
		_sync_test_email_btn()
		return (
			entry_notif_from,
			entry_notif_smtp,
			entry_notif_to,
			entry_notif_user,
			entry_notif_pass,
		)

	def _build_output_tab(self, out_tab):
		"""Create Output tab (log and quick action buttons)."""
		out_tab.columnconfigure(0, weight=1)
		out_tab.rowconfigure(1, weight=1)
		btn_row = ttk.Frame(out_tab)
		btn_row.grid(row=0, column=0, sticky="ew", pady=(0, 4))
		ttk.Button(btn_row, text="Status", command=lambda: self._run_action("-s")).pack(side="left", padx=(0, 4))
		ttk.Button(btn_row, text="Debug info", command=lambda: self._run_action("--debug-info")).pack(side="left", padx=(0, 4))
		self.log = scrolledtext.ScrolledText(out_tab, width=80, height=24, wrap="word", state="disabled")
		self.log.grid(row=1, column=0, sticky="nsew", padx=0, pady=0)

	def _bind_ini_persistence(
		self,
		entry_wd,
		entry_user,
		entry_computer,
		entry_work_file,
		entry_results_file,
		entry_log_filename,
		entry_worker_disk,
		entry_archive,
		entry_max_exp,
		entry_checkin,
		entry_days_work,
		entry_cert_limit,
		entry_get_min,
		entry_get_max,
		entry_min_bit,
		entry_max_bit,
		entry_notif_from,
		entry_notif_smtp,
		entry_notif_to,
		entry_notif_user,
		entry_notif_pass,
	):
		"""Wire ``FocusOut`` on entries to save fields to ``prime.ini``; trace program changes."""
		entry_wd.bind("<FocusOut>", lambda e: self._on_blur_workdir(), add="+")
		entry_user.bind("<FocusOut>", lambda e: self._on_blur_username(), add="+")
		entry_computer.bind("<FocusOut>", lambda e: self._on_blur_computer(), add="+")
		entry_work_file.bind("<FocusOut>", lambda e: self._on_blur_work_file(), add="+")
		entry_results_file.bind("<FocusOut>", lambda e: self._on_blur_results_file(), add="+")
		entry_log_filename.bind("<FocusOut>", lambda e: self._on_blur_log_filename(), add="+")
		entry_worker_disk.bind("<FocusOut>", lambda e: self._on_blur_worker_disk_space(), add="+")
		entry_archive.bind("<FocusOut>", lambda e: self._on_blur_archive_dir(), add="+")
		entry_max_exp.bind("<FocusOut>", lambda e: self._on_blur_max_exponents(), add="+")
		entry_checkin.bind("<FocusOut>", lambda e: self._on_blur_checkin(), add="+")
		entry_days_work.bind("<FocusOut>", lambda e: self._on_blur_days_work(), add="+")
		entry_cert_limit.bind("<FocusOut>", lambda e: self._on_blur_cert_cpu_limit(), add="+")
		entry_get_min.bind("<FocusOut>", lambda e: self._on_blur_get_min_exp(), add="+")
		entry_get_max.bind("<FocusOut>", lambda e: self._on_blur_get_max_exp(), add="+")
		entry_min_bit.bind("<FocusOut>", lambda e: self._on_blur_min_bit(), add="+")
		entry_max_bit.bind("<FocusOut>", lambda e: self._on_blur_max_bit(), add="+")
		for ent in (
			entry_notif_from,
			entry_notif_smtp,
			entry_notif_to,
			entry_notif_user,
			entry_notif_pass,
		):
			ent.bind("<FocusOut>", lambda e: self._on_blur_notification_field(), add="+")
		if hasattr(self.program, "trace_add"):
			self.program.trace_add("write", lambda *_: self._on_program_change())
		else:
			self.program.trace("w", lambda *_: self._on_program_change())

	def _gimps_program_key(self):
		"""Return the selected GIMPS program key, or None if none is selected."""
		s = (self.program.get() or "").strip()
		return s if s in PROGRAM_KEYS else None

	def _new_config_parser(self):
		"""Return a ``ConfigParser`` that preserves option name case (matches ``prime.ini``)."""
		cp = ConfigParser()
		cp.optionxform = lambda option: option
		return cp

	def _read_ini_file(self, path):
		"""Load ``path`` into a parser, or return empty PrimeNet/Email/Internals sections if missing."""
		cp = self._new_config_parser()
		if not os.path.isfile(path):
			for sec in (SEC_PRIMENET, SEC_EMAIL, SEC_INTERNALS):
				if not cp.has_section(sec):
					cp.add_section(sec)
			return cp
		try:
			with io.open(path, "r", encoding="utf-8") as f:
				if hasattr(cp, "read_file"):
					cp.read_file(f)
				else:
					cp.readfp(f)
		except (ConfigParserError, IOError, OSError):
			pass
		for sec in (SEC_PRIMENET, SEC_EMAIL, SEC_INTERNALS):
			if not cp.has_section(sec):
				cp.add_section(sec)
		return cp

	def _write_ini_file(self, path, cp):
		"""Write parser ``cp`` to ``path``; return ``(True, None)`` or ``(False, error_message)``."""
		parent = os.path.dirname(path)
		if parent and not os.path.isdir(parent):
			return False, "Parent directory does not exist: {!r}".format(parent)
		try:
			with io.open(path, "w", encoding="utf-8") as f:
				cp.write(f)
		except (IOError, OSError) as e:
			return False, str(e)
		return True, None

	def _normalize_workdir_str(self, s):
		"""Expand user, normalize path, defaulting to cwd when ``s`` is empty."""
		s = (s or "").strip()
		if not s:
			return os.path.normpath(os.getcwd())
		return os.path.normpath(os.path.expanduser(s))

	def _resolve_workdir_from_var(self):
		"""Return ``(absolute_workdir, None)`` or ``(None, warning_message)`` if the path is not a directory."""
		wd = self._normalize_workdir_str(self.workdir.get())
		if not os.path.isdir(wd):
			return None, "Work directory does not exist:\n{}".format(wd)
		return wd, None

	def _persist_ini_updates(self, updates_by_section):
		"""Merge ``{section: {key: value}}`` into ``prime.ini`` under the current work directory."""
		if self._suppress_ini_write:
			return
		wd, err = self._resolve_workdir_from_var()
		if err:
			messagebox.showwarning("AutoPrimeNet", err)
			return
		path = os.path.join(wd, LOCALFILE_DEFAULT)
		cp = self._read_ini_file(path)
		for section, pairs in updates_by_section.items():
			if not cp.has_section(section):
				cp.add_section(section)
			for k, v in pairs.items():
				cp.set(section, k, v if isinstance(v, str) else str(v))
		ok, msg = self._write_ini_file(path, cp)
		if not ok:
			messagebox.showerror("AutoPrimeNet", "Could not save {!r}:\n{}".format(path, msg))

	def _persist_gimps_program_ini(self, chosen):
		"""Write only the selected GIMPS program as True; remove other program keys from [PrimeNet]."""
		if self._suppress_ini_write:
			return
		wd, err = self._resolve_workdir_from_var()
		if err:
			messagebox.showwarning("AutoPrimeNet", err)
			return
		chosen = (chosen or "").strip()
		if chosen not in PROGRAM_KEYS:
			return
		path = os.path.join(wd, LOCALFILE_DEFAULT)
		cp = self._read_ini_file(path)
		if not cp.has_section(SEC_PRIMENET):
			cp.add_section(SEC_PRIMENET)
		for k in PROGRAM_KEYS:
			if k == chosen:
				cp.set(SEC_PRIMENET, k, "True")
			elif cp.has_option(SEC_PRIMENET, k):
				cp.remove_option(SEC_PRIMENET, k)
		ok, msg = self._write_ini_file(path, cp)
		if not ok:
			messagebox.showerror("AutoPrimeNet", "Could not save {!r}:\n{}".format(path, msg))

	@staticmethod
	def _default_results_filename(program_key):
		"""Default results basename: ``results.json.txt`` (mfak*), ``results-0.txt`` (PRPLL worker 0), else ``results.txt``."""
		p = (program_key or "").strip()
		if not p:
			return "results.txt"
		if p == "prpll":
			return "results-0.txt"
		if p in ("mfaktc", "mfakto"):
			return "results.json.txt"
		return "results.txt"

	@staticmethod
	def _default_work_filename(program_key):
		"""Default work file basename (``autoprimenet -i``; PRPLL uses per-worker ``worktodo-N.txt``)."""
		p = (program_key or "").strip()
		if not p:
			return "worktodo.txt"
		if p == "prpll":
			return "worktodo-0.txt"
		return "worktodo.txt"

	def _strip_tf_only_ini_keys(self):
		"""Remove min_bit, max_bit, and force_target_bits from ``[PrimeNet]`` when TF is not selected."""
		wd, err = self._resolve_workdir_from_var()
		if err:
			return
		path = os.path.join(wd, LOCALFILE_DEFAULT)
		cp = self._read_ini_file(path)
		if not cp.has_section(SEC_PRIMENET):
			return
		changed = False
		for k in TF_ONLY_INI_OPTS:
			if cp.has_option(SEC_PRIMENET, k):
				cp.remove_option(SEC_PRIMENET, k)
				changed = True
		if changed:
			self._write_ini_file(path, cp)

	def _clear_tf_option_widgets(self):
		"""Reset trial-factoring option widgets to empty / unchecked."""
		self.min_bit.set("")
		self.max_bit.set("")
		self.force_target_bits.set(0)

	def _update_tf_options_visibility(self):
		"""Enable TF bit-level controls only for trial-factoring work types; strip ini keys when disabled."""
		try:
			code = self._get_workpref_code()
		except (TypeError, ValueError):
			code = None
		tf = code is not None and workpref_is_trial_factoring(code)
		self.tf_opts_frame.grid()
		state = "normal" if tf else "disabled"
		for w in self._tf_opts_widgets:
			try:
				w.configure(state=state)
			except tk.TclError:
				pass
		if not tf:
			self._clear_tf_option_widgets()
			if getattr(self, "_gui_init_done", False) and not self._suppress_ini_write:
				self._strip_tf_only_ini_keys()

	def _tf1g_controls_get_min_exponent(self):
		"""True when TF1G mode fixes GetMinExponent (mfaktc/mfakto with checkbox on)."""
		return self.program.get() in ("mfaktc", "mfakto") and self.tf1g.get()

	def _update_get_exp_visibility(self):
		"""Show TF1G note and hide min/max exponent row when TF1G drives GetMinExponent."""
		if self._tf1g_controls_get_min_exponent():
			self.tf1g_exp_note.grid()
			self.get_exp_frame.grid_remove()
		else:
			self.tf1g_exp_note.grid_remove()
			self.get_exp_frame.grid()

	def _on_force_target_bits_toggle(self):
		"""Persist ``force_target_bits`` to ``prime.ini`` when the TF checkbox toggles."""
		if self._suppress_ini_write:
			return
		wp = self._get_workpref_code()
		if wp is None or not workpref_is_trial_factoring(wp):
			return
		wd, err = self._resolve_workdir_from_var()
		if err:
			messagebox.showwarning("AutoPrimeNet", err)
			return
		path = os.path.join(wd, LOCALFILE_DEFAULT)
		cp = self._read_ini_file(path)
		if not cp.has_section(SEC_PRIMENET):
			cp.add_section(SEC_PRIMENET)
		if self.force_target_bits.get():
			cp.set(SEC_PRIMENET, "force_target_bits", "True")
		else:
			if cp.has_option(SEC_PRIMENET, "force_target_bits"):
				cp.remove_option(SEC_PRIMENET, "force_target_bits")
		ok, msg = self._write_ini_file(path, cp)
		if not ok:
			messagebox.showerror("AutoPrimeNet", "Could not save {!r}:\n{}".format(path, msg))

	def _on_blur_work_file(self):
		"""Validate and save ``work_file`` (basename only) to ``prime.ini``."""
		wf = (self.work_file.get() or "").strip()
		if os.path.dirname(wf):
			messagebox.showwarning("AutoPrimeNet", "Work file must be a filename only (no path).")
			return
		if not wf:
			messagebox.showwarning("AutoPrimeNet", "Work file cannot be empty.")
			return
		self.work_file.set(wf)
		self._persist_ini_updates({SEC_PRIMENET: {"work_file": wf}})

	def _on_blur_results_file(self):
		"""Validate and save ``results_file`` (basename only) to ``prime.ini``."""
		rf = (self.results_file.get() or "").strip()
		if os.path.dirname(rf):
			messagebox.showwarning("AutoPrimeNet", "Results file must be a filename only (no path).")
			return
		if not rf:
			messagebox.showwarning("AutoPrimeNet", "Results file cannot be empty.")
			return
		self.results_file.set(rf)
		self._persist_ini_updates({SEC_PRIMENET: {"results_file": rf}})

	def _on_blur_log_filename(self):
		"""Validate and save ``logfile`` (basename only) to ``prime.ini``."""
		lf = (self.log_filename.get() or "").strip()
		if os.path.dirname(lf):
			messagebox.showwarning("AutoPrimeNet", "Log file must be a filename only (no path).")
			return
		if not lf:
			messagebox.showwarning("AutoPrimeNet", "Log file cannot be empty.")
			return
		self.log_filename.set(lf)
		self._persist_ini_updates({SEC_PRIMENET: {"logfile": lf}})

	def _on_blur_worker_disk_space(self):
		"""Validate and persist ``WorkerDiskSpace`` (GiB) to ``prime.ini``."""
		try:
			d = float((self.worker_disk_space.get() or "0").strip())
			if d < 0.0:
				raise ValueError
		except ValueError:
			messagebox.showwarning("AutoPrimeNet", "Worker disk space must be a non-negative number (GiB per worker).")
			return
		self.worker_disk_space.set(str(d))
		self._persist_ini_updates({SEC_PRIMENET: {"WorkerDiskSpace": str(d)}})

	def _on_blur_archive_dir(self):
		"""Save or clear ``ProofArchiveDir`` in ``prime.ini``."""
		ad = (self.archive_dir.get() or "").strip()
		self.archive_dir.set(ad)
		wd, err = self._resolve_workdir_from_var()
		if err:
			messagebox.showwarning("AutoPrimeNet", err)
			return
		path = os.path.join(wd, LOCALFILE_DEFAULT)
		cp = self._read_ini_file(path)
		if not cp.has_section(SEC_PRIMENET):
			cp.add_section(SEC_PRIMENET)
		if ad:
			cp.set(SEC_PRIMENET, "ProofArchiveDir", ad)
		else:
			if cp.has_option(SEC_PRIMENET, "ProofArchiveDir"):
				cp.remove_option(SEC_PRIMENET, "ProofArchiveDir")
		ok, msg = self._write_ini_file(path, cp)
		if not ok:
			messagebox.showerror("AutoPrimeNet", "Could not save {!r}:\n{}".format(path, msg))

	def _on_blur_cert_cpu_limit(self):
		"""Validate and save PRPLL ``CertDailyCPULimit`` (1–100) when cert work is enabled."""
		if not self.cert_work_var.get():
			return
		try:
			n = int((self.cert_cpu_limit.get() or "10").strip(), 10)
			if n < 1 or n > 100:
				raise ValueError
		except ValueError:
			messagebox.showwarning("AutoPrimeNet", "Certification time limit must be an integer from 1 through 100.")
			return
		self.cert_cpu_limit.set(str(n))
		self._persist_ini_updates({SEC_PRIMENET: {"CertDailyCPULimit": str(n)}})

	def _on_cert_work_toggle(self):
		"""Show or hide cert limit row and persist ``CertWork`` to ``prime.ini``."""
		self._update_cert_limit_row_visibility()
		if self._suppress_ini_write:
			return
		enabled = bool(self.cert_work_var.get())
		self._persist_ini_updates({SEC_PRIMENET: {"CertWork": str(enabled)}})

	def _update_cert_limit_row_visibility(self):
		"""Grid or hide the certification CPU limit row based on ``CertWork``."""
		if self.cert_work_var.get():
			self.cert_limit_row.grid()
		else:
			self.cert_limit_row.grid_remove()

	def _update_cert_work_controls_for_program(self):
		"""Enable cert-work controls for PRPLL only; otherwise disable and hide limit row."""
		prpll = self._gimps_program_key() == "prpll"
		state = "normal" if prpll else "disabled"
		for w in self._cert_work_widgets:
			try:
				w.configure(state=state)
			except tk.TclError:
				pass
		if prpll:
			self._update_cert_limit_row_visibility()
		else:
			self.cert_limit_row.grid_remove()

	def _on_blur_get_min_exp(self):
		"""Validate ``GetMinExponent`` or remove it when blank (skipped when TF1G controls it)."""
		if self._tf1g_controls_get_min_exponent():
			return
		raw = (self.get_min_exp.get() or "").strip()
		wd, err = self._resolve_workdir_from_var()
		if err:
			messagebox.showwarning("AutoPrimeNet", err)
			return
		path = os.path.join(wd, LOCALFILE_DEFAULT)
		cp = self._read_ini_file(path)
		if not cp.has_section(SEC_PRIMENET):
			cp.add_section(SEC_PRIMENET)
		if not raw:
			if cp.has_option(SEC_PRIMENET, "GetMinExponent"):
				cp.remove_option(SEC_PRIMENET, "GetMinExponent")
			ok, msg = self._write_ini_file(path, cp)
			if not ok:
				messagebox.showerror("AutoPrimeNet", "Could not save {!r}:\n{}".format(path, msg))
			return
		try:
			v = int(raw, 10)
			if not 2 <= v <= 9999999999:
				raise ValueError
		except ValueError:
			messagebox.showwarning("AutoPrimeNet", "Get min exponent must be an integer from 2 through 9,999,999,999.")
			return
		cp.set(SEC_PRIMENET, "GetMinExponent", str(v))
		ok, msg = self._write_ini_file(path, cp)
		if not ok:
			messagebox.showerror("AutoPrimeNet", "Could not save {!r}:\n{}".format(path, msg))

	def _on_blur_get_max_exp(self):
		"""Validate ``GetMaxExponent`` or remove the option when blank."""
		raw = (self.get_max_exp.get() or "").strip()
		wd, err = self._resolve_workdir_from_var()
		if err:
			messagebox.showwarning("AutoPrimeNet", err)
			return
		path = os.path.join(wd, LOCALFILE_DEFAULT)
		cp = self._read_ini_file(path)
		if not cp.has_section(SEC_PRIMENET):
			cp.add_section(SEC_PRIMENET)
		if not raw:
			if cp.has_option(SEC_PRIMENET, "GetMaxExponent"):
				cp.remove_option(SEC_PRIMENET, "GetMaxExponent")
			ok, msg = self._write_ini_file(path, cp)
			if not ok:
				messagebox.showerror("AutoPrimeNet", "Could not save {!r}:\n{}".format(path, msg))
			return
		try:
			v = int(raw, 10)
			if not 2 <= v <= 9999999999:
				raise ValueError
		except ValueError:
			messagebox.showwarning("AutoPrimeNet", "Get max exponent must be an integer from 2 through 9,999,999,999.")
			return
		cp.set(SEC_PRIMENET, "GetMaxExponent", str(v))
		ok, msg = self._write_ini_file(path, cp)
		if not ok:
			messagebox.showerror("AutoPrimeNet", "Could not save {!r}:\n{}".format(path, msg))

	def _on_blur_min_bit(self):
		"""Validate ``min_bit`` for trial factoring or remove it when blank."""
		if not workpref_is_trial_factoring(self._get_workpref_code()):
			return
		raw = (self.min_bit.get() or "").strip()
		wd, err = self._resolve_workdir_from_var()
		if err:
			messagebox.showwarning("AutoPrimeNet", err)
			return
		path = os.path.join(wd, LOCALFILE_DEFAULT)
		cp = self._read_ini_file(path)
		if not cp.has_section(SEC_PRIMENET):
			cp.add_section(SEC_PRIMENET)
		if not raw:
			if cp.has_option(SEC_PRIMENET, "min_bit"):
				cp.remove_option(SEC_PRIMENET, "min_bit")
			ok, msg = self._write_ini_file(path, cp)
			if not ok:
				messagebox.showerror("AutoPrimeNet", "Could not save {!r}:\n{}".format(path, msg))
			return
		try:
			v = int(raw, 10)
			if v < 1:
				raise ValueError
		except ValueError:
			messagebox.showwarning("AutoPrimeNet", "Min bit level must be a positive integer.")
			return
		cp.set(SEC_PRIMENET, "min_bit", str(v))
		ok, msg = self._write_ini_file(path, cp)
		if not ok:
			messagebox.showerror("AutoPrimeNet", "Could not save {!r}:\n{}".format(path, msg))

	def _on_blur_max_bit(self):
		"""Validate ``max_bit`` for trial factoring or remove it when blank."""
		if not workpref_is_trial_factoring(self._get_workpref_code()):
			return
		raw = (self.max_bit.get() or "").strip()
		wd, err = self._resolve_workdir_from_var()
		if err:
			messagebox.showwarning("AutoPrimeNet", err)
			return
		path = os.path.join(wd, LOCALFILE_DEFAULT)
		cp = self._read_ini_file(path)
		if not cp.has_section(SEC_PRIMENET):
			cp.add_section(SEC_PRIMENET)
		if not raw:
			if cp.has_option(SEC_PRIMENET, "max_bit"):
				cp.remove_option(SEC_PRIMENET, "max_bit")
			ok, msg = self._write_ini_file(path, cp)
			if not ok:
				messagebox.showerror("AutoPrimeNet", "Could not save {!r}:\n{}".format(path, msg))
			return
		try:
			v = int(raw, 10)
			if v < 1:
				raise ValueError
		except ValueError:
			messagebox.showwarning("AutoPrimeNet", "Max bit level must be a positive integer.")
			return
		cp.set(SEC_PRIMENET, "max_bit", str(v))
		ok, msg = self._write_ini_file(path, cp)
		if not ok:
			messagebox.showerror("AutoPrimeNet", "Could not save {!r}:\n{}".format(path, msg))

	def _get_workpref_code(self):
		"""Return the numeric work preference, the program default, or None if no GIMPS program is selected."""
		key = self._gimps_program_key()
		if not key:
			return None
		text = self.workpref_combo.get().strip()
		if not text:
			return default_workpref(key)
		head = text.split(" - ", 1)[0].strip()
		try:
			return int(head)
		except ValueError:
			return default_workpref(key)

	def _refresh_workpref_choices(self):
		"""Rebuild work-preference combobox values for the current program and apply pending selection."""
		prog = self._gimps_program_key()
		if not prog:
			try:
				self.workpref_combo.configure(state="normal")
				self.workpref_combo["values"] = ()
				self.workpref_combo.set("")
				self.workpref_combo.configure(state="disabled")
			except tk.TclError:
				pass
			self._update_gpu_picker_visibility()
			self._update_tf_options_visibility()
			self._update_get_exp_visibility()
			return
		try:
			self.workpref_combo.configure(state="readonly")
		except tk.TclError:
			pass
		codes = supported_workprefs(prog)
		lines = [format_workpref_line(c) for c in codes]
		self.workpref_combo["values"] = lines
		pending = getattr(self, "_workpref_code_pending", None)
		if pending is not None:
			cur = pending if pending in codes else default_workpref(prog)
			self._workpref_code_pending = None
		else:
			cur = self._get_workpref_code()
			if cur is None or cur not in codes:
				cur = default_workpref(prog)
		self.workpref_combo.set(format_workpref_line(cur))
		self._update_gpu_picker_visibility()
		self._update_tf_options_visibility()
		self._update_get_exp_visibility()

	def _update_mfak_and_gpu_visibility(self):
		"""Show TF1G and GPU hint rows based on selected GIMPS program."""
		p = self._gimps_program_key()
		if not p:
			self.tf1g_frame.grid_remove()
			self.gpu_hint.grid_remove()
		elif p in ("mfaktc", "mfakto"):
			self.tf1g_frame.grid()
			self.gpu_hint.grid()
		else:
			self.tf1g_frame.grid_remove()
			if p != "mlucas":
				self.gpu_hint.grid()
			else:
				self.gpu_hint.grid_remove()
		self._update_gpu_picker_visibility()
		self._update_get_exp_visibility()

	def _gpu_picker_should_show(self):
		"""True if the current program/work type needs an explicit GPU choice for PrimeNet."""
		key = self._gimps_program_key()
		if not key:
			return False
		try:
			code = self._get_workpref_code()
		except (TypeError, ValueError):
			code = None
		if code is None:
			return False
		return workpref_requires_gpu_picker(key, code)

	def _update_gpu_picker_visibility(self):
		"""Show the GPU combobox when required; trigger a fetch if not yet loaded."""
		if self._gpu_picker_should_show():
			self.gpu_pick_frame.grid()
			self._ensure_gpu_combo_ready()
		else:
			self.gpu_pick_frame.grid_remove()

	def _ensure_gpu_combo_ready(self):
		"""Populate or refresh the GPU list once, then apply the saved selection."""
		if self._gpu_list_fetched:
			self._apply_gpu_combo_selection()
			return
		self._fetch_and_fill_gpu_combo()

	def _fetch_gpu_devices_via_cli(self):
		"""Run ``autoprimenet --list-gpus``; return ``(list|None, error_message|None)``."""
		try:
			cmd = list(_autoprimenet_command()) + ["--list-gpus"]
		except RuntimeError as e:
			return None, str(e)
		kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "cwd": self.workdir.get().strip() or None}
		if sys.version_info >= (3, 7):
			kwargs["text"] = True
			kwargs["encoding"] = "utf-8"
			kwargs["errors"] = "replace"
		else:
			kwargs["universal_newlines"] = True
		try:
			if sys.version_info >= (3, 5):
				try:
					r = subprocess.run(cmd, check=False, timeout=120, **kwargs)
				except subprocess.TimeoutExpired:
					return None, "timed out"
				out = (r.stdout or "").strip()
				if r.returncode != 0:
					return None, "autoprimenet exited with code {}".format(r.returncode)
			else:
				proc = subprocess.Popen(cmd, **kwargs)
				out, _ = proc.communicate()
				out = (out or "").strip()
				if proc.returncode != 0:
					return None, "autoprimenet exited with code {}".format(proc.returncode)
		except (OSError, IOError) as e:
			return None, str(e)
		if not out:
			return [], None
		try:
			data = json.loads(out)
		except (ValueError, TypeError) as e:
			return None, str(e)
		if not isinstance(data, list):
			return None, "expected a JSON array"
		return data, None

	def _gpu_display_line(self, idx1, g):
		"""Format one GPU entry for the combobox (1-based index)."""
		src = g.get("source") or "?"
		name = g.get("name") or "GPU"
		return "{}. {} ({})".format(idx1, name, src)

	def _fetch_and_fill_gpu_combo(self):
		"""Load GPU list from CLI into ``_gpu_devices_json`` and fill the combobox."""
		rows, err = self._fetch_gpu_devices_via_cli()
		self._gpu_list_fetched = True
		if err:
			messagebox.showwarning("AutoPrimeNet", "Could not list GPUs:\n{}".format(err))
			rows = []
		if rows is None:
			rows = []
		self._gpu_devices_json = rows
		lines = ["CPU — auto-detect (PrimeNet CpuBrand from system)"]
		for i, g in enumerate(rows):
			lines.append(self._gpu_display_line(i + 1, g))
		self._suppress_ini_write = True
		try:
			self.gpu_device_combo["values"] = lines
			self._apply_gpu_combo_selection()
		finally:
			self._suppress_ini_write = False

	def _apply_gpu_combo_selection(self):
		"""Select combobox index from pending ini value, CpuBrand match, or default to CPU."""
		vals = self.gpu_device_combo["values"]
		if not vals:
			return
		want = getattr(self, "_gpu_pick_pending", None)
		self._gpu_pick_pending = None
		idx = None
		if want is not None and str(want).strip() != "":
			w = str(want).strip().lower()
			if w == "cpu":
				idx = 0
			else:
				try:
					n = int(str(want).strip(), 10)
					if 1 <= n <= len(self._gpu_devices_json):
						idx = n
				except ValueError:
					idx = None
		if idx is None and self._gpu_devices_json:
			wd, err = self._resolve_workdir_from_var()
			if not err:
				cp = self._read_ini_file(os.path.join(wd, LOCALFILE_DEFAULT))
				if cp.has_option(SEC_PRIMENET, "CpuBrand"):
					cb = cp.get(SEC_PRIMENET, "CpuBrand").strip()
					for i, g in enumerate(self._gpu_devices_json):
						if (g.get("name") or "").strip() == cb:
							idx = i + 1
							break
		if idx is None:
			idx = 0
		if idx >= len(vals):
			idx = 0
		self._suppress_ini_write = True
		try:
			self.gpu_device_combo.current(idx)
		finally:
			self._suppress_ini_write = False

	def _on_gpu_device_selected(self, _event=None):
		"""Persist CPU vs GPU index when the user picks a device."""
		if self._suppress_ini_write:
			return
		idx = self.gpu_device_combo.current()
		if idx < 0:
			return
		if idx == 0:
			self._persist_gpu_device_choice("cpu")
		else:
			self._persist_gpu_device_choice(str(idx))

	def _persist_gpu_device_choice(self, mode):
		"""Write GPU choice to ini (cpu or 1-based index); sync hardware via ``--sync-ini`` when CPU is chosen."""
		wd, err = self._resolve_workdir_from_var()
		if err:
			messagebox.showwarning("AutoPrimeNet", err)
			return
		path = os.path.join(wd, LOCALFILE_DEFAULT)
		cp = self._read_ini_file(path)
		if not cp.has_section(SEC_INTERNALS):
			cp.add_section(SEC_INTERNALS)
		if mode == "cpu":
			cp.set(SEC_INTERNALS, INI_GUI_GPU_INDEX, "cpu")
			if cp.has_section(SEC_PRIMENET):
				for opt in ("CpuBrand", "NumCores", "CpuNumHyperthreads", "CpuSpeed", "memory", "Memory"):
					if cp.has_option(SEC_PRIMENET, opt):
						cp.remove_option(SEC_PRIMENET, opt)
			ok, msg = self._write_ini_file(path, cp)
			if not ok:
				messagebox.showerror("AutoPrimeNet", "Could not save {!r}:\n{}".format(path, msg))
				return
			try:
				base = self._build_base_argv()
			except RuntimeError:
				return
			self._sync_hardware_to_ini(base, quiet=True)
			return
		try:
			i = int(mode, 10) - 1
		except ValueError:
			return
		if i < 0 or i >= len(self._gpu_devices_json):
			return
		g = self._gpu_devices_json[i]
		name = (g.get("name") or "GPU").strip()
		if len(name) < 4:
			name = (name + " GPU").strip()[:64]
		if len(name) < 4:
			name = "GPU dev"
		name = name[:64]
		cores = g.get("cores")
		frequency = g.get("frequency_mhz")
		try:
			frequency = int(frequency) if frequency is not None else 1000
		except (TypeError, ValueError):
			frequency = 1000
		memory = g.get("memory_mib")
		try:
			memory = int(memory) if memory is not None else 1024
		except (TypeError, ValueError):
			memory = 1024
		if not cp.has_section(SEC_PRIMENET):
			cp.add_section(SEC_PRIMENET)
		cp.set(SEC_INTERNALS, INI_GUI_GPU_INDEX, mode)
		cp.set(SEC_PRIMENET, "CpuBrand", name)
		if cores is not None:
			try:
				nc = int(cores)
				cp.set(SEC_PRIMENET, "NumCores", str(nc))
				cp.set(SEC_PRIMENET, "CpuNumHyperthreads", "1")
			except (TypeError, ValueError):
				for opt in ("NumCores", "CpuNumHyperthreads"):
					if cp.has_option(SEC_PRIMENET, opt):
						cp.remove_option(SEC_PRIMENET, opt)
		else:
			for opt in ("NumCores", "CpuNumHyperthreads"):
				if cp.has_option(SEC_PRIMENET, opt):
					cp.remove_option(SEC_PRIMENET, opt)
		cp.set(SEC_PRIMENET, "CpuSpeed", str(frequency))
		cp.set(SEC_PRIMENET, "memory", str(memory))
		cp.set(SEC_PRIMENET, "Memory", str(int(memory * 0.9)))
		ok, msg = self._write_ini_file(path, cp)
		if not ok:
			messagebox.showerror("AutoPrimeNet", "Could not save {!r}:\n{}".format(path, msg))

	def _on_refresh_gpu_list(self):
		"""Re-run GPU discovery, preserving the current CPU/GPU selection when possible."""
		cur = self.gpu_device_combo.current()
		self._gpu_pick_pending = "cpu" if cur <= 0 else str(cur)
		self._gpu_list_fetched = False
		self._fetch_and_fill_gpu_combo()

	def _persist_workpreference_to_ini(self):
		"""Save ``WorkPreference`` to ``[PrimeNet]`` and strip per-worker duplicates."""
		if self._suppress_ini_write:
			return
		key = self._gimps_program_key()
		if not key:
			return
		wd, err = self._resolve_workdir_from_var()
		if err:
			messagebox.showwarning("AutoPrimeNet", err)
			return
		try:
			code = self._get_workpref_code()
		except (TypeError, ValueError):
			code = None
		if code is None:
			return
		if code not in supported_workprefs(key):
			messagebox.showwarning("AutoPrimeNet", "Work preference {:n} is not valid for {}.".format(code, key))
			self._refresh_workpref_choices()
			return
		code_str = str(int(code))
		path = os.path.join(wd, LOCALFILE_DEFAULT)
		cp = self._read_ini_file(path)
		if not cp.has_section(SEC_PRIMENET):
			cp.add_section(SEC_PRIMENET)
		cp.set(SEC_PRIMENET, "WorkPreference", code_str)
		for i in range(1, 32):
			ws = "Worker #{}".format(i)
			if cp.has_section(ws) and cp.has_option(ws, "WorkPreference"):
				cp.remove_option(ws, "WorkPreference")
		ok, msg = self._write_ini_file(path, cp)
		if not ok:
			messagebox.showerror("AutoPrimeNet", "Could not save {!r}:\n{}".format(path, msg))

	def _on_workpref_selected(self, _event=None):
		"""Handle combobox selection: save workpref, refresh dependent UI, queue registration."""
		self._persist_workpreference_to_ini()
		self._update_gpu_picker_visibility()
		self._update_tf_options_visibility()
		self._update_get_exp_visibility()
		self._schedule_register_attempt("prereqs")

	def _on_blur_workpref(self):
		"""On focus loss, persist work preference and update TF/GPU/exp visibility."""
		if self._suppress_ini_write:
			return
		self._persist_workpreference_to_ini()
		self._update_gpu_picker_visibility()
		self._update_tf_options_visibility()
		self._update_get_exp_visibility()
		self._schedule_register_attempt("prereqs")

	def _on_blur_computer(self):
		"""Truncate and save ``ComputerID`` (PrimeNet computer name) to ``prime.ini``."""
		cn = (self.computer_id.get() or "").strip()[:20]
		self.computer_id.set(cn)
		self._persist_ini_updates({SEC_PRIMENET: {"ComputerID": cn}})

	def _on_tf1g_toggle(self):
		"""Set or clear ``GetMinExponent`` for TF1G (mfaktc/mfakto) and refresh exponent UI."""
		if self._suppress_ini_write:
			return
		if self.program.get() not in ("mfaktc", "mfakto"):
			return
		wd, err = self._resolve_workdir_from_var()
		if err:
			messagebox.showwarning("AutoPrimeNet", err)
			return
		path = os.path.join(wd, LOCALFILE_DEFAULT)
		cp = self._read_ini_file(path)
		if self.tf1g.get():
			if not cp.has_section(SEC_PRIMENET):
				cp.add_section(SEC_PRIMENET)
			cp.set(SEC_PRIMENET, "GetMinExponent", str(MAX_PRIMENET_EXP))
		else:
			if cp.has_section(SEC_PRIMENET) and cp.has_option(SEC_PRIMENET, "GetMinExponent"):
				cp.remove_option(SEC_PRIMENET, "GetMinExponent")
		ok, msg = self._write_ini_file(path, cp)
		if not ok:
			messagebox.showerror("AutoPrimeNet", "Could not save {!r}:\n{}".format(path, msg))
		if self.tf1g.get():
			self.get_min_exp.set("")
		self._update_get_exp_visibility()

	def _load_ui_from_ini(self):
		"""Populate all widgets from ``prime.ini`` for the work directory; enforce single-worker layout."""
		self._suppress_ini_write = True
		try:
			start = self._normalize_workdir_str(self.workdir.get())
			path = os.path.join(start, LOCALFILE_DEFAULT)
			cp = self._read_ini_file(path)
			if cp.has_option(SEC_INTERNALS, INI_GUI_WORKDIR):
				gw = cp.get(SEC_INTERNALS, INI_GUI_WORKDIR).strip()
				if gw:
					gwn = self._normalize_workdir_str(gw)
					if os.path.isdir(gwn) and os.path.normcase(gwn) != os.path.normcase(start):
						start = gwn
						path = os.path.join(start, LOCALFILE_DEFAULT)
						cp = self._read_ini_file(path)
			self.workdir.set(start)
			if cp.has_option(SEC_PRIMENET, "username"):
				self.user_id.set(cp.get(SEC_PRIMENET, "username"))
			self._primeuserid_for_register = self._normalized_primeuserid_from_cp(cp)
			chosen = None
			for key in PROGRAM_KEYS:
				if cp.has_option(SEC_PRIMENET, key):
					try:
						if cp.getboolean(SEC_PRIMENET, key):
							chosen = key
							break
					except ValueError:
						val = cp.get(SEC_PRIMENET, key).strip().lower()
						if val in ("1", "true", "yes", "on"):
							chosen = key
							break
			if chosen:
				self.program.set(chosen)
			else:
				self.program.set("")
			self._prev_program_for_defaults = chosen
			if cp.has_option(SEC_PRIMENET, "work_file"):
				self.work_file.set(cp.get(SEC_PRIMENET, "work_file").strip())
			else:
				self.work_file.set(self._default_work_filename(chosen or ""))
			rf_def = self._default_results_filename(chosen or "")
			if cp.has_option(SEC_PRIMENET, "results_file"):
				self.results_file.set(cp.get(SEC_PRIMENET, "results_file").strip())
			else:
				self.results_file.set(rf_def)
			if cp.has_option(SEC_PRIMENET, "logfile"):
				self.log_filename.set(cp.get(SEC_PRIMENET, "logfile").strip())
			else:
				self.log_filename.set("autoprimenet.log")
			if cp.has_option(SEC_PRIMENET, "WorkerDiskSpace"):
				self.worker_disk_space.set(cp.get(SEC_PRIMENET, "WorkerDiskSpace").strip())
			else:
				self.worker_disk_space.set("0.0")
			if cp.has_option(SEC_PRIMENET, "ProofArchiveDir"):
				self.archive_dir.set(cp.get(SEC_PRIMENET, "ProofArchiveDir").strip())
			else:
				self.archive_dir.set("")
			if cp.has_option(SEC_PRIMENET, "HoursBetweenCheckins"):
				self.checkin.set(cp.get(SEC_PRIMENET, "HoursBetweenCheckins"))
			if cp.has_option(SEC_PRIMENET, "DaysOfWork"):
				self.days_work.set(cp.get(SEC_PRIMENET, "DaysOfWork"))
			if cp.has_option(SEC_PRIMENET, "MaxExponents"):
				self.max_exponents.set(cp.get(SEC_PRIMENET, "MaxExponents").strip())
			else:
				self.max_exponents.set("")
			if cp.has_option(SEC_PRIMENET, "CertWork"):
				try:
					self.cert_work_var.set(1 if cp.getboolean(SEC_PRIMENET, "CertWork") else 0)
				except ValueError:
					v = cp.get(SEC_PRIMENET, "CertWork").strip().lower()
					self.cert_work_var.set(1 if v in ("1", "true", "yes", "on") else 0)
			else:
				self.cert_work_var.set(0)
			if cp.has_option(SEC_PRIMENET, "CertDailyCPULimit"):
				self.cert_cpu_limit.set(cp.get(SEC_PRIMENET, "CertDailyCPULimit").strip())
			else:
				self.cert_cpu_limit.set("10")
			if cp.has_option(SEC_PRIMENET, "ComputerID"):
				self.computer_id.set(cp.get(SEC_PRIMENET, "ComputerID")[:20])
			self._workpref_code_pending = None
			if cp.has_option(SEC_PRIMENET, "WorkPreference"):
				try:
					self._workpref_code_pending = int(cp.get(SEC_PRIMENET, "WorkPreference").strip())
				except ValueError:
					pass
			if self._workpref_code_pending is None and cp.has_section("Worker #1") and cp.has_option("Worker #1", "WorkPreference"):
				try:
					self._workpref_code_pending = int(cp.get("Worker #1", "WorkPreference").strip())
				except ValueError:
					pass
			if cp.has_option(SEC_PRIMENET, "GetMinExponent"):
				try:
					self.tf1g.set(1 if int(cp.get(SEC_PRIMENET, "GetMinExponent").strip()) >= MAX_PRIMENET_EXP else 0)
				except ValueError:
					self.tf1g.set(0)
			else:
				self.tf1g.set(0)
			self.get_min_exp.set("")
			self.get_max_exp.set("")
			if self.program.get() in ("mfaktc", "mfakto") and self.tf1g.get():
				pass
			elif cp.has_option(SEC_PRIMENET, "GetMinExponent"):
				try:
					gme = int(cp.get(SEC_PRIMENET, "GetMinExponent").strip(), 10)
					if gme < MAX_PRIMENET_EXP:
						self.get_min_exp.set(str(gme))
				except ValueError:
					pass
			if cp.has_option(SEC_PRIMENET, "GetMaxExponent"):
				self.get_max_exp.set(cp.get(SEC_PRIMENET, "GetMaxExponent").strip())
			self.min_bit.set("")
			self.max_bit.set("")
			self.force_target_bits.set(0)
			if cp.has_option(SEC_PRIMENET, "min_bit"):
				self.min_bit.set(cp.get(SEC_PRIMENET, "min_bit").strip())
			if cp.has_option(SEC_PRIMENET, "max_bit"):
				self.max_bit.set(cp.get(SEC_PRIMENET, "max_bit").strip())
			if cp.has_option(SEC_PRIMENET, "force_target_bits"):
				try:
					self.force_target_bits.set(1 if cp.getboolean(SEC_PRIMENET, "force_target_bits") else 0)
				except ValueError:
					v = cp.get(SEC_PRIMENET, "force_target_bits").strip().lower()
					self.force_target_bits.set(1 if v in ("1", "true", "yes", "on") else 0)
			if cp.has_option(SEC_INTERNALS, INI_GUI_GPU_INDEX):
				self._gpu_pick_pending = cp.get(SEC_INTERNALS, INI_GUI_GPU_INDEX).strip()
			else:
				self._gpu_pick_pending = None
			if cp.has_option(SEC_PRIMENET, "no_report_100m"):
				try:
					suppress_100m = cp.getboolean(SEC_PRIMENET, "no_report_100m")
				except ValueError:
					suppress_100m = True
				self.report_100m_var.set(0 if suppress_100m else 1)
			else:
				self.report_100m_var.set(1)
			if cp.has_section(SEC_EMAIL):
				if cp.has_option(SEC_EMAIL, "from_email"):
					self.email_from.set(cp.get(SEC_EMAIL, "from_email").strip())
				else:
					self.email_from.set("")
				if cp.has_option(SEC_EMAIL, "smtp_server"):
					self.email_smtp.set(cp.get(SEC_EMAIL, "smtp_server").strip())
				else:
					self.email_smtp.set("")
				if cp.has_option(SEC_EMAIL, "to_emails"):
					self.email_to.set(cp.get(SEC_EMAIL, "to_emails").strip())
				else:
					self.email_to.set("")
				if cp.has_option(SEC_EMAIL, "tls"):
					try:
						self.email_tls.set(1 if cp.getboolean(SEC_EMAIL, "tls") else 0)
					except ValueError:
						self.email_tls.set(0)
				else:
					self.email_tls.set(0)
				if cp.has_option(SEC_EMAIL, "starttls"):
					try:
						self.email_starttls.set(1 if cp.getboolean(SEC_EMAIL, "starttls") else 0)
					except ValueError:
						self.email_starttls.set(0)
				else:
					self.email_starttls.set(0)
				if self.email_tls.get() and self.email_starttls.get():
					self.email_starttls.set(0)
				if cp.has_option(SEC_EMAIL, "username"):
					self.email_username.set(cp.get(SEC_EMAIL, "username").strip())
				else:
					self.email_username.set("")
				if cp.has_option(SEC_EMAIL, "password"):
					self.email_password.set(cp.get(SEC_EMAIL, "password").strip())
				else:
					self.email_password.set("")
			else:
				self.email_from.set("")
				self.email_smtp.set("")
				self.email_to.set("")
				self.email_tls.set(0)
				self.email_starttls.set(0)
				self.email_username.set("")
				self.email_password.set("")
			self._update_cert_limit_row_visibility()
			self._update_cert_work_controls_for_program()
		finally:
			self._suppress_ini_write = False
			self._ensure_single_worker_ini()
			self.root.after(100, lambda: self._schedule_register_attempt("prereqs"))

	def _ensure_single_worker_ini(self):
		"""Force ``NumWorkers`` 1, hoist ``WorkPreference`` from Worker #1, and drop per-worker prefs."""
		wd, err = self._resolve_workdir_from_var()
		if err:
			return
		path = os.path.join(wd, LOCALFILE_DEFAULT)
		cp = self._read_ini_file(path)
		if not cp.has_section(SEC_PRIMENET):
			cp.add_section(SEC_PRIMENET)
		changed = False
		if not cp.has_option(SEC_PRIMENET, "NumWorkers") or cp.get(SEC_PRIMENET, "NumWorkers").strip() != "1":
			cp.set(SEC_PRIMENET, "NumWorkers", "1")
			changed = True
		if not cp.has_option(SEC_PRIMENET, "WorkPreference") and cp.has_section("Worker #1") and cp.has_option(
			"Worker #1", "WorkPreference"
		):
			cp.set(SEC_PRIMENET, "WorkPreference", cp.get("Worker #1", "WorkPreference"))
			changed = True
		for i in range(1, 32):
			ws = "Worker #{}".format(i)
			if cp.has_section(ws) and cp.has_option(ws, "WorkPreference"):
				cp.remove_option(ws, "WorkPreference")
				changed = True
		if changed:
			ok, msg = self._write_ini_file(path, cp)
			if not ok:
				messagebox.showerror("AutoPrimeNet", "Could not save {!r}:\n{}".format(path, msg))

	def _split_to_emails_list(self):
		"""Return non-empty To addresses from the notifications field (comma or semicolon separated)."""
		raw = (self.email_to.get() or "").strip()
		if not raw:
			return []
		parts = []
		for chunk in raw.replace(";", ",").split(","):
			c = chunk.strip()
			if c:
				parts.append(c)
		return parts

	def _flush_notifications_to_ini(self):
		"""Write report-100m and e-mail settings from the UI into ``prime.ini``."""
		if self._suppress_ini_write:
			return
		if self.email_tls.get() and self.email_starttls.get():
			self.email_starttls.set(0)
		wd, err = self._resolve_workdir_from_var()
		if err:
			return
		path = os.path.join(wd, LOCALFILE_DEFAULT)
		cp = self._read_ini_file(path)
		if not cp.has_section(SEC_PRIMENET):
			cp.add_section(SEC_PRIMENET)
		if not cp.has_section(SEC_EMAIL):
			cp.add_section(SEC_EMAIL)
		if self.report_100m_var.get():
			if cp.has_option(SEC_PRIMENET, "no_report_100m"):
				cp.remove_option(SEC_PRIMENET, "no_report_100m")
		else:
			cp.set(SEC_PRIMENET, "no_report_100m", "True")
		fe = (self.email_from.get() or "").strip()
		if fe:
			cp.set(SEC_EMAIL, "from_email", fe)
		elif cp.has_option(SEC_EMAIL, "from_email"):
			cp.remove_option(SEC_EMAIL, "from_email")
		sm = (self.email_smtp.get() or "").strip()
		if sm:
			cp.set(SEC_EMAIL, "smtp_server", sm)
		elif cp.has_option(SEC_EMAIL, "smtp_server"):
			cp.remove_option(SEC_EMAIL, "smtp_server")
		tos = self._split_to_emails_list()
		if tos:
			cp.set(SEC_EMAIL, "to_emails", ",".join(tos))
		elif cp.has_option(SEC_EMAIL, "to_emails"):
			cp.remove_option(SEC_EMAIL, "to_emails")
		if self.email_tls.get():
			cp.set(SEC_EMAIL, "tls", "True")
		elif cp.has_option(SEC_EMAIL, "tls"):
			cp.remove_option(SEC_EMAIL, "tls")
		if self.email_starttls.get():
			cp.set(SEC_EMAIL, "starttls", "True")
		elif cp.has_option(SEC_EMAIL, "starttls"):
			cp.remove_option(SEC_EMAIL, "starttls")
		eu = (self.email_username.get() or "").strip()
		if eu:
			cp.set(SEC_EMAIL, "username", eu)
		elif cp.has_option(SEC_EMAIL, "username"):
			cp.remove_option(SEC_EMAIL, "username")
		ep = (self.email_password.get() or "").strip()
		if ep:
			cp.set(SEC_EMAIL, "password", ep)
		elif cp.has_option(SEC_EMAIL, "password"):
			cp.remove_option(SEC_EMAIL, "password")
		ok, msg = self._write_ini_file(path, cp)
		if not ok:
			messagebox.showerror("AutoPrimeNet", "Could not save {!r}:\n{}".format(path, msg))

	def _on_blur_notification_field(self):
		"""Persist notification tab fields after focus leaves an e-mail entry."""
		if self._suppress_ini_write:
			return
		wd, err = self._resolve_workdir_from_var()
		if err:
			messagebox.showwarning("AutoPrimeNet", err)
			return
		self._flush_notifications_to_ini()

	def _on_report_100m_toggle(self):
		"""Save ``--report-100m`` / ``--no-report-100m`` state to ``prime.ini``."""
		if self._suppress_ini_write:
			return
		wd, err = self._resolve_workdir_from_var()
		if err:
			messagebox.showwarning("AutoPrimeNet", err)
			return
		self._flush_notifications_to_ini()

	def _on_notif_tls_toggle(self):
		"""Mutually exclusive TLS vs STARTTLS; flush notification settings."""
		if self.email_tls.get():
			self.email_starttls.set(0)
		if self._suppress_ini_write:
			return
		self._on_blur_notification_field()

	def _on_notif_starttls_toggle(self):
		"""Mutually exclusive STARTTLS vs TLS; flush notification settings."""
		if self.email_starttls.get():
			self.email_tls.set(0)
		if self._suppress_ini_write:
			return
		self._on_blur_notification_field()

	def _on_blur_workdir(self):
		"""Store absolute work directory path in ``[Internals]`` as ``GUIWorkdir``."""
		if self._suppress_ini_write:
			return
		wd, err = self._resolve_workdir_from_var()
		if err:
			messagebox.showwarning("AutoPrimeNet", err)
			return
		wd_abs = os.path.normpath(os.path.abspath(wd))
		self._persist_ini_updates({SEC_INTERNALS: {INI_GUI_WORKDIR: wd_abs}})

	def _sync_create_account_link_visibility(self, *_args):
		"""Show Create account only for the default anonymous user ID."""
		if not getattr(self, "_create_account_link", None):
			return
		u = (self.user_id.get() or "").strip()
		if not u or u.upper() == "ANONYMOUS":
			self._create_account_link.grid()
		else:
			self._create_account_link.grid_remove()

	def _on_blur_username(self):
		"""Save PrimeNet ``username``, sync ``user_name`` if still anonymous, and schedule re-registration if it changed."""
		uid = (self.user_id.get() or "").strip() or "ANONYMOUS"
		updates = {"username": uid}
		if not self._suppress_ini_write:
			wd, err = self._resolve_workdir_from_var()
			if not err:
				cp = self._read_ini_file(os.path.join(wd, LOCALFILE_DEFAULT))
				if uid.upper() != "ANONYMOUS":
					prev_un = ""
					if cp.has_section(SEC_PRIMENET) and cp.has_option(SEC_PRIMENET, "user_name"):
						prev_un = cp.get(SEC_PRIMENET, "user_name").strip()
					if not prev_un or prev_un.upper() == "ANONYMOUS":
						updates["user_name"] = uid
		self._persist_ini_updates({SEC_PRIMENET: updates})
		self._schedule_register_attempt("username")

	def _on_blur_checkin(self):
		"""Validate check-in hours (1–168) and save ``HoursBetweenCheckins``."""
		try:
			ch = int(self.checkin.get().strip() or "1")
			if not 1 <= ch <= 168:
				raise ValueError
		except ValueError:
			messagebox.showwarning("AutoPrimeNet", "Check-in hours must be between 1 and 168.")
			return
		self._persist_ini_updates({SEC_PRIMENET: {"HoursBetweenCheckins": str(ch)}})

	def _on_blur_days_work(self):
		"""Validate days of work (0–180) and save ``DaysOfWork``."""
		try:
			d = float(self.days_work.get().strip())
			if not 0.0 <= d <= 180.0:
				raise ValueError
		except ValueError:
			messagebox.showwarning("AutoPrimeNet", "Days of work must be a number from 0 through 180.")
			return
		self._persist_ini_updates({SEC_PRIMENET: {"DaysOfWork": str(d)}})

	def _on_blur_max_exponents(self):
		"""Validate ``MaxExponents`` or remove it when blank."""
		if self._suppress_ini_write:
			return
		wd, err = self._resolve_workdir_from_var()
		if err:
			messagebox.showwarning("AutoPrimeNet", err)
			return
		raw = (self.max_exponents.get() or "").strip()
		path = os.path.join(wd, LOCALFILE_DEFAULT)
		cp = self._read_ini_file(path)
		if not cp.has_section(SEC_PRIMENET):
			cp.add_section(SEC_PRIMENET)
		if not raw:
			if cp.has_option(SEC_PRIMENET, "MaxExponents"):
				cp.remove_option(SEC_PRIMENET, "MaxExponents")
				ok, msg = self._write_ini_file(path, cp)
				if not ok:
					messagebox.showerror("AutoPrimeNet", "Could not save {!r}:\n{}".format(path, msg))
			return
		try:
			n = int(raw)
			if n < 1 or n > 1000000:
				raise ValueError
		except ValueError:
			messagebox.showwarning("AutoPrimeNet", "Max exponents must be an integer from 1 through 1000000.")
			return
		cp.set(SEC_PRIMENET, "MaxExponents", str(n))
		ok, msg = self._write_ini_file(path, cp)
		if not ok:
			messagebox.showerror("AutoPrimeNet", "Could not save {!r}:\n{}".format(path, msg))

	def _on_program_change(self):
		"""Update program flags in ini, workpref list, visibility, and queue registration when program changes."""
		if self._suppress_ini_write:
			return
		newp = (self.program.get() or "").strip()
		if newp not in PROGRAM_KEYS:
			self._prev_program_for_defaults = None
			self._refresh_workpref_choices()
			self._update_mfak_and_gpu_visibility()
			self._update_cert_work_controls_for_program()
			return
		prev = getattr(self, "_prev_program_for_defaults", None)
		if prev in PROGRAM_KEYS:
			old_def = self._default_results_filename(prev)
			if self.results_file.get().strip() == old_def:
				self.results_file.set(self._default_results_filename(newp))
				self._on_blur_results_file()
			old_wf = self._default_work_filename(prev)
			if self.work_file.get().strip() == old_wf:
				self.work_file.set(self._default_work_filename(newp))
				self._on_blur_work_file()
		self._prev_program_for_defaults = newp
		self._persist_gimps_program_ini(newp)
		self._refresh_workpref_choices()
		self._update_mfak_and_gpu_visibility()
		self._update_cert_work_controls_for_program()
		self._persist_workpreference_to_ini()
		self._schedule_register_attempt("prereqs")

	def _browse_workdir(self):
		"""Let the user pick a work directory, then reload settings from that folder's ``prime.ini``."""
		path = filedialog.askdirectory(initialdir=self.workdir.get() or os.getcwd())
		if path:
			self.workdir.set(path)
			self._on_blur_workdir()
			self._load_ui_from_ini()
			if self._gpu_list_fetched:
				self._apply_gpu_combo_selection()
			self._update_gpu_picker_visibility()
			self._update_tf_options_visibility()
			self._update_get_exp_visibility()
			self._update_cert_work_controls_for_program()

	@staticmethod
	def _normalized_primeuserid_from_cp(cp):
		"""Return stripped ``[PrimeNet] username`` or ``ANONYMOUS``."""
		if not cp.has_section(SEC_PRIMENET):
			return "ANONYMOUS"
		if cp.has_option(SEC_PRIMENET, "username"):
			u = cp.get(SEC_PRIMENET, "username").strip()
			return u if u else "ANONYMOUS"
		return "ANONYMOUS"

	def _ini_has_computer_guid(self):
		"""True if current ``prime.ini`` has a non-empty ``ComputerGUID``."""
		wd, err = self._resolve_workdir_from_var()
		if err:
			return False
		path = os.path.join(wd, LOCALFILE_DEFAULT)
		if not os.path.isfile(path):
			return False
		cp = self._read_ini_file(path)
		if not cp.has_section(SEC_PRIMENET):
			return False
		if not cp.has_option(SEC_PRIMENET, "ComputerGUID"):
			return False
		return bool(cp.get(SEC_PRIMENET, "ComputerGUID").strip())

	def _registration_prereqs_ok(self):
		"""True when work directory exists and current workpref is valid for the program."""
		wd, err = self._resolve_workdir_from_var()
		if err or not (wd and os.path.isdir(wd)):
			return False
		key = self._gimps_program_key()
		if not key:
			return False
		try:
			wp = self._get_workpref_code()
		except (TypeError, ValueError):
			return False
		if wp is None:
			return False
		return wp in supported_workprefs(key)

	def _schedule_register_attempt(self, reason):
		"""Debounce PrimeNet registration; *reason* is ``prereqs`` or ``username``."""
		aid = getattr(self, "_register_after_id", None)
		if aid is not None:
			try:
				self.root.after_cancel(aid)
			except tk.TclError:
				pass
			self._register_after_id = None

		def run():
			"""Run the deferred registration attempt."""
			self._register_after_id = None
			self._try_register_via_subprocess(reason)

		self._register_after_id = self.root.after(400, run)

	def _try_register_via_subprocess(self, reason):
		"""Run ``autoprimenet --register-only`` in a subprocess when prerequisites and gates allow."""
		if self.proc is not None and self.proc.poll() is None:
			return
		if not self._registration_prereqs_ok():
			return
		current_uid = (self.user_id.get() or "").strip() or "ANONYMOUS"
		if reason == "prereqs":
			if self._ini_has_computer_guid():
				return
		elif reason == "username":
			if current_uid == self._primeuserid_for_register:
				return
		else:
			return
		try:
			self._flush_notifications_to_ini()
			base = self._build_base_argv()
		except RuntimeError:
			return
		self._sync_hardware_to_ini(base, quiet=True)
		rcmd = list(base) + ["--register-only"]
		line_cmd = subprocess.list2cmdline(rcmd)
		kwargs = {
			"stdout": subprocess.PIPE,
			"stderr": subprocess.STDOUT,
			"cwd": self.workdir.get().strip() or None,
		}
		if sys.version_info >= (3, 7):
			kwargs["text"] = True
			kwargs["encoding"] = "utf-8"
			kwargs["errors"] = "replace"
		else:
			kwargs["universal_newlines"] = True
		try:
			completed = subprocess.run(rcmd, **kwargs)
			rc = completed.returncode
			out = completed.stdout
		except OSError as e:
			try:
				self.notebook.select(self._tab_output)
			except tk.TclError:
				pass
			self._append_log("\n$ " + line_cmd + "\n\n[could not start: {}]\n".format(e))
			messagebox.showerror("AutoPrimeNet", "PrimeNet registration could not start:\n{}".format(e))
			return
		if not isinstance(out, str):
			try:
				out = (out or b"").decode("utf-8", "replace")
			except (AttributeError, TypeError):
				out = str(out or "")
		self._append_log("\n$ " + line_cmd + "\n\n" + out)
		if rc != 0:
			try:
				self.notebook.select(self._tab_output)
			except tk.TclError:
				pass
			messagebox.showerror(
				"AutoPrimeNet",
				"PrimeNet registration failed (exit code {}). See Output for details.".format(rc),
			)
			return
		wd, err = self._resolve_workdir_from_var()
		if not err:
			path = os.path.join(wd, LOCALFILE_DEFAULT)
			cp = self._read_ini_file(path)
			self._primeuserid_for_register = self._normalized_primeuserid_from_cp(cp)
		else:
			self._primeuserid_for_register = current_uid

	def _append_log(self, text):
		"""Append *text* to the Output tab and scroll to the end."""
		self.log.configure(state="normal")
		self.log.insert("end", text)
		self.log.see("end")
		self.log.configure(state="disabled")

	def _deferred_sync_ini(self):
		"""After startup, merge hardware into ``prime.ini`` via ``--sync-ini`` if no run is active."""
		if self.proc is not None and self.proc.poll() is None:
			return
		try:
			self._flush_notifications_to_ini()
			base = self._build_base_argv()
		except RuntimeError:
			return
		self._sync_hardware_to_ini(base, quiet=True)

	def _sync_hardware_to_ini(self, base_cmd, quiet=False):
		"""Run ``--sync-ini`` with the same argv prefix as a normal launch; optionally log errors."""
		scmd = list(base_cmd) + ["--sync-ini"]
		kwargs = {
			"stdout": subprocess.PIPE,
			"stderr": subprocess.STDOUT,
			"cwd": self.workdir.get().strip() or None,
		}
		if sys.version_info >= (3, 7):
			kwargs["text"] = True
			kwargs["encoding"] = "utf-8"
			kwargs["errors"] = "replace"
		else:
			kwargs["universal_newlines"] = True
		try:
			proc = subprocess.Popen(scmd, **kwargs)
		except OSError as e:
			if not quiet:
				self._append_log("\n[--sync-ini could not start: {}]\n".format(e))
			return
		out = ""
		rc = -1
		if sys.version_info >= (3, 3):
			try:
				out, _ = proc.communicate(timeout=180)
				rc = proc.returncode
			except subprocess.TimeoutExpired:
				try:
					proc.kill()
				except OSError:
					pass
				if not quiet:
					self._append_log("\n[--sync-ini timed out]\n")
				return
		else:
			out, _ = proc.communicate()
			rc = proc.returncode
		if not isinstance(out, str):
			try:
				out = out.decode("utf-8", "replace")
			except (AttributeError, UnicodeDecodeError):
				out = str(out)
		if rc != 0 and not quiet:
			self._append_log("\n[--sync-ini exit code {}]\n{}\n".format(rc, out))

	def _poll_out(self):
		"""Drain subprocess stdout lines from the queue into the log widget (periodic ``after``)."""
		try:
			while True:
				line = self.out_queue.get_nowait()
				self._append_log(line)
		except queue.Empty:
			pass
		self.root.after(100, self._poll_out)

	def _build_base_argv(self):
		"""Build the autoprimenet argv prefix (workdir, user, flags, workpref, e-mail) from the UI."""
		key = self._gimps_program_key()
		if not key:
			raise RuntimeError("Choose a GIMPS program in the Setup tab.")
		cmd = list(_autoprimenet_command())
		wd = self.workdir.get().strip()
		if wd:
			cmd.extend(["-w", wd])
		cmd.extend(["-u", self.user_id.get().strip() or "ANONYMOUS"])
		try:
			ch = int(self.checkin.get().strip() or "1")
			if 1 <= ch <= 168:
				cmd.extend(["--checkin", str(ch)])
		except ValueError:
			pass
		try:
			dw = float(self.days_work.get().strip() or "3")
			if 0.0 <= dw <= 180.0:
				cmd.extend(["--days-work", str(dw)])
		except ValueError:
			pass

		flag = {
			"mlucas": "-m",
			"gpuowl": "-g",
			"prpll": "--prpll",
			"prmers": "--prmers",
			"cudalucas": "--cudalucas",
			"mfaktc": "--mfaktc",
			"mfakto": "--mfakto",
		}.get(key, "-m")
		cmd.append(flag)

		try:
			wp = self._get_workpref_code()
			if wp is not None and wp in supported_workprefs(key):
				cmd.extend(["-T", str(wp)])
		except (TypeError, ValueError):
			pass

		cn = (self.computer_id.get() or "").strip()[:20]
		if cn:
			cmd.extend(["-H", cn])

		if key in ("mfaktc", "mfakto") and self.tf1g.get():
			cmd.extend(["--min-exp", str(MAX_PRIMENET_EXP)])

		if key == "prpll":
			if self.cert_work_var.get():
				cmd.extend(["--cert-work"])
				try:
					n = int((self.cert_cpu_limit.get() or "10").strip(), 10)
					if not 1 <= n <= 100:
						raise ValueError
				except ValueError:
					n = 10
				cmd.extend(["--cert-work-limit", str(n)])
			else:
				cmd.extend(["--no-cert-work"])
		else:
			cmd.extend(["--no-cert-work"])

		if self.report_100m_var.get():
			cmd.append("--report-100m")
		else:
			cmd.append("--no-report-100m")

		fe = (self.email_from.get() or "").strip()
		sm = (self.email_smtp.get() or "").strip()
		if fe and sm:
			cmd.extend(["-f", fe, "-S", sm])
			for addr in self._split_to_emails_list():
				cmd.extend(["--to-email", addr])
			if self.email_tls.get():
				cmd.append("--tls")
			if self.email_starttls.get():
				cmd.append("--starttls")
			eu = (self.email_username.get() or "").strip()
			if eu:
				cmd.extend(["-U", eu])

		return cmd

	def _register_exponent_work_type_from_combo(self, line):
		line = (line or "").strip()
		for sep in ("\u2014", "\u2013", "—", " - "):
			if sep in line:
				return int(line.split(sep, 1)[0].strip())
		if "-" in line:
			return int(line.split("-", 1)[0].strip())
		return int(line.split()[0])

	def _register_exponent_build_spec(self, wt, n_raw, field_vars, pminus1ed_var):
		"""Build JSON spec dict for ``--register-exponents-json`` (stdin); raises ValueError on invalid input."""
		n_raw = (n_raw or "").strip()
		if not n_raw:
			raise ValueError("Exponent n is required.")

		def gv(k):
			return (field_vars[k].get() or "").strip()

		spec = {"work_type": wt, "n": int(n_raw)}

		if wt in (100, 101):
			spec["sieve_depth"] = float(gv("sieve_depth") or "99")
			spec["pminus1ed"] = 1 if pminus1ed_var.get() else 0
			return spec
		if wt == 2:
			if not gv("sieve_depth") or not gv("factor_to"):
				raise ValueError("Trial factoring requires sieve depth and factor-to bit levels.")
			spec["sieve_depth"] = float(gv("sieve_depth"))
			spec["factor_to"] = float(gv("factor_to"))
			return spec
		if wt == 3:
			if not gv("B1") or not gv("B2"):
				raise ValueError("Pminus1= requires B1 and B2.")
			spec["B1"] = int(gv("B1"))
			spec["B2"] = int(gv("B2"))
			if gv("sieve_depth"):
				spec["sieve_depth"] = float(gv("sieve_depth"))
			if gv("B2_start"):
				spec["B2_start"] = int(gv("B2_start"))
			kf = gv("known_factors")
			if kf:
				spec["known_factors"] = [int(x.strip()) for x in kf.replace(";", ",").split(",") if x.strip()]
			return spec
		if wt == 4:
			for req in ("B1", "B2", "sieve_depth", "tests_saved"):
				if not gv(req):
					raise ValueError("Pfactor= requires B1, B2, sieve depth, and tests_saved.")
			spec["B1"] = int(gv("B1"))
			spec["B2"] = int(gv("B2"))
			spec["sieve_depth"] = float(gv("sieve_depth"))
			spec["tests_saved"] = float(gv("tests_saved"))
			kf = gv("known_factors")
			if kf:
				spec["known_factors"] = [int(x.strip()) for x in kf.replace(";", ",").split(",") if x.strip()]
			return spec
		if wt == 5:
			if not gv("B1") or not gv("curves_to_do"):
				raise ValueError("ECM requires B1 and curves to test.")
			spec["B1"] = int(gv("B1"))
			if gv("B2"):
				spec["B2"] = int(gv("B2"))
			spec["curves_to_do"] = int(gv("curves_to_do"))
			kf = gv("known_factors")
			if kf:
				spec["known_factors"] = [int(x.strip()) for x in kf.replace(";", ",").split(",") if x.strip()]
			return spec
		if wt == 150:
			for req in ("sieve_depth", "tests_saved", "B1", "B2"):
				if not gv(req):
					raise ValueError("First-time PRP requires sieve depth, tests_saved, B1, and B2.")
			spec["sieve_depth"] = float(gv("sieve_depth"))
			spec["tests_saved"] = float(gv("tests_saved"))
			spec["B1"] = int(gv("B1"))
			spec["B2"] = int(gv("B2"))
			kf = gv("known_factors")
			if kf:
				spec["known_factors"] = [int(x.strip()) for x in kf.replace(";", ",").split(",") if x.strip()]
			return spec
		if wt == 151:
			for req in ("sieve_depth", "tests_saved", "B1", "B2"):
				if not gv(req):
					raise ValueError("PRP double-check requires sieve depth, tests_saved, B1, and B2.")
			spec["sieve_depth"] = float(gv("sieve_depth"))
			spec["tests_saved"] = float(gv("tests_saved"))
			spec["B1"] = int(gv("B1"))
			spec["B2"] = int(gv("B2"))
			spec["prp_base"] = int(gv("prp_base") or "3")
			spec["prp_residue_type"] = int(gv("prp_residue_type") or "1")
			kf = gv("known_factors")
			if kf:
				spec["known_factors"] = [int(x.strip()) for x in kf.replace(";", ",").split(",") if x.strip()]
			return spec
		raise ValueError("Unsupported work type.")

	def _parse_last_json_object_from_text(self, text):
		"""Return the object parsed from the last ``{...}`` in *text*, or None if parsing fails."""
		if text is None:
			return None
		if not isinstance(text, type(u"")):
			try:
				text = (text or b"").decode("utf-8", "replace")
			except (AttributeError, TypeError):
				text = str(text or "")
		start = text.rfind("{")
		if start < 0:
			return None
		try:
			return json.loads(text[start:])
		except ValueError:
			return None

	def _on_register_exponent_dialog(self):
		"""Open a dialog to collect manual register-exponents fields and run ``--register-exponents-json`` with JSON on stdin."""
		if self.proc is not None and self.proc.poll() is None:
			messagebox.showwarning("AutoPrimeNet", "A run is already in progress. Stop it first.")
			return
		wd, err = self._resolve_workdir_from_var()
		if err:
			messagebox.showwarning("AutoPrimeNet", err)
			return

		top = tk.Toplevel(self.root)
		_apply_window_icon(top)
		top.title("Register exponent on PrimeNet")
		top.transient(self.root)
		top.grab_set()
		frm = ttk.Frame(top, padding=12)
		frm.grid(row=0, column=0, sticky="nsew")
		top.columnconfigure(0, weight=1)
		top.rowconfigure(0, weight=1)

		ttk.Label(frm, text="Work type").grid(row=0, column=0, sticky="nw", pady=(0, 4))
		wt_combo = ttk.Combobox(
			frm,
			width=52,
			state="readonly",
			values=[label for _, label in REGISTER_EXPONENT_WORKTYPES],
		)
		wt_combo.grid(row=0, column=1, columnspan=2, sticky="ew", pady=(0, 4))
		wt_combo.current(6)

		n_var = tk.StringVar()
		ex_row = ttk.Frame(frm)
		ex_row.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 4))
		ttk.Label(ex_row, text="Exponent n (prime)").pack(side="left")
		entry_n = ttk.Entry(ex_row, textvariable=n_var, width=18)
		entry_n.pack(side="left", padx=(8, 8))

		pminus1ed_var = tk.IntVar(value=1)
		cb_pminus = ttk.Checkbutton(frm, text="Exponent has been P-1 factored (LL only)", variable=pminus1ed_var)
		cb_pminus.grid(row=2, column=0, columnspan=3, sticky="w", pady=(0, 4))

		dyn = ttk.Frame(frm)
		dyn.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(4, 4))
		field_vars = {k: tk.StringVar() for k, _ in _REGISTER_EXPONENT_FIELDS}
		field_rows = {}

		def refresh_dynamic():
			for w in dyn.winfo_children():
				w.destroy()
			field_rows.clear()
			try:
				wt = self._register_exponent_work_type_from_combo(wt_combo.get())
			except (ValueError, IndexError):
				return
			vis = _REGISTER_EXPONENT_VISIBLE.get(wt, set())
			if wt in (100, 101):
				cb_pminus.grid()
			else:
				cb_pminus.grid_remove()
			r = 0
			for key, lbl in _REGISTER_EXPONENT_FIELDS:
				if key not in vis:
					continue
				ttk.Label(dyn, text=lbl).grid(row=r, column=0, sticky="nw", pady=2)
				e = ttk.Entry(dyn, textvariable=field_vars[key], width=44)
				e.grid(row=r, column=1, columnspan=2, sticky="ew", pady=2)
				field_rows[key] = e
				r += 1
			defaults = {
				(100, "sieve_depth"): "99",
				(101, "sieve_depth"): "99",
				(150, "sieve_depth"): "99",
				(150, "tests_saved"): "1.3",
				(151, "sieve_depth"): "99",
				(151, "tests_saved"): "1.3",
				(151, "prp_base"): "3",
				(151, "prp_residue_type"): "1",
				(4, "tests_saved"): "0",
				(5, "curves_to_do"): "100",
			}
			for (wtk, fk), val in defaults.items():
				if wtk == wt and fk in field_rows and not field_vars[fk].get().strip():
					field_vars[fk].set(val)

		def on_wt_change(_event=None):
			refresh_dynamic()

		wt_combo.bind("<<ComboboxSelected>>", on_wt_change)
		refresh_dynamic()

		suggest_msg = tk.StringVar(value="")
		ttk.Label(frm, textvariable=suggest_msg, wraplength=480, justify="left", font=("TkDefaultFont", 8)).grid(
			row=4, column=0, columnspan=3, sticky="w", pady=(0, 4)
		)

		def run_fetch():
			"""Call ``--suggest-register-fields`` on exponent blur; fill empty fields only."""
			suggest_msg.set("")
			try:
				n_raw = (n_var.get() or "").strip()
				if not n_raw:
					return
				p = int(n_raw)
				wt = self._register_exponent_work_type_from_combo(wt_combo.get())
			except (ValueError, IndexError):
				return
			try:
				self._flush_notifications_to_ini()
				base = self._build_base_argv()
			except RuntimeError as e:
				suggest_msg.set(str(e))
				return
			self._sync_hardware_to_ini(base, quiet=True)
			ts = (field_vars.get("tests_saved") and (field_vars["tests_saved"].get() or "").strip()) or ""
			try:
				tsh = float(ts) if ts else 1.3
			except ValueError:
				tsh = 1.3
			req = {"n": p, "work_type": wt, "tests_saved": tsh}
			rcmd = list(base) + ["--suggest-register-fields"]
			try:
				try:
					payload = json.dumps(req, ensure_ascii=False) + "\n"
				except TypeError:
					payload = json.dumps(req) + "\n"
				if sys.version_info[0] < 3:
					if isinstance(payload, unicode):
						payload_b = payload.encode("utf-8")
					else:
						payload_b = payload
				else:
					payload_b = payload.encode("utf-8")
			except (TypeError, ValueError) as e:
				suggest_msg.set("Could not build request: {}".format(e))
				return
			try:
				proc = subprocess.Popen(
					rcmd,
					stdin=subprocess.PIPE,
					stdout=subprocess.PIPE,
					stderr=subprocess.STDOUT,
					cwd=self.workdir.get().strip() or None,
				)
				out, _ = proc.communicate(input=payload_b)
			except OSError as e:
				suggest_msg.set(str(e))
				return
			data = self._parse_last_json_object_from_text(out)
			if not isinstance(data, dict):
				suggest_msg.set("Could not parse suggestions from autoprimenet output.")
				return
			if not data.get("ok"):
				suggest_msg.set((data.get("error") or "Suggestion failed").strip())
				return
			vis = _REGISTER_EXPONENT_VISIBLE.get(wt, set())

			def apply_key(key, val_str):
				if key not in vis or key not in field_vars:
					return
				if (field_vars[key].get() or "").strip():
					return
				field_vars[key].set(val_str)

			if data.get("sieve_depth") is not None:
				apply_key("sieve_depth", str(int(data["sieve_depth"])))
			if data.get("factor_to") is not None:
				apply_key("factor_to", str(int(data["factor_to"])))
			if "B1" in data and data["B1"] is not None:
				apply_key("B1", str(int(data["B1"])))
			if "B2" in data and data["B2"] is not None:
				apply_key("B2", str(int(data["B2"])))
			if data.get("known_factors"):
				apply_key("known_factors", ",".join(str(x) for x in data["known_factors"]))
			hints = []
			if data.get("warning"):
				hints.append(data["warning"])
			if data.get("walk_error"):
				hints.append("walk: {}".format(data["walk_error"]))
			if data.get("p1_b1_mid") is not None and data.get("B1") == 0:
				hints.append(
					"GpuOwl/PRPLL often use B1=0, B2=0 on the CLI; MID-style bounds from mersenne.ca logic: B1={:n}, B2={:n}.".format(
						data["p1_b1_mid"], data.get("p1_b2_mid", 0)
					)
				)
			if hints:
				suggest_msg.set(" ".join(hints))
			else:
				suggest_msg.set("Filled fields from mersenne.ca (empty slots only).")

		def on_n_focus_out(_event=None):
			run_fetch()

		entry_n.bind("<FocusOut>", on_n_focus_out)

		btn_row = ttk.Frame(frm)
		btn_row.grid(row=5, column=0, columnspan=3, sticky="w", pady=(8, 0))

		def do_register():
			try:
				wt = self._register_exponent_work_type_from_combo(wt_combo.get())
				spec = self._register_exponent_build_spec(wt, n_var.get(), field_vars, pminus1ed_var)
			except ValueError as e:
				messagebox.showwarning("AutoPrimeNet", str(e), parent=top)
				return
			try:
				self._flush_notifications_to_ini()
				base = self._build_base_argv()
			except RuntimeError as e:
				messagebox.showerror("AutoPrimeNet", str(e), parent=top)
				return
			self._sync_hardware_to_ini(base, quiet=True)
			rcmd = list(base) + ["--register-exponents-json"]
			line_cmd = subprocess.list2cmdline(rcmd)
			try:
				payload = json.dumps(spec, indent=2, ensure_ascii=False) + "\n"
				if sys.version_info[0] < 3:
					if isinstance(payload, unicode):
						payload_b = payload.encode("utf-8")
					else:
						payload_b = payload
				else:
					payload_b = payload.encode("utf-8")
			except (TypeError, ValueError) as e:
				messagebox.showerror("AutoPrimeNet", "Could not serialize JSON: {}".format(e), parent=top)
				return
			try:
				proc = subprocess.Popen(
					rcmd,
					stdin=subprocess.PIPE,
					stdout=subprocess.PIPE,
					stderr=subprocess.STDOUT,
					cwd=self.workdir.get().strip() or None,
				)
				out, _ = proc.communicate(input=payload_b)
				rc = proc.returncode
			except OSError as e:
				try:
					self.notebook.select(self._tab_output)
				except tk.TclError:
					pass
				self._append_log("\n$ " + line_cmd + "\n\n[could not start: {}]\n".format(e))
				messagebox.showerror("AutoPrimeNet", str(e), parent=top)
				return
			if out is None:
				out = b""
			if not isinstance(out, type(u"")):
				try:
					out = (out or b"").decode("utf-8", "replace")
				except (AttributeError, TypeError):
					out = str(out or "")
			self._append_log("\n$ " + line_cmd + "\n\n" + out)
			if rc != 0:
				try:
					self.notebook.select(self._tab_output)
				except tk.TclError:
					pass
				messagebox.showerror(
					"AutoPrimeNet",
					"Registration failed (exit code {}). See Output for details.".format(rc),
					parent=top,
				)
			else:
				messagebox.showinfo("AutoPrimeNet", "Exponent registered. Check Output for log details.", parent=top)
				top.destroy()

		ttk.Button(btn_row, text="Register", command=do_register).pack(side="left", padx=(0, 8))
		ttk.Button(btn_row, text="Cancel", command=top.destroy).pack(side="left")

	def _load_work_todo_assignments_via_cli(self):
		"""Run ``--list-work-todo-exponents`` into a temp file; return ``(rows, None)`` or ``(None, error_message)``."""
		try:
			self._flush_notifications_to_ini()
			base = self._build_base_argv()
		except RuntimeError as e:
			return None, str(e)
		self._sync_hardware_to_ini(base, quiet=True)
		fd, path = tempfile.mkstemp(suffix=".json", prefix="apn_wtodo_")
		os.close(fd)
		rcmd = list(base) + ["--list-work-todo-exponents", path]
		line_cmd = subprocess.list2cmdline(rcmd)
		kwargs = {
			"stdout": subprocess.PIPE,
			"stderr": subprocess.STDOUT,
			"cwd": self.workdir.get().strip() or None,
		}
		if sys.version_info >= (3, 7):
			kwargs["text"] = True
			kwargs["encoding"] = "utf-8"
			kwargs["errors"] = "replace"
		else:
			kwargs["universal_newlines"] = True
		try:
			completed = subprocess.run(rcmd, **kwargs)
			out = completed.stdout or ""
		except OSError as e:
			try:
				os.unlink(path)
			except OSError:
				pass
			return None, str(e)
		if not isinstance(out, str):
			try:
				out = (out or b"").decode("utf-8", "replace")
			except (AttributeError, TypeError):
				out = str(out or "")
		if completed.returncode != 0:
			try:
				os.unlink(path)
			except OSError:
				pass
			self._append_log("\n$ " + line_cmd + "\n\n" + out)
			return None, "List work todo failed (exit code {}). See Output for details.".format(completed.returncode)
		try:
			with io.open(path, "r", encoding="utf-8") as f:
				raw = f.read()
		except (IOError, OSError) as e:
			try:
				os.unlink(path)
			except OSError:
				pass
			return None, "Could not read list output: {}".format(e)
		try:
			os.unlink(path)
		except OSError:
			pass
		try:
			rows = json.loads(raw)
		except ValueError as e:
			return None, "Invalid JSON from list-work-todo-exponents: {}".format(e)
		if not isinstance(rows, list):
			return None, "Expected a JSON array from list-work-todo-exponents."
		normalized = []
		for item in rows:
			if not isinstance(item, dict):
				continue
			try:
				w = int(item["worker"])
				exp = int(item["exponent"])
				line = item.get("assignment", "")
			except (KeyError, TypeError, ValueError):
				continue
			normalized.append({"worker": w, "exponent": exp, "assignment": line})
		return normalized, None

	def _on_unreserve_exponent_dialog(self):
		"""List assignments from the local work todo file(s) and run ``--unreserve`` for the user's choice."""
		if self.proc is not None and self.proc.poll() is None:
			messagebox.showwarning("AutoPrimeNet", "A run is already in progress. Stop it first.")
			return
		wd, err = self._resolve_workdir_from_var()
		if err:
			messagebox.showwarning("AutoPrimeNet", err)
			return
		rows, err = self._load_work_todo_assignments_via_cli()
		if err:
			messagebox.showerror("AutoPrimeNet", err)
			return
		if not rows:
			messagebox.showinfo(
				"AutoPrimeNet",
				"No assignments were found in your work todo file(s). "
				"If you use multiple workers, ensure NumWorkers and work todo names match your setup.",
			)
			return

		top = tk.Toplevel(self.root)
		_apply_window_icon(top)
		top.title("Unreserve exponent")
		top.transient(self.root)
		top.grab_set()
		frm = ttk.Frame(top, padding=12)
		frm.grid(row=0, column=0, sticky="nsew")
		top.columnconfigure(0, weight=1)
		top.rowconfigure(0, weight=1)
		frm.columnconfigure(0, weight=1)
		frm.rowconfigure(1, weight=1)

		ttk.Label(
			frm,
			text="Select an assignment to unreserve on PrimeNet and remove from the work todo (same as --unreserve). "
			"If the same exponent appears more than once, the first matching line in worker order is used.",
			wraplength=480,
			justify="left",
		).grid(row=0, column=0, sticky="ew", pady=(0, 8))

		lb_frame = ttk.Frame(frm)
		lb_frame.grid(row=1, column=0, sticky="nsew")
		lb_frame.columnconfigure(0, weight=1)
		lb_frame.rowconfigure(0, weight=1)
		listbox = tk.Listbox(lb_frame, height=12, width=86, selectmode="browse", exportselection=False)
		scroll = ttk.Scrollbar(lb_frame, orient="vertical", command=listbox.yview)
		listbox.configure(yscrollcommand=scroll.set)
		listbox.grid(row=0, column=0, sticky="nsew")
		scroll.grid(row=0, column=1, sticky="ns")

		for r in rows:
			ash = r.get("assignment") or ""
			if len(ash) > 72:
				ash = ash[:69] + "..."
			listbox.insert("end", "Worker {:n} — M{:n} — {}".format(r["worker"], r["exponent"], ash))

		btn_row = ttk.Frame(frm)
		btn_row.grid(row=2, column=0, sticky="w", pady=(10, 0))

		def do_unreserve():
			sel = listbox.curselection()
			if not sel:
				messagebox.showwarning("AutoPrimeNet", "Select an assignment first.", parent=top)
				return
			idx = int(sel[0])
			exp = rows[idx]["exponent"]
			if not messagebox.askyesno(
				"Unreserve exponent",
				"Unreserve M{:n} on PrimeNet and remove that line from the work todo?\n\n"
				"Only do this if you are sure you will not finish this exponent.".format(exp),
				parent=top,
			):
				return
			try:
				self._flush_notifications_to_ini()
				base = self._build_base_argv()
			except RuntimeError as e:
				messagebox.showerror("AutoPrimeNet", str(e), parent=top)
				return
			self._sync_hardware_to_ini(base, quiet=False)
			cmd = list(base) + ["--unreserve", str(exp)]
			try:
				self.notebook.select(self._tab_output)
			except tk.TclError:
				pass
			top.destroy()
			self._spawn_autoprimenet_subprocess(cmd)

		listbox.bind("<Double-Button-1>", lambda _e: do_unreserve())

		ttk.Button(btn_row, text="Unreserve selected", command=do_unreserve).pack(side="left", padx=(0, 8))
		ttk.Button(btn_row, text="Close", command=top.destroy).pack(side="left")

	def _on_unreserve_all(self):
		"""Ask for confirmation, then run ``--unreserve-all`` (report results and remove all assignments)."""
		if self.proc is not None and self.proc.poll() is None:
			messagebox.showwarning("AutoPrimeNet", "A run is already in progress. Stop it first.")
			return
		wd, err = self._resolve_workdir_from_var()
		if err:
			messagebox.showwarning("AutoPrimeNet", err)
			return
		if not messagebox.askyesno(
			"Unreserve all assignments",
			"This will report assignment results to PrimeNet, unreserve every assignment in your work todo file(s), "
			"and remove those lines — the same as running --unreserve-all.\n\n"
			"Only continue if you intend to drop all current work and will not finish these exponents.\n\n"
			"Proceed?",
		):
			return
		try:
			self._flush_notifications_to_ini()
			cmd = self._build_base_argv()
		except RuntimeError as e:
			messagebox.showerror("AutoPrimeNet", str(e))
			return
		self._sync_hardware_to_ini(cmd, quiet=False)
		cmd.append("--unreserve-all")
		try:
			self.notebook.select(self._tab_output)
		except tk.TclError:
			pass
		self._spawn_autoprimenet_subprocess(cmd)

	def _spawn_autoprimenet_subprocess(self, cmd):
		"""Start *cmd* as ``self.proc`` and stream stdout into ``out_queue`` on a background thread."""
		self._append_log("\n$ " + subprocess.list2cmdline(cmd) + "\n\n")
		kwargs = {
			"stdout": subprocess.PIPE,
			"stderr": subprocess.STDOUT,
			"cwd": self.workdir.get().strip() or None,
		}
		if sys.version_info >= (3, 7):
			kwargs["text"] = True
			kwargs["encoding"] = "utf-8"
			kwargs["errors"] = "replace"
		else:
			kwargs["universal_newlines"] = True

		try:
			self.proc = subprocess.Popen(cmd, **kwargs)
		except OSError as e:
			messagebox.showerror("AutoPrimeNet", "Failed to start process:\n{}".format(e))
			self.proc = None
			return

		def reader():
			"""Read process stdout line by line until EOF, then enqueue exit code."""
			proc = self.proc
			try:
				for line in iter(proc.stdout.readline, ""):
					if line == "":
						break
					self.out_queue.put(line)
			finally:
				if proc.stdout:
					proc.stdout.close()
				rc = proc.wait()
				self.out_queue.put("\n[exit code {}]\n".format(rc))
				if self.proc is proc:
					self.proc = None

		self.reader_thread = threading.Thread(target=reader)
		self.reader_thread.daemon = True
		self.reader_thread.start()

	def _on_test_email(self):
		"""Validate addresses, sync ini, then run ``--test-email`` and show output."""
		if self.proc is not None and self.proc.poll() is None:
			messagebox.showwarning("AutoPrimeNet", "A run is already in progress. Stop it first.")
			return
		wd, err = self._resolve_workdir_from_var()
		if err:
			messagebox.showwarning("AutoPrimeNet", err)
			return
		fe = (self.email_from.get() or "").strip()
		sm = (self.email_smtp.get() or "").strip()
		if not fe or not sm:
			messagebox.showwarning("AutoPrimeNet", "From e-mail and SMTP server are required.")
			return
		_, fa = parseaddr(fe)
		if not fa or "@" not in fa:
			messagebox.showwarning("AutoPrimeNet", "From e-mail does not look valid.")
			return
		for to_line in self._split_to_emails_list():
			_, ta = parseaddr(to_line)
			addr = ta or to_line.strip()
			if addr and "@" not in addr:
				messagebox.showwarning("AutoPrimeNet", "To address {!r} does not look valid.".format(to_line))
				return
		self._flush_notifications_to_ini()
		try:
			cmd = self._build_base_argv()
		except RuntimeError as e:
			messagebox.showerror("AutoPrimeNet", str(e))
			return
		cmd.append("--test-email")
		self._sync_hardware_to_ini(cmd, quiet=False)
		try:
			self.notebook.select(self._tab_output)
		except tk.TclError:
			pass
		self._spawn_autoprimenet_subprocess(cmd)

	def _run_action(self, extra_flag):
		"""Sync ini and spawn autoprimenet with *extra_flag* (e.g. ``-s``, ``--debug-info``)."""
		if self.proc is not None and self.proc.poll() is None:
			messagebox.showwarning("AutoPrimeNet", "A run is already in progress. Stop it first.")
			return
		self._flush_notifications_to_ini()
		try:
			cmd = self._build_base_argv()
		except RuntimeError as e:
			messagebox.showerror("AutoPrimeNet", str(e))
			return
		self._sync_hardware_to_ini(cmd, quiet=False)
		if extra_flag:
			cmd.append(extra_flag)

		try:
			self.notebook.select(self._tab_output)
		except tk.TclError:
			pass

		self._spawn_autoprimenet_subprocess(cmd)


def main():
	"""Entry point: verify autoprimenet is available, create the Tk root, and run the event loop."""
	try:
		_autoprimenet_command()
	except RuntimeError as e:
		print(str(e), file=sys.stderr)
		sys.exit(1)
	if _GUI_USE_TTKBOOTSTRAP:
		root = _GuiRoot(themename="cosmo")
	else:
		root = _GuiRoot()
	_apply_window_icon(root)
	app = AutoPrimeNetGUI(root)
	try:
		root.mainloop()
	except KeyboardInterrupt:
		pass


if __name__ == "__main__":
	main()
