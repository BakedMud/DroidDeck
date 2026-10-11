# gamescope patches carried by the app

Built by `.github/workflows/build-gamescope.yml` on top of the exact gamescope the Linux runtime
ships (3.16.29, Arch Linux ARM's package, same build options), and staged from the apk over
`/usr/local/bin/gamescope` at each session start - the hosted runtime image is never touched.
The binary's shared-library needs are checked against `runtime-sonames.txt`, the runtime's own
library list, before anything is published.

The pinned `gamescope-3.16.29-p12-powervr` archive matches this source patch stack through patch
0125. It includes patch 0114 and the PowerVR fixes below, with patch 0113 removed.
It was built from `7ae3bd972e9f911afeb13b7e69a8dac3ecd3f3ba` in
[CI run 38034329682](https://github.com/wallentx/DroidDeck/actions/runs/38034329682),
which passed full compilation, the presentation policy tests and the runtime dependency check.
The published archive's SHA-256 was checked against the CI artifact. Device validation of the
frame-handoff correction remains separate.

- `0002-steamcompmgr-fallback-appid-focus.patch` - Armada (armada-os/armada), verbatim.
- `0009-fix-arm64-steam-night-mode.patch` - Armada, verbatim: the ARM64 client packs the
  night-mode property differently; the slider did nothing.
- `0019-steamcompmgr-arm64-virtual-white.patch` - Armada, verbatim: the colour-temperature
  slider's (x, y) arrives as one 64-bit element from the ARM64 client; y is recovered from x.
- `0020-color-p3-red-is-wide-gamut.patch` - Armada, verbatim.
- `0100-realtime-queue-and-gamepad-cursor.patch` - this app, two of Armada's ported by hand onto
  3.16.29: realtime-priority Vulkan queues on request (`GAMESCOPE_FORCE_VULKAN_REALTIME=1`)
  without CAP_SYS_NICE, which proot can never have (a no-op on KGSL Turnip, which has a single
  submit-queue priority); and the gamepad-driven cursor sprite following the X pointer that XTest
  moves (it sat frozen). The X pointer is asked for only while a cursor image is drawn - every
  vblank while shown, every 50 ms while hidden for inactivity - since each ask is a blocking round
  trip to Xwayland on the paint thread; with no image, wlserver's position is used as upstream does.
- `0110-wayland-backend-touch.patch` - this app: the nested Wayland backend bound only the host's
  pointer and keyboard, so a finger on the phone's screen never reached Steam. It now binds
  `wl_touch` too and hands each finger to wlserver's touch path (`wlserver_touchdown` / `motion` /
  `up`) - the one a Steam Deck's touchscreen drives - so what a touch does follows the client's
  touch mode (Steam's Big Picture sets Passthrough: a real touch, rows scroll under a finger).
  Finger ids are offset by one, since the nested pointer already moves wlserver's touch 0.
- `0111-wayland-pointer-warps-in-passthrough.patch` - this app: the nested pointer's motion goes to
  wlserver as touch 0, and in Passthrough (Big Picture's touch mode) a motion for a touch that is not
  down moves nothing - so in the app's touchpad mode the Steam client saw no hover and a click landed
  wherever the pointer had last been. The motion now always warps the real pointer as well
  (`bAlwaysWarpCursor`), which the other touch modes did already.
- `0112-restore-iconified-game-on-resume.patch` - this app: the Steam menu is an overlay that
  takes input without changing the focus window, and a fullscreen wine game minimizes itself when
  it loses input. gamescope only takes a window out of iconic when the focus window changes, and
  wine will not activate a window it believes iconic, so after Resume the game stayed minimized: a
  black screen with its small caption in the top-left corner (Titanfall 2, GE-Proton 11). The
  iconify request is remembered and the window goes back to NormalState before input returns to it,
  then focus is handed over again. `GAMESCOPE_RESTORE_FOCUS_WINDOW` on the root window asks for the
  same restore from outside (the session script's resume watcher).
- `0114-take-override-redirect-from-mapnotify.patch` - upstream (ValveSoftware/gamescope 3829340),
  verbatim; drop it once the runtime's gamescope includes it. Wine creates a game's window
  override-redirect and makes it managed before mapping it, which sends no X event. When gamescope
  read the window's attributes first, it kept the window as override-redirect and left it out of
  `GAMESCOPE_FOCUSABLE_WINDOWS`, so the client kept its loading screen over the running game
  (Skyrim SE, A Plague Tale: Innocence, on the games' own Xwayland). MapNotify now refreshes the flag.

- `0120-nested-swapchain-capability-fallback.patch` - this app: nested Vulkan backends
  negotiate presentation IDs/waits and mutable swapchain support. Surfaces lacking mutable
  formats or storage usage receive copies from ordinary offscreen composition images, using
  the same command-buffer barriers and presentation-layout transition as direct composition.
  Swapchains respect advertised alpha modes, image counts and extents, and accept RGBA as well
  as BGRA. Without presentation waits, FIFO acquisition anchors estimated frame scheduling;
  it does not provide measured presentation timestamps. The build runs the capability-policy
  tests in `tests/nested-swapchain.cpp`; actual presentation still needs device validation.

`release.env` may set `GAMESCOPE_REPO` to fetch a component from a downstream repository
while retaining the configured source for the other APK assets. The archive remains SHA-256 pinned.

- `0121-powervr-single-pixel-rcas.patch` - this app: select the existing single-pixel
  RCAS shader on Imagination GPUs as well as Qualcomm. On the tested PowerVR device,
  the quad-swizzled shader fails pipeline creation for layers 3 through 8 with YCbCr
  mask 2; the single-pixel shader passed all 24 tested layer/mask combinations using
  the APK's exact SPIR-V and libhybris runtime. Both variants cover the same 16x16
  dispatch tile. This is a compilation compatibility fix, not a performance claim.

Sixteen more of Armada's patches are DRM/lease/HDR-on-KMS work for a native display, which this
app's Wayland-hosted gamescope never reaches, or need a newer gamescope than the runtime has.

`0122-powervr-linear-dmabuf-import.patch` preserves explicit linear DMA-BUF layouts
when PowerVR rejects the mutable-format capability query. The retry is limited to
single-plane RGBA8/BGRA8, checks base-format import support, and retains the original
image flags and plane layout. Other rejected PowerVR layouts fail rather than being
silently interpreted as optimal tiling. Imported memory types are selected from the
intersection of the image and FD masks; CPU-mapping requirements remain strict.
The device probe reproduced the old 8 MiB requirement against a 4,616,192-byte
linear buffer, then verified UNORM and sRGB GPU sampling with the preserved layout.
The build tests the retry guard and memory-type selection. Steam still needs an
end-to-end device test after installing the new bundle.

`0123-powervr-rgba-copy-surface.patch` requires an RGBA SDR surface for PowerVR's
copy-based nested presentation. Gamescope's SDR shaders declare `rgba8`, which
requires an `R8G8B8A8_UNORM` storage view under Vulkan's
[shader/image format compatibility rules](https://docs.vulkan.org/spec/latest/appendices/spirvenv.html#spirvenv-image-formats).
Binding a BGRA storage view is a shader/view mismatch, not a demonstrated
PowerVR-specific format-support defect. A same-format copy to a BGRA swapchain
does not correct the mismatch. On the tested PowerVR device, a native
store-then-sample probe reproduced 128/64/192 becoming 192/64/128 with BGRA,
while RGBA retained 128/64/192; a live X11 checkerboard reproduced the reversal.

The small selection helper keeps the workaround limited to that reproduced SDR
copy path and requires an advertised `R8G8B8A8_UNORM` surface with
`SRGB_NONLINEAR` color space. It fails explicitly if none is available. Other
vendors, direct composition and HDR retain their existing format selection;
game-facing BGRA requests are unchanged. The build's policy tests cover format
ordering, RGBA-only and BGRA-only surfaces, incompatible formats/color spaces,
empty lists, and those unchanged paths. Broadening the workaround needs a
supported BGRA-only path and validation on other drivers. This fixes channel
order; it does not address Steam's separate corrupted DRI3 window contents.

`0124-sdl-preserve-explicit-refresh.patch` keeps an explicit nested refresh (`-r`) when SDL reports
the desktop mode again after its window is shown, moved or resized. Without it, a 60 Hz DroidDeck
cap was replaced by the panel's 120 Hz desktop mode for Steam's Xwayland display while Gamescope's
frame scheduler remained at 60 Hz. With no configured rate, SDL desktop-rate discovery is unchanged.
The mode-update log records configured, desktop and effective rates only when that tuple changes.
The correction is included from p11 onward. Device logs confirmed the requested 60 Hz mode,
but the captured old-frame replay persisted and prompted the subsequent handoff corrections.

`0125-sdl-wait-before-present.patch` completes the existing SDL compositor wait before
`vkQueuePresentKHR`. Gamescope otherwise submits its composition and copy, presents with no
render-finished semaphore, and waits afterward; the Android Vulkan bridge can therefore hand the
native consumer an already-signaled fence while the swapchain image still contains an older frame.
The patch moves the existing wait rather than adding another one. It serializes composition and
presentation, giving up their previous overlap; a future pipelined implementation requires a binary
render-finished semaphore per swapchain image. The pinned p12 archive includes this correction;
its effect on the captured replay still needs a repeat device recording.
