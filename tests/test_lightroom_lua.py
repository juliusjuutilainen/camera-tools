"""Run real Lua 5.1 and the Python helper with a simulated Lightroom host.

Install the optional test runtime with `uv run --with lupa ...`. These tests
exercise the host boundary, not native Lightroom rendering or a real catalog.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

try:
    from lupa.lua51 import LuaRuntime
except ImportError:
    LuaRuntime = None


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "lightroom" / "CameraTools.lrplugin"

SDK = r"""
local modules, tasks = {}, {}
prefs = { dateBasis = 'modified', includeVideos = true }
catalog = { path = '/test/Photos.lrcat', entries = {}, calls = {}, busy = false }
function catalog:getPath() return self.path end
function catalog:findPhotoByPath(path) return self.entries[path] end
function catalog:addPhoto(path)
    assert(self.writeAccess, 'Missing SDK catalog write gate')
    if self.failPath == path then error('Unsupported media') end
    local photo = { path = path }
    self.entries[path] = photo
    self.calls[#self.calls + 1] = path
    return photo
end
function catalog:withWriteAccessDo(name, callback, timeout)
    assert(timeout.timeout > 0)
    if self.busy then return 'aborted' end
    local before = {}
    for key, value in pairs(self.entries) do before[key] = value end
    self.writeAccess = true
    local ok, message = pcall(callback)
    self.writeAccess = false
    if not ok or self.failCommit then
        self.entries = before
        error(message or 'Commit failed')
    end
    return 'executed'
end

sdk = {}
sdk.LrApplication = { activeCatalog = function() return catalog end }
sdk.LrFileUtils = {
    exists = py_exists, delete = py_delete,
    createAllDirectories = py_mkdir,
    chooseUniqueFileName = py_unique,
}
sdk.LrPathUtils = {
    child = function(parent, child) return parent .. '/' .. child end,
    isAbsolute = function(path) return path:sub(1, 1) == '/' end,
    leafName = function(path) return path:match('([^/]+)$') or path end,
    getStandardFilePath = function(kind) return py_standard(kind) end,
}
sdk.LrTasks = {
    pcall = pcall, execute = py_execute, yield = function() end,
    startAsyncTask = function(fn) tasks[#tasks + 1] = fn end,
    sleep = function()
        local task = table.remove(tasks, 1)
        assert(task, 'No worker task was scheduled')
        task()
    end,
}
function flushTasks()
    while #tasks > 0 do sdk.LrTasks.sleep() end
end
sdk.LrFunctionContext = {
    callWithContext = function(name, fn)
        local cleanup = {}
        local context = { addCleanupHandler = function(self, handler) cleanup[#cleanup + 1] = handler end }
        local ok, message = pcall(fn, context)
        for index = #cleanup, 1, -1 do cleanup[index]() end
        if not ok then error(message) end
    end,
}
cancelStage = nil
cancelCatalogAfter = nil
sdk.LrDialogs = {
    message = py_message,
    attachErrorDialogToFunctionContext = function() end,
    presentModalDialog = py_dialog,
    runOpenPanel = function() return nil end,
    showModalProgressDialog = function(args)
        local completed = 0
        return {
            setIndeterminate = function() end,
            setCaption = function() end,
            setPortionComplete = function(self, amount) completed = amount end,
            done = function() end,
            isCanceled = function()
                return cancelStage == args.title or
                    (args.title == 'Adding photos to Lightroom' and cancelCatalogAfter and completed >= cancelCatalogAfter)
            end,
        }
    end,
}
sdk.LrBinding = { makePropertyTable = function() return {} end }
sdk.LrPrefs = { prefsForPlugin = function() return prefs end }
sdk.LrShell = { revealInShell = function() end }
local factory = setmetatable({}, {
    __index = function(self, name)
        if name == 'control_spacing' or name == 'dialog_spacing' then return function() return 8 end end
        return function(self, args) args.kind = name; return args end
    end,
})
sdk.LrView = { bind = function(key) return { binding = key } end, osFactory = function() return factory end }
function import(name)
    assert(sdk[name], 'Unmocked SDK: ' .. name)
    return sdk[name]
end
function require(name)
    if not modules[name] then modules[name] = assert(loadstring(py_module(name)))() end
    return modules[name]
end
_PLUGIN = { path = py_plugin }
WIN_ENV = false
"""


@unittest.skipIf(LuaRuntime is None, "Run with uv run --with lupa to test Lua 5.1")
class LightroomLuaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "card's ää $(touch SHOULD_NOT_EXIST)"
        self.source.mkdir()
        self.destination = self.root / "library's 📷"
        self.app_data = self.root / "app data"
        self.app_data.mkdir()
        self.plugin = self.root / "plugin's $HOME" / "CameraTools.lrplugin"
        executable = self.plugin / "bin/camera-tools-helper/camera-tools-helper"
        executable.parent.mkdir(parents=True)
        executable.write_text(
            f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(ROOT)!r})\n"
            "from camera_tools.lightroom import main\nraise SystemExit(main())\n"
        )
        executable.chmod(0o755)
        self.dialogs = []
        self.messages = []
        self.preview_choice = "ok"
        self.settings_choice = "ok"
        self.on_preview = None
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        globals_ = self.lua.globals()
        globals_.py_exists = lambda p: "directory" if Path(p).is_dir() else "file" if Path(p).is_file() else None
        globals_.py_delete = self.delete
        globals_.py_mkdir = lambda p: Path(p).mkdir(parents=True, exist_ok=True)
        globals_.py_unique = self.unique
        globals_.py_standard = lambda kind: str(self.app_data if kind == "appData" else self.destination if kind == "pictures" else self.root)
        globals_.py_execute = lambda cmd: subprocess.run(cmd, shell=True, cwd=self.root, timeout=20).returncode
        globals_.py_module = lambda name: (PLUGIN / name).read_text()
        globals_.py_plugin = str(self.plugin)
        globals_.py_dialog = self.dialog
        globals_.py_message = lambda *args: self.messages.append(args)
        self.lua.execute(SDK)

    def delete(self, path):
        path = Path(path)
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)

    def unique(self, value):
        path = Path(value)
        index = 1
        while path.exists():
            path = Path(f"{value}-{index}")
            index += 1
        return str(path)

    def dialog(self, args):
        self.dialogs.append(args["title"])
        if len(self.dialogs) > 15:
            raise AssertionError(f"Dialog loop: {self.messages}")
        if args["title"] == "Import from camera":
            props = args["contents"]["bind_to_object"]
            props["source"] = str(self.source)
            props["destination"] = str(self.destination)
            return self.settings_choice
        if args["title"] == "Review camera import":
            self.assertFalse(self.destination.exists(), "Preview must not create the destination")
            if self.on_preview:
                self.on_preview()
            return self.preview_choice
        return "ok"

    def photo(self, name="IMG.JPG", data=b"photograph"):
        path = self.source / name
        path.write_bytes(data)
        os.utime(path, (1789905600, 1789905600))
        return path

    def run_app(self, retry=False):
        self.lua.execute(f"require('App.lua').start({'true' if retry else 'false'}); flushTasks()")

    def report(self, name="response.json"):
        job = Path(self.lua.globals().prefs.lastJob)
        return json.loads((job / name).read_text())

    def add_to_catalog(self, files):
        self.lua.globals().testFiles = self.lua.table_from([self.lua.table_from(item) for item in files])
        return self.lua.execute("return require('Catalog.lua').add({}, catalog, testFiles)")

    def test_all_plugin_files_compile_in_lua51(self):
        for path in PLUGIN.glob("*.lua"):
            with self.subTest(file=path.name):
                self.lua.compile(path.read_text(), name=path.name)
        info = self.lua.execute((PLUGIN / "Info.lua").read_text())
        for item in info.LrExportMenuItems.values():
            self.assertTrue((PLUGIN / item.file).is_file())
            self.assertIsNone(item.enabledWhen, "Import must work in an empty catalog")

    def test_full_plugin_preview_copy_catalog_and_retry(self):
        self.photo()
        self.photo("IMG.RAF", b"raw")
        self.photo("IMG.xmp", b"sidecar")
        self.run_app()
        result = self.report()
        self.assertEqual(result["copied"], 3)
        self.assertEqual(len(result["files"]), 2)
        self.assertEqual(self.report("catalog-result.json")["added"], 2)
        for item in result["files"]:
            self.assertTrue(Path(item["path"]).is_file())
        self.assertFalse((self.root / "SHOULD_NOT_EXIST").exists())
        self.run_app(retry=True)
        self.assertEqual(self.report("catalog-result.json")["existing"], 2)
        self.assertEqual(len(self.lua.globals().catalog.calls), 2)

    def test_cancelled_preview_never_copies_or_touches_catalog(self):
        self.photo()
        self.preview_choice = "cancel"
        self.run_app()
        self.assertFalse(self.destination.exists())
        self.assertEqual(len(self.lua.globals().catalog.calls), 0)
        self.assertIsNone(self.lua.globals().prefs.lastJob)
        self.assertEqual(list((self.app_data / "CameraTools/Imports").iterdir()), [])

    def test_changed_source_after_preview_is_not_catalogued(self):
        path = self.photo()
        self.on_preview = lambda: path.write_bytes(b"changed since preview")
        self.run_app()
        self.assertTrue(self.report()["errors"])
        self.assertEqual(self.report("catalog-result.json")["added"], 0)
        self.assertEqual(len(self.lua.globals().catalog.calls), 0)

    def test_helper_cancel_signal_reaches_copy_phase(self):
        self.photo()
        self.lua.globals().cancelStage = "Copying camera files"
        self.run_app()
        self.assertTrue(self.report()["cancelled"])
        self.assertFalse(self.destination.exists())
        self.assertEqual(len(self.lua.globals().catalog.calls), 0)

    def test_failed_cancel_signal_keeps_supervising_the_copy(self):
        self.photo()
        self.lua.globals().cancelStage = "Copying camera files"
        self.lua.execute(r"""
            local bridge = require 'Bridge.lua'
            local write = bridge.write
            bridge.write = function(path, contents)
                if path:match('/cancel$') then error('Simulated disk full') end
                return write(path, contents)
            end
        """)
        self.run_app()
        self.assertEqual(self.report()["copied"], 1)
        self.assertEqual(self.report("catalog-result.json")["added"], 1)
        self.assertIn("Import finished with items to review", self.dialogs)

    def test_catalog_retry_requires_original_catalog(self):
        self.photo()
        self.run_app()
        self.lua.globals().catalog.path = "/test/Other.lrcat"
        with self.assertRaisesRegex(Exception, "Open the catalog used"):
            self.run_app(retry=True)
        self.assertEqual(len(self.lua.globals().catalog.calls), 1)

    def test_catalog_uses_actual_names_deduplicates_and_reports_rejection(self):
        renamed = self.photo("IMG__2.JPG")
        rejected = self.photo("UNSUPPORTED.JPG")
        self.lua.globals().catalog.failPath = str(rejected)
        files = [{"path": str(p), "kind": "photo"} for p in (renamed, renamed, rejected)]
        result = self.add_to_catalog(files)
        self.assertEqual(result.added, 1)
        self.assertEqual(result.pending, 0)
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(self.lua.globals().catalog.calls[1], str(renamed))
        self.assertEqual(self.add_to_catalog(files).existing, 1)

    def test_rolled_back_catalog_batch_does_not_report_success(self):
        path = self.photo()
        self.lua.globals().catalog.failCommit = True
        result = self.add_to_catalog([{"path": str(path), "kind": "photo"}])
        self.assertEqual(result.added, 0)
        self.assertEqual(result.pending, 1)
        self.assertEqual(len(result.errors), 1)
        self.assertIsNone(self.lua.globals().catalog.entries[str(path)])

    def test_busy_catalog_preserves_all_pending_files(self):
        path = self.photo()
        self.lua.globals().catalog.busy = True
        result = self.add_to_catalog([{"path": str(path), "kind": "photo"}])
        self.assertEqual(result.added, 0)
        self.assertEqual(result.pending, 1)
        self.assertIn("busy", result.errors[1])

    def test_cancel_catalog_commits_completed_files_and_retry_finishes(self):
        paths = [self.photo(f"IMG{index}.JPG") for index in range(3)]
        files = [{"path": str(path), "kind": "photo"} for path in paths]
        self.lua.globals().cancelCatalogAfter = 1
        result = self.add_to_catalog(files)
        self.assertTrue(result.cancelled)
        self.assertEqual(result.added, 1)
        self.assertEqual(result.pending, 2)
        self.lua.globals().cancelCatalogAfter = None
        result = self.add_to_catalog(files)
        self.assertEqual(result.added, 2)
        self.assertEqual(result.existing, 1)
        self.assertEqual(result.pending, 0)


if __name__ == "__main__":
    unittest.main()
