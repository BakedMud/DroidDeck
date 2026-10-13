package com.droiddeck.launcher.gpu;

import android.content.Context;
import android.os.Build;
import android.system.ErrnoException;
import android.system.Os;
import android.system.OsConstants;
import com.droiddeck.launcher.core.FileUtils;
import com.droiddeck.launcher.core.TarZst;
import java.io.File;
import java.io.IOException;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.List;
import java.util.TreeSet;
import java.util.UUID;
import java.util.function.Predicate;
import org.json.JSONObject;

/** Isolated glibc-to-Android Vulkan bridge. It does not replace the rootfs's Mesa libraries. */
public final class SystemVulkanDriver {
    private static final String ASSET = "hybris/runtime.tzst";
    /** Vendor policy is independent from the shared libhybris HAL loader and Vulkan layer. */
    public enum Profile {
        POWERVR("powervr", "PowerVR", "/vendor/lib64/hw/vulkan.powervr.so", true,
                "dxvk", "native", "VK_LAYER_HYBRIS_compat", "VK_LAYER_HYBRIS_compat", null, null,
                new String[] {"pvrsrvkm", "pvr", "powervr"}),
        XCLIPSE("xclipse", "Xclipse", "/vendor/lib64/hw/vulkan.samsung.so", true,
                "dxvk-2.6.2", "native", "VK_LAYER_HYBRIS_compat", "VK_LAYER_HYBRIS_compat", null, "0",
                new String[] {"sgpu"}),
        MALI("mali", "Mali", "/vendor/lib64/hw/vulkan.mali.so", false,
                null, "x11-shm", "VK_LAYER_HYBRIS_compat", "VK_LAYER_HYBRIS_compat", null, null,
                new String[] {});

        public final String id;
        public final String gpuName;
        public final String halPath;
        public final boolean requiresRenderNode;
        public final String dxvkPayload;
        public final String presentation;
        final String sessionLayers;
        final String gameLayers;
        final String extraLayerPath;
        final String sessionBcTextures;
        private final String[] drmDrivers;

        Profile(String id, String gpuName, String halPath, boolean requiresRenderNode,
                String dxvkPayload, String presentation, String sessionLayers,
                String gameLayers, String extraLayerPath, String sessionBcTextures, String[] drmDrivers) {
            this.id = id;
            this.gpuName = gpuName;
            this.halPath = halPath;
            this.requiresRenderNode = requiresRenderNode;
            this.dxvkPayload = dxvkPayload;
            this.presentation = presentation;
            this.sessionLayers = sessionLayers;
            this.gameLayers = gameLayers;
            this.extraLayerPath = extraLayerPath;
            this.sessionBcTextures = sessionBcTextures;
            this.drmDrivers = drmDrivers;
        }

        public static Profile fromHal(String hal) {
            for (Profile profile : values()) if (profile.halPath.equals(hal)) return profile;
            return null;
        }
    }

    private static final String[] REQUIRED = {
        "lib/libhybris-vulkan-icd.so.0", "lib/libhybris-common.so.1",
        "lib/libhybris/linker/q.so", "lib/libVkLayer_hybris_compat.so",
        "run-proton.py", "wsi/libVkLayer_window_system_integration.so",
        "wsi/VkLayer_window_system_integration.json",
        "dxvk/x32/d3d8.dll", "dxvk/x32/d3d9.dll", "dxvk/x32/d3d10core.dll",
        "dxvk/x32/d3d11.dll", "dxvk/x32/dxgi.dll",
        "dxvk/x64/d3d8.dll", "dxvk/x64/d3d9.dll", "dxvk/x64/d3d10core.dll",
        "dxvk/x64/d3d11.dll", "dxvk/x64/dxgi.dll"
    };

    private SystemVulkanDriver() {}

    public static Profile profile() {
        return findProfile(path -> new File(path).isFile());
    }

    /** An ambiguous HAL installation must not accidentally activate another vendor's quirks. */
    static Profile findProfile(Predicate<String> present) {
        Profile selected = null;
        for (Profile profile : Profile.values()) {
            if (!present.test(profile.halPath)) continue;
            if (selected != null) return null;
            selected = profile;
        }
        return selected;
    }

