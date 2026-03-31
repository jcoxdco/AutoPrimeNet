# -*- coding: utf-8 -*-
"""Tests for ``autoprimenet_gui`` (requires Tcl/Tk; skipped when tkinter is missing)."""

from __future__ import print_function, unicode_literals

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

try:
	import tkinter as tk
except ImportError:
	tk = None

TKINTER_AVAILABLE = tk is not None

if TKINTER_AVAILABLE:
	import autoprimenet_gui as apg
else:
	apg = None


@unittest.skipUnless(TKINTER_AVAILABLE, "tkinter not installed (e.g. apt install python3-tk)")
class TestPureHelpers(unittest.TestCase):
	"""Module-level helpers (no GUI instance)."""

	def test_supported_workprefs_covers_programs(self):
		for prog in apg.PROGRAM_KEYS:
			codes = apg.supported_workprefs(prog)
			self.assertTrue(codes)
			self.assertEqual(codes, sorted(set(codes)))
		# Unknown program falls through to mlucas-like set
		self.assertIn(apg._WP.PRP_FIRST, apg.supported_workprefs("unknown"))

	def test_workpref_is_trial_factoring(self):
		self.assertTrue(apg.workpref_is_trial_factoring(2))
		self.assertTrue(apg.workpref_is_trial_factoring(12))
		self.assertFalse(apg.workpref_is_trial_factoring(150))
		self.assertFalse(apg.workpref_is_trial_factoring("x"))
		self.assertFalse(apg.workpref_is_trial_factoring(None))

	def test_workpref_requires_gpu_picker(self):
		self.assertTrue(apg.workpref_requires_gpu_picker("mfaktc", 12))
		self.assertFalse(apg.workpref_requires_gpu_picker("mfaktc", 2))
		self.assertTrue(apg.workpref_requires_gpu_picker("gpuowl", 150))
		self.assertFalse(apg.workpref_requires_gpu_picker("mlucas", 150))
		self.assertFalse(apg.workpref_requires_gpu_picker("mlucas", "bad"))

	def test_default_workpref(self):
		self.assertEqual(apg.default_workpref("mfaktc"), apg._WP.GPU_FACTOR)
		self.assertEqual(apg.default_workpref("cudalucas"), apg._WP.LL_DBLCHK)
		self.assertEqual(apg.default_workpref("mlucas"), apg._WP.PRP_FIRST)

	def test_format_workpref_line_known_and_unknown(self):
		line = apg.format_workpref_line(apg._WP.PRP_FIRST)
		self.assertIn("150", line)
		self.assertIn("PRP", line)
		unknown = apg.format_workpref_line(99999)
		self.assertIn("99999", unknown)
		self.assertIn("Worktype", unknown)

	def test_repo_dir_not_frozen(self):
		d = apg._repo_dir()
		self.assertTrue(os.path.isdir(d))
		self.assertTrue(os.path.isfile(os.path.join(d, "autoprimenet_gui.py")))

	def test_autoprimenet_command_source_tree(self):
		cmd = apg._autoprimenet_command()
		self.assertGreaterEqual(len(cmd), 2)
		self.assertTrue(os.path.isfile(cmd[-1]) or cmd[-1].endswith("autoprimenet.py"))

	def test_gui_icon_paths_non_frozen(self):
		paths = apg._gui_icon_paths()
		self.assertIsInstance(paths, list)
		for p in paths:
			self.assertIn("favicon.ico", p)

	def test_register_exponent_constants(self):
		self.assertTrue(apg.REGISTER_EXPONENT_WORKTYPES)
		self.assertTrue(apg._REGISTER_EXPONENT_FIELDS)
		for visible in apg._REGISTER_EXPONENT_VISIBLE.values():
			self.assertIsInstance(visible, set)


@unittest.skipUnless(TKINTER_AVAILABLE, "tkinter not installed")
class TestApplyWindowIcon(unittest.TestCase):
	def test_apply_window_icon_no_crash(self):
		root = tk.Tk()
		root.withdraw()
		try:
			apg._apply_window_icon(root)
		finally:
			root.destroy()


