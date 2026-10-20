"""CSL 套件路径与插件加载失败的回归测试。"""

import importlib.util
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

import cslmatch


class PackagePathsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def write(self, name, text="I\n800\nOBJ\n"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_standard_bluebell_alias_resolves_to_manifest_directory(self):
        obj = self.write("BB_Airbus/A306/A306_AAW.obj")
        self.write("BB_Airbus/xsb_aircraft.txt",
                   "EXPORT_NAME __Bluebell_Airbus\n"
                   "OBJ8_AIRCRAFT A306_AAW\n"
                   "OBJ8 SOLID YES __Bluebell_Airbus/A306/A306_AAW.obj\n"
                   "VERT_OFFSET 4.6\nAIRLINE A306 AAW\n")
        models = cslmatch.parse_package(str(obj.parent.parent))
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0].path, str(obj))
        self.assertEqual(models[0].vert_offset, 4.6)
        self.assertEqual((models[0].icao, models[0].airline), ("A306", "AAW"))

    def test_cross_package_reference_resolves_even_when_target_is_scanned_last(self):
        obj = self.write("z_target/A306/a.obj")
        self.write("z_target/xsb_aircraft.txt", "EXPORT_NAME Shared\n")
        self.write("a_source/xsb_aircraft.txt",
                   "EXPORT_NAME Local\nOBJ8_AIRCRAFT A\n"
                   "OBJ8 SOLID YES Shared/A306/a.obj\nICAO A306\n")
        with mock.patch.object(cslmatch, "find_packages", return_value=[
                str(self.root / "a_source"), str(self.root / "z_target")]):
            models = cslmatch.ModelSet.load(str(self.root))
        self.assertEqual(len(models), 1)
        self.assertEqual(models.match("A306")[0].path, str(obj))

    def test_multiple_export_aliases_reference_the_same_directory(self):
        obj = self.write("package/a.obj")
        self.write("package/xsb_aircraft.txt",
                   "EXPORT_NAME First\nEXPORT_NAME Second\n"
                   "OBJ8_AIRCRAFT A\nOBJ8 SOLID YES First/a.obj\nICAO A320\n"
                   "OBJ8_AIRCRAFT B\nOBJ8 SOLID YES Second/a.obj\nICAO B738\n")
        models = cslmatch.parse_package(str(obj.parent))
        self.assertEqual([m.path for m in models], [str(obj), str(obj)])

    def test_relative_and_windows_paths_with_spaces_remain_supported(self):
        obj = self.write("package/A 320/model CCA.obj")
        for relative in ("A 320/model CCA.obj", "A 320\\model CCA.obj"):
            with self.subTest(relative=relative):
                self.write("package/xsb_aircraft.txt",
                           "OBJ8_AIRCRAFT A\nOBJ8 SOLID YES " + relative +
                           "\nICAO A320\n")
                models = cslmatch.parse_package(str(obj.parent.parent))
                self.assertEqual([m.path for m in models], [str(obj)])

    def test_windows_alias_separator_resolves(self):
        obj = self.write("package/A320/a.obj")
        self.write("package/xsb_aircraft.txt",
                   "EXPORT_NAME Alias\nOBJ8_AIRCRAFT A\n"
                   "OBJ8 SOLID YES Alias\\A320\\a.obj\nICAO A320\n")
        self.assertEqual(cslmatch.parse_package(str(obj.parent.parent))[0].path,
                         str(obj))

    def test_alias_and_obj_names_preserve_repeated_spaces(self):
        obj = self.write("package/A  320/model  CCA.obj")
        self.write("package/xsb_aircraft.txt",
                   "EXPORT_NAME Shared  Models\nOBJ8_AIRCRAFT A\n"
                   "OBJ8 SOLID YES Shared  Models/A  320/model  CCA.obj\nICAO A320\n")
        models = cslmatch.parse_package(str(obj.parent.parent))
        self.assertEqual([m.path for m in models], [str(obj)])

    def test_missing_obj_is_diagnosed_and_matching_uses_valid_fallback(self):
        obj = self.write("package/good.obj")
        self.write("package/xsb_aircraft.txt",
                   "OBJ8_AIRCRAFT Missing\nOBJ8 SOLID YES absent.obj\nICAO B738\n"
                   "OBJ8_AIRCRAFT Good\nOBJ8 SOLID YES good.obj\nICAO A320\n")
        with self.assertLogs("cslmatch", level="WARNING") as logs:
            models = cslmatch.ModelSet.load(str(self.root))
        self.assertEqual(len(models), 1)
        self.assertEqual(models.match("B738")[0].path, str(obj))
        self.assertIn("absent.obj", " ".join(logs.output))

    def test_no_existing_objs_means_no_available_model(self):
        self.write("package/xsb_aircraft.txt",
                   "OBJ8_AIRCRAFT Missing\nOBJ8 SOLID YES absent.obj\nICAO B738\n")
        with self.assertLogs("cslmatch", level="WARNING"):
            models = cslmatch.ModelSet.load(str(self.root))
        self.assertEqual(len(models), 0)
        self.assertIsNone(models.match("B738")[0])