    public static String hal() {
        Profile selected = profile();
        return selected == null ? null : selected.halPath;
    }

    public static String systemHalPath() { return hal(); }
    public static boolean isPowerVr() { return profile() == Profile.POWERVR; }
    public static boolean isXclipse() { return profile() == Profile.XCLIPSE; }
    public static boolean isMali() { return profile() == Profile.MALI; }
    public static boolean isSupportedGpu() { return profile() != null; }

    public static String gpuName() {
        Profile selected = profile();
        return selected == null ? "Vulkan" : selected.gpuName;
    }

    public static boolean requiresRenderNode() {
        Profile selected = profile();
        return selected != null && selected.requiresRenderNode;
    }

    public static boolean isAvailable() {
        Profile selected = profile();
        return selected != null && (!selected.requiresRenderNode || renderNodeAvailable());
    }

    public static boolean renderNodeAvailable() {
        return renderNodePath() != null;
    }

    /** Select this HAL profile's GPU node, skipping display controllers and other vendors. */
    public static String renderNodePath() {
        Profile selected = profile();
        if (selected == null || !selected.requiresRenderNode) return null;
        String[] nodes = new File("/dev/dri").list();
        if (nodes == null) nodes = new File("/sys/class/drm").list();
        return findRenderNode(nodes, node -> renderNodeAvailable(node, selected));
    }

    static String findRenderNode(String[] nodes, Predicate<String> available) {
        TreeSet<String> candidates = new TreeSet<>();
        // Android can deny directory listing while allowing direct access. Probe the kernel's
        // reserved render range too; directory entries also cover dynamically allocated minors.
        for (int minor = 128; minor < 192; minor++) candidates.add("renderD" + minor);
        if (nodes != null) for (String node : nodes)
            if (node.matches("renderD[0-9]+")) candidates.add(node);
        for (String node : candidates) {
            String path = "/dev/dri/" + node;
            if (available.test(path)) return path;
        }
        return null;
    }

    private static boolean renderNodeAvailable(String node, Profile selected) {
        try {
            if (!renderNodeAvailable(OsConstants.S_ISCHR(Os.stat(node).st_mode),
                    Os.access(node, OsConstants.R_OK), Os.access(node, OsConstants.W_OK))) return false;
            return renderNodeDevice(new File("/sys/class/drm/" + new File(node).getName() + "/device"), selected);
        } catch (ErrnoException error) {
            return false;
        }
    }

    static boolean renderNodeDriver(Profile profile, String driver) {
        if (profile == null) return false;
        for (String name : profile.drmDrivers) if (name.equals(driver)) return true;
        return false;
    }

    static boolean renderNodeDevice(File device, Profile selected) {
        try {
            return renderNodeDriver(selected, Files.readSymbolicLink(new File(device, "driver").toPath())
                    .getFileName().toString());
        } catch (IOException error) {
            // Some Android policies expose uevent but deny reading the driver symlink.
            try (BufferedReader input = Files.newBufferedReader(
                    new File(device, "uevent").toPath(), StandardCharsets.US_ASCII)) {
                String line;
                while ((line = input.readLine()) != null)
                    if (line.startsWith("DRIVER=")) return renderNodeDriver(selected, line.substring(7));
            } catch (IOException ignored) { }
            return false;
        }
    }

    static boolean renderNodeAvailable(boolean characterDevice, boolean readable, boolean writable) {
        return characterDevice && readable && writable;
    }

    public static boolean usesDefault(String choice) {
        return usesDefault(isSupportedGpu(), choice);
    }

    /** The bundled DXVK profile requires features missing from Mali; keep its choices editable. */
    public static boolean managesDxvk(String choice) {
        Profile selected = profile();
        return usesDefault(selected != null && selected.dxvkPayload != null, choice);
    }

    static boolean usesDefault(boolean systemGpu, String choice) {
        return systemGpu && (choice == null || choice.isEmpty());
    }

