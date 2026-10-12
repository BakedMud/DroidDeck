# Experimental system Vulkan backends

PowerVR, Xclipse and Mali use the bundled libhybris ICD to load the device's Android Vulkan
HAL from the Linux guest. The Android compositor uses the system Vulkan loader.
The guest Gamescope SDL backend presents through libhybris Wayland WSI and
DroidDeck's `android_wlegl` / Android hardware buffer receiver.

| Selection | PowerVR | Xclipse | Mali |
| --- | --- | --- | --- |
| HAL under `/vendor/lib64/hw/` | `vulkan.powervr.so` | `vulkan.samsung.so` | `vulkan.mali.so` |
| DRM | Accessible, identifiable PowerVR render node | Accessible, identifiable `sgpu` render node | No DRM-node requirement or handle emulation |
| Inner X11 presentation | Existing DRI3/Steam GL profile | Existing DRI3/Steam GL profile | Bundled X11 WSI with `WSI_X11_FORCE_SHM=1` |
| Proton DXVK | Managed patched `dxvk` | Managed upstream `dxvk-2.6.2` for 128-byte push constants | User-selected Proton/DXVK; WineD3D preference preserved |
| Session / game compatibility layers | compat / WSI + compat | Xclipse / WSI + Xclipse + compat | compat / WSI + compat |

X11 layer settings are applied after the outer Gamescope launch, including direct
program and desktop sessions. `droiddeck-gpu` restores the Wayland bridge settings
before starting a nested Gamescope and passes X11 settings only to its client.
Shared-memory presentation adds a CPU copy; it does not imply software Vulkan
rendering. Actual rendering and performance still require device testing.

The app selects a profile and passes capabilities to the guest helpers. Presentation
and DXVK policy are independent: a future driver can use shared-memory presentation
with a managed DXVK payload, or native presentation with a user-selected payload.

| Environment key | Meaning |
| --- | --- |
| `DROIDDECK_SYSTEM_VULKAN_RUNTIME` | Selected bridge directory; takes precedence over the legacy alias |
| `DROIDDECK_SYSTEM_VULKAN_GPU` | Diagnostic vendor identity; used for defaults by older profiles only |
| `DROIDDECK_SYSTEM_VULKAN_PRESENTATION` | `native` preserves normal presentation; `x11-shm` enables the X11 client fallback |
| `DROIDDECK_SYSTEM_VULKAN_DXVK` | `managed` uses the runtime wrapper/payload; `user` preserves Proton/DXVK/WineD3D choices |
| `DROIDDECK_HYBRIS_SESSION_LAYERS` | Outer/session layer list, restored before launching a nested Gamescope |

`DROIDDECK_HYBRIS_LAYERS` separately specifies game compatibility layers;
`DROIDDECK_HYBRIS_LAYER_PATH` adds their directory, and `DROIDDECK_HYBRIS_DXVK`
names the managed DXVK payload. The X11 WSI layer is added only for inner clients.
Xclipse's session uses its vendor layer alone; games also get BC compatibility.
Absent or empty capability fields retain older defaults: Mali uses `x11-shm`/`user`,
and other system profiles use `native`/`managed`. The old
`DROIDDECK_POWERVR_RUNTIME` alias remains available for PowerVR and Xclipse helpers.
Manual driver selections retain their existing behavior.

```text
Android compositor
  +-- Gamescope: SDL/Wayland, session layers
        +-- session client: X11 presentation selected by profile
        +-- MangoApp: independently applies the same X11 profile
```

## Runtime compatibility ownership

The app currently loads the pinned libhybris runtime artifact and keeps the
packaged Xclipse layer active. Source changes to HAL/ICD compatibility under
`tools/hybris` do not replace that artifact: deployment requires rebuilding the
runtime, validating it, and updating its pin and checksum. The Xclipse layer must
remain active until the replacement runtime supplies its tested workarounds.
The capability metadata above prepares common launch behavior without claiming
that the native compatibility migration is already deployed.

