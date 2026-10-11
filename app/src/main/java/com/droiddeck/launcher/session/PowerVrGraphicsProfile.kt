package com.droiddeck.launcher.session

import android.content.Context
import android.util.AtomicFile
import com.droiddeck.launcher.R
import com.droiddeck.launcher.gpu.SystemVulkanDriver
import com.droiddeck.launcher.runtime.GraphicsProfilePack
import com.droiddeck.launcher.runtime.LinuxRuntime
import com.droiddeck.launcher.runtime.LinuxRuntimeInstaller
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.Paths

/** App-owned selection and installation of the optional PowerVR game graphics profile. */
object PowerVrGraphicsProfile {
    enum class Mode { UNDECIDED, STANDARD, EXPERIMENTAL }
    data class Choice(val mode: Mode, val version: String? = null)

    private const val SETTINGS = "power-vr-graphics.json"
    private const val MANIFEST_ASSET = "graphics-profile/manifest.json"
    private const val WRAPPER_ASSET = "graphics-profile/run-proton.py"
    private const val PROFILE_FORMAT = 1
    private const val CONNECT_TIMEOUT_MS = 15_000
    private const val READ_TIMEOUT_MS = 60_000
    private val VERSION = Regex("^[0-9a-f]{64}$")
    private val transitionLock = Any()

    private val ROOTFS_LIBRARIES = listOf(
        "usr/lib/libxcb.so.1", "usr/lib/libxcb-shm.so.0", "usr/lib/libxcb-sync.so.1",
        "usr/lib/libxcb-dri3.so.0", "usr/lib/libxcb-present.so.0", "usr/lib/libX11-xcb.so.1",
        "usr/lib/libX11.so.6", "usr/lib/libXrandr.so.2", "usr/lib/libstdc++.so.6",
        "usr/lib/libm.so.6", "usr/lib/libgcc_s.so.1", "usr/lib/libc.so.6",
        "usr/lib/ld-linux-aarch64.so.1", "usr/lib/libwayland-client.so.0",
        "usr/lib/libwayland-server.so.0", "usr/lib/libwayland-egl.so.1",
    )
    private val CRITICAL_FILES = buildList {
        add("run-proton.py")
        for (arch in listOf("x32", "x64")) {
            for (dll in listOf("d3d8.dll", "d3d9.dll", "d3d10core.dll", "d3d11.dll", "dxgi.dll")) {
                add("dxvk/$arch/$dll")
            }
        }
        add("wsi/libVkLayer_window_system_integration.so")
        add("wsi/VkLayer_window_system_integration.json")
    }

    private fun settings(context: Context) = AtomicFile(File(context.filesDir, SETTINGS))
    private fun packs(context: Context) = File(context.filesDir, "graphics_profiles/packs")

    fun choice(context: Context): Choice {
        return try {
            val json = JSONObject(settings(context).readFully().toString(Charsets.UTF_8))
            if (json.getInt("format") != PROFILE_FORMAT) {
                Choice(Mode.UNDECIDED)
            } else when (json.getString("mode")) {
                "standard" -> Choice(Mode.STANDARD)
                "experimental" -> json.optString("version").takeIf(VERSION::matches)
                    ?.let { Choice(Mode.EXPERIMENTAL, it) } ?: Choice(Mode.UNDECIDED)
                else -> Choice(Mode.UNDECIDED)
            }
        } catch (_: Exception) {
            Choice(Mode.UNDECIDED)
        }
    }

    fun currentVersion(context: Context): String = currentSpec(context).version

    fun isInstalled(context: Context, version: String): Boolean =
        VERSION.matches(version) && GraphicsProfilePack.verify(packs(context), version)

    /** Provenance from the selected installed pack, not from the APK's newer available manifest. */
    fun installedDxvkRevision(context: Context, version: String): String? {
        if (!VERSION.matches(version)) return null
        val file = File(GraphicsProfilePack.directory(packs(context), version), "provenance/dxvk-source-commit")
        if (!file.isFile || file.length() > 128) return null
        return file.readText().trim().takeIf { it.matches(Regex("(?:[0-9a-f]{40}|[0-9a-f]{64})")) }
    }

    /** Serializes driver selection with enabling the profile, including completed downloads. */
    internal fun selectRuntimeIfAllowed(context: Context, select: () -> Unit): Boolean = synchronized(transitionLock) {
        if (SystemVulkanDriver.isPowerVr() && choice(context).mode == Mode.EXPERIMENTAL) return@synchronized false
        select()
        true
    }