class PluginLoadRetryTest(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).parent / "plugin" / "PI_XpcTraffic.py"
        spec = importlib.util.spec_from_file_location("pi_xpc_retry", path)
        self.plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.plugin)
        self.now = 0.0
        self.loads = []
        self.logs = []
        self.unloaded = []
        self.destroyed = []
        self.plugin.xp = types.SimpleNamespace(
            getElapsedTime=lambda: self.now,
            getSystemPath=lambda: "",
            loadObjectAsync=lambda *args: self.loads.append(args),
            createInstance=lambda ref, datarefs: "instance:" + ref,
            unloadObject=self.unloaded.append,
            destroyInstance=self.destroyed.append,
            log=self.logs.append)
        self.interface = self.plugin.PythonInterface.__new__(self.plugin.PythonInterface)
        self.aircraft = self.plugin.RenderedAircraft("TST1")
        self.interface.aircraft = {"TST1": self.aircraft}

    def request(self, path="a.obj"):
        self.interface._ensure_model(self.aircraft, path)

    def callback(self, ref, index=-1):
        _, callback, refcon = self.loads[index]
        callback(ref, refcon)

    def test_null_callback_retries_after_cooldown_and_creates_instance(self):
        self.request()
        self.callback(None)
        for _ in range(20):
            self.request()
        self.assertEqual(len(self.loads), 1)
        self.now = 60.0
        self.request()
        self.assertEqual(len(self.loads), 2)
        self.callback("object")
        self.assertEqual(self.aircraft.instance, "instance:object")
        self.assertTrue(any("a.obj" in line for line in self.logs))

    def test_pending_and_successful_loads_are_not_duplicated(self):
        self.request()
        self.now = 60.0
        self.request()
        self.assertEqual(len(self.loads), 1)
        self.callback("object")
        self.now = 120.0
        self.request()
        self.assertEqual(len(self.loads), 1)

    def test_synchronous_loader_exception_is_diagnosed_and_retried(self):
        def fail(*args):
            raise RuntimeError("load refused")
        self.plugin.xp.loadObjectAsync = fail
        try:
            self.request()
        except RuntimeError as error:
            self.fail("loader exception escaped: " + str(error))
        self.assertFalse(self.aircraft.loading)
        self.plugin.xp.loadObjectAsync = lambda *args: self.loads.append(args)
        self.request()
        self.assertEqual(self.loads, [])
        self.now = 60.0
        self.request()
        self.callback("object")
        self.assertEqual(self.aircraft.instance, "instance:object")
        self.assertTrue(any("load refused" in line for line in self.logs))

    def test_stale_failure_does_not_clear_new_pending_load(self):
        self.request("a.obj")
        self.request("b.obj")
        self.callback(None, 0)
        self.assertTrue(self.aircraft.loading)
        self.now = 60.0
        self.request("b.obj")
        self.assertEqual(len(self.loads), 2)
        self.callback("new", 1)
        self.assertEqual(self.aircraft.instance, "instance:new")

    def test_stale_success_is_unloaded_without_replacing_current_instance(self):
        self.request("a.obj")
        self.request("b.obj")
        self.callback("new", 1)
        self.callback("old", 0)
        self.assertEqual(self.aircraft.instance, "instance:new")
        self.assertEqual(self.unloaded, ["old"])

    def test_new_model_bypasses_previous_failure_cooldown(self):
        self.request("a.obj")
        self.callback(None)
        self.request("b.obj")
        self.callback("new")
        self.assertEqual(self.aircraft.instance, "instance:new")

    def test_instance_creation_failures_unload_and_retry_after_cooldown(self):
        for result in (None, RuntimeError("instance refused")):
            with self.subTest(result=result):
                self.loads.clear()
                self.unloaded.clear()
                self.now = 0.0
                self.aircraft = self.plugin.RenderedAircraft("TST1")
                self.interface.aircraft = {"TST1": self.aircraft}
                self.plugin.xp.createInstance = mock.Mock(side_effect=[result])
                self.request()
                try:
                    self.callback("object")
                except RuntimeError as error:
                    self.fail("instance exception escaped: " + str(error))
                self.assertFalse(self.aircraft.loading)
                self.assertEqual(self.unloaded, ["object"])
                self.request()
                self.assertEqual(len(self.loads), 1)
                self.now = 60.0
                self.plugin.xp.createInstance = lambda ref, data: "instance:" + ref
                self.request()
                self.callback("retry")
                self.assertEqual(self.aircraft.instance, "instance:retry")

    def test_removed_aircraft_callbacks_cannot_affect_recreated_callsign(self):
        for stale_ref in ("old", None):
            with self.subTest(stale_ref=stale_ref):
                self.loads.clear()
                self.unloaded.clear()
                self.aircraft = self.plugin.RenderedAircraft("TST1")
                self.interface.aircraft = {"TST1": self.aircraft}
                self.request("old.obj")
                self.interface.aircraft.pop("TST1").destroy()
                self.aircraft = self.plugin.RenderedAircraft("TST1")
                self.interface.aircraft["TST1"] = self.aircraft
                self.request("new.obj")
                self.callback(stale_ref, 0)
                self.assertTrue(self.aircraft.loading)
                self.assertIsNone(self.aircraft.object_ref)
                self.assertIsNone(self.aircraft.instance)
                self.assertEqual(self.unloaded, ["old"] if stale_ref else [])
                self.now = 60.0
                self.request("new.obj")
                self.assertEqual(len(self.loads), 2)
                self.callback("new", 1)
                self.assertEqual(self.aircraft.instance, "instance:new")


if __name__ == "__main__":
    unittest.main()
