# Experimental system Vulkan backends

PowerVR and Mali use the bundled libhybris ICD to load the device's Android Vulkan
HAL from the Linux guest. The Android compositor uses the system Vulkan loader.
The guest Gamescope SDL backend presents through libhybris Wayland WSI and
DroidDeck's `android_wlegl` / Android hardware buffer receiver.

| Selection | PowerVR | Mali |
| --- | --- | --- |
| HAL | `/vendor/lib64/hw/vulkan.powervr.so` | `/vendor/lib64/hw/vulkan.mali.so` |
| DRM | Identifiable, readable/writable PowerVR render node required | No PowerVR render-node requirement or handle emulation |
| Inner X11 presentation | Existing DRI3/Steam GL profile | Bundled X11 WSI with `WSI_X11_FORCE_SHM=1` |
| Proton DXVK | Bundled patched DXVK, managed by the runtime | Proton/user-selected DXVK stays selectable |
| WineD3D override | Removed for the managed DXVK profile | Preserved |

X11 layer settings are applied after the outer Gamescope launch, including direct
program and desktop sessions. `droiddeck-gpu` restores the Wayland bridge settings
before starting a nested Gamescope and passes X11 settings only to its client.
Shared-memory presentation adds a CPU copy; it does not imply software Vulkan
rendering. Actual rendering and performance still require device testing.

`DROIDDECK_SYSTEM_VULKAN_RUNTIME` and `DROIDDECK_SYSTEM_VULKAN_GPU` identify the
selected bridge. The old `DROIDDECK_POWERVR_RUNTIME` alias remains available for
PowerVR guest helpers. Manual driver selections retain their existing behavior.

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
