import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SESSION = Path(__file__).resolve().parents[1] / "linuxfs/overlay/usr/local/bin/droiddeck-session"
GRAPHICS_KEYS = ("BL_STEAM_GL_PRESENT", "DISABLE_GAMESCOPE_WSI", "LIBGL_KOPPER_DISABLE",
                 "DROIDDECK_SYSTEM_VULKAN_GPU", "DROIDDECK_SYSTEM_VULKAN_RUNTIME", "DROIDDECK_POWERVR_RUNTIME",
                 "VK_LAYER_PATH", "VK_INSTANCE_LAYERS", "WSI_X11_FORCE_SHM", "ENABLE_GAMESCOPE_WSI",
                 "VK_LOADER_LAYERS_DISABLE", "DISABLE_WSI_LAYER")


class SteamPresentationTest(unittest.TestCase):
    def launch(self, enabled=None, inherited=None):
        source = SESSION.read_text()
        graphics_start = source.index('bl_system_runtime=${DROIDDECK_SYSTEM_VULKAN_RUNTIME:')
        graphics_end = source.index("# gamescope's reaper", graphics_start)
        begin = source.index("    bl_steam_path=$PATH")
        end = source.index("\n    # Decky", begin)
        command = source.index('      PATH="$bl_steam_path"')
        command_end = source.index("\n      rc=$?", command)
        script = (source[graphics_start:graphics_end] + source[begin:end] + '\n_bl_ui=(-gamepadui); _bl_cdp=(); _bl_lang=()\n'
                  + source[command:command_end])
        env = dict(os.environ)
        for key in GRAPHICS_KEYS:
            env.pop(key, None)
        if enabled is not None:
            env["BL_STEAM_GL_PRESENT"] = enabled
        env.update(inherited or {})
        with tempfile.TemporaryDirectory(prefix="droiddeck-steam-gl-") as tmp:
            program = Path(tmp) / "steamrtarm64/steam"
            program.parent.mkdir()
            program.write_text(f"#!{sys.executable}\n" +
                               'import json, os, sys\nprint(json.dumps({"args":sys.argv[1:],'
                               '"disable":os.getenv("DISABLE_GAMESCOPE_WSI"),'
                               '"kopper":os.getenv("LIBGL_KOPPER_DISABLE"),'
                               '"path":os.getenv("PATH"),'
                               f'"graphics":{{key:os.getenv(key) for key in {GRAPHICS_KEYS!r}}},'
                               '"helper":os.getenv("BL_STEAM_WEBHELPER")}))\n')
            program.chmod(0o755)
            env.update(steam_root=tmp, steam_channel="steamdeck_publicbeta")
            result = subprocess.run(["bash", "-c", script, "test", "steam://open/main"],
                                    env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            launch = json.loads(result.stdout.splitlines()[-1])
            mali = env.get("DROIDDECK_SYSTEM_VULKAN_GPU") == "mali" and (
                env.get("DROIDDECK_SYSTEM_VULKAN_RUNTIME") or env.get("DROIDDECK_POWERVR_RUNTIME"))
            self.assertEqual(enabled == "1" and not mali, launch["path"].startswith("/usr/local/libexec/droiddeck-steam:"))
            self.assertEqual(launch["helper"], str(Path(tmp) / "steamrtarm64/steamwebhelper"))
            return launch

    def test_default_keeps_vulkan(self):
        result = self.launch()
        self.assertIn("-cef-use-angle=vulkan", result["args"])
        self.assertNotIn("-cef-disable-gpu-compositing", result["args"])
        self.assertIsNone(result["disable"])
        self.assertIsNone(result["kopper"])

    def test_system_powervr_keeps_gpu_gl_with_software_web_compositing(self):
        result = self.launch("1")
        self.assertIn("-cef-use-angle=gl", result["args"])
        self.assertIn("-cef-use-gl=angle", result["args"])
        self.assertIn("-cef-force-gpu", result["args"])
        self.assertIn("-cef-disable-gpu-compositing", result["args"])
        self.assertNotIn("-cef-disable-gpu", result["args"])
        self.assertEqual(result["disable"], "1")
        self.assertEqual(result["kopper"], "true")
        self.assertEqual(result["args"][-1], "steam://open/main")

    def test_disabled_option_preserves_existing_environment(self):
        result = self.launch("0", {"DISABLE_GAMESCOPE_WSI": "0", "LIBGL_KOPPER_DISABLE": "false"})
        self.assertIn("-cef-use-angle=vulkan", result["args"])
        self.assertNotIn("-cef-disable-gpu-compositing", result["args"])
        self.assertEqual(result["disable"], "0")
        self.assertEqual(result["kopper"], "false")

    def test_mali_uses_vulkan_x11_shm_with_bundled_layers(self):
        for key in ("DROIDDECK_SYSTEM_VULKAN_RUNTIME", "DROIDDECK_POWERVR_RUNTIME"):
            with self.subTest(runtime_key=key):
                result = self.launch(inherited={
                    "DROIDDECK_SYSTEM_VULKAN_GPU": "mali", key: "/runtime with spaces",
                    "VK_LAYER_PATH": "/runtime with spaces/lib", "VK_INSTANCE_LAYERS": "VK_LAYER_HYBRIS_compat",
                    "DISABLE_WSI_LAYER": "1", "WSI_X11_FORCE_SHM": "0",
                })
                self.assertIn("-cef-use-angle=vulkan", result["args"])
                self.assertNotIn("-cef-disable-gpu-compositing", result["args"])
                self.assertEqual(result["graphics"]["VK_LAYER_PATH"], "/runtime with spaces/wsi:/runtime with spaces/lib")
                self.assertEqual(result["graphics"]["VK_INSTANCE_LAYERS"],
                                 "VK_LAYER_window_system_integration:VK_LAYER_HYBRIS_compat")
                self.assertEqual(result["graphics"]["WSI_X11_FORCE_SHM"], "1")
                self.assertEqual(result["graphics"]["ENABLE_GAMESCOPE_WSI"], "0")
                self.assertEqual(result["graphics"]["VK_LOADER_LAYERS_DISABLE"], "*gamescope*")
                self.assertIsNone(result["graphics"]["DISABLE_WSI_LAYER"])
                self.assertIsNone(result["disable"])
                self.assertIsNone(result["kopper"])

    def test_generic_runtime_takes_precedence_over_legacy_alias(self):
        result = self.launch(inherited={
            "DROIDDECK_SYSTEM_VULKAN_GPU": "mali", "DROIDDECK_SYSTEM_VULKAN_RUNTIME": "/new",
            "DROIDDECK_POWERVR_RUNTIME": "/old",
        })
        self.assertEqual(result["graphics"]["VK_LAYER_PATH"], "/new/wsi:/new/lib")

    def test_manual_driver_does_not_enable_the_mali_profile(self):
        for gpu, runtime in (("mali", ""), ("", "/runtime"), ("powervr", "/runtime")):
            with self.subTest(gpu=gpu, runtime=runtime):
                result = self.launch(inherited={
                    "DROIDDECK_SYSTEM_VULKAN_GPU": gpu, "DROIDDECK_SYSTEM_VULKAN_RUNTIME": runtime,
                    "VK_LAYER_PATH": "custom/layers", "VK_INSTANCE_LAYERS": "custom_layer", "WSI_X11_FORCE_SHM": "0",
                })
                self.assertEqual(result["graphics"]["VK_LAYER_PATH"], "custom/layers")
                self.assertEqual(result["graphics"]["VK_INSTANCE_LAYERS"], "custom_layer")
                self.assertEqual(result["graphics"]["WSI_X11_FORCE_SHM"], "0")

    def test_mali_client_layers_do_not_reach_gamescope_wayland(self):
        source = SESSION.read_text()
        compositor = '  exec gamescope "${args[@]}" -- "$0" "$mode" "$@"'
        # The first pass execs the compositor; client settings must remain below that boundary.
        before, _ = source.split(compositor, 1)
        self.assertNotIn("WSI_X11_FORCE_SHM", before)
        self.assertNotIn("VK_LAYER_window_system_integration", before)
        begin = source.index('  width=${BL_WIDTH:-1280}')
        script = 'mode=steam\n' + source[begin:source.index(compositor) + len(compositor)]
        with tempfile.TemporaryDirectory(prefix="droiddeck-gamescope-env-") as tmp:
            gamescope = Path(tmp) / "gamescope"
            gamescope.write_text(f"#!{sys.executable}\nimport json, os, sys\n"
                                 + 'print(json.dumps({"args":sys.argv[1:],"sdl":os.getenv("SDL_VIDEODRIVER"),'
                                 + f'"graphics":{{key:os.getenv(key) for key in {GRAPHICS_KEYS!r}}}}}))\n')
            gamescope.chmod(0o755)
            env = {key: value for key, value in os.environ.items() if key not in GRAPHICS_KEYS}
            env.update(PATH=tmp + os.pathsep + env.get("PATH", ""), BL_MANGOAPP="0", BL_GAMESCOPE_BACKEND="sdl",
                       DROIDDECK_SYSTEM_VULKAN_GPU="mali", DROIDDECK_SYSTEM_VULKAN_RUNTIME="/runtime",
                       VK_LAYER_PATH="/runtime/lib", VK_INSTANCE_LAYERS="VK_LAYER_HYBRIS_compat")
            run = subprocess.run(["bash", "-c", script], env=env, text=True, capture_output=True, timeout=5)
            self.assertEqual(run.returncode, 0, run.stderr)
            result = json.loads(run.stdout.splitlines()[-1])
            self.assertEqual(result["sdl"], "wayland")
            self.assertEqual(result["args"][:2], ["--backend", "sdl"])
            self.assertEqual(result["graphics"]["VK_LAYER_PATH"], "/runtime/lib")
            self.assertEqual(result["graphics"]["VK_INSTANCE_LAYERS"], "VK_LAYER_HYBRIS_compat")
            self.assertIsNone(result["graphics"]["WSI_X11_FORCE_SHM"])


if __name__ == "__main__":
    unittest.main()