    /** Installs the current APK's profile but does not select it. This performs blocking I/O. */
    fun install(context: Context, onProgress: (Int) -> Unit = {}): String {
        if (LinuxRuntimeInstaller.isBusy()) throw IOException("The Linux runtime is busy")
        if (!LinuxRuntime.isInstalled(context)) throw IOException("Install the Linux runtime before the graphics profile")
        if (!SystemVulkanDriver.isPowerVr()) throw IOException("The experimental graphics profile requires a PowerVR GPU")

        SystemVulkanDriver.prepare(context)
        requireLibraries(LinuxRuntime.rootDir(context))
        if (LinuxRuntimeInstaller.isBusy()) {
            throw IOException("The Linux runtime became busy while preparing the graphics profile")
        }

        val spec = currentSpec(context)
        val wrapper = context.assets.open(WRAPPER_ASSET).use { it.readBytes() }
        GraphicsProfilePack.install(
            packs(context), File(context.cacheDir, "graphics_profiles"), spec, wrapper,
            fetch = { archive, target, progress -> fetch(archive, target, progress) },
            onProgress = onProgress,
        )
        return spec.version
    }

    fun enable(context: Context, version: String) {
        enableIf(context, version) { true }
    }

    fun useStandard(context: Context) {
        useStandardIf(context) { true }
    }

    /** Selects an installed pack only while the caller's request token remains current. */
    fun enableIf(context: Context, version: String, isCurrent: () -> Boolean): Boolean {
        if (!isCurrent()) return false
        if (!isInstalled(context, version)) {
            if (!isCurrent()) return false
            throw IOException("Graphics profile $version is missing or corrupt; repair it in DroidDeck")
        }
        return changeIf(context, Choice(Mode.EXPERIMENTAL, version), isCurrent)
    }

    /** Selects standard graphics only while the caller's request token remains current. */
    fun useStandardIf(context: Context, isCurrent: () -> Boolean): Boolean =
        changeIf(context, Choice(Mode.STANDARD), isCurrent)

    /** Republishes the current choice without rewriting a stale choice observed by the caller. */
    fun republishIf(context: Context, isCurrent: () -> Boolean): Choice? = synchronized(transitionLock) {
        if (!isCurrent()) return@synchronized null
        val selected = choice(context)
        GameEnvironmentStore.publish(context)
        if (isCurrent()) selected else null
    }

    /** The optional block added to the guest environment JSON. It never downloads or verifies files. */
    fun publication(context: Context): JSONObject? {
        val selected = choice(context)
        val version = selected.version
        if (selected.mode != Mode.EXPERIMENTAL || version == null) return null

        val base = packs(context).absoluteFile
        val pack = GraphicsProfilePack.directory(base, version).absoluteFile
        val hybris = SystemVulkanDriver.runtimeDirectory(context).absoluteFile
        val managed = JSONObject().apply {
            put("DROIDDECK_PROTON_WRAPPER", File(pack, "run-proton.py").path)
            put("HYBRIS_BC_TEXTURES", "dxvk")
            put("VK_LAYER_PATH", File(pack, "wsi").path + ":" + File(hybris, "lib").path)
            put("VK_INSTANCE_LAYERS", "VK_LAYER_window_system_integration:VK_LAYER_HYBRIS_compat")
            put("ENABLE_GAMESCOPE_WSI", "0")
            put("VK_LOADER_LAYERS_DISABLE", "*gamescope*")
            put("DISABLE_WSI_LAYER", JSONObject.NULL)
            put("PROTON_USE_WINED3D", JSONObject.NULL)
            put("DISABLE_VK_LAYER_VALVE_steam_fossilize_1", "1")
            put("ENABLE_VK_LAYER_VALVE_steam_fossilize_1", JSONObject.NULL)
        }
        return JSONObject().apply {
            put("format", PROFILE_FORMAT)
            put("packBase", base.path)
            put("version", version)
            put("hybrisRuntime", hybris.path)
            put("managedEnvironment", managed)
            put("validation", JSONObject().apply {
                put("receipt", ".complete")
                put("manifest", "pack.json")
                put("criticalFiles", JSONArray(CRITICAL_FILES))
            })
        }
    }

    private fun currentSpec(context: Context): GraphicsProfilePack.Spec =
        context.assets.open(MANIFEST_ASSET).use { GraphicsProfilePack.parse(it.readBytes()) }