    /** APK-versioned directories stay intact while an older session still has libraries mapped. */
    public static synchronized File prepare(Context context) throws IOException {
        Profile selected = profile();
        if (selected == null) throw new IOException("System Vulkan driver is unavailable");
        if (selected.requiresRenderNode && !renderNodeAvailable()) throw new IOException(
                "System " + selected.gpuName + " requires an identifiable, readable and writable DRM render node");
        File target = runtimeDirectory(context);
        File base = target.getParentFile();
        if (complete(target)) return target;
        if (!base.isDirectory() && !base.mkdirs()) throw new IOException("Cannot create system Vulkan directory");
        File staged = new File(base, ".stage-" + UUID.randomUUID());
        try {
            if (!TarZst.extractAsset(context, ASSET, staged)) throw new IOException("Cannot unpack system Vulkan runtime");
            for (String name : REQUIRED)
                if (!present(staged, name)) throw new IOException("Missing system Vulkan component: " + name);
            if (!new File(staged, "run-proton.py").canExecute())
                throw new IOException("System Vulkan Proton wrapper is not executable");
            JSONObject library = new JSONObject();
            library.put("library_path", new File(target, REQUIRED[0]).getPath());
            library.put("api_version", "1.3.0");
            JSONObject manifest = new JSONObject();
            manifest.put("file_format_version", "1.0.0");
            manifest.put("ICD", library);
            Files.write(new File(staged, "icd.json").toPath(), manifest.toString().getBytes(StandardCharsets.UTF_8));
            if (target.exists()) throw new IOException("Incomplete system Vulkan runtime directory: " + target.getName());
            if (!staged.renameTo(target)) throw new IOException("Cannot publish system Vulkan runtime");
            return target;
        } catch (org.json.JSONException error) {
            throw new IOException("Cannot write system Vulkan manifest", error);
        } finally {
            if (staged.exists()) FileUtils.delete(staged);
        }
    }

    /** The current APK's versioned runtime path, without extracting or modifying it. */
    public static File runtimeDirectory(Context context) throws IOException {
        String version;
        try (BufferedReader input = new BufferedReader(new InputStreamReader(
                context.getAssets().open("hybris/version.txt"), StandardCharsets.US_ASCII))) {
            version = input.readLine();
        }
        if (version == null || !version.matches("[0-9a-f]{64}")) throw new IOException("Invalid system Vulkan runtime version");
        return new File(new File(context.getFilesDir(), "system_vulkan"), version);
    }

    /** Collect abandoned stages, and obsolete versions once older guests can no longer load them. */
    public static synchronized void pruneUnused(Context context, boolean guestsStopped) throws IOException {
        pruneUnused(runtimeDirectory(context), guestsStopped);
    }

    static synchronized void pruneUnused(File current, boolean guestsStopped) {
        File base = current.getParentFile();
        if (base == null || Files.isSymbolicLink(base.toPath()) || Files.isSymbolicLink(current.toPath())) return;
        File[] entries = base.listFiles();
        if (entries == null) return;
        // Retain the previous good version if publishing the replacement failed.
        boolean pruneVersions = guestsStopped && complete(current);
        for (File entry : entries) {
            if (entry.equals(current)) continue;
            String name = entry.getName();
            boolean oldVersion = pruneVersions && name.matches("[0-9a-f]{64}");
            boolean abandonedStage = name.matches("\\.stage-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}");
            // The prepare monitor excludes any extraction still writing a .stage directory.
            if (oldVersion || abandonedStage) FileUtils.delete(entry);
        }
    }

    private static boolean complete(File directory) {
        if (!new File(directory, "icd.json").isFile()) return false;
        for (String name : REQUIRED) if (!present(directory, name)) return false;
        return new File(directory, "run-proton.py").canExecute();
    }

    private static boolean present(File directory, String name) {
        File file = new File(directory, name);
        return file.isFile() && file.length() > 0;
    }

