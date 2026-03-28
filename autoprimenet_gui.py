#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Optional Tk launcher for AutoPrimeNet: builds CLI arguments and runs autoprimenet in a subprocess."""

from __future__ import division, print_function, unicode_literals

import io
import os
import queue
import shlex
import subprocess
import sys
import threading

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
INI_GUI_WORKDIR = "GUIWorkdir"
INI_GUI_TIMEOUT = "GuiTimeoutSeconds"
INI_GUI_EXTRA = "GuiExtraArgs"

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


def _apply_window_icon(root):
	"""Set the window icon to favicon.ico where supported (e.g. Windows title bar)."""
	for path in _gui_icon_paths():
		path = os.path.normpath(os.path.abspath(path))
		if not os.path.isfile(path):
			continue
		try:
			root.iconbitmap(path)
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


def _split_extra_args(text):
	if not text or not str(text).strip():
		return []
	s = str(text).strip()
	if os.name == "nt":
		try:
			return shlex.split(s, posix=False)
		except TypeError:
			return shlex.split(s)
	return shlex.split(s)


class AutoPrimeNetGUI(object):
	def __init__(self, root):
		self.root = root
		self.root.title("AutoPrimeNet")
		self.proc = None
		self.reader_thread = None
		self.out_queue = queue.Queue()
		self._poll_out()

		main = ttk.Frame(root, padding=8)
		main.grid(row=0, column=0, sticky="nsew")
		root.columnconfigure(0, weight=1)
		root.rowconfigure(0, weight=1)
		main.columnconfigure(1, weight=1)

		r = 0
		ttk.Label(main, text="Work directory").grid(row=r, column=0, sticky="w")
		self.workdir = tk.StringVar(value=os.getcwd())
		entry_wd = ttk.Entry(main, textvariable=self.workdir, width=48)
		entry_wd.grid(row=r, column=1, sticky="ew", padx=(4, 4))
		ttk.Button(main, text="Browse…", command=self._browse_workdir).grid(row=r, column=2, sticky="e")
		r += 1

		ttk.Label(main, text="PrimeNet user ID").grid(row=r, column=0, sticky="w")
		self.user_id = tk.StringVar(value="ANONYMOUS")
		entry_user = ttk.Entry(main, textvariable=self.user_id, width=24)
		entry_user.grid(row=r, column=1, sticky="w", padx=(4, 0))
		r += 1

		ttk.Label(main, text="GIMPS program").grid(row=r, column=0, sticky="nw")
		prog = ttk.Frame(main)
		prog.grid(row=r, column=1, columnspan=2, sticky="w", padx=(4, 0))
		self.program = tk.StringVar(value="mlucas")
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

		ttk.Label(main, text="Workers").grid(row=r, column=0, sticky="w")
		self.num_workers = tk.StringVar(value="1")
		entry_workers = ttk.Entry(main, textvariable=self.num_workers, width=8)
		entry_workers.grid(row=r, column=1, sticky="w", padx=(4, 0))
		r += 1

		ttk.Label(main, text="Timeout (seconds)").grid(row=r, column=0, sticky="w")
		self.timeout = tk.StringVar(value="3600")
		entry_timeout = ttk.Entry(main, textvariable=self.timeout, width=12)
		entry_timeout.grid(row=r, column=1, sticky="w", padx=(4, 0))
		ttk.Label(main, text="0 = run once and exit").grid(row=r, column=2, sticky="w")
		r += 1

		ttk.Label(main, text="Check-in (hours)").grid(row=r, column=0, sticky="w")
		self.checkin = tk.StringVar(value="1")
		entry_checkin = ttk.Entry(main, textvariable=self.checkin, width=8)
		entry_checkin.grid(row=r, column=1, sticky="w", padx=(4, 0))
		r += 1

		ttk.Label(main, text="Days of work").grid(row=r, column=0, sticky="w")
		self.days_work = tk.StringVar(value="3")
		entry_days_work = ttk.Entry(main, textvariable=self.days_work, width=8)
		entry_days_work.grid(row=r, column=1, sticky="w", padx=(4, 0))
		ttk.Label(main, text="0–180 (PrimeNet queue horizon)").grid(row=r, column=2, sticky="w")
		r += 1

		ttk.Label(main, text="Extra arguments").grid(row=r, column=0, sticky="nw")
		self.extra = scrolledtext.ScrolledText(main, width=60, height=4, wrap="word")
		self.extra.grid(row=r, column=1, columnspan=2, sticky="ew", padx=(4, 0))
		ttk.Label(
			main,
			text="Optional: same as command line (e.g. -d or --dir path). On Windows use shlex rules or one token per line.",
			font=("TkDefaultFont", 8),
		).grid(row=r + 1, column=1, columnspan=2, sticky="w", padx=(4, 0))
		r += 2

		btn_row = ttk.Frame(main)
		btn_row.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(8, 4))
		ttk.Button(btn_row, text="Run", command=lambda: self._run_action(None)).pack(side="left", padx=(0, 4))
		ttk.Button(btn_row, text="Setup", command=lambda: self._run_action("--setup")).pack(side="left", padx=(0, 4))
		ttk.Button(btn_row, text="Status", command=lambda: self._run_action("-s")).pack(side="left", padx=(0, 4))
		ttk.Button(btn_row, text="Debug info", command=lambda: self._run_action("--debug-info")).pack(side="left", padx=(0, 4))
		ttk.Button(btn_row, text="Ping", command=lambda: self._run_action("--ping")).pack(side="left", padx=(0, 4))
		ttk.Button(btn_row, text="Stop", command=self._stop).pack(side="left", padx=(16, 0))
		r += 1

		ttk.Label(main, text="Output").grid(row=r, column=0, sticky="nw")
		self.log = scrolledtext.ScrolledText(main, width=80, height=18, wrap="word", state="disabled")
		self.log.grid(row=r, column=1, columnspan=2, sticky="nsew", padx=(4, 0), pady=(4, 0))
		main.rowconfigure(r, weight=1)

		self._suppress_ini_write = False

		self._load_ui_from_ini()
		self._bind_ini_persistence(
			entry_wd, entry_user, entry_workers, entry_timeout, entry_checkin, entry_days_work
		)

	def _bind_ini_persistence(
		self, entry_wd, entry_user, entry_workers, entry_timeout, entry_checkin, entry_days_work
	):
		entry_wd.bind("<FocusOut>", lambda e: self._on_blur_workdir(), add="+")
		entry_user.bind("<FocusOut>", lambda e: self._on_blur_username(), add="+")
		entry_workers.bind("<FocusOut>", lambda e: self._on_blur_workers(), add="+")
		entry_timeout.bind("<FocusOut>", lambda e: self._on_blur_timeout(), add="+")
		entry_checkin.bind("<FocusOut>", lambda e: self._on_blur_checkin(), add="+")
		entry_days_work.bind("<FocusOut>", lambda e: self._on_blur_days_work(), add="+")
		if hasattr(self.program, "trace_add"):
			self.program.trace_add("write", lambda *_: self._on_program_change())
		else:
			self.program.trace("w", lambda *_: self._on_program_change())
		self.extra.bind("<FocusOut>", lambda e: self._on_blur_extra(), add="+")

	def _new_config_parser(self):
		cp = ConfigParser()
		cp.optionxform = lambda option: option
		return cp

	def _read_ini_file(self, path):
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
		s = (s or "").strip()
		if not s:
			return os.path.normpath(os.getcwd())
		return os.path.normpath(os.path.expanduser(s))

	def _resolve_workdir_from_var(self):
		wd = self._normalize_workdir_str(self.workdir.get())
		if not os.path.isdir(wd):
			return None, "Work directory does not exist:\n{}".format(wd)
		return wd, None

	def _persist_ini_updates(self, updates_by_section):
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

	def _program_ini_bools(self, chosen):
		chosen = (chosen or "mlucas").strip()
		if chosen not in PROGRAM_KEYS:
			chosen = "mlucas"
		return {k: str(k == chosen) for k in PROGRAM_KEYS}

	def _load_ui_from_ini(self):
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
			if cp.has_option(SEC_PRIMENET, "NumWorkers"):
				self.num_workers.set(cp.get(SEC_PRIMENET, "NumWorkers"))
			if cp.has_option(SEC_PRIMENET, "HoursBetweenCheckins"):
				self.checkin.set(cp.get(SEC_PRIMENET, "HoursBetweenCheckins"))
			if cp.has_option(SEC_PRIMENET, "DaysOfWork"):
				self.days_work.set(cp.get(SEC_PRIMENET, "DaysOfWork"))
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
			if cp.has_option(SEC_INTERNALS, INI_GUI_TIMEOUT):
				self.timeout.set(cp.get(SEC_INTERNALS, INI_GUI_TIMEOUT))
			if cp.has_option(SEC_INTERNALS, INI_GUI_EXTRA):
				self.extra.delete("1.0", "end")
				self.extra.insert("1.0", cp.get(SEC_INTERNALS, INI_GUI_EXTRA))
		finally:
			self._suppress_ini_write = False

	def _on_blur_workdir(self):
		if self._suppress_ini_write:
			return
		wd, err = self._resolve_workdir_from_var()
		if err:
			messagebox.showwarning("AutoPrimeNet", err)
			return
		wd_abs = os.path.normpath(os.path.abspath(wd))
		self._persist_ini_updates({SEC_INTERNALS: {INI_GUI_WORKDIR: wd_abs}})

	def _on_blur_username(self):
		self._persist_ini_updates({SEC_PRIMENET: {"username": self.user_id.get().strip() or "ANONYMOUS"}})

	def _on_blur_workers(self):
		try:
			n = int(self.num_workers.get().strip() or "1")
			if n < 1:
				raise ValueError
		except ValueError:
			messagebox.showwarning("AutoPrimeNet", "Workers must be a positive integer.")
			return
		self._persist_ini_updates({SEC_PRIMENET: {"NumWorkers": str(n)}})

	def _on_blur_timeout(self):
		try:
			t = int(self.timeout.get().strip() or "0")
			if t < 0:
				raise ValueError
		except ValueError:
			messagebox.showwarning("AutoPrimeNet", "Timeout must be a non-negative integer (seconds).")
			return
		self._persist_ini_updates({SEC_INTERNALS: {INI_GUI_TIMEOUT: str(t)}})

	def _on_blur_checkin(self):
		try:
			ch = int(self.checkin.get().strip() or "1")
			if not 1 <= ch <= 168:
				raise ValueError
		except ValueError:
			messagebox.showwarning("AutoPrimeNet", "Check-in hours must be between 1 and 168.")
			return
		self._persist_ini_updates({SEC_PRIMENET: {"HoursBetweenCheckins": str(ch)}})

	def _on_blur_days_work(self):
		try:
			d = float(self.days_work.get().strip())
			if not 0.0 <= d <= 180.0:
				raise ValueError
		except ValueError:
			messagebox.showwarning("AutoPrimeNet", "Days of work must be a number from 0 through 180.")
			return
		self._persist_ini_updates({SEC_PRIMENET: {"DaysOfWork": str(d)}})

	def _on_program_change(self):
		if self._suppress_ini_write:
			return
		updates = dict(self._program_ini_bools(self.program.get()))
		self._persist_ini_updates({SEC_PRIMENET: updates})

	def _on_blur_extra(self):
		text = self.extra.get("1.0", "end-1c")
		self._persist_ini_updates({SEC_INTERNALS: {INI_GUI_EXTRA: text}})

	def _browse_workdir(self):
		path = filedialog.askdirectory(initialdir=self.workdir.get() or os.getcwd())
		if path:
			self.workdir.set(path)
			self._on_blur_workdir()
			self._load_ui_from_ini()

	def _append_log(self, text):
		self.log.configure(state="normal")
		self.log.insert("end", text)
		self.log.see("end")
		self.log.configure(state="disabled")

	def _poll_out(self):
		try:
			while True:
				line = self.out_queue.get_nowait()
				self._append_log(line)
		except queue.Empty:
			pass
		self.root.after(100, self._poll_out)

	def _build_base_argv(self):
		cmd = list(_autoprimenet_command())
		wd = self.workdir.get().strip()
		if wd:
			cmd.extend(["-w", wd])
		uid = self.user_id.get().strip()
		if uid:
			cmd.extend(["-u", uid])
		try:
			nw = int(self.num_workers.get().strip() or "1")
			if nw >= 1:
				cmd.extend(["--num-workers", str(nw)])
		except ValueError:
			pass
		try:
			t = int(self.timeout.get().strip() or "3600")
			if t >= 0:
				cmd.extend(["-t", str(t)])
		except ValueError:
			cmd.extend(["-t", "3600"])
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
		}.get(self.program.get(), "-m")
		cmd.append(flag)

		extra = self.extra.get("1.0", "end")
		cmd.extend(_split_extra_args(extra))
		return cmd

	def _run_action(self, extra_flag):
		if self.proc is not None and self.proc.poll() is None:
			messagebox.showwarning("AutoPrimeNet", "A run is already in progress. Stop it first.")
			return
		try:
			cmd = self._build_base_argv()
		except RuntimeError as e:
			messagebox.showerror("AutoPrimeNet", str(e))
			return
		if extra_flag:
			cmd.append(extra_flag)

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

	def _stop(self):
		if self.proc is None or self.proc.poll() is not None:
			return
		try:
			self.proc.terminate()
		except OSError:
			pass


def main():
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