    private fun changeIf(context: Context, next: Choice, isCurrent: () -> Boolean): Boolean = synchronized(transitionLock) {
        if (!isCurrent()) return@synchronized false
        if (next.mode == Mode.EXPERIMENTAL && !SystemVulkanDriver.usesDefault(SessionPrefs.linuxDriver(context))) {
            throw IOException(context.getString(R.string.power_vr_graphics_custom_driver))
        }
        val file = settings(context)
        val previous = try { file.readFully() } catch (_: java.io.FileNotFoundException) { null }
        writeChoice(file, next)
        try {
            GameEnvironmentStore.publish(context)
            if (!isCurrent()) {
                restore(file, previous)
                GameEnvironmentStore.publish(context)
                return@synchronized false
            }
            true
        } catch (error: Exception) {
            try {
                restore(file, previous)
                GameEnvironmentStore.publish(context)
            } catch (restore: Exception) {
                error.addSuppressed(restore)
            }
            throw error
        }
    }

    private fun restore(file: AtomicFile, previous: ByteArray?) {
        if (previous == null) file.delete() else write(file, previous)
    }

    private fun writeChoice(file: AtomicFile, value: Choice) {
        val json = JSONObject().put("format", PROFILE_FORMAT).put(
            "mode", when (value.mode) {
                Mode.STANDARD -> "standard"
                Mode.EXPERIMENTAL -> "experimental"
                Mode.UNDECIDED -> "undecided"
            },
        )
        value.version?.let { json.put("version", it) }
        write(file, json.toString().toByteArray(Charsets.UTF_8))
    }

    private fun write(file: AtomicFile, bytes: ByteArray) {
        val output = file.startWrite()
        try {
            output.write(bytes)
            file.finishWrite(output)
        } catch (error: Exception) {
            file.failWrite(output)
            throw error
        }
    }

    private fun requireLibraries(root: File) {
        for (path in ROOTFS_LIBRARIES) {
            if (!guestFile(root, path).isFile) throw IOException("The Linux runtime is missing /$path")
        }
    }

    /** Resolve absolute guest symlinks inside [root], rather than against Android's own `/`. */
    private fun guestFile(root: File, guestPath: String, depth: Int = 0): File {
        if (depth > 32) throw IOException("Too many symlinks while resolving /$guestPath")
        val normalized = Paths.get("/", guestPath).normalize()
        if (!normalized.isAbsolute || normalized.startsWith("/..")) throw IOException("Invalid guest library path: $guestPath")
        var current = root.toPath()
        // Path iteration omits the root component, so this is already relative to `/`.
        val parts = normalized.toList()
        for ((index, part) in parts.withIndex()) {
            val candidate = current.resolve(part)
            if (Files.isSymbolicLink(candidate)) {
                val target = Files.readSymbolicLink(candidate)
                val prefix = if (target.isAbsolute) target else Paths.get("/", root.toPath().relativize(current).toString()).resolve(target)
                val remaining = parts.drop(index + 1).fold(prefix) { path: Path, item -> path.resolve(item) }
                val next = remaining.normalize()
                if (!next.isAbsolute || next.startsWith("/..")) throw IOException("Guest library symlink escapes the runtime: /$guestPath")
                return guestFile(root, next.toString(), depth + 1)
            }
            current = candidate
        }
        return current.toFile()
    }

    private fun fetch(archive: GraphicsProfilePack.Archive, target: File, progress: (Int) -> Unit) {
        val source = URL(archive.url)
        if (source.protocol != "https") throw IOException("Graphics archive URL must use HTTPS")
        val connection = source.openConnection() as HttpURLConnection
        connection.connectTimeout = CONNECT_TIMEOUT_MS
        connection.readTimeout = READ_TIMEOUT_MS
        connection.instanceFollowRedirects = true
        connection.setRequestProperty("User-Agent", "DroidDeck-Android")
        try {
            if (connection.responseCode / 100 != 2) throw IOException("Graphics archive ${archive.id}: HTTP ${connection.responseCode}")
            if (connection.url.protocol != "https") throw IOException("Graphics archive ${archive.id} redirected away from HTTPS")
            val announced = connection.contentLengthLong
            if (announced > archive.size) throw IOException("Graphics archive ${archive.id} is larger than expected")
            target.parentFile?.mkdirs()
            connection.inputStream.buffered(1 shl 16).use { input ->
                FileOutputStream(target).use { output ->
                    val buffer = ByteArray(1 shl 16)
                    var total = 0L
                    var last = -1
                    while (true) {
                        val count = input.read(buffer)
                        if (count < 0) break
                        total += count
                        if (total > archive.size) throw IOException("Graphics archive ${archive.id} exceeds its declared size")
                        output.write(buffer, 0, count)
                        val percent = if (archive.size == 0L) 100 else (total * 100 / archive.size).toInt()
                        if (percent != last) {
                            last = percent
                            progress(percent)
                        }
                    }
                }
            }
        } finally {
            connection.disconnect()
        }
    }
}