@unittest.skipUnless(TKINTER_AVAILABLE, "tkinter not installed")
class TestAutoPrimeNetGUI(unittest.TestCase):
	"""``AutoPrimeNetGUI`` with a temp work directory and no real subprocess work."""

	def setUp(self):
		self._cwd = os.getcwd()
		self.tmp = tempfile.mkdtemp(prefix="apn_gui_test_")
		os.chdir(self.tmp)
		self.root = tk.Tk()
		self.root.withdraw()
		self.root.after = mock.MagicMock(return_value="mock_after_id")
		self.root.after_cancel = mock.MagicMock()
		self._patches = []
		for name in ("showwarning", "showerror", "showinfo", "askyesno"):
			p = mock.patch.object(apg.messagebox, name, mock.MagicMock())
			p.start()
			self._patches.append(p)
		self._sync_hw_patch = mock.patch.object(
			apg.AutoPrimeNetGUI,
			"_sync_hardware_to_ini",
			lambda self_, base_cmd, quiet=False: None,
		)
		self._sync_hw_patch.start()
		self._patches.append(self._sync_hw_patch)
		self.app = apg.AutoPrimeNetGUI(self.root)

	def tearDown(self):
		for p in reversed(self._patches):
			p.stop()
		try:
			self.root.destroy()
		except tk.TclError:
			pass
		os.chdir(self._cwd)
		try:
			os.rmdir(self.tmp)
		except OSError:
			shutil.rmtree(self.tmp, ignore_errors=True)

	def test_normalize_workdir_str(self):
		self.assertEqual(self.app._normalize_workdir_str(""), os.path.normpath(os.getcwd()))
		self.assertEqual(self.app._normalize_workdir_str(self.tmp), os.path.normpath(self.tmp))

	def test_resolve_workdir_from_var(self):
		wd, err = self.app._resolve_workdir_from_var()
		self.assertIsNone(err)
		self.assertEqual(os.path.normcase(wd), os.path.normcase(self.tmp))
		self.app.workdir.set(os.path.join(self.tmp, "nope_not_a_dir"))
		wd2, err2 = self.app._resolve_workdir_from_var()
		self.assertIsNone(wd2)
		self.assertIn("does not exist", err2)

	def test_read_write_ini_roundtrip(self):
		path = os.path.join(self.tmp, apg.LOCALFILE_DEFAULT)
		cp = self.app._read_ini_file(path)
		self.assertTrue(cp.has_section(apg.SEC_PRIMENET))
		cp.set(apg.SEC_PRIMENET, "username", "user_x")
		ok, msg = self.app._write_ini_file(path, cp)
		self.assertTrue(ok)
		self.assertIsNone(msg)
		cp2 = self.app._read_ini_file(path)
		self.assertEqual(cp2.get(apg.SEC_PRIMENET, "username"), "user_x")

	def test_write_ini_missing_parent_fails(self):
		bad = os.path.join(self.tmp, "missing", "prime.ini")
		cp = self.app._new_config_parser()
		cp.add_section(apg.SEC_PRIMENET)
		ok, msg = self.app._write_ini_file(bad, cp)
		self.assertFalse(ok)
		self.assertIn("does not exist", msg)

	def test_normalized_primeuserid_from_cp(self):
		cp = self.app._new_config_parser()
		cp.add_section(apg.SEC_PRIMENET)
		self.assertEqual(apg.AutoPrimeNetGUI._normalized_primeuserid_from_cp(cp), "ANONYMOUS")
		cp.set(apg.SEC_PRIMENET, "username", "  me  ")
		self.assertEqual(apg.AutoPrimeNetGUI._normalized_primeuserid_from_cp(cp), "me")
		cp.set(apg.SEC_PRIMENET, "username", "   ")
		self.assertEqual(apg.AutoPrimeNetGUI._normalized_primeuserid_from_cp(cp), "ANONYMOUS")

	def test_default_results_and_work_filename(self):
		self.assertEqual(apg.AutoPrimeNetGUI._default_results_filename("prpll"), "results-0.txt")
		self.assertEqual(apg.AutoPrimeNetGUI._default_results_filename("mfaktc"), "results.json.txt")
		self.assertEqual(apg.AutoPrimeNetGUI._default_work_filename("prpll"), "worktodo-0.txt")
		self.assertEqual(apg.AutoPrimeNetGUI._default_work_filename(""), "worktodo.txt")

	def test_split_to_emails_list(self):
		self.app.email_to.set("a@b.com; c@d.org , ")
		self.assertEqual(self.app._split_to_emails_list(), ["a@b.com", "c@d.org"])
		self.app.email_to.set("")
		self.assertEqual(self.app._split_to_emails_list(), [])

	def test_gimps_program_key(self):
		self.app.program.set("")
		self.assertIsNone(self.app._gimps_program_key())
		self.app.program.set("mlucas")
		self.assertEqual(self.app._gimps_program_key(), "mlucas")

	def test_get_workpref_code_and_build_argv(self):
		self.app.program.set("mlucas")
		self.app._refresh_workpref_choices()
		cmd = self.app._build_base_argv()
		self.assertIn("-m", cmd)
		self.assertIn("-T", cmd)
		self.assertIn("--no-cert-work", cmd)

	def test_build_base_argv_prpll_cert_and_report_flags(self):
		self.app.program.set("prpll")
		self.app._refresh_workpref_choices()
		self.app.cert_work_var.set(1)
		self.app.cert_cpu_limit.set("25")
		self.app.report_100m_var.set(0)
		cmd = self.app._build_base_argv()
		self.assertIn("--prpll", cmd)
		self.assertIn("--cert-work", cmd)
		self.assertIn("--cert-work-limit", cmd)
		self.assertIn("25", cmd)
		self.assertIn("--no-report-100m", cmd)

	def test_build_base_argv_mfak_tf1g_min_exp(self):
		self.app.program.set("mfaktc")
		self.app._refresh_workpref_choices()
		self.app.tf1g.set(1)
		cmd = self.app._build_base_argv()
		self.assertIn("--mfaktc", cmd)
		self.assertIn("--min-exp", cmd)
		self.assertEqual(cmd[cmd.index("--min-exp") + 1], str(apg.MAX_PRIMENET_EXP))

	def test_build_base_argv_email_flags(self):
		self.app.program.set("mlucas")
		self.app._refresh_workpref_choices()
		self.app.email_from.set("A <a@b.com>")
		self.app.email_smtp.set("smtp.example.com")
		self.app.email_to.set("c@d.org")
		self.app.email_tls.set(1)
		self.app.email_username.set("u")
		cmd = self.app._build_base_argv()
		self.assertIn("-f", cmd)
		self.assertIn("-S", cmd)
		self.assertIn("--to-email", cmd)
		self.assertIn("--tls", cmd)
		self.assertIn("-U", cmd)

	def test_build_base_argv_raises_without_program(self):
		self.app.program.set("")
		with self.assertRaises(RuntimeError) as ctx:
			self.app._build_base_argv()
		self.assertIn("GIMPS program", str(ctx.exception))

	def test_register_exponent_work_type_from_combo(self):
		self.assertEqual(self.app._register_exponent_work_type_from_combo("150 — First"), 150)
		self.assertEqual(self.app._register_exponent_work_type_from_combo("2 - Trial"), 2)
		self.assertEqual(self.app._register_exponent_work_type_from_combo("100"), 100)

	def test_register_exponent_build_spec_all_types(self):
		root = self.root

		def sv(v):
			s = tk.StringVar(master=root, value=v)
			return s

		p1 = tk.IntVar(master=root, value=1)
		fields = {k: sv("") for k, _ in apg._REGISTER_EXPONENT_FIELDS}

		spec = self.app._register_exponent_build_spec(100, "127", fields, p1)
		self.assertEqual(spec["work_type"], 100)
		self.assertEqual(spec["n"], 127)
		self.assertEqual(spec["pminus1ed"], 1)

		fields["sieve_depth"].set("88")
		p0 = tk.IntVar(master=root, value=0)
		spec101 = self.app._register_exponent_build_spec(101, "131", fields, p0)
		self.assertEqual(spec101["pminus1ed"], 0)

		fields["sieve_depth"].set("1")
		fields["factor_to"].set("2")
		spec2 = self.app._register_exponent_build_spec(2, "10007", fields, p0)
		self.assertEqual(spec2["factor_to"], 2.0)

		fields["B1"].set("10")
		fields["B2"].set("20")
		fields["sieve_depth"].set("3")
		fields["B2_start"].set("5")
		fields["known_factors"].set("3, 5")
		spec3 = self.app._register_exponent_build_spec(3, "31", fields, p0)
		self.assertEqual(spec3["known_factors"], [3, 5])

		fields["tests_saved"].set("1.5")
		spec4 = self.app._register_exponent_build_spec(4, "61", fields, p0)
		self.assertIn("tests_saved", spec4)

		fields["curves_to_do"].set("7")
		spec5 = self.app._register_exponent_build_spec(5, "127", fields, p0)
		self.assertEqual(spec5["curves_to_do"], 7)

		for k in ("sieve_depth", "tests_saved", "B1", "B2"):
			fields[k].set("1" if k != "sieve_depth" else "2.5")
		spec150 = self.app._register_exponent_build_spec(150, "8191", fields, p0)
		self.assertEqual(spec150["work_type"], 150)

		fields["prp_base"].set("5")
		fields["prp_residue_type"].set("2")
		spec151 = self.app._register_exponent_build_spec(151, "8191", fields, p0)
		self.assertEqual(spec151["prp_base"], 5)
		self.assertEqual(spec151["prp_residue_type"], 2)

	def test_register_exponent_build_spec_errors(self):
		root = self.root
		fields = {k: tk.StringVar(master=root, value="") for k, _ in apg._REGISTER_EXPONENT_FIELDS}
		p = tk.IntVar(master=root, value=0)
		with self.assertRaises(ValueError):
			self.app._register_exponent_build_spec(150, "", fields, p)
		fields["sieve_depth"].set("1")
		fields["tests_saved"].set("1")
		fields["B1"].set("1")
		fields["B2"].set("1")
		with self.assertRaises(ValueError):
			self.app._register_exponent_build_spec(99, "3", fields, p)

	def test_parse_last_json_object_from_text(self):
		self.assertIsNone(self.app._parse_last_json_object_from_text(None))
		self.assertIsNone(self.app._parse_last_json_object_from_text("no brace"))
		obj = self.app._parse_last_json_object_from_text('noise {"a": 1, "b": [2]}')
		self.assertEqual(obj, {"a": 1, "b": [2]})
		self.assertIsNone(self.app._parse_last_json_object_from_text("{not json"))
		if sys.version_info[0] >= 3:
			raw = b'xx {"c": 3}'
			obj_b = self.app._parse_last_json_object_from_text(raw)
			self.assertEqual(obj_b, {"c": 3})

	def test_gpu_display_line(self):
		line = self.app._gpu_display_line(1, {"name": "RTX", "source": "CUDA"})
		self.assertIn("RTX", line)
		self.assertIn("CUDA", line)

	def test_gpu_picker_should_show(self):
		self.app.program.set("gpuowl")
		self.app._refresh_workpref_choices()
		self.assertTrue(self.app._gpu_picker_should_show())

	def test_tf1g_controls_get_min_exponent_and_visibility(self):
		self.app.program.set("mfaktc")
		self.app.tf1g.set(1)
		self.assertTrue(self.app._tf1g_controls_get_min_exponent())
		self.app._update_get_exp_visibility()
		self.app.program.set("mlucas")
		self.app.tf1g.set(0)
		self.assertFalse(self.app._tf1g_controls_get_min_exponent())

	def test_append_log_and_poll_out(self):
		self.app.out_queue.put("line1\n")
		self.app._poll_out()
		content = self.app.log.get("1.0", "end")
		self.assertIn("line1", content)

	def test_ini_has_computer_guid_and_registration_prereqs(self):
		self.assertFalse(self.app._ini_has_computer_guid())
		path = os.path.join(self.tmp, apg.LOCALFILE_DEFAULT)
		cp = self.app._new_config_parser()
		cp.add_section(apg.SEC_PRIMENET)
		cp.set(apg.SEC_PRIMENET, "ComputerGUID", "abc")
		cp.set(apg.SEC_PRIMENET, "mlucas", "True")
		with io.open(path, "w", encoding="utf-8") as f:
			cp.write(f)
		self.app._load_ui_from_ini()
		self.assertTrue(self.app._ini_has_computer_guid())
		self.assertTrue(self.app._registration_prereqs_ok())

	def test_persist_gpu_device_choice_cpu_branch(self):
		path = os.path.join(self.tmp, apg.LOCALFILE_DEFAULT)
		cp = self.app._new_config_parser()
		cp.add_section(apg.SEC_PRIMENET)
		cp.set(apg.SEC_PRIMENET, "mlucas", "True")
		cp.set(apg.SEC_PRIMENET, "CpuBrand", "X")
		with io.open(path, "w", encoding="utf-8") as f:
			cp.write(f)
		self.app._load_ui_from_ini()
		self.app._persist_gpu_device_choice("cpu")
		cp2 = self.app._read_ini_file(path)
		self.assertEqual(cp2.get(apg.SEC_INTERNALS, apg.INI_GUI_GPU_INDEX), "cpu")

	def test_persist_gpu_device_choice_gpu_branch_short_name(self):
		self.app._gpu_devices_json = [{"name": "ab", "cores": 96, "frequency_mhz": "x", "memory_mib": None}]
		self.app._persist_gpu_device_choice("1")
		path = os.path.join(self.tmp, apg.LOCALFILE_DEFAULT)
		cp = self.app._read_ini_file(path)
		self.assertTrue(cp.has_option(apg.SEC_PRIMENET, "CpuBrand"))
		brand = cp.get(apg.SEC_PRIMENET, "CpuBrand")
		self.assertGreaterEqual(len(brand), 4)

	def test_flush_notifications_to_ini(self):
		self.app.program.set("mlucas")
		self.app._refresh_workpref_choices()
		self.app.report_100m_var.set(0)
		self.app.email_from.set("f@example.com")
		self.app.email_smtp.set("smtp.example.com")
		self.app.email_to.set("t@example.com")
		self.app._flush_notifications_to_ini()
		path = os.path.join(self.tmp, apg.LOCALFILE_DEFAULT)
		cp = self.app._read_ini_file(path)
		self.assertTrue(cp.has_option(apg.SEC_PRIMENET, "no_report_100m"))
		self.assertEqual(cp.get(apg.SEC_EMAIL, "from_email"), "f@example.com")

	def test_on_blur_work_file_validation(self):
		self.app.work_file.set(os.path.join("bad", "path.txt"))
		self.app._on_blur_work_file()
		apg.messagebox.showwarning.assert_called()

	def test_on_blur_worker_disk_space_invalid(self):
		self.app.worker_disk_space.set("-1")
		self.app._on_blur_worker_disk_space()
		apg.messagebox.showwarning.assert_called()

	def test_ensure_single_worker_ini(self):
		path = os.path.join(self.tmp, apg.LOCALFILE_DEFAULT)
		cp = self.app._new_config_parser()
		cp.add_section(apg.SEC_PRIMENET)
		cp.set(apg.SEC_PRIMENET, "NumWorkers", "4")
		cp.add_section("Worker #1")
		cp.set("Worker #1", "WorkPreference", "150")
		with io.open(path, "w", encoding="utf-8") as f:
			cp.write(f)
		self.app._ensure_single_worker_ini()
		cp2 = self.app._read_ini_file(path)
		self.assertEqual(cp2.get(apg.SEC_PRIMENET, "NumWorkers"), "1")
		self.assertEqual(cp2.get(apg.SEC_PRIMENET, "WorkPreference"), "150")

	def test_fetch_gpu_devices_via_cli_mocked(self):
		class R(object):
			returncode = 0
			stdout = '[{"name":"GPU","source":"test"}]'

		with mock.patch.object(apg.subprocess, "run", return_value=R()):
			devs, err = self.app._fetch_gpu_devices_via_cli()
		self.assertIsNone(err)
		self.assertEqual(len(devs), 1)

	def test_try_register_via_subprocess_mocked(self):
		class R(object):
			returncode = 0
			stdout = "ok"

		self.app.program.set("mlucas")
		self.app._refresh_workpref_choices()
		path = os.path.join(self.tmp, apg.LOCALFILE_DEFAULT)
		cp = self.app._new_config_parser()
		cp.add_section(apg.SEC_PRIMENET)
		cp.set(apg.SEC_PRIMENET, "mlucas", "True")
		with io.open(path, "w", encoding="utf-8") as f:
			cp.write(f)
		self.app._load_ui_from_ini()
		with mock.patch.object(apg.subprocess, "run", return_value=R()):
			self.app._try_register_via_subprocess("prereqs")
		apg.messagebox.showerror.assert_not_called()

	def test_load_work_todo_assignments_via_cli(self):
		self.app.program.set("mlucas")
		self.app._refresh_workpref_choices()

		def fake_run(cmd, **kwargs):
			class R(object):
				returncode = 0
				stdout = ""

			path = cmd[-1]
			with io.open(path, "w", encoding="utf-8") as f:
				f.write(
					json.dumps(
						[
							{"worker": 1, "exponent": 31, "assignment": "line"},
							{"worker": "x"},
						]
					)
				)
			return R()

		with mock.patch.object(apg.subprocess, "run", side_effect=fake_run):
			rows, err = self.app._load_work_todo_assignments_via_cli()
		self.assertIsNone(err)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["exponent"], 31)

	def test_load_work_todo_assignments_nonzero_exit(self):
		self.app.program.set("mlucas")
		self.app._refresh_workpref_choices()

		def fake_run(cmd, **kwargs):
			class R(object):
				returncode = 1
				stdout = "fail"

			return R()

		with mock.patch.object(apg.subprocess, "run", side_effect=fake_run):
			rows, err = self.app._load_work_todo_assignments_via_cli()
		self.assertIsNone(rows)
		self.assertIn("exit code", err)

	def test_sync_hardware_to_ini_timeout_branch(self):
		class Proc(object):
			returncode = -1

			def communicate(self, timeout=None):
				raise subprocess.TimeoutExpired(cmd=[], timeout=timeout)

			def kill(self):
				pass

		self._sync_hw_patch.stop()
		try:
			with mock.patch.object(apg.subprocess, "Popen", return_value=Proc()):
				self.app._sync_hardware_to_ini(["x", "y"], quiet=True)
		finally:
			self._sync_hw_patch.start()

	def test_on_test_email_blocks_when_proc_running(self):
		class P(object):
			def poll(self):
				return None

		self.app.proc = P()
		self.app._on_test_email()
		apg.messagebox.showwarning.assert_called()

	def test_on_blur_checkin_valid_persists(self):
		self.app.program.set("mlucas")
		self.app._refresh_workpref_choices()
		self.app.checkin.set("48")
		self.app._on_blur_checkin()
		path = os.path.join(self.tmp, apg.LOCALFILE_DEFAULT)
		cp = self.app._read_ini_file(path)
		self.assertEqual(cp.get(apg.SEC_PRIMENET, "HoursBetweenCheckins"), "48")

	def test_on_blur_checkin_invalid_warns(self):
		self.app.checkin.set("999")
		self.app._on_blur_checkin()
		apg.messagebox.showwarning.assert_called()

	def test_on_blur_days_work_invalid_warns(self):
		self.app.days_work.set("200")
		self.app._on_blur_days_work()
		apg.messagebox.showwarning.assert_called()

	def test_on_blur_max_exponents_valid_and_blank(self):
		self.app.program.set("mlucas")
		self.app._refresh_workpref_choices()
		self.app.max_exponents.set("5")
		self.app._on_blur_max_exponents()
		path = os.path.join(self.tmp, apg.LOCALFILE_DEFAULT)
		cp = self.app._read_ini_file(path)
		self.assertEqual(cp.get(apg.SEC_PRIMENET, "MaxExponents"), "5")
		self.app.max_exponents.set("")
		self.app._on_blur_max_exponents()
		cp2 = self.app._read_ini_file(path)
		self.assertFalse(cp2.has_option(apg.SEC_PRIMENET, "MaxExponents"))

	def test_on_blur_get_min_and_max_exponent(self):
		self.app.get_min_exp.set("1000")
		self.app._on_blur_get_min_exp()
		self.app.get_max_exp.set("2000")
		self.app._on_blur_get_max_exp()
		path = os.path.join(self.tmp, apg.LOCALFILE_DEFAULT)
		cp = self.app._read_ini_file(path)
		self.assertEqual(cp.get(apg.SEC_PRIMENET, "GetMinExponent"), "1000")
		self.assertEqual(cp.get(apg.SEC_PRIMENET, "GetMaxExponent"), "2000")

	def test_on_blur_min_max_bit_for_trial_factoring(self):
		self.app.program.set("mfaktc")
		self.app._refresh_workpref_choices()
		self.app.workpref_combo.set(apg.format_workpref_line(apg._WP.FACTOR))
		self.app.min_bit.set("65")
		self.app.max_bit.set("70")
		self.app._on_blur_min_bit()
		self.app._on_blur_max_bit()
		path = os.path.join(self.tmp, apg.LOCALFILE_DEFAULT)
		cp = self.app._read_ini_file(path)
		self.assertEqual(cp.get(apg.SEC_PRIMENET, "min_bit"), "65")
		self.assertEqual(cp.get(apg.SEC_PRIMENET, "max_bit"), "70")

	def test_program_change_updates_default_results_for_mfak(self):
		path = os.path.join(self.tmp, apg.LOCALFILE_DEFAULT)
		cp = self.app._new_config_parser()
		cp.add_section(apg.SEC_PRIMENET)
		cp.set(apg.SEC_PRIMENET, "mlucas", "True")
		with io.open(path, "w", encoding="utf-8") as f:
			cp.write(f)
		self.app._load_ui_from_ini()
		self.assertEqual(self.app.results_file.get(), "results.txt")
		self.app.program.set("mfaktc")
		self.assertEqual(self.app.results_file.get(), "results.json.txt")

	def test_refresh_workpref_choices_no_program_disables_combo(self):
		self.app.program.set("")
		self.app._refresh_workpref_choices()
		self.assertEqual(self.app.workpref_combo.get(), "")

	def test_on_workpref_selected_persists(self):
		self.app.program.set("mlucas")
		self.app._refresh_workpref_choices()
		self.app._on_workpref_selected()
		path = os.path.join(self.tmp, apg.LOCALFILE_DEFAULT)
		cp = self.app._read_ini_file(path)
		self.assertTrue(cp.has_option(apg.SEC_PRIMENET, "WorkPreference"))

	def test_run_action_spawns(self):
		self.app.program.set("mlucas")
		self.app._refresh_workpref_choices()
		with mock.patch.object(self.app, "_spawn_autoprimenet_subprocess") as sp:
			self.app._run_action("-s")
		sp.assert_called_once()
		args, _kw = sp.call_args
		self.assertIn("-s", args[0])

	def test_spawn_autoprimenet_subprocess_oserror_shows_message(self):
		self.app.program.set("mlucas")
		self.app._refresh_workpref_choices()
		cmd = self.app._build_base_argv()
		with mock.patch.object(apg.subprocess, "Popen", side_effect=OSError("no such program")):
			self.app._spawn_autoprimenet_subprocess(cmd)
		apg.messagebox.showerror.assert_called()
		self.assertIsNone(self.app.proc)

	def test_persist_ini_updates_respects_suppress(self):
		self.app._suppress_ini_write = True
		self.app._persist_ini_updates({apg.SEC_PRIMENET: {"username": "ghost"}})
		path = os.path.join(self.tmp, apg.LOCALFILE_DEFAULT)
		if os.path.isfile(path):
			cp = self.app._read_ini_file(path)
			if cp.has_option(apg.SEC_PRIMENET, "username"):
				self.assertNotEqual(cp.get(apg.SEC_PRIMENET, "username"), "ghost")

	def test_schedule_register_attempt_cancels_previous(self):
		self.app._register_after_id = "old"
		self.app._schedule_register_attempt("prereqs")
		self.root.after_cancel.assert_called_with("old")

	def test_fetch_gpu_devices_via_cli_bad_json(self):
		class R(object):
			returncode = 0
			stdout = "not-json"

		with mock.patch.object(apg.subprocess, "run", return_value=R()):
			devs, err = self.app._fetch_gpu_devices_via_cli()
		self.assertIsNone(devs)
		self.assertIsNotNone(err)

	def test_fetch_gpu_devices_nonzero_rc(self):
		class R(object):
			returncode = 2
			stdout = ""

		with mock.patch.object(apg.subprocess, "run", return_value=R()):
			devs, err = self.app._fetch_gpu_devices_via_cli()
		self.assertIsNone(devs)

	def test_sync_hardware_to_ini_failure_logged_when_not_quiet(self):
		class Proc(object):
			returncode = 1

			def communicate(self, timeout=None):
				return "stderr-ish", None

		self._sync_hw_patch.stop()
		try:
			with mock.patch.object(apg.subprocess, "Popen", return_value=Proc()):
				self.app._sync_hardware_to_ini(["a", "b"], quiet=False)
		finally:
			self._sync_hw_patch.start()
		content = self.app.log.get("1.0", "end")
		self.assertIn("sync-ini", content.lower())

	def test_deferred_sync_ini_skips_when_proc_active(self):
		class P(object):
			def poll(self):
				return None

		self.app.proc = P()
		with mock.patch.object(self.app, "_sync_hardware_to_ini") as s:
			self.app._deferred_sync_ini()
		s.assert_not_called()

	def test_on_force_target_bits_toggle_persists_when_tf_workpref(self):
		self.app.program.set("mfaktc")
		self.app._refresh_workpref_choices()
		self.app.workpref_combo.set(apg.format_workpref_line(apg._WP.FACTOR))
		self.app.force_target_bits.set(1)
		self.app._on_force_target_bits_toggle()
		path = os.path.join(self.tmp, apg.LOCALFILE_DEFAULT)
		cp = self.app._read_ini_file(path)
		self.assertEqual(cp.get(apg.SEC_PRIMENET, "force_target_bits"), "True")

	def test_register_exponent_work_type_unicode_dash(self):
		line = "151\u2014Double-check PRP"
		self.assertEqual(self.app._register_exponent_work_type_from_combo(line), 151)


@unittest.skipUnless(TKINTER_AVAILABLE, "tkinter not installed")
class TestMain(unittest.TestCase):
	def test_main_exits_when_autoprimenet_missing(self):
		with mock.patch.object(apg, "_autoprimenet_command", side_effect=RuntimeError("no exe")), mock.patch.object(
			apg.sys, "stderr", new=io.StringIO()
		):
			with self.assertRaises(SystemExit) as cm:
				apg.main()
		self.assertEqual(cm.exception.code, 1)
