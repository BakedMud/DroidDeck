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
| Session / game compatibility layers | compat / WSI + compat | compat / WSI + compat | compat / WSI + compat |
| Session / managed-game BC emulation | Existing policy / `dxvk` | `0` / `dxvk` | Existing policy / user-controlled |

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
| `DROIDDECK_HYBRIS_SESSION_BC_TEXTURES` | Optional outer/session BC policy, restored before nested compositors; Xclipse sets `0` |

`DROIDDECK_HYBRIS_LAYERS` separately specifies game compatibility layers;
`DROIDDECK_HYBRIS_LAYER_PATH` adds their directory, and `DROIDDECK_HYBRIS_DXVK`
names the managed DXVK payload. The X11 WSI layer is added only for inner clients.
Xclipse uses the shared compatibility layer throughout. Its outer/session environment
sets `HYBRIS_BC_TEXTURES=0`; managed game launches override it to `dxvk` in their
own process environment, and nested compositors restore the session policy.
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

The app now selects the shared `VK_LAYER_HYBRIS_compat` layer for Xclipse and no
longer stages or enables the standalone Xclipse layer. The runtime is pinned to
[component release `78831b13`](https://github.com/wallentx/DroidDeck/releases/tag/hybris-78831b134c818e9d1077a8b73a11364c3d47d36d),
built by [component CI](https://github.com/wallentx/DroidDeck/actions/runs/38223214145).
`tools/powervr/release.json` records the verified archive and tree checksums, recipe
inputs, producer commit, and upstream source revision.

Common/ICD code prepares the selected Android HAL, including the Xclipse shader
compiler's bionic environment. The shared compatibility layer handles Vulkan call
and feature workarounds, with Xclipse changes gated by the selected HAL and physical
device. PowerVR and Mali retain their existing BC policy; Xclipse disables BC
emulation in the outer/session path and enables its DXVK profile only for managed
games. The 128-byte push-constant limit still selects DXVK 2.6.2.

BCn decoding and several SPIR-V transformations already exist in the pinned layer.
Descriptor and dynamic-state compatibility is partial: filtering an unsupported
path is not equivalent to implementing its emulation. The native migration and
component tests do not establish support for every driver version or fix Mali's
first frame by themselves. Full APK CI and installed-device regressions remain
separate validation gates.

The component build is available through the existing APK workflow. Replace
`<reviewed-branch-or-tag>` with the reviewed branch or tag containing the component
recipe and workflow:

```sh
gh workflow run build.yml --repo wallentx/DroidDeck \
  --ref '<reviewed-branch-or-tag>' -f component=hybris
```

This selects an artifact-only libhybris job. It produces `runtime.tzst`, its
checksum, and `hybris-component.json`, a candidate release pin with the producer
commit, build URL, archive/tree hashes, and exact recipe inputs. It does not publish
the archive or modify `release.json`. The archive also records the build recipe
and ships the original compiler manifest and patched source, including new files.

For subsequent native changes, review and build the component, publish that exact
archive, verify its download, and update the `hybris` entry in
`tools/powervr/release.json`. APK assembly rejects a recipe or patch stack that no
longer matches its pinned component. Preserve the separation between the Xclipse
outer/session BC policy and the managed-game override when changing launch code.

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

Historical installed-APK evidence goes beyond the Termux probe. The
[full APK build at `b35888b`](https://github.com/wallentx/DroidDeck/actions/runs/38304474692)
passed CI. On the Pixel 8 Pro, the installed 0.3.2 APK's native ARM64 Steam client
reached `READY` using shared-memory presentation, and Old School Rally downloaded
successfully. Game rendering remains unverified on that Mali build.

The stock `DXVK 3.1.1-27-g25ca` payload still cannot create an adapter because
Mali lacks required `fillModeNonSolid` support, alongside the other capability
gates above. The tested Old School Rally build produced white or black output
with the WineD3D fallbacks and rejected `-force-vulkan` because Vulkan support was
not built into the game. Managed DXVK and WineD3D choices remain user-controlled.

The newer shared experimental Mali DXVK source at
[`48fdc1a`](https://github.com/wallentx/DroidDeck/commit/48fdc1a) contains the
three-architecture native wrapper and a separate
[component build](https://github.com/wallentx/DroidDeck/actions/runs/38361775286).
The new component has not received installed-device validation. The successful
[PowerVR/Xclipse APK build](https://github.com/wallentx/DroidDeck/actions/runs/38357695607?pr=7)
covers base revision `500451b`; it does not validate the newer Mali component.

Focused JVM tests use stubbed Android dependencies; guest tests validate
environment and command selection. Full Android/Compose CI and installed-device
validation of the exact component artifact remain required.

## Related implementation discussion

All 21 comments available on [DroidDeck issue #165](https://github.com/Droid-Deck/DroidDeck/issues/165)
were reviewed while preparing this extension. Particularly relevant:

- [Mali libhybris rendering report](https://github.com/Droid-Deck/DroidDeck/issues/165#issuecomment-6014766410).
- [Android handle transport, SDL and PowerVR DRM rationale](https://github.com/Droid-Deck/DroidDeck/issues/165#issuecomment-6102946636).
- [Mali shader, feature and DXVK compatibility gaps](https://github.com/Droid-Deck/DroidDeck/issues/165#issuecomment-6108722387).
- [Mali device findings and follow-up](https://github.com/Droid-Deck/DroidDeck/issues/165#issuecomment-6123415216), which reinforce the need to validate exact DXVK and wrapper artifacts on device.
- [Xclipse port findings](https://github.com/Droid-Deck/DroidDeck/issues/165#issuecomment-6114421494), which informed capability checks but are not enabled as Mali workarounds.