    public static String icdPath(Context context) {
        try {
            return new File(prepare(context), "icd.json").getPath();
        } catch (IOException error) {
            throw new IllegalStateException("System Vulkan runtime is unavailable", error);
        }
    }

    public static void addEnvironment(Context context, List<String> environment) {
        File directory;
        try { directory = prepare(context); }
        catch (IOException error) { throw new IllegalStateException("System Vulkan runtime is unavailable", error); }
        addEnvironment(directory, systemHalPath(), requiresRenderNode() ? renderNodePath() : null,
                Build.VERSION.SDK_INT, environment);
    }

    static void addEnvironment(File directory, String hal, String node, int sdk, List<String> environment) {
        Profile selected = Profile.fromHal(hal);
        if (selected == null) throw new IllegalStateException("System Vulkan driver is unavailable");
        if (selected.requiresRenderNode && node == null)
            throw new IllegalStateException("System " + selected.gpuName + " render node is unavailable");
        environment.add("HYBRIS_ANDROID_SDK_VERSION=" + sdk);
        environment.add("HYBRIS_VULKAN_HAL=" + selected.halPath);
        environment.add("HYBRIS_LINKER_DIR=" + new File(directory, "lib/libhybris/linker").getPath());
        // Prefer framework dependencies for the system Vulkan loader, including libbase.so.
        environment.add("HYBRIS_LD_LIBRARY_PATH=/system/lib64:/system_ext/lib64:/vendor/lib64/egl:/vendor/lib64/hw:/vendor/lib64");
        String layerPath = new File(directory, "lib").getPath();
        if (selected.extraLayerPath != null) layerPath += ":" + selected.extraLayerPath;
        environment.add("VK_LAYER_PATH=" + layerPath);
        environment.add("VK_INSTANCE_LAYERS=" + selected.sessionLayers);
        environment.add("DROIDDECK_HYBRIS_SESSION_LAYERS=" + selected.sessionLayers);
        environment.add("DROIDDECK_HYBRIS_LAYERS=" + selected.gameLayers);
        if (selected.sessionBcTextures != null) {
            // The shared layer replaces Xclipse's vendor-only outer layer. Enable BC
            // emulation only for managed games; nested compositors restore this policy.
            environment.add("HYBRIS_BC_TEXTURES=" + selected.sessionBcTextures);
            environment.add("DROIDDECK_HYBRIS_SESSION_BC_TEXTURES=" + selected.sessionBcTextures);
        }
        if (selected.extraLayerPath != null)
            environment.add("DROIDDECK_HYBRIS_LAYER_PATH=" + selected.extraLayerPath);
        if (selected.dxvkPayload != null)
            environment.add("DROIDDECK_HYBRIS_DXVK=" + selected.dxvkPayload);
        // The outer compositor always uses Android-buffer Wayland presentation.
        environment.add("BL_GAMESCOPE_BACKEND=sdl");
        if (selected.requiresRenderNode) {
            // The DRM preload confirms the pvr driver before emulating handles. Both render-node
            // profiles use the managed WSI layer for Steam instead of the broken plain DRI3 path.
            environment.add("BL_POWERVR_DRM_HANDLES=1");
            environment.add("BL_POWERVR_RENDER_NODE=" + node);
            environment.add("BL_STEAM_GL_PRESENT=1");
            environment.add("BL_STEAM_WSI_LAYER_PATH=" + new File(directory, "wsi").getPath());
        }
        environment.add("DROIDDECK_SYSTEM_VULKAN_GPU=" + selected.id);
        environment.add("DROIDDECK_SYSTEM_VULKAN_RUNTIME=" + directory.getPath());
        environment.add("DROIDDECK_SYSTEM_VULKAN_PRESENTATION=" + selected.presentation);
        environment.add("DROIDDECK_SYSTEM_VULKAN_DXVK=" + (selected.dxvkPayload == null ? "user" : "managed"));
        // Older helpers use this name for both managed profiles. Mali must retain its own DXVK choice.
        if (selected.dxvkPayload != null)
            environment.add("DROIDDECK_POWERVR_RUNTIME=" + directory.getPath());
    }
}