The native follow-up patch uses the existing split: common/ICD code prepares the
selected Android HAL, while `VK_LAYER_HYBRIS_compat` handles Vulkan call and feature
workarounds. BCn decoding and several SPIR-V transformations already exist in the
pinned layer. Descriptor and dynamic-state compatibility is partial: filtering an
unsupported path is not equivalent to implementing its emulation. A shared vendor
mechanism does not establish support for every driver version or fix Mali's first
frame by itself.

The component build is available through the existing APK workflow:

```sh
gh workflow run build.yml --repo wallentx/DroidDeck \
  --ref wallentx/powervr-mali -f component=hybris
```

This selects an artifact-only libhybris job. It produces `runtime.tzst`, its
checksum, and `hybris-component.json`, a candidate release pin with the producer
commit, build URL, archive/tree hashes, and exact recipe inputs. It does not publish
the archive or modify `release.json`. The archive also records the build recipe
and ships the original compiler manifest and patched source, including new files.

After native review and a successful component build, publish that exact archive,
verify its download, and update the `hybris` entry in `tools/powervr/release.json`.
Until then, APK assembly intentionally rejects the changed recipe/patch stack.
Only then replace the active standalone Xclipse layer with the shared layer. Its
outer/session path must explicitly set `HYBRIS_BC_TEXTURES=0` to preserve the prior
vendor-only behavior; managed game launches still select `HYBRIS_BC_TEXTURES=dxvk`.
Full APK CI and device regressions follow that activation.

## Pixel 8 Pro / Tensor G3 evidence

The supplied 2026-10-11 Steam report selected `turnip-sdk36` and the rootfs
`freedreno_icd.json`; both the Android compositor and Gamescope failed to find a
physical device. The device has `ro.hardware.vulkan=mali` and `vulkan.mali.so`.

A direct system-loader probe from Termux (UID 10514, `untrusted_app_27`) identified
Mali-G715, Vulkan 1.4.343, 256-byte push constants, and maintenance6. It exposed
DMA-BUF, DRM format modifier, external semaphore FD, Android hardware buffer,
foreign queue and robustness2 extensions, but no `VK_EXT_physical_device_drm`.

That probe also found `fillModeNonSolid`, `multiViewport`, `shaderClipDistance`,
`shaderCullDistance`, `vertexPipelineStoresAndAtomics`, `textureCompressionBC`
and `robustBufferAccess2` disabled. Several are mandatory in the pinned DXVK
build; the PowerVR BC patch does not implement the other missing features.
Consequently, Mali must not be locked to the PowerVR DXVK payload. Compatible
DXVK builds/layers or game-specific choices still need separate validation.

The subsequent installed-APK attempt on Pixel 8 Pro hangs at "waiting for the
first frame". Aligning the Mali profile with the merged PowerVR/Xclipse code does
not establish the cause or fix that hang; device troubleshooting remains pending.

These are native capability results under Termux's UID, not a successful
libhybris, Steam or game launch under DroidDeck's UID. Focused JVM tests use
stubbed Android dependencies; guest tests validate environment and command
selection. Full Android/Compose CI and an installed APK launch remain required.

## Related implementation discussion

All 21 comments available on [DroidDeck issue #165](https://github.com/Droid-Deck/DroidDeck/issues/165)
were reviewed while preparing this extension. Particularly relevant:

- [Mali libhybris rendering report](https://github.com/Droid-Deck/DroidDeck/issues/165#issuecomment-6014766410).
- [Android handle transport, SDL and PowerVR DRM rationale](https://github.com/Droid-Deck/DroidDeck/issues/165#issuecomment-6102946636).
- [Mali shader, feature and DXVK compatibility gaps](https://github.com/Droid-Deck/DroidDeck/issues/165#issuecomment-6108722387).
- [Xclipse port findings](https://github.com/Droid-Deck/DroidDeck/issues/165#issuecomment-6114421494), which informed capability checks but are not enabled as Mali workarounds.
