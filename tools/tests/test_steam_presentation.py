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
                 "DROIDDECK_SYSTEM_VULKAN_PRESENTATION", "DROIDDECK_SYSTEM_VULKAN_DXVK",
                 "DROIDDECK_HYBRIS_SESSION_LAYERS", "DROIDDECK_HYBRIS_LAYER_PATH",
                 "DROIDDECK_HYBRIS_SESSION_BC_TEXTURES", "HYBRIS_BC_TEXTURES",
                 "VK_LAYER_PATH", "VK_INSTANCE_LAYERS", "WSI_X11_FORCE_SHM", "ENABLE_GAMESCOPE_WSI",
                 "VK_LOADER_LAYERS_DISABLE", "DISABLE_WSI_LAYER", "SDL_FRAMEBUFFER_ACCELERATION")


class SteamPresentationTest(unittest.TestCase):
    def launch(self, enabled=None, inherited=None, with_wsi=True):
        source = SESSION.read_text()
        graphics_start = source.index('bl_system_runtime=${DROIDDECK_SYSTEM_VULKAN_RUNTIME:')
        graphics_end = source.index("# gamescope's reaper", graphics_start)
        begin = source.index("    bl_steam_path=$PATH")
        end = source.index("\n    # Decky", begin)
        cef_begin = source.index("    _bl_cef=", end)
        cef_end = source.index("\n    # Deck mode", cef_begin)
        override_begin = source.index('    if [ "${BL_STEAM_GL_PRESENT:-}" = 1 ]; then', cef_end)
        override_end = source.index('\n    echo "== steam args:', override_begin)
        command = source.index('      PATH="$bl_steam_path"')
        command_end = source.index("\n      rc=$?", command)
        script = (source[graphics_start:graphics_end] + "\n" + source[begin:end] + "\n" +
                  source[cef_begin:cef_end] + "\n" +
                  source[override_begin:override_end] + "\n" +
                  '\n_bl_ui=(-gamepadui); _bl_cdp=(); _bl_lang=()\n'
                  + source[command:command_end])
        env = dict(os.environ)
        for key in GRAPHICS_KEYS + ("BL_STEAM_WSI_LAYER_PATH", "BL_STEAM_GL_WSI_ACTIVE"):
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
                               f'"graphics":{{key:os.getenv(key) for key in {GRAPHICS_KEYS!r}}},'
                               '"helper":os.getenv("BL_STEAM_WEBHELPER")}))\n')
            program.chmod(0o755)
            env.update(steam_root=tmp, steam_channel="steamdeck_publicbeta")
            result = subprocess.run(["bash", "-c", script, "test", "steam://open/main"],
                                    env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            launch = json.loads(result.stdout.splitlines()[-1])
            self.assertEqual(launch["helper"], str(Path(tmp) / "steamrtarm64/steamwebhelper"))
            return launch

    def test_default_keeps_vulkan(self):
        result = self.launch()
        self.assertIn("-cef-use-angle=vulkan", result["args"])
        self.assertNotIn("-cef-disable-gpu-compositing", result["args"])
        self.assertIsNone(result["disable"])
        self.assertIsNone(result["kopper"])
        self.assertFalse(result["path"].startswith("/usr/local/libexec/droiddeck-steam:"))

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
        self.assertTrue(result["path"].startswith("/usr/local/libexec/droiddeck-steam:"))

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
        self.assertFalse(result["path"].startswith("/usr/local/libexec/droiddeck-steam:"))

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
                self.assertTrue(result["path"].startswith("/usr/local/libexec/droiddeck-steam:"))
                self.assertIsNone(result["graphics"]["SDL_FRAMEBUFFER_ACCELERATION"])

    def test_explicit_shm_uses_vulkan_and_keeps_vendor_layers(self):
        result = self.launch("1", {
            "DROIDDECK_SYSTEM_VULKAN_GPU": "future-vendor", "DROIDDECK_SYSTEM_VULKAN_RUNTIME": "/runtime",
            "DROIDDECK_SYSTEM_VULKAN_PRESENTATION": "x11-shm",
            "DROIDDECK_HYBRIS_SESSION_LAYERS": "VK_LAYER_vendor_session",
            "DROIDDECK_HYBRIS_LAYER_PATH": "/vendor-layer",
        })
        self.assertIn("-cef-use-angle=vulkan", result["args"])
        self.assertNotIn("-cef-disable-gpu-compositing", result["args"])
        self.assertTrue(result["path"].startswith("/usr/local/libexec/droiddeck-steam:"))
        self.assertIsNone(result["graphics"]["SDL_FRAMEBUFFER_ACCELERATION"])
        self.assertEqual(result["graphics"]["VK_LAYER_PATH"], "/runtime/wsi:/runtime/lib:/vendor-layer")
        self.assertEqual(result["graphics"]["VK_INSTANCE_LAYERS"],
                         "VK_LAYER_window_system_integration:VK_LAYER_vendor_session")
        self.assertEqual(result["graphics"]["WSI_X11_FORCE_SHM"], "1")

    def test_managed_wsi_capability_overrides_legacy_native_profile_defaults(self):
        for gpu in ("mali", "xclipse"):
            with self.subTest(gpu=gpu):
                result = self.launch("1", {
                    "DROIDDECK_SYSTEM_VULKAN_GPU": gpu, "DROIDDECK_SYSTEM_VULKAN_RUNTIME": "/runtime",
                    "DROIDDECK_SYSTEM_VULKAN_PRESENTATION": "native",
                    "VK_LAYER_PATH": "/runtime/lib", "VK_INSTANCE_LAYERS": "VK_LAYER_HYBRIS_compat",
                    "DROIDDECK_HYBRIS_SESSION_LAYERS": "VK_LAYER_HYBRIS_compat",
                    "DROIDDECK_HYBRIS_SESSION_BC_TEXTURES": "0", "HYBRIS_BC_TEXTURES": "0",
                })
                self.assertIn("-cef-use-angle=gl", result["args"])
                self.assertNotIn("-cef-disable-gpu-compositing", result["args"])
                self.assertTrue(result["path"].startswith("/usr/local/libexec/droiddeck-steam:"))
                self.assertTrue(result["graphics"]["VK_LAYER_PATH"].endswith("/wsi:/runtime/lib"))
                self.assertEqual(result["graphics"]["VK_INSTANCE_LAYERS"],
                                 "VK_LAYER_window_system_integration:VK_LAYER_HYBRIS_compat")
                self.assertEqual(result["graphics"]["HYBRIS_BC_TEXTURES"], "0")
                self.assertIsNone(result["graphics"]["WSI_X11_FORCE_SHM"])

    def test_generic_runtime_takes_precedence_over_legacy_alias(self):
        result = self.launch(inherited={
            "DROIDDECK_SYSTEM_VULKAN_GPU": "mali", "DROIDDECK_SYSTEM_VULKAN_RUNTIME": "/new",
            "DROIDDECK_POWERVR_RUNTIME": "/old",
        })
        self.assertEqual(result["graphics"]["VK_LAYER_PATH"], "/new/wsi:/new/lib")

    def test_manual_driver_does_not_enable_the_mali_profile(self):
        for gpu, runtime in (("mali", ""), ("", "/runtime"), ("powervr", "/runtime"), ("xclipse", "/runtime")):
            with self.subTest(gpu=gpu, runtime=runtime):
                result = self.launch(inherited={
                    "DROIDDECK_SYSTEM_VULKAN_GPU": gpu, "DROIDDECK_SYSTEM_VULKAN_RUNTIME": runtime,
                    "VK_LAYER_PATH": "custom/layers", "VK_INSTANCE_LAYERS": "custom_layer", "WSI_X11_FORCE_SHM": "0",
                })
                self.assertEqual(result["graphics"]["VK_LAYER_PATH"], "custom/layers")
                self.assertEqual(result["graphics"]["VK_INSTANCE_LAYERS"], "custom_layer")
                self.assertEqual(result["graphics"]["WSI_X11_FORCE_SHM"], "0")
                self.assertFalse(result["path"].startswith("/usr/local/libexec/droiddeck-steam:"))

    def test_shm_without_runtime_and_explicit_native_keep_default_path(self):
        for runtime, presentation in (("", "x11-shm"), ("/runtime", "native")):
            with self.subTest(runtime=runtime, presentation=presentation):
                result = self.launch(inherited={
                    "DROIDDECK_SYSTEM_VULKAN_GPU": "mali", "DROIDDECK_SYSTEM_VULKAN_RUNTIME": runtime,
                    "DROIDDECK_SYSTEM_VULKAN_PRESENTATION": presentation,
                    "SDL_FRAMEBUFFER_ACCELERATION": "1",
                })
                self.assertFalse(result["path"].startswith("/usr/local/libexec/droiddeck-steam:"))
                self.assertEqual(result["graphics"]["SDL_FRAMEBUFFER_ACCELERATION"], "1")

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
                       DROIDDECK_SYSTEM_VULKAN_PRESENTATION="x11-shm",
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
