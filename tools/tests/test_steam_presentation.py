import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SESSION = Path(__file__).resolve().parents[1] / "linuxfs/overlay/usr/local/bin/droiddeck-session"


class SteamPresentationTest(unittest.TestCase):
    def launch(self, enabled=None, inherited=None, with_wsi=True):
        source = SESSION.read_text()
        begin = source.index("    bl_steam_path=$PATH")
        end = source.index("\n    # Decky", begin)
        cef_begin = source.index("    _bl_cef=", end)
        cef_end = source.index("\n    # Deck mode", cef_begin)
        override_begin = source.index('    if [ "${BL_STEAM_GL_PRESENT:-}" = 1 ]; then', cef_end)
        override_end = source.index('\n    echo "== steam args:', override_begin)
        command = source.index('      PATH="$bl_steam_path"')
        command_end = source.index("\n      rc=$?", command)
        script = (source[begin:end] + "\n" + source[cef_begin:cef_end] + "\n" +
                  source[override_begin:override_end] + "\n" +
                  '\n_bl_ui=(-gamepadui); _bl_cdp=(); _bl_lang=()\n'
                  + source[command:command_end])
        env = dict(os.environ)
        for key in ("BL_STEAM_GL_PRESENT", "BL_STEAM_WSI_LAYER_PATH", "BL_STEAM_GL_WSI_ACTIVE",
                    "DISABLE_GAMESCOPE_WSI", "LIBGL_KOPPER_DISABLE", "VK_LAYER_PATH",
                    "VK_INSTANCE_LAYERS", "VK_LOADER_LAYERS_DISABLE"):
            env.pop(key, None)
        if enabled is not None:
            env["BL_STEAM_GL_PRESENT"] = enabled
        env.update(inherited or {})
        with tempfile.TemporaryDirectory(prefix="droiddeck-steam-gl-") as tmp:
            if enabled == "1" and with_wsi:
                wsi = Path(tmp) / "wsi"
                wsi.mkdir()
                (wsi / "libVkLayer_window_system_integration.so").write_bytes(b"layer")
                (wsi / "VkLayer_window_system_integration.json").write_text("{}")
                env["BL_STEAM_WSI_LAYER_PATH"] = str(wsi)
                env["DROIDDECK_HYBRIS_LAYERS"] = "VK_LAYER_HYBRIS_compat"
            program = Path(tmp) / "steamrtarm64/steam"
            program.parent.mkdir()
            program.write_text(f"#!{sys.executable}\n" +
                               'import json, os, sys\nprint(json.dumps({"args":sys.argv[1:],'
                               '"disable":os.getenv("DISABLE_GAMESCOPE_WSI"),'
                               '"kopper":os.getenv("LIBGL_KOPPER_DISABLE"),'
                               '"wsi_active":os.getenv("BL_STEAM_GL_WSI_ACTIVE"),'
                               '"layer_path":os.getenv("VK_LAYER_PATH"),'
                               '"layers":os.getenv("VK_INSTANCE_LAYERS"),'
                               '"loader_disable":os.getenv("VK_LOADER_LAYERS_DISABLE"),'
                               '"path":os.getenv("PATH"),'
                               '"helper":os.getenv("BL_STEAM_WEBHELPER")}))\n')
            program.chmod(0o755)
            env.update(steam_root=tmp, steam_channel="steamdeck_publicbeta")
            result = subprocess.run(["bash", "-c", script, "test", "steam://open/main"],
                                    env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            launch = json.loads(result.stdout.splitlines()[-1])
            self.assertEqual(enabled == "1", launch["path"].startswith("/usr/local/libexec/droiddeck-steam:"))
            self.assertEqual(launch["helper"], str(Path(tmp) / "steamrtarm64/steamwebhelper"))
            return launch

    def test_default_keeps_vulkan(self):
        result = self.launch()
        self.assertIn("-cef-use-angle=vulkan", result["args"])
        self.assertNotIn("-cef-disable-gpu-compositing", result["args"])
        self.assertIsNone(result["disable"])
        self.assertIsNone(result["kopper"])

    def test_system_powervr_presents_gpu_gl_through_managed_wsi(self):
        result = self.launch("1")
        self.assertIn("-cef-use-angle=gl", result["args"])
        self.assertIn("-cef-use-gl=angle", result["args"])
        self.assertIn("-cef-force-gpu", result["args"])
        self.assertNotIn("-cef-disable-gpu-compositing", result["args"])
        self.assertNotIn("-cef-disable-gpu", result["args"])
        self.assertEqual(result["disable"], "1")
        self.assertIsNone(result["kopper"])
        self.assertEqual(result["wsi_active"], "1")
        self.assertTrue(result["layer_path"].endswith("/wsi"))
        self.assertEqual(result["layers"], "VK_LAYER_window_system_integration:VK_LAYER_HYBRIS_compat")
        self.assertEqual(result["loader_disable"], "*gamescope*")
        self.assertEqual(result["args"][-1], "steam://open/main")

    def test_system_powervr_missing_wsi_keeps_safe_software_fallback(self):
        result = self.launch("1", with_wsi=False)
        self.assertIn("-cef-use-angle=gl", result["args"])
        self.assertIn("-cef-disable-gpu-compositing", result["args"])
        self.assertEqual(result["disable"], "1")
        self.assertEqual(result["kopper"], "true")
        self.assertIsNone(result["wsi_active"])

    def test_disabled_option_preserves_existing_environment(self):
        result = self.launch("0", {"DISABLE_GAMESCOPE_WSI": "0", "LIBGL_KOPPER_DISABLE": "false"})
        self.assertIn("-cef-use-angle=vulkan", result["args"])
        self.assertNotIn("-cef-disable-gpu-compositing", result["args"])
        self.assertEqual(result["disable"], "0")
        self.assertEqual(result["kopper"], "false")


if __name__ == "__main__":
    unittest.main()
